from __future__ import annotations

import os
import socket
import threading
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

try:
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:  # Unit tests run without the optional DB driver.
    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, value):
            self.obj = value

from ..core.security import mask_payload
from ..ontology.axiom_executor import execute_axioms
from ..ontology.data_quality import validate_catalog_data_quality
from .execution import ActionExecutionService, ActionStateMachine
from .loan_application_repository import (
    PostgresLoanApplicationRepository,
    effect_from_payload as loan_application_effect_from_payload,
)
from .credit_report_repository import (
    PostgresCreditReportRepository,
    effect_from_payload as credit_report_effect_from_payload,
)
from .anti_fraud_report_repository import (
    PostgresAntiFraudReportRepository,
    effect_from_payload as anti_fraud_effect_from_payload,
)
from .risk_assessment_repository import (
    PostgresRiskAssessmentRepository,
    RiskAssessmentEffect,
)
from .phone_dispatch import (
    PhoneDispatchNotConfigured,
    PhoneDispatchResponseInvalid,
    PhoneDispatchTimeout,
    PostgresPhoneDispatchCoordinator,
)
from .operations import ExternalCircuitOpenError
from ..ontology.models import OntologyProposal
from ..ontology.version_repository import OntologyVersionRepository
from .phone_verification_repository import (
    PhoneVerificationPersistenceError,
    PostgresPhoneVerificationTaskRepository,
)
from .phone_verification_result_repository import (
    PhoneVerificationResultPersistenceError,
    PostgresPhoneVerificationResultRepository,
)
from .manual_review_repository import (
    ManualReviewPersistenceError,
    PostgresManualReviewRepository,
    manual_review_effect_from_payload,
)
from .decision_recommendation_repository import (
    DecisionRecommendationPersistenceError,
    PostgresDecisionRecommendationRepository,
    decision_recommendation_effect_from_payload,
)


OUTBOX_STATUSES = (
    "PENDING",
    "PROCESSING",
    "RETRY",
    "SUCCEEDED",
    "DEAD_LETTER",
    "COMPENSATED",
    "CANCELLED",
)
_STATUS_SQL = ",".join(f"'{status}'" for status in OUTBOX_STATUSES)

OUTBOX_SCHEMA_STATEMENTS = (
    f"""
    CREATE TABLE IF NOT EXISTS mvp_control.action_outbox (
        event_id varchar(36) PRIMARY KEY,
        execution_id varchar(36) NOT NULL
            REFERENCES mvp_control.action_executions(execution_id) ON DELETE RESTRICT,
        event_type varchar(64) NOT NULL,
        aggregate_type varchar(64) NOT NULL,
        aggregate_id varchar(128) NOT NULL,
        ontology_version varchar(64) NOT NULL,
        deduplication_key varchar(160) NOT NULL,
        payload jsonb NOT NULL,
        status varchar(24) NOT NULL CHECK (status IN ({_STATUS_SQL})),
        attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        max_attempts integer NOT NULL CHECK (max_attempts > 0),
        available_at timestamptz NOT NULL DEFAULT now(),
        locked_at timestamptz,
        lock_owner varchar(160),
        last_error_code varchar(64),
        last_error_message varchar(500),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        processed_at timestamptz,
        UNIQUE (event_type,deduplication_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS action_outbox_ready_idx
    ON mvp_control.action_outbox (status,available_at,created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS action_outbox_execution_idx
    ON mvp_control.action_outbox (execution_id,created_at)
    """,
)


class OutboxError(RuntimeError):
    pass


class OutboxConcurrentUpdate(OutboxError):
    pass


