from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping
from uuid import uuid4

try:
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:
    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, value):
            self.obj = value

from ..core.security import mask_payload
from .anti_fraud_report_repository import PostgresAntiFraudReportRepository
from .credit_report_repository import PostgresCreditReportRepository
from .decision_recommendation_repository import (
    PostgresDecisionRecommendationRepository,
)
from .loan_application_repository import PostgresLoanApplicationRepository
from .manual_review_repository import PostgresManualReviewRepository
from .outbox import (
    AntiFraudReportOutboxWorker,
    CreditReportOutboxWorker,
    DecisionRecommendationOutboxWorker,
    LoanApplicationOutboxWorker,
    ManualReviewOutboxWorker,
    OutboxEvent,
    PhoneVerificationResultOutboxWorker,
    PhoneVerificationTaskOutboxWorker,
    PostgresActionOutboxRepository,
    RiskAssessmentOutboxWorker,
    json_safe,
)
from .phone_dispatch import PostgresPhoneDispatchCoordinator
from .phone_verification_repository import PostgresPhoneVerificationTaskRepository
from .phone_verification_result_repository import (
    PostgresPhoneVerificationResultRepository,
)
from .risk_assessment_repository import PostgresRiskAssessmentRepository


REPLAY_STATUSES = ("REQUESTED", "PROCESSING", "SUCCEEDED", "FAILED")
REPLAYABLE_EVENT_TYPES = frozenset(
    {
        "RISK_ASSESSMENT_GRAPH_SYNC",
        "LOAN_APPLICATION_GRAPH_SYNC",
        "CREDIT_REPORT_GRAPH_SYNC",
        "ANTI_FRAUD_REPORT_GRAPH_SYNC",
        "PHONE_VERIFICATION_TASK_DISPATCH",
        "PHONE_VERIFICATION_RESULT_GRAPH_SYNC",
        "MANUAL_REVIEW_GRAPH_SYNC",
        "DECISION_RECOMMENDATION_GRAPH_SYNC",
    }
)
EVENT_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


class OutboxReplayError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class OutboxReplayConflict(OutboxReplayError):
    pass