class OutboxHandlerError(OutboxError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    execution_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    ontology_version: str
    deduplication_key: str
    payload: Mapping[str, Any]
    status: str
    attempt_count: int
    max_attempts: int
    available_at: datetime | None = None
    locked_at: datetime | None = None
    lock_owner: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    processed_at: datetime | None = None


def json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    return value


def _event_from_row(row: Mapping[str, Any]) -> OutboxEvent:
    return OutboxEvent(
        event_id=row["event_id"],
        execution_id=row["execution_id"],
        event_type=row["event_type"],
        aggregate_type=row["aggregate_type"],
        aggregate_id=row["aggregate_id"],
        ontology_version=row["ontology_version"],
        deduplication_key=row["deduplication_key"],
        payload=row.get("payload") or {},
        status=row["status"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        available_at=row.get("available_at"),
        locked_at=row.get("locked_at"),
        lock_owner=row.get("lock_owner"),
        last_error_code=row.get("last_error_code"),
        last_error_message=row.get("last_error_message"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        processed_at=row.get("processed_at"),
    )


class PostgresActionOutboxRepository:
    _schema_lock = threading.Lock()
    _schema_ready = False

    def __init__(self, postgres_service):
        self.postgres = postgres_service

    def ensure_schema(self) -> None:
        if type(self)._schema_ready:
            return
        with type(self)._schema_lock:
            if type(self)._schema_ready:
                return
            with self.postgres.conn() as connection, connection.cursor() as cursor:
                for statement in OUTBOX_SCHEMA_STATEMENTS:
                    cursor.execute(statement)
            type(self)._schema_ready = True

    def enqueue(
        self,
        cursor,
        *,
        execution_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        ontology_version: str,
        deduplication_key: str,
        payload: Mapping[str, Any],
        max_attempts: int,
    ) -> OutboxEvent:
        if max_attempts <= 0:
            raise OutboxError("max_attempts 必须大于 0")
        event_id = str(uuid4())
        cursor.execute(
            """
            INSERT INTO mvp_control.action_outbox (
                event_id,execution_id,event_type,aggregate_type,aggregate_id,
                ontology_version,deduplication_key,payload,status,max_attempts
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)
            ON CONFLICT (event_type,deduplication_key) DO NOTHING
            RETURNING *
            """,
            (
                event_id,
                execution_id,
                event_type,
                aggregate_type,
                aggregate_id,
                ontology_version,
                deduplication_key,
                Jsonb(json_safe(payload)),
                max_attempts,
            ),
        )
        row = cursor.fetchone()
        if row:
            return _event_from_row(row)
        cursor.execute(
            """
            SELECT * FROM mvp_control.action_outbox
            WHERE event_type=%s AND deduplication_key=%s
            """,
            (event_type, deduplication_key),
        )
        existing = cursor.fetchone()
        if not existing:
            raise OutboxConcurrentUpdate("outbox 幂等事件并发读取失败")
        if existing["execution_id"] != execution_id:
            raise OutboxConcurrentUpdate("outbox 幂等键已被其他 Action 执行占用")
        return _event_from_row(existing)

    def get(self, event_id: str) -> OutboxEvent:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM mvp_control.action_outbox WHERE event_id=%s",
                (event_id,),
            )
            row = cursor.fetchone()
        if not row:
            raise OutboxError(f"outbox 事件不存在：{event_id}")
        return _event_from_row(row)

    def claim_dead_letter(self, event_id: str, worker_id: str) -> OutboxEvent:
        """Acquire one dead letter for an explicit operator replay."""
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM mvp_control.action_outbox
                WHERE event_id=%s FOR UPDATE
                """,
                (event_id,),
            )
            current = cursor.fetchone()
            if not current:
                raise OutboxError(f"outbox 事件不存在：{event_id}")
            if current["status"] != "DEAD_LETTER":
                raise OutboxConcurrentUpdate(
                    f"只有 DEAD_LETTER 事件可以人工重放，当前为 {current['status']}"
                )
            cursor.execute(
                """
                UPDATE mvp_control.action_outbox
                SET status='PROCESSING',attempt_count=attempt_count+1,
                    locked_at=now(),lock_owner=%s,updated_at=now()
                WHERE event_id=%s RETURNING *
                """,
                (worker_id, event_id),
            )
            return _event_from_row(cursor.fetchone())

    def restore_dead_letter(
        self,
        event_id: str,
        worker_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> OutboxEvent:
        """Return a failed manual replay to DEAD_LETTER without hiding history."""
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE mvp_control.action_outbox
                SET status='DEAD_LETTER',lock_owner=NULL,locked_at=NULL,
                    last_error_code=%s,last_error_message=%s,
                    processed_at=now(),updated_at=now()
                WHERE event_id=%s AND status='PROCESSING' AND lock_owner=%s
                RETURNING *
                """,
                (
                    str(error_code)[:64],
                    str(mask_payload(error_message))[:500],
                    event_id,
                    worker_id,
                ),
            )
            row = cursor.fetchone()
        if not row:
            raise OutboxConcurrentUpdate("人工重放失败回写缺少有效租约")
        return _event_from_row(row)

    def list_for_execution(self, execution_id: str) -> list[dict[str, Any]]:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM mvp_control.action_outbox
                WHERE execution_id=%s ORDER BY created_at,event_id
                """,
                (execution_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def list_events(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        limit = max(1, min(int(limit), 500))
        if status is not None and status not in OUTBOX_STATUSES:
            raise OutboxError(f"未知 outbox 状态：{status}")
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            if status:
                cursor.execute(
                    """
                    SELECT * FROM mvp_control.action_outbox
                    WHERE status=%s ORDER BY created_at DESC LIMIT %s
                    """,
                    (status, limit),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM mvp_control.action_outbox
                    ORDER BY created_at DESC LIMIT %s
                    """,
                    (limit,),
                )
            return [dict(row) for row in cursor.fetchall()]

    def status_summary(self, *, lease_seconds: int = 60) -> dict[str, Any]:
        self.ensure_schema()
        lease_seconds = max(5, int(lease_seconds))
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status,count(*) AS count
                FROM mvp_control.action_outbox GROUP BY status
                """
            )
            counts = {status: 0 for status in OUTBOX_STATUSES}
            counts.update({row["status"]: row["count"] for row in cursor.fetchall()})
            cursor.execute(
                """
                SELECT count(*) AS count FROM mvp_control.action_outbox
                WHERE status='PROCESSING'
                  AND locked_at < now()-(%s*interval '1 second')
                """,
                (lease_seconds,),
            )
            stale_processing = cursor.fetchone()["count"]
        return {
            "healthy": counts["DEAD_LETTER"] == 0 and stale_processing == 0,
            "counts": counts,
            "stale_processing": stale_processing,
            "requires_attention": counts["DEAD_LETTER"] + stale_processing,
        }

    def stale_executions_without_event(
        self,
        *,
        action_name: str = "generate_risk_assessment",
        older_than_seconds: int,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Find Actions that crashed before their atomic business/outbox commit."""
        self.ensure_schema()
        older_than_seconds = max(5, int(older_than_seconds))
        limit = max(1, min(int(limit), 500))
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT execution.* FROM mvp_control.action_executions AS execution
                LEFT JOIN mvp_control.action_outbox AS event
                  ON event.execution_id=execution.execution_id
                WHERE execution.action_name=%s
                  AND execution.status IN (
                    'RECEIVED','AUTHORIZING','VALIDATING','RUNNING','VERIFYING','COMMITTING'
                  )
                  AND execution.updated_at < now()-(%s*interval '1 second')
                  AND event.event_id IS NULL
                ORDER BY execution.updated_at,execution.execution_id
                LIMIT %s
                """,
                (action_name, older_than_seconds, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        event_id: str | None = None,
        event_type: str | None = None,
    ) -> OutboxEvent | None:
        self.ensure_schema()
        lease_seconds = max(5, int(lease_seconds))
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            if event_id:
                cursor.execute(
                    """
                    SELECT event_id FROM mvp_control.action_outbox
                    WHERE event_id=%s AND (
                        (status IN ('PENDING','RETRY') AND available_at<=now())
                        OR (status='PROCESSING' AND locked_at < now()-(%s*interval '1 second'))
                    )
                    ORDER BY available_at,created_at,event_id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                    """,
                    (event_id, lease_seconds),
                )
            elif event_type:
                cursor.execute(
                    """
                    SELECT event_id FROM mvp_control.action_outbox
                    WHERE event_type=%s AND (
                        (status IN ('PENDING','RETRY') AND available_at<=now())
                        OR (status='PROCESSING' AND locked_at < now()-(%s*interval '1 second'))
                    )
                    ORDER BY available_at,created_at,event_id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                    """,
                    (event_type, lease_seconds),
                )
            else:
                cursor.execute(
                    """
                    SELECT event_id FROM mvp_control.action_outbox
                    WHERE (
                        (status IN ('PENDING','RETRY') AND available_at<=now())
                        OR (status='PROCESSING' AND locked_at < now()-(%s*interval '1 second'))
                    )
                    ORDER BY available_at,created_at,event_id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                    """,
                    (lease_seconds,),
                )
            candidate = cursor.fetchone()
            if not candidate:
                return None
            cursor.execute(
                """
                UPDATE mvp_control.action_outbox
                SET status='PROCESSING',attempt_count=attempt_count+1,
                    locked_at=now(),lock_owner=%s,updated_at=now()
                WHERE event_id=%s RETURNING *
                """,
                (worker_id, candidate["event_id"]),
            )
            return _event_from_row(cursor.fetchone())

    def mark_succeeded(self, event_id: str, worker_id: str) -> OutboxEvent:
        return self._finish(
            event_id,
            worker_id,
            status="SUCCEEDED",
            allowed=("PROCESSING", "SUCCEEDED"),
        )

    def mark_compensated(self, event_id: str) -> OutboxEvent:
        return self._finish(
            event_id,
            None,
            status="COMPENSATED",
            allowed=("DEAD_LETTER", "COMPENSATED"),
        )

    def cancel(self, event_id: str, reason: str) -> OutboxEvent:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE mvp_control.action_outbox
                SET status='CANCELLED',last_error_message=%s,lock_owner=NULL,
                    locked_at=NULL,processed_at=now(),updated_at=now()
                WHERE event_id=%s AND status NOT IN ('SUCCEEDED','COMPENSATED')
                RETURNING *
                """,
                (str(mask_payload(reason))[:500], event_id),
            )
            row = cursor.fetchone()
        if not row:
            return self.get(event_id)
        return _event_from_row(row)

    def _finish(
        self,
        event_id: str,
        worker_id: str | None,
        *,
        status: str,
        allowed: tuple[str, ...],
    ) -> OutboxEvent:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM mvp_control.action_outbox
                WHERE event_id=%s FOR UPDATE
                """,
                (event_id,),
            )
            current = cursor.fetchone()
            if not current:
                raise OutboxError(f"outbox 事件不存在：{event_id}")
            if current["status"] == status:
                return _event_from_row(current)
            if current["status"] not in allowed:
                raise OutboxConcurrentUpdate(
                    f"outbox 不能从 {current['status']} 进入 {status}"
                )
            if worker_id and current.get("lock_owner") != worker_id:
                raise OutboxConcurrentUpdate("outbox 事件租约不属于当前 worker")
            cursor.execute(
                """
                UPDATE mvp_control.action_outbox
                SET status=%s,lock_owner=NULL,locked_at=NULL,
                    last_error_code=NULL,last_error_message=NULL,
                    processed_at=now(),updated_at=now()
                WHERE event_id=%s RETURNING *
                """,
                (status, event_id),
            )
            return _event_from_row(cursor.fetchone())

    def mark_failed(
        self,
        event_id: str,
        worker_id: str,
        *,
        error_code: str,
        error_message: str,
        retry_delay_seconds: int,
    ) -> OutboxEvent:
        self.ensure_schema()
        safe_message = str(mask_payload(error_message))[:500]
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM mvp_control.action_outbox
                WHERE event_id=%s FOR UPDATE
                """,
                (event_id,),
            )
            current = cursor.fetchone()
            if not current:
                raise OutboxError(f"outbox 事件不存在：{event_id}")
            if current["status"] != "PROCESSING" or current.get("lock_owner") != worker_id:
                raise OutboxConcurrentUpdate("outbox 失败回写缺少有效租约")
            exhausted = current["attempt_count"] >= current["max_attempts"]
            target = "DEAD_LETTER" if exhausted else "RETRY"
            cursor.execute(
                """
                UPDATE mvp_control.action_outbox
                SET status=%s,last_error_code=%s,last_error_message=%s,
                    available_at=CASE WHEN %s='RETRY'
                        THEN now()+(%s*interval '1 second') ELSE available_at END,
                    lock_owner=NULL,locked_at=NULL,updated_at=now(),
                    processed_at=CASE WHEN %s='DEAD_LETTER' THEN now() ELSE NULL END
                WHERE event_id=%s RETURNING *
                """,
                (
                    target,
                    error_code[:64],
                    safe_message,
                    target,
                    max(1, int(retry_delay_seconds)),
                    target,
                    event_id,
                ),
            )
            return _event_from_row(cursor.fetchone())


def _effect_from_payload(payload: Mapping[str, Any]) -> RiskAssessmentEffect:
    raw = payload.get("effect")
    if not isinstance(raw, Mapping):
        raise OutboxHandlerError("ACTION_OUTBOX_PAYLOAD_INVALID", "outbox 缺少补偿效果")
    return RiskAssessmentEffect(
        operation=str(raw.get("operation") or ""),
        assessment_id=str(raw.get("assessment_id") or ""),
        application_id=str(raw.get("application_id") or ""),
        previous_row=raw.get("previous_row") if isinstance(raw.get("previous_row"), Mapping) else None,
    )


class RiskAssessmentOutboxWorker:
    EVENT_TYPE = "RISK_ASSESSMENT_GRAPH_SYNC"
    ACTION_NAME = "generate_risk_assessment"

    def __init__(
        self,
        *,
        postgres_service,
        nebula_factory: Callable[[], Any],
        proposal_loader: Callable[[str], Any],
        outbox_repository: PostgresActionOutboxRepository,
        execution_repository,
        report_repository: PostgresRiskAssessmentRepository,
        worker_id: str | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
        sync_handler: Callable[[Any], Mapping[str, Any]] | None = None,
    ):
        self.postgres = postgres_service
        self.nebula_factory = nebula_factory
        self.proposal_loader = proposal_loader
        self.outbox = outbox_repository
        self.executions = execution_repository
        self.reports = report_repository
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{str(uuid4())[:8]}"
        )
        self.lease_seconds = max(5, int(lease_seconds))
        self.retry_base_seconds = max(1, int(retry_base_seconds))
        self.retry_max_seconds = max(self.retry_base_seconds, int(retry_max_seconds))
        self.sync_handler = sync_handler

    def _sync_and_verify(self, proposal) -> dict[str, Any]:
        if self.sync_handler:
            return dict(self.sync_handler(proposal))
        versions = OntologyVersionRepository(self.postgres)
        # Publication, manual sync and every outbox handler share this lock.
        # Read the snapshot only after acquiring it, so an older snapshot cannot
        # arrive after (and delete) a newer projection. Execution/audit identity
        # remains pinned to the event version; the shared graph uses active TBox.
        with versions.publication_guard():
            active = versions.active()
            if not active:
                raise OutboxHandlerError(
                    "ACTION_ACTIVE_PROJECTION_UNAVAILABLE", "没有已发布的活动图投影版本"
                )
            projection = OntologyProposal.model_validate(active["proposal"])
            return self._sync_active_projection(projection)

    def _sync_active_projection(self, proposal) -> dict[str, Any]:
        profile = self.postgres.validation_profile()
        data_quality = validate_catalog_data_quality(proposal, profile)
        if not data_quality.get("valid"):
            raise OutboxHandlerError(
                "ACTION_DATA_QUALITY_GATE_FAILED",
                "业务写入后未通过目录数据质量门禁",
            )
        axioms = execute_axioms(proposal, profile)
        if not axioms.get("valid"):
            raise OutboxHandlerError(
                "ACTION_AXIOM_GATE_FAILED",
                "业务写入后未通过可执行公理门禁",
            )
        graph = self.nebula_factory()
        try:
            synchronization = graph.sync_postgres_profile(profile, proposal)
            mapping = graph.mapping_report(proposal)
        finally:
            graph.close()
        if not mapping.get("valid"):
            raise OutboxHandlerError(
                "ACTION_GRAPH_MAPPING_FAILED",
                "NebulaGraph 同步后的映射校验失败",
            )
        return {
            "projection_ontology_version": proposal.version,
            "synchronization": synchronization,
            "mapping_summary": mapping.get("summary"),
            "data_quality": data_quality.get("status"),
            "axioms": axioms.get("status"),
        }

    def _result(self, event: OutboxEvent, graph_result: Mapping[str, Any]) -> dict[str, Any]:
        payload = event.payload
        report = payload.get("report") or {}
        effect = payload.get("effect") or {}
        return {
            "application_id": report.get("application_id"),
            "assessment_version": payload.get("assessment_version"),
            "assessment_id": report.get("assessment_id"),
            "risk_level": report.get("risk_level"),
            "pd_value": float(report["pd_value"]) if report.get("pd_value") is not None else None,
            "check_result": report.get("check_result"),
            "evidence_summary": payload.get("evidence_summary") or {},
            "persistence": {
                "postgres": effect.get("operation"),
                "nebulagraph": "synchronized",
                "outbox_event_id": event.event_id,
                **dict(graph_result),
            },
            "decision_boundary": "risk_assessment_only_not_final_approval",
            "warnings": payload.get("warnings") or [],
        }

    def _finalize_success(
        self, event: OutboxEvent, graph_result: Mapping[str, Any]
    ) -> None:
        current = self.executions.get(event.execution_id)
        if current.status == "SUCCEEDED":
            self.outbox.mark_succeeded(event.event_id, self.worker_id)
            return
        if current.status != "COMMITTING":
            if ActionStateMachine.is_terminal(current.status):
                self.outbox.cancel(event.event_id, f"Action 已进入终态 {current.status}")
                return
            raise OutboxHandlerError(
                "ACTION_OUTBOX_STATE_CONFLICT",
                f"Action 状态 {current.status} 不能完成图同步",
            )
        service = ActionExecutionService(
            self.executions, proposal=self.proposal_loader(event.ontology_version)
        )
        events = self.executions.list_audit_events(event.execution_id)
        if not any(item["event_type"] == "risk_assessment_recorded" for item in events):
            report = event.payload.get("report") or {}
            service.record_event(
                event.execution_id,
                event_type="risk_assessment_recorded",
                actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
                details={
                    "assessment_id": report.get("assessment_id"),
                    "operation": (event.payload.get("effect") or {}).get("operation"),
                    "graph_mapping_valid": True,
                    "outbox_event_id": event.event_id,
                },
            )
        current = self.executions.get(event.execution_id)
        service.advance(
            current.execution_id,
            "SUCCEEDED",
            expected_version=current.version,
            event_type="action_succeeded",
            actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
            result_summary=self._result(event, graph_result),
        )
        self.outbox.mark_succeeded(event.event_id, self.worker_id)

    def _compensate_dead_letter(self, event: OutboxEvent, original_code: str) -> None:
        actor_id = str(event.payload.get("actor_id") or "outbox-worker")
        proposal = self.proposal_loader(event.ontology_version)
        service = ActionExecutionService(self.executions, proposal=proposal)
        current = self.executions.get(event.execution_id)
        try:
            if current.status == "COMMITTING":
                current = service.advance(
                    current.execution_id,
                    "COMPENSATING",
                    expected_version=current.version,
                    event_type="compensation_started",
                    actor_id=actor_id,
                    details={
                        "original_error_code": original_code,
                        "outbox_event_id": event.event_id,
                    },
                )
            if current.status != "COMPENSATING":
                raise OutboxHandlerError(
                    "ACTION_OUTBOX_STATE_CONFLICT",
                    f"Action 状态 {current.status} 不能执行死信补偿",
                )
            effect = _effect_from_payload(event.payload)
            self.reports.compensate(effect)
            self._sync_and_verify(proposal)
            current = self.executions.get(event.execution_id)
            service.advance(
                current.execution_id,
                "COMPENSATED",
                expected_version=current.version,
                event_type="action_compensated",
                actor_id=actor_id,
                result_summary={
                    "compensated": True,
                    "original_error_code": original_code,
                    "assessment_id": effect.assessment_id,
                    "outbox_event_id": event.event_id,
                },
            )
            self.outbox.mark_compensated(event.event_id)
        except Exception as exc:
            current = self.executions.get(event.execution_id)
            if current.status == "COMPENSATING":
                service.advance(
                    current.execution_id,
                    "COMPENSATION_FAILED",
                    expected_version=current.version,
                    event_type="compensation_failed",
                    actor_id=actor_id,
                    error_code="ACTION_COMPENSATION_FAILED",
                    error_message=str(exc),
                )
            raise

    def _apply_manual_replay_success(
        self, event: OutboxEvent, graph_result: Mapping[str, Any]
    ) -> None:
        """Apply event-specific retained-fact metadata after a terminal redrive."""

    def replay_dead_letter(
        self,
        event_id: str,
        *,
        actor_id: str,
        replay_id: str,
        reason_digest: str,
    ) -> tuple[OutboxEvent, dict[str, Any]]:
        """Reconcile a dead letter while preserving the original Action terminal state."""
        event = self.outbox.claim_dead_letter(event_id, self.worker_id)
        graph_result: dict[str, Any] = {}
        try:
            if event.event_type != self.EVENT_TYPE:
                raise OutboxHandlerError(
                    "ACTION_OUTBOX_HANDLER_NOT_FOUND",
                    "人工重放事件类型与 handler 不匹配",
                )
            current = self.executions.get(event.execution_id)
            if current.status not in {
                "COMMITTING", "FAILED", "COMPENSATION_FAILED"
            }:
                raise OutboxHandlerError(
                    "ACTION_OUTBOX_REPLAY_NOT_ALLOWED",
                    f"Action 状态 {current.status} 不允许人工重放",
                )
            proposal = self.proposal_loader(event.ontology_version)
            graph_result = self._sync_and_verify(proposal)
            if current.status == "COMMITTING":
                self._finalize_success(event, graph_result)
            else:
                self._apply_manual_replay_success(event, graph_result)
                self.executions.record_event(
                    event.execution_id,
                    event_type="manual_outbox_replay_succeeded",
                    actor_id=actor_id,
                    details={
                        "replay_id": replay_id,
                        "outbox_event_id": event.event_id,
                        "event_type": event.event_type,
                        "reason_digest": reason_digest,
                        "original_action_status_preserved": current.status,
                        "graph_mapping_valid": True,
                    },
                )
                self.outbox.mark_succeeded(event.event_id, self.worker_id)
            return self.outbox.get(event.event_id), graph_result
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, OutboxHandlerError)
                else "ACTION_OUTBOX_MANUAL_REPLAY_FAILED"
            )
            self.outbox.restore_dead_letter(
                event.event_id,
                self.worker_id,
                error_code=code,
                error_message=str(exc) or type(exc).__name__,
            )
            raise

    def process_event(self, event_id: str | None = None) -> OutboxEvent | None:
        event = self.outbox.claim(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            event_id=event_id,
            event_type=None if event_id else self.EVENT_TYPE,
        )
        if event is None:
            return self.outbox.get(event_id) if event_id else None
        if event.event_type != self.EVENT_TYPE:
            self.outbox.cancel(event.event_id, "没有注册对应 outbox handler")
            return self.outbox.get(event.event_id)
        try:
            current = self.executions.get(event.execution_id)
            if current.status == "SUCCEEDED":
                self.outbox.mark_succeeded(event.event_id, self.worker_id)
                return self.outbox.get(event.event_id)
            if ActionStateMachine.is_terminal(current.status):
                self.outbox.cancel(
                    event.event_id, f"Action 已进入终态 {current.status}"
                )
                return self.outbox.get(event.event_id)
            proposal = self.proposal_loader(event.ontology_version)
            graph_result = self._sync_and_verify(proposal)
            self._finalize_success(event, graph_result)
        except Exception as exc:
            code = exc.code if isinstance(exc, OutboxHandlerError) else "ACTION_GRAPH_SYNC_FAILED"
            delay = min(
                self.retry_max_seconds,
                self.retry_base_seconds * (2 ** max(0, event.attempt_count - 1)),
            )
            failed = self.outbox.mark_failed(
                event.event_id,
                self.worker_id,
                error_code=code,
                error_message=str(exc) or type(exc).__name__,
                retry_delay_seconds=delay,
            )
            if failed.status == "DEAD_LETTER":
                self._compensate_dead_letter(failed, code)
        return self.outbox.get(event.event_id)

    def recover_stale_executions(self, limit: int = 100) -> int:
        finder = getattr(self.outbox, "stale_executions_without_event", None)
        if not callable(finder):
            return 0
        recovered = 0
        for row in finder(
            action_name=self.ACTION_NAME,
            older_than_seconds=self.lease_seconds,
            limit=limit,
        ):
            current = self.executions.get(row["execution_id"])
            if ActionStateMachine.is_terminal(current.status):
                continue
            proposal = self.proposal_loader(current.ontology_version)
            service = ActionExecutionService(self.executions, proposal=proposal)
            service.advance(
                current.execution_id,
                "FAILED",
                expected_version=current.version,
                event_type="action_failed",
                actor_id="outbox-recovery",
                details={
                    "recovery_reason": "stale_execution_without_outbox",
                    "action_name": self.ACTION_NAME,
                },
                error_code="ACTION_EXECUTION_RECOVERED_AFTER_CRASH",
                error_message="服务中断发生在业务数据与 outbox 原子事务提交之前；未发现已提交副作用",
            )
            recovered += 1
        return recovered

    def drain(self, max_events: int = 20) -> dict[str, int]:
        recovered = self.recover_stale_executions(max_events)
        processed = succeeded = retried = dead = compensated = 0
        for _ in range(max(1, int(max_events))):
            event = self.process_event()
            if event is None:
                break
            processed += 1
            succeeded += event.status == "SUCCEEDED"
            retried += event.status == "RETRY"
            dead += event.status == "DEAD_LETTER"
            compensated += event.status == "COMPENSATED"
        return {
            "processed": processed,
            "succeeded": succeeded,
            "retried": retried,
            "dead_letter": dead,
            "compensated": compensated,
            "recovered_stale_executions": recovered,
        }