@dataclass(frozen=True)
class OutboxReplayRecord:
    replay_id: str
    event_id: str
    execution_id: str
    event_type: str
    request_id: str
    requested_by: str
    requested_role: str
    reason: str
    reason_digest: str
    status: str
    result_summary: Mapping[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


REPLAY_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS mvp_control.outbox_replay_requests (
        replay_id varchar(36) PRIMARY KEY,
        event_id varchar(36) NOT NULL
            REFERENCES mvp_control.action_outbox(event_id) ON DELETE RESTRICT,
        execution_id varchar(36) NOT NULL
            REFERENCES mvp_control.action_executions(execution_id) ON DELETE RESTRICT,
        event_type varchar(64) NOT NULL,
        request_id varchar(128) NOT NULL,
        requested_by varchar(128) NOT NULL,
        requested_role varchar(64) NOT NULL,
        reason varchar(500) NOT NULL,
        reason_digest char(64) NOT NULL,
        status varchar(24) NOT NULL
            CHECK (status IN ('REQUESTED','PROCESSING','SUCCEEDED','FAILED')),
        result_summary jsonb,
        error_code varchar(64),
        error_message varchar(500),
        created_at timestamptz NOT NULL DEFAULT now(),
        started_at timestamptz,
        completed_at timestamptz,
        UNIQUE (event_id,request_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS outbox_replay_requests_event_idx
    ON mvp_control.outbox_replay_requests (event_id,created_at DESC)
    """,
)


def replay_reason_digest(reason: str) -> str:
    return hashlib.sha256(reason.encode("utf-8")).hexdigest()


def _record(row: Mapping[str, Any]) -> OutboxReplayRecord:
    return OutboxReplayRecord(
        replay_id=str(row["replay_id"]),
        event_id=str(row["event_id"]),
        execution_id=str(row["execution_id"]),
        event_type=str(row["event_type"]),
        request_id=str(row["request_id"]),
        requested_by=str(row["requested_by"]),
        requested_role=str(row["requested_role"]),
        reason=str(row["reason"]),
        reason_digest=str(row["reason_digest"]),
        status=str(row["status"]),
        result_summary=row.get("result_summary"),
        error_code=row.get("error_code"),
        error_message=row.get("error_message"),
        created_at=row.get("created_at"),
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
    )


class PostgresOutboxReplayRepository:
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
                cursor.execute("CREATE SCHEMA IF NOT EXISTS mvp_control")
                for statement in REPLAY_SCHEMA:
                    cursor.execute(statement)
            type(self)._schema_ready = True

    def reserve(
        self,
        *,
        event_id: str,
        request_id: str,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> tuple[OutboxReplayRecord, bool, OutboxEvent]:
        self.ensure_schema()
        digest = replay_reason_digest(reason)
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT event.*,execution.status AS action_status
                FROM mvp_control.action_outbox AS event
                JOIN mvp_control.action_executions AS execution
                  ON execution.execution_id=event.execution_id
                WHERE event.event_id=%s FOR UPDATE OF event
                """,
                (event_id,),
            )
            event_row = cursor.fetchone()
            if not event_row:
                raise OutboxReplayError(
                    "ACTION_OUTBOX_EVENT_NOT_FOUND", "outbox 事件不存在"
                )
            cursor.execute(
                """
                SELECT * FROM mvp_control.outbox_replay_requests
                WHERE event_id=%s AND request_id=%s
                """,
                (event_id, request_id),
            )
            existing = cursor.fetchone()
            if existing:
                if (
                    existing["reason_digest"] != digest
                    or existing["requested_by"] != actor_id
                ):
                    raise OutboxReplayConflict(
                        "ACTION_OUTBOX_REPLAY_CONFLICT",
                        "同一重放 request_id 的内容或操作人发生变化",
                    )
                return _record(existing), False, _event_from_joined_row(event_row)
            if event_row["event_type"] not in REPLAYABLE_EVENT_TYPES:
                raise OutboxReplayError(
                    "ACTION_OUTBOX_HANDLER_NOT_FOUND", "outbox 事件没有重放 handler"
                )
            if event_row["status"] != "DEAD_LETTER":
                raise OutboxReplayConflict(
                    "ACTION_OUTBOX_REPLAY_NOT_ALLOWED",
                    f"只有 DEAD_LETTER 可以人工重放，当前为 {event_row['status']}",
                )
            if event_row["action_status"] not in {
                "COMMITTING", "FAILED", "COMPENSATION_FAILED"
            }:
                raise OutboxReplayConflict(
                    "ACTION_OUTBOX_REPLAY_NOT_ALLOWED",
                    f"Action 状态 {event_row['action_status']} 不允许人工重放",
                )
            cursor.execute(
                """
                SELECT replay_id FROM mvp_control.outbox_replay_requests
                WHERE event_id=%s AND status IN ('REQUESTED','PROCESSING')
                FOR UPDATE
                """,
                (event_id,),
            )
            if cursor.fetchone():
                raise OutboxReplayConflict(
                    "ACTION_OUTBOX_REPLAY_IN_PROGRESS", "该事件已有重放正在执行"
                )
            replay_id = str(uuid4())
            cursor.execute(
                """
                INSERT INTO mvp_control.outbox_replay_requests (
                    replay_id,event_id,execution_id,event_type,request_id,
                    requested_by,requested_role,reason,reason_digest,status
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'REQUESTED')
                RETURNING *
                """,
                (
                    replay_id,
                    event_id,
                    event_row["execution_id"],
                    event_row["event_type"],
                    request_id,
                    actor_id,
                    actor_role,
                    reason,
                    digest,
                ),
            )
            return _record(cursor.fetchone()), True, _event_from_joined_row(event_row)

    def mark_processing(self, replay_id: str) -> OutboxReplayRecord:
        return self._transition(replay_id, "REQUESTED", "PROCESSING")

    def mark_succeeded(
        self, replay_id: str, result_summary: Mapping[str, Any]
    ) -> OutboxReplayRecord:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE mvp_control.outbox_replay_requests
                SET status='SUCCEEDED',result_summary=%s,error_code=NULL,
                    error_message=NULL,completed_at=now()
                WHERE replay_id=%s AND status='PROCESSING' RETURNING *
                """,
                (Jsonb(json_safe(result_summary)), replay_id),
            )
            row = cursor.fetchone()
        if not row:
            raise OutboxReplayConflict(
                "ACTION_OUTBOX_REPLAY_CONFLICT", "重放成功状态并发冲突"
            )
        return _record(row)

    def mark_failed(
        self, replay_id: str, *, error_code: str, error_message: str
    ) -> OutboxReplayRecord:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE mvp_control.outbox_replay_requests
                SET status='FAILED',error_code=%s,error_message=%s,completed_at=now()
                WHERE replay_id=%s AND status='PROCESSING' RETURNING *
                """,
                (
                    error_code[:64],
                    str(mask_payload(error_message))[:500],
                    replay_id,
                ),
            )
            row = cursor.fetchone()
        if not row:
            raise OutboxReplayConflict(
                "ACTION_OUTBOX_REPLAY_CONFLICT", "重放失败状态并发冲突"
            )
        return _record(row)

    def _transition(
        self, replay_id: str, source: str, target: str
    ) -> OutboxReplayRecord:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE mvp_control.outbox_replay_requests
                SET status=%s,started_at=CASE WHEN %s='PROCESSING' THEN now()
                    ELSE started_at END
                WHERE replay_id=%s AND status=%s RETURNING *
                """,
                (target, target, replay_id, source),
            )
            row = cursor.fetchone()
        if not row:
            raise OutboxReplayConflict(
                "ACTION_OUTBOX_REPLAY_CONFLICT", "重放请求状态并发冲突"
            )
        return _record(row)

    def list_requests(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self.ensure_schema()
        limit = max(1, min(int(limit), 500))
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM mvp_control.outbox_replay_requests
                ORDER BY created_at DESC,replay_id DESC LIMIT %s
                """,
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def alerts(
        self, *, lease_seconds: int = 60, limit: int = 100
    ) -> dict[str, Any]:
        self.ensure_schema()
        lease_seconds = max(5, int(lease_seconds))
        limit = max(1, min(int(limit), 500))
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT event.event_id,event.execution_id,event.event_type,
                       event.aggregate_type,event.aggregate_id,event.status,
                       event.attempt_count,event.max_attempts,event.last_error_code,
                       event.last_error_message,event.created_at,event.updated_at,
                       execution.action_name,execution.status AS action_status,
                       replay.replay_id AS latest_replay_id,
                       replay.status AS latest_replay_status,
                       replay.completed_at AS latest_replay_completed_at
                FROM mvp_control.action_outbox AS event
                JOIN mvp_control.action_executions AS execution
                  ON execution.execution_id=event.execution_id
                LEFT JOIN LATERAL (
                    SELECT * FROM mvp_control.outbox_replay_requests request
                    WHERE request.event_id=event.event_id
                    ORDER BY request.created_at DESC,request.replay_id DESC LIMIT 1
                ) AS replay ON true
                WHERE event.status='DEAD_LETTER'
                   OR (event.status='PROCESSING'
                       AND event.locked_at < now()-(%s*interval '1 second'))
                ORDER BY event.updated_at DESC,event.event_id DESC LIMIT %s
                """,
                (lease_seconds, limit),
            )
            events = [dict(row) for row in cursor.fetchall()]
        return {
            "requires_attention": len(events),
            "events": events,
            "replayable_event_types": sorted(REPLAYABLE_EVENT_TYPES),
        }


def _event_from_joined_row(row: Mapping[str, Any]) -> OutboxEvent:
    return OutboxEvent(
        event_id=str(row["event_id"]),
        execution_id=str(row["execution_id"]),
        event_type=str(row["event_type"]),
        aggregate_type=str(row["aggregate_type"]),
        aggregate_id=str(row["aggregate_id"]),
        ontology_version=str(row["ontology_version"]),
        deduplication_key=str(row["deduplication_key"]),
        payload=row.get("payload") or {},
        status=str(row["status"]),
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        available_at=row.get("available_at"),
        locked_at=row.get("locked_at"),
        lock_owner=row.get("lock_owner"),
        last_error_code=row.get("last_error_code"),
        last_error_message=row.get("last_error_message"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        processed_at=row.get("processed_at"),
    )


class OutboxReplayService:
    def __init__(
        self,
        *,
        repository: PostgresOutboxReplayRepository,
        execution_repository,
        workers: Mapping[str, Any],
    ):
        self.repository = repository
        self.repository.ensure_schema()
        self.executions = execution_repository
        self.workers = dict(workers)

    def execute(
        self,
        *,
        event_id: str,
        request_id: str,
        reason: str,
        actor_id: str,
        actor_role: str,
    ) -> dict[str, Any]:
        event_id = str(event_id or "").strip()
        request_id = str(request_id or "").strip()
        reason = " ".join(str(reason or "").strip().split())
        if not EVENT_ID_PATTERN.fullmatch(event_id):
            raise OutboxReplayError(
                "ACTION_OUTBOX_EVENT_INVALID", "event_id 必须是有效 UUID"
            )
        if not request_id or len(request_id) > 128:
            raise OutboxReplayError(
                "ACTION_OUTBOX_REPLAY_REQUEST_INVALID", "request_id 不能为空且不能超过128字符"
            )
        if len(reason) < 10 or len(reason) > 500:
            raise OutboxReplayError(
                "ACTION_OUTBOX_REPLAY_REQUEST_INVALID", "重放原因必须为10至500字符"
            )
        if not actor_id or not actor_role:
            raise OutboxReplayError(
                "ACTION_OUTBOX_REPLAY_UNAUTHORIZED", "服务端操作身份未配置"
            )
        replay, created, event = self.repository.reserve(
            event_id=event_id,
            request_id=request_id,
            actor_id=actor_id,
            actor_role=actor_role,
            reason=reason,
        )
        if not created:
            return self._response(replay, idempotent_replay=True)
        self.repository.mark_processing(replay.replay_id)
        self.executions.record_event(
            event.execution_id,
            event_type="manual_outbox_replay_requested",
            actor_id=actor_id,
            details={
                "replay_id": replay.replay_id,
                "outbox_event_id": event.event_id,
                "event_type": event.event_type,
                "reason_digest": replay.reason_digest,
            },
        )
        worker = self.workers.get(event.event_type)
        if worker is None:
            failed = self.repository.mark_failed(
                replay.replay_id,
                error_code="ACTION_OUTBOX_HANDLER_NOT_FOUND",
                error_message="没有注册人工重放 handler",
            )
            return self._response(failed, idempotent_replay=False)
        try:
            resolved_event, result = worker.replay_dead_letter(
                event.event_id,
                actor_id=actor_id,
                replay_id=replay.replay_id,
                reason_digest=replay.reason_digest,
            )
            completed = self.repository.mark_succeeded(
                replay.replay_id,
                {
                    "event_id": resolved_event.event_id,
                    "event_status": resolved_event.status,
                    "original_action_status_preserved": self.executions.get(
                        event.execution_id
                    ).status,
                    "remediation": result,
                },
            )
            return self._response(completed, idempotent_replay=False)
        except Exception as exc:
            code = getattr(exc, "code", "ACTION_OUTBOX_MANUAL_REPLAY_FAILED")
            failed = self.repository.mark_failed(
                replay.replay_id,
                error_code=str(code),
                error_message=str(exc) or type(exc).__name__,
            )
            self.executions.record_event(
                event.execution_id,
                event_type="manual_outbox_replay_failed",
                actor_id=actor_id,
                details={
                    "replay_id": replay.replay_id,
                    "outbox_event_id": event.event_id,
                    "error_code": str(code),
                    "reason_digest": replay.reason_digest,
                },
            )
            return self._response(failed, idempotent_replay=False)

    @staticmethod
    def _response(
        replay: OutboxReplayRecord, *, idempotent_replay: bool
    ) -> dict[str, Any]:
        return {
            "replay_id": replay.replay_id,
            "event_id": replay.event_id,
            "execution_id": replay.execution_id,
            "event_type": replay.event_type,
            "status": replay.status,
            "idempotent_replay": idempotent_replay,
            "result": dict(replay.result_summary or {}),
            "error_code": replay.error_code,
            "error_message": replay.error_message,
            "original_action_history_preserved": True,
        }


def build_outbox_replay_workers(
    *,
    postgres_service,
    nebula_factory,
    proposal_loader,
    outbox_repository: PostgresActionOutboxRepository,
    execution_repository,
    phone_dispatcher,
    phone_circuit_breaker=None,
    lease_seconds: int = 60,
    retry_base_seconds: int = 5,
    retry_max_seconds: int = 300,
) -> dict[str, Any]:
    common = {
        "postgres_service": postgres_service,
        "nebula_factory": nebula_factory,
        "proposal_loader": proposal_loader,
        "outbox_repository": outbox_repository,
        "execution_repository": execution_repository,
        "lease_seconds": lease_seconds,
        "retry_base_seconds": retry_base_seconds,
        "retry_max_seconds": retry_max_seconds,
    }
    tasks = PostgresPhoneVerificationTaskRepository(postgres_service)
    workers = (
        RiskAssessmentOutboxWorker(
            **common,
            report_repository=PostgresRiskAssessmentRepository(postgres_service),
        ),
        LoanApplicationOutboxWorker(
            **common,
            application_repository=PostgresLoanApplicationRepository(postgres_service),
        ),
        CreditReportOutboxWorker(
            **common,
            report_repository=PostgresCreditReportRepository(postgres_service),
        ),
        AntiFraudReportOutboxWorker(
            **common,
            report_repository=PostgresAntiFraudReportRepository(postgres_service),
        ),
        PhoneVerificationTaskOutboxWorker(
            **common,
            task_repository=tasks,
            dispatch_coordinator=PostgresPhoneDispatchCoordinator(
                postgres_service, phone_dispatcher, phone_circuit_breaker
            ),
        ),
        PhoneVerificationResultOutboxWorker(
            **common,
            result_repository=PostgresPhoneVerificationResultRepository(
                postgres_service, tasks
            ),
        ),
        ManualReviewOutboxWorker(
            **common,
            review_repository=PostgresManualReviewRepository(postgres_service),
        ),
        DecisionRecommendationOutboxWorker(
            **common,
            recommendation_repository=PostgresDecisionRecommendationRepository(
                postgres_service
            ),
        ),
    )
    return {worker.EVENT_TYPE: worker for worker in workers}