class LoanApplicationOutboxWorker(RiskAssessmentOutboxWorker):
    EVENT_TYPE = "LOAN_APPLICATION_GRAPH_SYNC"
    ACTION_NAME = "create_loan_application"

    def __init__(
        self,
        *,
        postgres_service,
        nebula_factory: Callable[[], Any],
        proposal_loader: Callable[[str], Any],
        outbox_repository: PostgresActionOutboxRepository,
        execution_repository,
        application_repository: PostgresLoanApplicationRepository,
        worker_id: str | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
        sync_handler: Callable[[Any], Mapping[str, Any]] | None = None,
    ):
        super().__init__(
            postgres_service=postgres_service,
            nebula_factory=nebula_factory,
            proposal_loader=proposal_loader,
            outbox_repository=outbox_repository,
            execution_repository=execution_repository,
            report_repository=application_repository,  # shared retry engine repository slot
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            sync_handler=sync_handler,
        )
        self.applications = application_repository

    def _result(
        self, event: OutboxEvent, graph_result: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "application_id": event.payload.get("application_id"),
            "application_status": event.payload.get("application_status"),
            "created_relations": event.payload.get("created_relations") or [],
            "entity_operations": event.payload.get("entity_operations") or {},
            "persistence": {
                "postgres": "created",
                "nebulagraph": "synchronized",
                "outbox_event_id": event.event_id,
                **dict(graph_result),
            },
            "decision_boundary": "origination_only_no_credit_decision",
        }

    def _finalize_success(
        self, event: OutboxEvent, graph_result: Mapping[str, Any]
    ) -> None:
        current = self.executions.get(event.execution_id)
        if current.status == "SUCCEEDED":
            self.outbox.mark_succeeded(event.event_id, self.worker_id)
            return
        if current.status != "COMMITTING":
            if ActionStateMachine.is_terminal(current.status):
                self.outbox.cancel(event.event_id, f"Action 已进入终态 {current.status}")
                return
            raise OutboxHandlerError(
                "ACTION_OUTBOX_STATE_CONFLICT",
                f"Action 状态 {current.status} 不能完成图同步",
            )
        service = ActionExecutionService(
            self.executions, proposal=self.proposal_loader(event.ontology_version)
        )
        events = self.executions.list_audit_events(event.execution_id)
        if not any(item["event_type"] == "application_created" for item in events):
            service.record_event(
                event.execution_id,
                event_type="application_created",
                actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
                details={
                    "application_id": event.payload.get("application_id"),
                    "created_relations": event.payload.get("created_relations") or [],
                    "graph_mapping_valid": True,
                    "outbox_event_id": event.event_id,
                },
            )
        current = self.executions.get(event.execution_id)
        service.advance(
            current.execution_id,
            "SUCCEEDED",
            expected_version=current.version,
            event_type="action_succeeded",
            actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
            result_summary=self._result(event, graph_result),
        )
        self.outbox.mark_succeeded(event.event_id, self.worker_id)

    def _compensate_dead_letter(self, event: OutboxEvent, original_code: str) -> None:
        actor_id = str(event.payload.get("actor_id") or "outbox-worker")
        proposal = self.proposal_loader(event.ontology_version)
        service = ActionExecutionService(self.executions, proposal=proposal)
        current = self.executions.get(event.execution_id)
        try:
            if current.status == "COMMITTING":
                current = service.advance(
                    current.execution_id,
                    "COMPENSATING",
                    expected_version=current.version,
                    event_type="compensation_started",
                    actor_id=actor_id,
                    details={
                        "original_error_code": original_code,
                        "outbox_event_id": event.event_id,
                    },
                )
            if current.status != "COMPENSATING":
                raise OutboxHandlerError(
                    "ACTION_OUTBOX_STATE_CONFLICT",
                    f"Action 状态 {current.status} 不能执行死信补偿",
                )
            effect = loan_application_effect_from_payload(event.payload)
            self.applications.compensate(effect)
            self._sync_and_verify(proposal)
            current = self.executions.get(event.execution_id)
            service.advance(
                current.execution_id,
                "COMPENSATED",
                expected_version=current.version,
                event_type="action_compensated",
                actor_id=actor_id,
                result_summary={
                    "compensated": True,
                    "original_error_code": original_code,
                    "application_id": effect.application_id,
                    "outbox_event_id": event.event_id,
                },
            )
            self.outbox.mark_compensated(event.event_id)
        except Exception as exc:
            current = self.executions.get(event.execution_id)
            if current.status == "COMPENSATING":
                service.advance(
                    current.execution_id,
                    "COMPENSATION_FAILED",
                    expected_version=current.version,
                    event_type="compensation_failed",
                    actor_id=actor_id,
                    error_code="ACTION_COMPENSATION_FAILED",
                    error_message=str(exc),
                )
            raise


class CreditReportOutboxWorker(RiskAssessmentOutboxWorker):
    EVENT_TYPE = "CREDIT_REPORT_GRAPH_SYNC"
    ACTION_NAME = "request_credit_report"

    def __init__(
        self,
        *,
        postgres_service,
        nebula_factory: Callable[[], Any],
        proposal_loader: Callable[[str], Any],
        outbox_repository: PostgresActionOutboxRepository,
        execution_repository,
        report_repository: PostgresCreditReportRepository,
        worker_id: str | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
        sync_handler: Callable[[Any], Mapping[str, Any]] | None = None,
    ):
        super().__init__(
            postgres_service=postgres_service,
            nebula_factory=nebula_factory,
            proposal_loader=proposal_loader,
            outbox_repository=outbox_repository,
            execution_repository=execution_repository,
            report_repository=report_repository,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            sync_handler=sync_handler,
        )

    def _result(
        self, event: OutboxEvent, graph_result: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "application_id": event.payload.get("application_id"),
            "report_id": event.payload.get("report_id"),
            "provider_request_id": event.payload.get("provider_request_id"),
            "provider_name": event.payload.get("provider_name"),
            "provider_mode": event.payload.get("provider_mode"),
            "synthetic": bool(event.payload.get("synthetic")),
            "subject_role": event.payload.get("subject_role"),
            "created_relations": ["HOLDS"],
            "persistence": {
                "postgres": (event.payload.get("effect") or {}).get("operation"),
                "nebulagraph": "synchronized",
                "outbox_event_id": event.event_id,
                **dict(graph_result),
            },
            "decision_boundary": "credit_report_registration_only_no_decision",
        }

    def _finalize_success(
        self, event: OutboxEvent, graph_result: Mapping[str, Any]
    ) -> None:
        current = self.executions.get(event.execution_id)
        if current.status == "SUCCEEDED":
            self.outbox.mark_succeeded(event.event_id, self.worker_id)
            return
        if current.status != "COMMITTING":
            if ActionStateMachine.is_terminal(current.status):
                self.outbox.cancel(event.event_id, f"Action 已进入终态 {current.status}")
                return
            raise OutboxHandlerError(
                "ACTION_OUTBOX_STATE_CONFLICT",
                f"Action 状态 {current.status} 不能完成图同步",
            )
        service = ActionExecutionService(
            self.executions, proposal=self.proposal_loader(event.ontology_version)
        )
        events = self.executions.list_audit_events(event.execution_id)
        if not any(item["event_type"] == "credit_report_recorded" for item in events):
            service.record_event(
                event.execution_id,
                event_type="credit_report_recorded",
                actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
                details={
                    "report_id": event.payload.get("report_id"),
                    "provider_request_id": event.payload.get("provider_request_id"),
                    "provider_mode": event.payload.get("provider_mode"),
                    "synthetic": bool(event.payload.get("synthetic")),
                    "relation": "HOLDS",
                    "graph_mapping_valid": True,
                    "outbox_event_id": event.event_id,
                },
            )
        current = self.executions.get(event.execution_id)
        service.advance(
            current.execution_id,
            "SUCCEEDED",
            expected_version=current.version,
            event_type="action_succeeded",
            actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
            result_summary=self._result(event, graph_result),
        )
        self.outbox.mark_succeeded(event.event_id, self.worker_id)

    def _compensate_dead_letter(self, event: OutboxEvent, original_code: str) -> None:
        actor_id = str(event.payload.get("actor_id") or "outbox-worker")
        proposal = self.proposal_loader(event.ontology_version)
        service = ActionExecutionService(self.executions, proposal=proposal)
        current = self.executions.get(event.execution_id)
        try:
            if current.status == "COMMITTING":
                current = service.advance(
                    current.execution_id,
                    "COMPENSATING",
                    expected_version=current.version,
                    event_type="compensation_started",
                    actor_id=actor_id,
                    details={
                        "original_error_code": original_code,
                        "outbox_event_id": event.event_id,
                    },
                )
            if current.status != "COMPENSATING":
                raise OutboxHandlerError(
                    "ACTION_OUTBOX_STATE_CONFLICT",
                    f"Action 状态 {current.status} 不能执行死信补偿",
                )
            effect = credit_report_effect_from_payload(event.payload)
            self.reports.compensate(effect)
            self._sync_and_verify(proposal)
            current = self.executions.get(event.execution_id)
            service.advance(
                current.execution_id,
                "COMPENSATED",
                expected_version=current.version,
                event_type="action_compensated",
                actor_id=actor_id,
                result_summary={
                    "compensated": True,
                    "original_error_code": original_code,
                    "report_id": effect.report_id,
                    "outbox_event_id": event.event_id,
                },
            )
            self.outbox.mark_compensated(event.event_id)
        except Exception as exc:
            current = self.executions.get(event.execution_id)
            if current.status == "COMPENSATING":
                service.advance(
                    current.execution_id,
                    "COMPENSATION_FAILED",
                    expected_version=current.version,
                    event_type="compensation_failed",
                    actor_id=actor_id,
                    error_code="ACTION_COMPENSATION_FAILED",
                    error_message=str(exc),
                )
            raise


class AntiFraudReportOutboxWorker(RiskAssessmentOutboxWorker):
    EVENT_TYPE = "ANTI_FRAUD_REPORT_GRAPH_SYNC"
    ACTION_NAME = "request_fraud_report"

    def __init__(
        self,
        *,
        postgres_service,
        nebula_factory: Callable[[], Any],
        proposal_loader: Callable[[str], Any],
        outbox_repository: PostgresActionOutboxRepository,
        execution_repository,
        report_repository: PostgresAntiFraudReportRepository,
        worker_id: str | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
        sync_handler: Callable[[Any], Mapping[str, Any]] | None = None,
    ):
        super().__init__(
            postgres_service=postgres_service,
            nebula_factory=nebula_factory,
            proposal_loader=proposal_loader,
            outbox_repository=outbox_repository,
            execution_repository=execution_repository,
            report_repository=report_repository,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            sync_handler=sync_handler,
        )

    def _result(
        self, event: OutboxEvent, graph_result: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "application_id": event.payload.get("application_id"),
            "fraud_report_id": event.payload.get("fraud_report_id"),
            "provider_request_id": event.payload.get("provider_request_id"),
            "provider_name": event.payload.get("provider_name"),
            "provider_mode": event.payload.get("provider_mode"),
            "synthetic": bool(event.payload.get("synthetic")),
            "association_risk_summary": (
                event.payload.get("association_risk_summary") or {}
            ),
            "created_relations": ["HAS_FRAUD_REPORT"],
            "persistence": {
                "postgres": (event.payload.get("effect") or {}).get("operation"),
                "nebulagraph": "synchronized",
                "outbox_event_id": event.event_id,
                **dict(graph_result),
            },
            "decision_boundary": "fraud_report_registration_only_no_decision",
        }

    def _finalize_success(
        self, event: OutboxEvent, graph_result: Mapping[str, Any]
    ) -> None:
        current = self.executions.get(event.execution_id)
        if current.status == "SUCCEEDED":
            self.outbox.mark_succeeded(event.event_id, self.worker_id)
            return
        if current.status != "COMMITTING":
            if ActionStateMachine.is_terminal(current.status):
                self.outbox.cancel(event.event_id, f"Action 已进入终态 {current.status}")
                return
            raise OutboxHandlerError(
                "ACTION_OUTBOX_STATE_CONFLICT",
                f"Action 状态 {current.status} 不能完成图同步",
            )
        service = ActionExecutionService(
            self.executions, proposal=self.proposal_loader(event.ontology_version)
        )
        events = self.executions.list_audit_events(event.execution_id)
        if not any(item["event_type"] == "fraud_report_recorded" for item in events):
            service.record_event(
                event.execution_id,
                event_type="fraud_report_recorded",
                actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
                details={
                    "fraud_report_id": event.payload.get("fraud_report_id"),
                    "provider_request_id": event.payload.get("provider_request_id"),
                    "provider_mode": event.payload.get("provider_mode"),
                    "synthetic": bool(event.payload.get("synthetic")),
                    "relation": "HAS_FRAUD_REPORT",
                    "graph_mapping_valid": True,
                    "outbox_event_id": event.event_id,
                },
            )
        current = self.executions.get(event.execution_id)
        service.advance(
            current.execution_id,
            "SUCCEEDED",
            expected_version=current.version,
            event_type="action_succeeded",
            actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
            result_summary=self._result(event, graph_result),
        )
        self.outbox.mark_succeeded(event.event_id, self.worker_id)

    def _compensate_dead_letter(self, event: OutboxEvent, original_code: str) -> None:
        actor_id = str(event.payload.get("actor_id") or "outbox-worker")
        proposal = self.proposal_loader(event.ontology_version)
        service = ActionExecutionService(self.executions, proposal=proposal)
        current = self.executions.get(event.execution_id)
        try:
            if current.status == "COMMITTING":
                current = service.advance(
                    current.execution_id,
                    "COMPENSATING",
                    expected_version=current.version,
                    event_type="compensation_started",
                    actor_id=actor_id,
                    details={
                        "original_error_code": original_code,
                        "outbox_event_id": event.event_id,
                    },
                )
            if current.status != "COMPENSATING":
                raise OutboxHandlerError(
                    "ACTION_OUTBOX_STATE_CONFLICT",
                    f"Action 状态 {current.status} 不能执行死信补偿",
                )
            effect = anti_fraud_effect_from_payload(event.payload)
            self.reports.compensate(effect)
            self._sync_and_verify(proposal)
            current = self.executions.get(event.execution_id)
            service.advance(
                current.execution_id,
                "COMPENSATED",
                expected_version=current.version,
                event_type="action_compensated",
                actor_id=actor_id,
                result_summary={
                    "compensated": True,
                    "original_error_code": original_code,
                    "fraud_report_id": effect.fraud_report_id,
                    "outbox_event_id": event.event_id,
                },
            )
            self.outbox.mark_compensated(event.event_id)
        except Exception as exc:
            current = self.executions.get(event.execution_id)
            if current.status == "COMPENSATING":
                service.advance(
                    current.execution_id,
                    "COMPENSATION_FAILED",
                    expected_version=current.version,
                    event_type="compensation_failed",
                    actor_id=actor_id,
                    error_code="ACTION_COMPENSATION_FAILED",
                    error_message=str(exc),
                )
            raise


class PhoneVerificationTaskOutboxWorker(RiskAssessmentOutboxWorker):
    """Synchronize HAS_PHONE_VERIFICATION, then deliver the retained task."""

    EVENT_TYPE = "PHONE_VERIFICATION_TASK_DISPATCH"
    ACTION_NAME = "create_phone_verification_task"

    def __init__(
        self,
        *,
        postgres_service,
        nebula_factory: Callable[[], Any],
        proposal_loader: Callable[[str], Any],
        outbox_repository: PostgresActionOutboxRepository,
        execution_repository,
        task_repository: PostgresPhoneVerificationTaskRepository,
        dispatch_coordinator: PostgresPhoneDispatchCoordinator,
        worker_id: str | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
        sync_handler: Callable[[Any], Mapping[str, Any]] | None = None,
    ):
        super().__init__(
            postgres_service=postgres_service,
            nebula_factory=nebula_factory,
            proposal_loader=proposal_loader,
            outbox_repository=outbox_repository,
            execution_repository=execution_repository,
            report_repository=task_repository,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            sync_handler=sync_handler,
        )
        self.tasks = task_repository
        self.dispatcher = dispatch_coordinator
        self.tasks.ensure_schema()

    @staticmethod
    def _delivery_error(exc: Exception) -> tuple[str, str]:
        if isinstance(exc, ExternalCircuitOpenError):
            return "ACTION_EXTERNAL_CIRCUIT_OPEN", str(exc)
        if isinstance(exc, PhoneDispatchNotConfigured):
            return "ACTION_DISPATCH_PENDING", str(exc)
        if isinstance(exc, PhoneDispatchTimeout):
            return "ACTION_EXTERNAL_TIMEOUT", str(exc)
        if isinstance(exc, PhoneDispatchResponseInvalid):
            return "ACTION_EXTERNAL_RESPONSE_INVALID", str(exc)
        if isinstance(exc, PhoneVerificationPersistenceError):
            return "ACTION_WRITE_FAILED", str(exc)
        if isinstance(exc, OutboxHandlerError):
            return exc.code, str(exc)
        return "ACTION_PHONE_DISPATCH_FAILED", str(exc) or type(exc).__name__

    def _result(
        self,
        event: OutboxEvent,
        graph_result: Mapping[str, Any],
        task: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "application_id": event.payload.get("application_id"),
            "verification_id": event.payload.get("verification_id"),
            "verification_round": event.payload.get("verification_round"),
            "question_count": event.payload.get("question_count"),
            "template_version": event.payload.get("template_version"),
            "dispatch_id": event.payload.get("dispatch_id"),
            "dispatch_status": "DISPATCHED",
            "external_task_id": task.get("external_task_id"),
            "provider_name": task.get("provider_name"),
            "provider_mode": task.get("provider_mode"),
            "synthetic": bool(task.get("synthetic")),
            "provider_response_cached": bool(
                task.get("provider_response_cached")
            ),
            "verified_trigger_signals": (
                event.payload.get("verified_trigger_signals") or []
            ),
            "created_relations": ["HAS_PHONE_VERIFICATION"],
            "persistence": {
                "postgres": (event.payload.get("effect") or {}).get("operation"),
                "nebulagraph": "synchronized",
                "outbox_event_id": event.event_id,
                **dict(graph_result),
            },
            "decision_boundary": "phone_task_creation_only_no_call_result_or_decision",
        }

    def _finalize_delivery(
        self,
        event: OutboxEvent,
        graph_result: Mapping[str, Any],
        task: Mapping[str, Any],
    ) -> None:
        current = self.executions.get(event.execution_id)
        if current.status == "SUCCEEDED":
            self.outbox.mark_succeeded(event.event_id, self.worker_id)
            return
        if current.status != "COMMITTING":
            if ActionStateMachine.is_terminal(current.status):
                self.outbox.cancel(event.event_id, f"Action 已进入终态 {current.status}")
                return
            raise OutboxHandlerError(
                "ACTION_OUTBOX_STATE_CONFLICT",
                f"Action 状态 {current.status} 不能完成电核任务投递",
            )
        service = ActionExecutionService(
            self.executions, proposal=self.proposal_loader(event.ontology_version)
        )
        events = self.executions.list_audit_events(event.execution_id)
        if not any(item["event_type"] == "phone_task_dispatched" for item in events):
            service.record_event(
                event.execution_id,
                event_type="phone_task_dispatched",
                actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
                details={
                    "verification_id": event.payload.get("verification_id"),
                    "dispatch_id": event.payload.get("dispatch_id"),
                    "external_task_id": task.get("external_task_id"),
                    "provider_mode": task.get("provider_mode"),
                    "synthetic": bool(task.get("synthetic")),
                    "relation": "HAS_PHONE_VERIFICATION",
                    "graph_mapping_valid": True,
                    "outbox_event_id": event.event_id,
                },
            )
        current = self.executions.get(event.execution_id)
        service.advance(
            current.execution_id,
            "SUCCEEDED",
            expected_version=current.version,
            event_type="action_succeeded",
            actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
            result_summary=self._result(event, graph_result, task),
        )
        self.outbox.mark_succeeded(event.event_id, self.worker_id)

    def _retain_dead_letter(
        self, event: OutboxEvent, error_code: str, error_message: str
    ) -> None:
        self.tasks.mark_dispatch_failure(
            str(event.payload.get("verification_id") or event.aggregate_id),
            error_code=error_code,
            error_message=error_message,
            dead_letter=True,
        )
        current = self.executions.get(event.execution_id)
        if current.status != "COMMITTING":
            return
        service = ActionExecutionService(
            self.executions, proposal=self.proposal_loader(event.ontology_version)
        )
        service.advance(
            current.execution_id,
            "FAILED",
            expected_version=current.version,
            event_type="action_failed",
            actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
            details={
                "retained_task": True,
                "verification_id": event.payload.get("verification_id"),
                "outbox_event_id": event.event_id,
            },
            result_summary={
                "application_id": event.payload.get("application_id"),
                "verification_id": event.payload.get("verification_id"),
                "verification_round": event.payload.get("verification_round"),
                "dispatch_status": "DEAD_LETTER",
                "retained_task": True,
                "outbox_event_id": event.event_id,
            },
            error_code=error_code,
            error_message=error_message,
        )

    def replay_dead_letter(
        self,
        event_id: str,
        *,
        actor_id: str,
        replay_id: str,
        reason_digest: str,
    ) -> tuple[OutboxEvent, dict[str, Any]]:
        event = self.outbox.claim_dead_letter(event_id, self.worker_id)
        graph_result: dict[str, Any] = {}
        try:
            if event.event_type != self.EVENT_TYPE:
                raise OutboxHandlerError(
                    "ACTION_OUTBOX_HANDLER_NOT_FOUND",
                    "人工重放事件类型与电核投递 handler 不匹配",
                )
            current = self.executions.get(event.execution_id)
            if current.status not in {
                "COMMITTING", "FAILED", "COMPENSATION_FAILED"
            }:
                raise OutboxHandlerError(
                    "ACTION_OUTBOX_REPLAY_NOT_ALLOWED",
                    f"Action 状态 {current.status} 不允许人工重放",
                )
            proposal = self.proposal_loader(event.ontology_version)
            graph_result = self._sync_and_verify(proposal)
            request = self.tasks.load_dispatch_request(
                str(event.payload.get("verification_id") or event.aggregate_id)
            )
            response, cached = self.dispatcher.dispatch(
                request, timeout_seconds=10, max_attempts=1
            )
            task = self.tasks.mark_dispatched(response, response_cached=cached)
            if current.status == "COMMITTING":
                self._finalize_delivery(event, graph_result, task)
            else:
                self.executions.record_event(
                    event.execution_id,
                    event_type="manual_outbox_replay_succeeded",
                    actor_id=actor_id,
                    details={
                        "replay_id": replay_id,
                        "outbox_event_id": event.event_id,
                        "event_type": event.event_type,
                        "reason_digest": reason_digest,
                        "original_action_status_preserved": current.status,
                        "external_task_id": task.get("external_task_id"),
                    },
                )
                self.outbox.mark_succeeded(event.event_id, self.worker_id)
            return self.outbox.get(event.event_id), {
                **graph_result,
                "dispatch_status": "DISPATCHED",
                "external_task_id": task.get("external_task_id"),
                "provider_mode": task.get("provider_mode"),
            }
        except Exception as exc:
            code, message = self._delivery_error(exc)
            self.tasks.mark_dispatch_failure(
                str(event.payload.get("verification_id") or event.aggregate_id),
                error_code=code,
                error_message=message,
                dead_letter=True,
            )
            self.outbox.restore_dead_letter(
                event.event_id,
                self.worker_id,
                error_code=code,
                error_message=message,
            )
            raise

    def process_event(self, event_id: str | None = None) -> OutboxEvent | None:
        event = self.outbox.claim(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            event_id=event_id,
            event_type=None if event_id else self.EVENT_TYPE,
        )
        if event is None:
            return self.outbox.get(event_id) if event_id else None
        if event.event_type != self.EVENT_TYPE:
            self.outbox.cancel(event.event_id, "没有注册对应 outbox handler")
            return self.outbox.get(event.event_id)
        try:
            current = self.executions.get(event.execution_id)
            if current.status == "SUCCEEDED":
                self.outbox.mark_succeeded(event.event_id, self.worker_id)
                return self.outbox.get(event.event_id)
            if ActionStateMachine.is_terminal(current.status):
                self.outbox.cancel(
                    event.event_id, f"Action 已进入终态 {current.status}"
                )
                return self.outbox.get(event.event_id)
            proposal = self.proposal_loader(event.ontology_version)
            graph_result = self._sync_and_verify(proposal)
            request = self.tasks.load_dispatch_request(
                str(event.payload.get("verification_id") or event.aggregate_id)
            )
            response, cached = self.dispatcher.dispatch(
                request, timeout_seconds=10, max_attempts=1
            )
            task = self.tasks.mark_dispatched(response, response_cached=cached)
            self._finalize_delivery(event, graph_result, task)
        except Exception as exc:
            code, message = self._delivery_error(exc)
            delay = min(
                self.retry_max_seconds,
                self.retry_base_seconds * (2 ** max(0, event.attempt_count - 1)),
            )
            failed = self.outbox.mark_failed(
                event.event_id,
                self.worker_id,
                error_code=code,
                error_message=message,
                retry_delay_seconds=delay,
            )
            if failed.status == "DEAD_LETTER":
                self._retain_dead_letter(failed, code, message)
            else:
                self.tasks.mark_dispatch_failure(
                    str(event.payload.get("verification_id") or event.aggregate_id),
                    error_code=code,
                    error_message=message,
                    dead_letter=False,
                )
        return self.outbox.get(event.event_id)


class PhoneVerificationResultOutboxWorker(RiskAssessmentOutboxWorker):
    """Synchronize a committed terminal result; never roll the factual result back."""

    EVENT_TYPE = "PHONE_VERIFICATION_RESULT_GRAPH_SYNC"
    ACTION_NAME = "record_phone_verification"

    def __init__(
        self,
        *,
        postgres_service,
        nebula_factory: Callable[[], Any],
        proposal_loader: Callable[[str], Any],
        outbox_repository: PostgresActionOutboxRepository,
        execution_repository,
        result_repository: PostgresPhoneVerificationResultRepository,
        worker_id: str | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
        sync_handler: Callable[[Any], Mapping[str, Any]] | None = None,
    ):
        super().__init__(
            postgres_service=postgres_service,
            nebula_factory=nebula_factory,
            proposal_loader=proposal_loader,
            outbox_repository=outbox_repository,
            execution_repository=execution_repository,
            report_repository=result_repository,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            sync_handler=sync_handler,
        )
        self.results = result_repository
        self.results.ensure_schema()

    def _result(
        self, event: OutboxEvent, graph_result: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "application_id": event.payload.get("application_id"),
            "verification_id": event.payload.get("verification_id"),
            "callback_id": event.payload.get("callback_id"),
            "record_status": "COMPLETED",
            "conclusion": event.payload.get("conclusion"),
            "persistence": {
                "postgres": "terminal_result_recorded",
                "nebulagraph": "synchronized",
                "outbox_event_id": event.event_id,
                **dict(graph_result),
            },
            "decision_boundary": "phone_result_only_no_correction_or_credit_decision",
        }

    def _finalize_success(
        self, event: OutboxEvent, graph_result: Mapping[str, Any]
    ) -> None:
        verification_id = str(
            event.payload.get("verification_id") or event.aggregate_id
        )
        current = self.executions.get(event.execution_id)
        if current.status == "SUCCEEDED":
            self.results.mark_graph_sync(verification_id, synchronized=True)
            self.outbox.mark_succeeded(event.event_id, self.worker_id)
            return
        if current.status != "COMMITTING":
            if ActionStateMachine.is_terminal(current.status):
                self.outbox.cancel(event.event_id, f"Action 已进入终态 {current.status}")
                return
            raise OutboxHandlerError(
                "ACTION_OUTBOX_STATE_CONFLICT",
                f"Action 状态 {current.status} 不能完成电核结果图同步",
            )
        self.results.mark_graph_sync(verification_id, synchronized=True)
        service = ActionExecutionService(
            self.executions, proposal=self.proposal_loader(event.ontology_version)
        )
        events = self.executions.list_audit_events(event.execution_id)
        if not any(item["event_type"] == "phone_result_recorded" for item in events):
            service.record_event(
                event.execution_id,
                event_type="phone_result_recorded",
                actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
                details={
                    "verification_id": verification_id,
                    "callback_id": event.payload.get("callback_id"),
                    "record_status": "COMPLETED",
                    "graph_mapping_valid": True,
                    "outbox_event_id": event.event_id,
                },
            )
        current = self.executions.get(event.execution_id)
        service.advance(
            current.execution_id,
            "SUCCEEDED",
            expected_version=current.version,
            event_type="action_succeeded",
            actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
            result_summary=self._result(event, graph_result),
        )
        self.outbox.mark_succeeded(event.event_id, self.worker_id)

    def _apply_manual_replay_success(
        self, event: OutboxEvent, graph_result: Mapping[str, Any]
    ) -> None:
        verification_id = str(
            event.payload.get("verification_id") or event.aggregate_id
        )
        self.results.mark_graph_sync(verification_id, synchronized=True)

    def _compensate_dead_letter(self, event: OutboxEvent, original_code: str) -> None:
        verification_id = str(
            event.payload.get("verification_id") or event.aggregate_id
        )
        self.results.mark_graph_sync(verification_id, synchronized=False)
        current = self.executions.get(event.execution_id)
        if current.status != "COMMITTING":
            return
        service = ActionExecutionService(
            self.executions, proposal=self.proposal_loader(event.ontology_version)
        )
        service.advance(
            current.execution_id,
            "FAILED",
            expected_version=current.version,
            event_type="action_failed",
            actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
            details={
                "retained_terminal_result": True,
                "verification_id": verification_id,
                "outbox_event_id": event.event_id,
            },
            result_summary={
                "application_id": event.payload.get("application_id"),
                "verification_id": verification_id,
                "callback_id": event.payload.get("callback_id"),
                "record_status": "COMPLETED",
                "result_sync_status": "DEAD_LETTER",
                "retained_terminal_result": True,
                "outbox_event_id": event.event_id,
            },
            error_code=original_code,
            error_message=event.last_error_message or "电核结果图同步失败",
        )


class ManualReviewOutboxWorker(RiskAssessmentOutboxWorker):
    EVENT_TYPE = "MANUAL_REVIEW_GRAPH_SYNC"
    ACTION_NAME = "trigger_manual_review"

    def __init__(
        self,
        *,
        postgres_service,
        nebula_factory: Callable[[], Any],
        proposal_loader: Callable[[str], Any],
        outbox_repository: PostgresActionOutboxRepository,
        execution_repository,
        review_repository: PostgresManualReviewRepository,
        worker_id: str | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
        sync_handler: Callable[[Any], Mapping[str, Any]] | None = None,
    ):
        super().__init__(
            postgres_service=postgres_service,
            nebula_factory=nebula_factory,
            proposal_loader=proposal_loader,
            outbox_repository=outbox_repository,
            execution_repository=execution_repository,
            report_repository=review_repository,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            sync_handler=sync_handler,
        )
        self.reviews = review_repository
        self.reviews.ensure_schema()

    def _result(
        self, event: OutboxEvent, graph_result: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "application_id": event.payload.get("application_id"),
            "application_status": "待人工复核",
            "routing_result": "转人工",
            "review_id": event.payload.get("review_id"),
            "review_evidence_reference": event.payload.get(
                "review_evidence_reference"
            ),
            "matched_rules": event.payload.get("matched_rules") or [],
            "source_execution_id": event.payload.get("source_execution_id"),
            "persistence": {
                "postgres": "manual_review_status_and_reason_recorded",
                "nebulagraph": "synchronized",
                "outbox_event_id": event.event_id,
                **dict(graph_result),
            },
            "decision_boundary": "manual_review_routing_only_not_final_approval",
        }

    def _finalize_success(
        self, event: OutboxEvent, graph_result: Mapping[str, Any]
    ) -> None:
        current = self.executions.get(event.execution_id)
        if current.status == "SUCCEEDED":
            self.outbox.mark_succeeded(event.event_id, self.worker_id)
            return
        if current.status != "COMMITTING":
            if ActionStateMachine.is_terminal(current.status):
                self.outbox.cancel(event.event_id, f"Action 已进入终态 {current.status}")
                return
            raise OutboxHandlerError(
                "ACTION_OUTBOX_STATE_CONFLICT",
                f"Action 状态 {current.status} 不能完成人工复核图同步",
            )
        service = ActionExecutionService(
            self.executions, proposal=self.proposal_loader(event.ontology_version)
        )
        events = self.executions.list_audit_events(event.execution_id)
        if not any(item["event_type"] == "manual_review_triggered" for item in events):
            service.record_event(
                event.execution_id,
                event_type="manual_review_triggered",
                actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
                details={
                    "application_id": event.payload.get("application_id"),
                    "review_id": event.payload.get("review_id"),
                    "matched_rules": event.payload.get("matched_rules") or [],
                    "application_status": "待人工复核",
                    "routing_result": "转人工",
                    "graph_mapping_valid": True,
                    "outbox_event_id": event.event_id,
                },
            )
        current = self.executions.get(event.execution_id)
        service.advance(
            current.execution_id,
            "SUCCEEDED",
            expected_version=current.version,
            event_type="action_succeeded",
            actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
            result_summary=self._result(event, graph_result),
        )
        self.outbox.mark_succeeded(event.event_id, self.worker_id)

    def _compensate_dead_letter(self, event: OutboxEvent, original_code: str) -> None:
        actor_id = str(event.payload.get("actor_id") or "outbox-worker")
        proposal = self.proposal_loader(event.ontology_version)
        service = ActionExecutionService(self.executions, proposal=proposal)
        current = self.executions.get(event.execution_id)
        try:
            if current.status == "COMMITTING":
                current = service.advance(
                    current.execution_id,
                    "COMPENSATING",
                    expected_version=current.version,
                    event_type="compensation_started",
                    actor_id=actor_id,
                    details={
                        "original_error_code": original_code,
                        "outbox_event_id": event.event_id,
                    },
                )
            if current.status != "COMPENSATING":
                raise OutboxHandlerError(
                    "ACTION_OUTBOX_STATE_CONFLICT",
                    f"Action 状态 {current.status} 不能执行人工复核补偿",
                )
            effect = manual_review_effect_from_payload(event.payload)
            self.reviews.compensate(effect)
            self._sync_and_verify(proposal)
            current = self.executions.get(event.execution_id)
            service.advance(
                current.execution_id,
                "COMPENSATED",
                expected_version=current.version,
                event_type="action_compensated",
                actor_id=actor_id,
                result_summary={
                    "compensated": True,
                    "original_error_code": original_code,
                    "application_id": effect.application_id,
                    "review_id": effect.review_id,
                    "outbox_event_id": event.event_id,
                },
            )
            self.outbox.mark_compensated(event.event_id)
        except Exception as exc:
            current = self.executions.get(event.execution_id)
            if current.status == "COMPENSATING":
                service.advance(
                    current.execution_id,
                    "COMPENSATION_FAILED",
                    expected_version=current.version,
                    event_type="compensation_failed",
                    actor_id=actor_id,
                    error_code="ACTION_COMPENSATION_FAILED",
                    error_message=str(exc),
                )
            raise


class DecisionRecommendationOutboxWorker(RiskAssessmentOutboxWorker):
    EVENT_TYPE = "DECISION_RECOMMENDATION_GRAPH_SYNC"
    ACTION_NAME = "record_decision_recommendation"

    def __init__(
        self,
        *,
        postgres_service,
        nebula_factory: Callable[[], Any],
        proposal_loader: Callable[[str], Any],
        outbox_repository: PostgresActionOutboxRepository,
        execution_repository,
        recommendation_repository: PostgresDecisionRecommendationRepository,
        worker_id: str | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
        sync_handler: Callable[[Any], Mapping[str, Any]] | None = None,
    ):
        super().__init__(
            postgres_service=postgres_service,
            nebula_factory=nebula_factory,
            proposal_loader=proposal_loader,
            outbox_repository=outbox_repository,
            execution_repository=execution_repository,
            report_repository=recommendation_repository,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            sync_handler=sync_handler,
        )
        self.recommendations = recommendation_repository
        self.recommendations.ensure_schema()

    def _result(
        self, event: OutboxEvent, graph_result: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "application_id": event.payload.get("application_id"),
            "recommendation_id": event.payload.get("recommendation_id"),
            "recommendation_version": event.payload.get("recommendation_version"),
            "recommended_decision": event.payload.get("recommended_decision"),
            "decision_authority": "recommendation_only",
            "evidence_reference": event.payload.get("evidence_reference"),
            "reason_codes": event.payload.get("reason_codes") or [],
            "approver_id": event.payload.get("approver_id"),
            "approver_role": event.payload.get("approver_role"),
            "application_status": (
                (event.payload.get("effect") or {}).get(
                    "applied_application_status"
                )
            ),
            "persistence": {
                "postgres": "recommendation_evidence_and_confirmation_recorded",
                "nebulagraph": "synchronized",
                "outbox_event_id": event.event_id,
                **dict(graph_result),
            },
            "decision_boundary": "recommendation_only_not_final_approval",
        }

    def _finalize_success(
        self, event: OutboxEvent, graph_result: Mapping[str, Any]
    ) -> None:
        current = self.executions.get(event.execution_id)
        if current.status == "SUCCEEDED":
            self.outbox.mark_succeeded(event.event_id, self.worker_id)
            return
        if current.status != "COMMITTING":
            if ActionStateMachine.is_terminal(current.status):
                self.outbox.cancel(event.event_id, f"Action 已进入终态 {current.status}")
                return
            raise OutboxHandlerError(
                "ACTION_OUTBOX_STATE_CONFLICT",
                f"Action 状态 {current.status} 不能完成决策建议图同步",
            )
        service = ActionExecutionService(
            self.executions, proposal=self.proposal_loader(event.ontology_version)
        )
        events = self.executions.list_audit_events(event.execution_id)
        if not any(item["event_type"] == "recommendation_recorded" for item in events):
            service.record_event(
                event.execution_id,
                event_type="recommendation_recorded",
                actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
                details={
                    "application_id": event.payload.get("application_id"),
                    "recommendation_id": event.payload.get("recommendation_id"),
                    "recommendation_version": event.payload.get(
                        "recommendation_version"
                    ),
                    "recommended_decision": event.payload.get(
                        "recommended_decision"
                    ),
                    "decision_authority": "recommendation_only",
                    "graph_mapping_valid": True,
                    "outbox_event_id": event.event_id,
                },
            )
        current = self.executions.get(event.execution_id)
        service.advance(
            current.execution_id,
            "SUCCEEDED",
            expected_version=current.version,
            event_type="action_succeeded",
            actor_id=str(event.payload.get("actor_id") or "outbox-worker"),
            result_summary=self._result(event, graph_result),
        )
        self.outbox.mark_succeeded(event.event_id, self.worker_id)

    def _compensate_dead_letter(self, event: OutboxEvent, original_code: str) -> None:
        actor_id = str(event.payload.get("actor_id") or "outbox-worker")
        proposal = self.proposal_loader(event.ontology_version)
        service = ActionExecutionService(self.executions, proposal=proposal)
        current = self.executions.get(event.execution_id)
        try:
            if current.status == "COMMITTING":
                current = service.advance(
                    current.execution_id,
                    "COMPENSATING",
                    expected_version=current.version,
                    event_type="compensation_started",
                    actor_id=actor_id,
                    details={
                        "original_error_code": original_code,
                        "outbox_event_id": event.event_id,
                    },
                )
            if current.status != "COMPENSATING":
                raise OutboxHandlerError(
                    "ACTION_OUTBOX_STATE_CONFLICT",
                    f"Action 状态 {current.status} 不能执行决策建议补偿",
                )
            effect = decision_recommendation_effect_from_payload(event.payload)
            self.recommendations.compensate(effect)
            self._sync_and_verify(proposal)
            current = self.executions.get(event.execution_id)
            service.advance(
                current.execution_id,
                "COMPENSATED",
                expected_version=current.version,
                event_type="action_compensated",
                actor_id=actor_id,
                result_summary={
                    "compensated": True,
                    "original_error_code": original_code,
                    "application_id": effect.application_id,
                    "recommendation_id": effect.recommendation_id,
                    "outbox_event_id": event.event_id,
                },
            )
            self.outbox.mark_compensated(event.event_id)
        except Exception as exc:
            current = self.executions.get(event.execution_id)
            if current.status == "COMPENSATING":
                service.advance(
                    current.execution_id,
                    "COMPENSATION_FAILED",
                    expected_version=current.version,
                    event_type="compensation_failed",
                    actor_id=actor_id,
                    error_code="ACTION_COMPENSATION_FAILED",
                    error_message=str(exc),
                )
            raise
