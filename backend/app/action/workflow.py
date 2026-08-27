from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

try:
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:  # Lightweight unit-test runtime; production installs psycopg.
    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, value):
            self.obj = value

from ..core.security import mask_payload
from .execution import sha256_digest


WORKFLOW_VERSION = "personal-credit-workflow-v1"
WORKFLOW_STATUSES = (
    "WAITING_INPUT",
    "RUNNING",
    "FAILED",
    "COMPLETED",
    "CANCELLED",
)
WORKFLOW_STEP_STATUSES = (
    "PENDING",
    "READY",
    "RUNNING",
    "WAITING",
    "COMPLETED",
    "FAILED",
    "SKIPPED",
)
ACTION_TERMINAL_FAILURES = frozenset(
    {"FAILED", "COMPENSATED", "COMPENSATION_FAILED"}
)

WORKFLOW_ACTIONS = (
    "create_loan_application",
    "request_credit_report",
    "request_fraud_report",
    "generate_risk_assessment",
    "create_phone_verification_task",
    "record_phone_verification",
    "trigger_manual_review",
    "record_decision_recommendation",
)

WORKFLOW_ACTION_LABELS = {
    "create_loan_application": "创建贷款申请",
    "request_credit_report": "发起征信查询",
    "request_fraud_report": "发起反欺诈查询",
    "generate_risk_assessment": "生成风险评估",
    "create_phone_verification_task": "创建电核任务",
    "record_phone_verification": "记录电核结果",
    "trigger_manual_review": "触发人工复核",
    "record_decision_recommendation": "记录决策建议",
}

WORKFLOW_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "create_loan_application": (),
    "request_credit_report": ("create_loan_application",),
    "request_fraud_report": ("create_loan_application",),
    "generate_risk_assessment": (
        "request_credit_report",
        "request_fraud_report",
    ),
    "create_phone_verification_task": ("generate_risk_assessment",),
    "record_phone_verification": ("create_phone_verification_task",),
    "trigger_manual_review": ("record_phone_verification",),
    "record_decision_recommendation": (
        "record_phone_verification",
        "trigger_manual_review",
    ),
}

OPTIONAL_WORKFLOW_ACTIONS = frozenset({"trigger_manual_review"})
APPLICATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}$")


class WorkflowError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def workflow_request_digest(payload: Mapping[str, Any]) -> str:
    if not isinstance(payload, Mapping):
        raise WorkflowError("WORKFLOW_REQUEST_INVALID", "Action 请求必须是对象")
    business_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"request_id", "callback_id", "correlation_id"}
    }
    return sha256_digest(business_payload)


def ready_workflow_actions(step_statuses: Mapping[str, str]) -> tuple[str, ...]:
    ready: list[str] = []
    for action_name in WORKFLOW_ACTIONS:
        if step_statuses.get(action_name) != "PENDING":
            continue
        dependencies = WORKFLOW_DEPENDENCIES[action_name]
        if all(
            step_statuses.get(dependency) in {"COMPLETED", "SKIPPED"}
            for dependency in dependencies
        ):
            ready.append(action_name)
    return tuple(ready)


_WORKFLOW_STATUS_SQL = ",".join(f"'{item}'" for item in WORKFLOW_STATUSES)
_STEP_STATUS_SQL = ",".join(f"'{item}'" for item in WORKFLOW_STEP_STATUSES)

WORKFLOW_SCHEMA_STATEMENTS = (
    "CREATE SCHEMA IF NOT EXISTS mvp_control",
    f"""
    CREATE TABLE IF NOT EXISTS mvp_control.personal_credit_workflows (
        workflow_id varchar(36) PRIMARY KEY,
        workflow_version varchar(64) NOT NULL,
        ontology_version varchar(64) NOT NULL,
        application_id varchar(32) NOT NULL,
        entry_mode varchar(16) NOT NULL CHECK (entry_mode IN ('NEW','EXISTING')),
        status varchar(32) NOT NULL CHECK (status IN ({_WORKFLOW_STATUS_SQL})),
        actor_id varchar(128) NOT NULL,
        actor_role varchar(64) NOT NULL,
        correlation_id varchar(128) NOT NULL UNIQUE,
        version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        completed_at timestamptz
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS personal_credit_workflows_active_application_idx
    ON mvp_control.personal_credit_workflows (application_id)
    WHERE status IN ('WAITING_INPUT','RUNNING','FAILED')
    """,
    """
    CREATE INDEX IF NOT EXISTS personal_credit_workflows_status_updated_idx
    ON mvp_control.personal_credit_workflows (status,updated_at DESC)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS mvp_control.personal_credit_workflow_steps (
        workflow_id varchar(36) NOT NULL
            REFERENCES mvp_control.personal_credit_workflows(workflow_id) ON DELETE RESTRICT,
        position integer NOT NULL CHECK (position > 0),
        action_name varchar(64) NOT NULL,
        action_label varchar(64) NOT NULL,
        optional boolean NOT NULL DEFAULT false,
        status varchar(32) NOT NULL CHECK (status IN ({_STEP_STATUS_SQL})),
        attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        execution_id varchar(36)
            REFERENCES mvp_control.action_executions(execution_id) ON DELETE RESTRICT,
        dispatch_request_id varchar(128),
        request_digest char(64),
        input_summary jsonb,
        result_summary jsonb,
        last_error_code varchar(96),
        last_error_message varchar(500),
        lease_expires_at timestamptz,
        started_at timestamptz,
        completed_at timestamptz,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (workflow_id,action_name),
        UNIQUE (workflow_id,position),
        UNIQUE (execution_id),
        UNIQUE (dispatch_request_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS personal_credit_workflow_steps_status_idx
    ON mvp_control.personal_credit_workflow_steps (status,updated_at)
    """,
    """
    ALTER TABLE mvp_control.personal_credit_workflow_steps
    ADD COLUMN IF NOT EXISTS input_summary jsonb
    """,
    """
    CREATE TABLE IF NOT EXISTS mvp_control.personal_credit_workflow_events (
        event_id bigserial PRIMARY KEY,
        workflow_id varchar(36) NOT NULL
            REFERENCES mvp_control.personal_credit_workflows(workflow_id) ON DELETE RESTRICT,
        sequence_no integer NOT NULL CHECK (sequence_no > 0),
        event_type varchar(64) NOT NULL,
        action_name varchar(64),
        from_status varchar(32),
        to_status varchar(32) NOT NULL,
        actor_id varchar(128) NOT NULL,
        details jsonb NOT NULL DEFAULT '{}'::jsonb,
        previous_event_digest char(64),
        event_digest char(64) NOT NULL CHECK (char_length(event_digest)=64),
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (workflow_id,sequence_no),
        UNIQUE (workflow_id,event_digest)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS personal_credit_workflow_events_instance_idx
    ON mvp_control.personal_credit_workflow_events (workflow_id,sequence_no)
    """,
    """
    CREATE OR REPLACE FUNCTION mvp_control.prevent_workflow_event_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'personal_credit_workflow_events is append-only';
    END;
    $$
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgname='personal_credit_workflow_events_append_only'
              AND tgrelid='mvp_control.personal_credit_workflow_events'::regclass
        ) THEN
            EXECUTE 'CREATE TRIGGER personal_credit_workflow_events_append_only
                BEFORE UPDATE OR DELETE ON mvp_control.personal_credit_workflow_events
                FOR EACH ROW EXECUTE FUNCTION mvp_control.prevent_workflow_event_mutation()';
        END IF;
    END;
    $$
    """,
)


def _safe_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise WorkflowError("WORKFLOW_REQUEST_INVALID", "流程摘要必须是对象")
    masked = mask_payload(dict(value))
    return dict(masked) if isinstance(masked, Mapping) else {}


def _safe_message(value: Any, limit: int = 500) -> str:
    masked = mask_payload(str(value or "").strip())
    return str(masked or "")[:limit]


class PostgresPersonalCreditWorkflowRepository:
    _schema_lock = threading.Lock()
    _schema_ready = False

    def __init__(self, postgres_service, *, dispatch_lease_seconds: int = 120):
        self.postgres = postgres_service
        self.dispatch_lease_seconds = max(30, int(dispatch_lease_seconds))

    def ensure_schema(self) -> None:
        if type(self)._schema_ready:
            return
        with type(self)._schema_lock:
            if type(self)._schema_ready:
                return
            with self.postgres.conn() as connection, connection.cursor() as cursor:
                for statement in WORKFLOW_SCHEMA_STATEMENTS:
                    cursor.execute(statement)
            type(self)._schema_ready = True

    @staticmethod
    def _validate_text(value: str, field_name: str, max_length: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise WorkflowError("WORKFLOW_REQUEST_INVALID", f"{field_name} 不能为空")
        normalized = value.strip()
        if len(normalized) > max_length:
            raise WorkflowError("WORKFLOW_REQUEST_INVALID", f"{field_name} 超出长度限制")
        return normalized

    @staticmethod
    def _insert_event(
        cursor,
        *,
        workflow_id: str,
        event_type: str,
        action_name: str | None,
        from_status: str | None,
        to_status: str,
        actor_id: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        safe_details = _safe_mapping(details)
        cursor.execute(
            """
            SELECT sequence_no,event_digest
            FROM mvp_control.personal_credit_workflow_events
            WHERE workflow_id=%s ORDER BY sequence_no DESC LIMIT 1
            """,
            (workflow_id,),
        )
        previous = cursor.fetchone()
        sequence_no = int(previous["sequence_no"]) + 1 if previous else 1
        previous_digest = previous["event_digest"] if previous else None
        digest = sha256_digest(
            {
                "workflow_id": workflow_id,
                "sequence_no": sequence_no,
                "event_type": event_type,
                "action_name": action_name,
                "from_status": from_status,
                "to_status": to_status,
                "actor_id": actor_id,
                "details": safe_details,
                "previous_event_digest": previous_digest,
            }
        )
        cursor.execute(
            """
            INSERT INTO mvp_control.personal_credit_workflow_events (
                workflow_id,sequence_no,event_type,action_name,from_status,to_status,
                actor_id,details,previous_event_digest,event_digest
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                workflow_id,
                sequence_no,
                event_type,
                action_name,
                from_status,
                to_status,
                actor_id,
                Jsonb(safe_details),
                previous_digest,
                digest,
            ),
        )

    @staticmethod
    def _workflow(cursor, workflow_id: str, *, lock: bool = False):
        cursor.execute(
            f"""
            SELECT * FROM mvp_control.personal_credit_workflows
            WHERE workflow_id=%s {'FOR UPDATE' if lock else ''}
            """,
            (workflow_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise WorkflowError("WORKFLOW_NOT_FOUND", "个人信审流程不存在")
        return row

    @staticmethod
    def _steps(cursor, workflow_id: str, *, lock: bool = False):
        cursor.execute(
            f"""
            SELECT * FROM mvp_control.personal_credit_workflow_steps
            WHERE workflow_id=%s ORDER BY position {'FOR UPDATE' if lock else ''}
            """,
            (workflow_id,),
        )
        return list(cursor.fetchall())

    def create(
        self,
        *,
        application_id: str,
        entry_mode: str,
        ontology_version: str,
        actor_id: str,
        actor_role: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        self.ensure_schema()
        application_id = self._validate_text(application_id, "application_id", 32)
        if not APPLICATION_ID_PATTERN.fullmatch(application_id):
            raise WorkflowError("WORKFLOW_REQUEST_INVALID", "application_id 格式无效")
        entry_mode = self._validate_text(entry_mode, "entry_mode", 16).upper()
        if entry_mode not in {"NEW", "EXISTING"}:
            raise WorkflowError(
                "WORKFLOW_REQUEST_INVALID", "entry_mode 仅支持 NEW 或 EXISTING"
            )
        ontology_version = self._validate_text(
            ontology_version, "ontology_version", 64
        )
        actor_id = self._validate_text(actor_id, "actor_id", 128)
        actor_role = self._validate_text(actor_role, "actor_role", 64)
        correlation_id = self._validate_text(correlation_id, "correlation_id", 128)
        if not IDENTIFIER_PATTERN.fullmatch(correlation_id):
            raise WorkflowError("WORKFLOW_REQUEST_INVALID", "request_id 格式无效")

        workflow_id = str(uuid4())
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (application_id,))
            cursor.execute(
                """
                SELECT * FROM mvp_control.personal_credit_workflows
                WHERE correlation_id=%s FOR UPDATE
                """,
                (correlation_id,),
            )
            repeated = cursor.fetchone()
            if repeated:
                if (
                    repeated["application_id"] == application_id
                    and repeated["entry_mode"] == entry_mode
                ):
                    return self._detail(cursor, repeated)
                raise WorkflowError(
                    "WORKFLOW_IDEMPOTENCY_CONFLICT",
                    "相同 request_id 已用于不同流程请求",
                )

            cursor.execute(
                """
                SELECT workflow_id FROM mvp_control.personal_credit_workflows
                WHERE application_id=%s
                  AND status IN ('WAITING_INPUT','RUNNING','FAILED')
                LIMIT 1 FOR UPDATE
                """,
                (application_id,),
            )
            active = cursor.fetchone()
            if active:
                raise WorkflowError(
                    "WORKFLOW_APPLICATION_ACTIVE",
                    f"申请已有未结束流程：{active['workflow_id']}",
                )

            cursor.execute(
                "SELECT 1 FROM public.loan_application WHERE application_id=%s",
                (application_id,),
            )
            application_exists = cursor.fetchone() is not None
            if entry_mode == "EXISTING" and not application_exists:
                raise WorkflowError("WORKFLOW_APPLICATION_NOT_FOUND", "贷款申请不存在")
            if entry_mode == "NEW" and application_exists:
                raise WorkflowError(
                    "WORKFLOW_APPLICATION_ALREADY_EXISTS",
                    "贷款申请已存在，请使用 EXISTING 模式",
                )

            cursor.execute(
                """
                INSERT INTO mvp_control.personal_credit_workflows (
                    workflow_id,workflow_version,ontology_version,application_id,
                    entry_mode,status,actor_id,actor_role,correlation_id
                ) VALUES (%s,%s,%s,%s,%s,'WAITING_INPUT',%s,%s,%s)
                RETURNING *
                """,
                (
                    workflow_id,
                    WORKFLOW_VERSION,
                    ontology_version,
                    application_id,
                    entry_mode,
                    actor_id,
                    actor_role,
                    correlation_id,
                ),
            )
            workflow = cursor.fetchone()
            for position, action_name in enumerate(WORKFLOW_ACTIONS, start=1):
                if action_name == "create_loan_application":
                    status = "READY" if entry_mode == "NEW" else "SKIPPED"
                elif action_name in {"request_credit_report", "request_fraud_report"}:
                    status = "READY" if entry_mode == "EXISTING" else "PENDING"
                else:
                    status = "PENDING"
                cursor.execute(
                    """
                    INSERT INTO mvp_control.personal_credit_workflow_steps (
                        workflow_id,position,action_name,action_label,optional,status,
                        completed_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,
                        CASE WHEN %s='SKIPPED' THEN now() ELSE NULL END)
                    """,
                    (
                        workflow_id,
                        position,
                        action_name,
                        WORKFLOW_ACTION_LABELS[action_name],
                        action_name in OPTIONAL_WORKFLOW_ACTIONS,
                        status,
                        status,
                    ),
                )
            self._insert_event(
                cursor,
                workflow_id=workflow_id,
                event_type="workflow_started",
                action_name=None,
                from_status=None,
                to_status="WAITING_INPUT",
                actor_id=actor_id,
                details={
                    "application_id": application_id,
                    "entry_mode": entry_mode,
                    "ontology_version": ontology_version,
                },
            )
            if entry_mode == "EXISTING":
                self._insert_event(
                    cursor,
                    workflow_id=workflow_id,
                    event_type="workflow_step_skipped",
                    action_name="create_loan_application",
                    from_status="PENDING",
                    to_status="SKIPPED",
                    actor_id=actor_id,
                    details={"reason": "使用已存在贷款申请作为流程入口"},
                )
            return self._detail(cursor, workflow)

    @staticmethod
    def _derive_workflow_status(steps: list[Mapping[str, Any]]) -> str:
        by_name = {item["action_name"]: item["status"] for item in steps}
        if by_name.get("record_decision_recommendation") == "COMPLETED":
            return "COMPLETED"
        if any(item["status"] == "FAILED" for item in steps):
            return "FAILED"
        if any(item["status"] in {"RUNNING", "WAITING"} for item in steps):
            return "RUNNING"
        return "WAITING_INPUT"

    def _recompute(
        self,
        cursor,
        workflow: Mapping[str, Any],
        *,
        actor_id: str,
    ):
        if workflow["status"] in {"COMPLETED", "CANCELLED"}:
            return workflow
        steps = self._steps(cursor, workflow["workflow_id"], lock=True)
        statuses = {item["action_name"]: item["status"] for item in steps}
        for action_name in ready_workflow_actions(statuses):
            cursor.execute(
                """
                UPDATE mvp_control.personal_credit_workflow_steps
                SET status='READY',updated_at=now()
                WHERE workflow_id=%s AND action_name=%s AND status='PENDING'
                """,
                (workflow["workflow_id"], action_name),
            )
            if cursor.rowcount:
                self._insert_event(
                    cursor,
                    workflow_id=workflow["workflow_id"],
                    event_type="workflow_step_ready",
                    action_name=action_name,
                    from_status="PENDING",
                    to_status="READY",
                    actor_id=actor_id,
                    details={"dependencies": WORKFLOW_DEPENDENCIES[action_name]},
                )
                statuses[action_name] = "READY"
        steps = self._steps(cursor, workflow["workflow_id"], lock=True)
        target_status = self._derive_workflow_status(steps)
        if target_status != workflow["status"]:
            previous_status = workflow["status"]
            cursor.execute(
                """
                UPDATE mvp_control.personal_credit_workflows
                SET status=%s,version=version+1,updated_at=now(),
                    completed_at=CASE WHEN %s='COMPLETED' THEN now() ELSE NULL END
                WHERE workflow_id=%s RETURNING *
                """,
                (target_status, target_status, workflow["workflow_id"]),
            )
            workflow = cursor.fetchone()
            self._insert_event(
                cursor,
                workflow_id=workflow["workflow_id"],
                event_type="workflow_status_changed",
                action_name=None,
                from_status=previous_status,
                to_status=target_status,
                actor_id=actor_id,
                details={},
            )
        else:
            cursor.execute(
                """
                UPDATE mvp_control.personal_credit_workflows
                SET updated_at=now() WHERE workflow_id=%s RETURNING *
                """,
                (workflow["workflow_id"],),
            )
            workflow = cursor.fetchone()
        return workflow

    def begin_step(
        self,
        workflow_id: str,
        action_name: str,
        *,
        dispatch_request_id: str,
        request_digest: str,
        input_summary: Mapping[str, Any] | None,
        actor_id: str,
    ) -> dict[str, Any]:
        self.ensure_schema()
        if action_name not in WORKFLOW_ACTIONS:
            raise WorkflowError("WORKFLOW_STEP_NOT_FOUND", "流程步骤不存在")
        if not IDENTIFIER_PATTERN.fullmatch(dispatch_request_id):
            raise WorkflowError("WORKFLOW_REQUEST_INVALID", "派发请求编号格式无效")
        safe_input = _safe_mapping(input_summary)
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            workflow = self._workflow(cursor, workflow_id, lock=True)
            if workflow["status"] != "WAITING_INPUT":
                raise WorkflowError(
                    "WORKFLOW_NOT_READY",
                    f"流程当前状态不允许执行步骤：{workflow['status']}",
                )
            cursor.execute(
                """
                SELECT * FROM mvp_control.personal_credit_workflow_steps
                WHERE workflow_id=%s AND action_name=%s FOR UPDATE
                """,
                (workflow_id, action_name),
            )
            step = cursor.fetchone()
            if not step:
                raise WorkflowError("WORKFLOW_STEP_NOT_FOUND", "流程步骤不存在")
            if step["status"] != "READY":
                raise WorkflowError(
                    "WORKFLOW_STEP_NOT_READY",
                    f"步骤当前不可执行：{step['status']}",
                )
            cursor.execute(
                """
                UPDATE mvp_control.personal_credit_workflow_steps
                SET status='RUNNING',attempt_count=attempt_count+1,
                    dispatch_request_id=%s,request_digest=%s,input_summary=%s,
                    lease_expires_at=now()+(%s*interval '1 second'),
                    started_at=COALESCE(started_at,now()),updated_at=now(),
                    last_error_code=NULL,last_error_message=NULL
                WHERE workflow_id=%s AND action_name=%s RETURNING *
                """,
                (
                    dispatch_request_id,
                    request_digest,
                    Jsonb(safe_input),
                    self.dispatch_lease_seconds,
                    workflow_id,
                    action_name,
                ),
            )
            step = cursor.fetchone()
            self._insert_event(
                cursor,
                workflow_id=workflow_id,
                event_type="workflow_step_dispatched",
                action_name=action_name,
                from_status="READY",
                to_status="RUNNING",
                actor_id=actor_id,
                details={
                    "attempt_count": step["attempt_count"],
                    "request_digest": request_digest,
                    "input_fields": sorted(safe_input),
                },
            )
            self._recompute(cursor, workflow, actor_id=actor_id)
            return dict(step)

    def attach_action_result(
        self,
        workflow_id: str,
        action_name: str,
        *,
        execution_id: str,
        action_status: str,
        result_summary: Mapping[str, Any] | None,
        error_code: str | None,
        error_message: str | None,
        actor_id: str,
    ) -> dict[str, Any]:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            workflow = self._workflow(cursor, workflow_id, lock=True)
            cursor.execute(
                """
                SELECT * FROM mvp_control.personal_credit_workflow_steps
                WHERE workflow_id=%s AND action_name=%s FOR UPDATE
                """,
                (workflow_id, action_name),
            )
            step = cursor.fetchone()
            if not step or step["status"] not in {"RUNNING", "WAITING"}:
                raise WorkflowError("WORKFLOW_STEP_CONFLICT", "流程步骤派发状态已变化")
            if action_status == "SUCCEEDED":
                target = "COMPLETED"
            elif action_status in ACTION_TERMINAL_FAILURES:
                target = "FAILED"
            else:
                target = "WAITING"
            safe_result = _safe_mapping(result_summary)
            cursor.execute(
                """
                UPDATE mvp_control.personal_credit_workflow_steps
                SET status=%s,execution_id=%s,result_summary=%s,
                    last_error_code=%s,last_error_message=%s,lease_expires_at=NULL,
                    completed_at=CASE WHEN %s IN ('COMPLETED','FAILED') THEN now()
                                      ELSE completed_at END,
                    updated_at=now()
                WHERE workflow_id=%s AND action_name=%s RETURNING *
                """,
                (
                    target,
                    execution_id,
                    Jsonb(safe_result),
                    error_code[:96] if error_code else None,
                    _safe_message(error_message) or None,
                    target,
                    workflow_id,
                    action_name,
                ),
            )
            updated = cursor.fetchone()
            self._insert_event(
                cursor,
                workflow_id=workflow_id,
                event_type=(
                    "workflow_step_completed"
                    if target == "COMPLETED"
                    else "workflow_step_failed"
                    if target == "FAILED"
                    else "workflow_step_waiting"
                ),
                action_name=action_name,
                from_status=step["status"],
                to_status=target,
                actor_id=actor_id,
                details={
                    "execution_id": execution_id,
                    "action_status": action_status,
                    "error_code": error_code,
                },
            )
            workflow = self._recompute(cursor, workflow, actor_id=actor_id)
            return self._detail(cursor, workflow)

    def record_dispatch_failure(
        self,
        workflow_id: str,
        action_name: str,
        *,
        execution_id: str | None,
        error_code: str,
        error_message: str,
        actor_id: str,
        retryable_without_execution: bool = False,
    ) -> dict[str, Any]:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            workflow = self._workflow(cursor, workflow_id, lock=True)
            cursor.execute(
                """
                SELECT * FROM mvp_control.personal_credit_workflow_steps
                WHERE workflow_id=%s AND action_name=%s FOR UPDATE
                """,
                (workflow_id, action_name),
            )
            step = cursor.fetchone()
            if not step or step["status"] not in {"RUNNING", "WAITING"}:
                raise WorkflowError("WORKFLOW_STEP_CONFLICT", "流程步骤派发状态已变化")
            target = (
                "READY"
                if retryable_without_execution and not execution_id
                else "FAILED"
            )
            cursor.execute(
                """
                UPDATE mvp_control.personal_credit_workflow_steps
                SET status=%s,execution_id=COALESCE(%s,execution_id),
                    last_error_code=%s,last_error_message=%s,
                    lease_expires_at=NULL,
                    completed_at=CASE WHEN %s='FAILED' THEN now() ELSE NULL END,
                    updated_at=now()
                WHERE workflow_id=%s AND action_name=%s RETURNING *
                """,
                (
                    target,
                    execution_id,
                    error_code[:96],
                    _safe_message(error_message),
                    target,
                    workflow_id,
                    action_name,
                ),
            )
            self._insert_event(
                cursor,
                workflow_id=workflow_id,
                event_type=(
                    "workflow_step_rejected" if target == "READY"
                    else "workflow_step_failed"
                ),
                action_name=action_name,
                from_status=step["status"],
                to_status=target,
                actor_id=actor_id,
                details={"execution_id": execution_id, "error_code": error_code},
            )
            workflow = self._recompute(cursor, workflow, actor_id=actor_id)
            return self._detail(cursor, workflow)

    def skip_optional_step(
        self,
        workflow_id: str,
        action_name: str,
        *,
        reason: str,
        actor_id: str,
    ) -> dict[str, Any]:
        self.ensure_schema()
        if action_name not in OPTIONAL_WORKFLOW_ACTIONS:
            raise WorkflowError("WORKFLOW_STEP_REQUIRED", "必需步骤不能跳过")
        normalized_reason = self._validate_text(reason, "reason", 500)
        if len(normalized_reason) < 10:
            raise WorkflowError("WORKFLOW_REQUEST_INVALID", "跳过原因至少需要 10 个字符")
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            workflow = self._workflow(cursor, workflow_id, lock=True)
            if workflow["status"] in {"COMPLETED", "CANCELLED"}:
                raise WorkflowError("WORKFLOW_TERMINAL", "终态流程不能再跳过步骤")
            cursor.execute(
                """
                SELECT * FROM mvp_control.personal_credit_workflow_steps
                WHERE workflow_id=%s AND action_name=%s FOR UPDATE
                """,
                (workflow_id, action_name),
            )
            step = cursor.fetchone()
            if not step or step["status"] not in {"READY", "FAILED"}:
                raise WorkflowError(
                    "WORKFLOW_STEP_NOT_READY", "可选步骤尚未就绪或正在执行"
                )
            cursor.execute(
                """
                UPDATE mvp_control.personal_credit_workflow_steps
                SET status='SKIPPED',completed_at=now(),lease_expires_at=NULL,
                    updated_at=now()
                WHERE workflow_id=%s AND action_name=%s
                """,
                (workflow_id, action_name),
            )
            self._insert_event(
                cursor,
                workflow_id=workflow_id,
                event_type="workflow_step_skipped",
                action_name=action_name,
                from_status=step["status"],
                to_status="SKIPPED",
                actor_id=actor_id,
                details={"reason": _safe_message(normalized_reason)},
            )
            workflow = self._recompute(cursor, workflow, actor_id=actor_id)
            return self._detail(cursor, workflow)

    def cancel(
        self,
        workflow_id: str,
        *,
        reason: str,
        actor_id: str,
    ) -> dict[str, Any]:
        self.ensure_schema()
        normalized_reason = self._validate_text(reason, "reason", 500)
        if len(normalized_reason) < 10:
            raise WorkflowError("WORKFLOW_REQUEST_INVALID", "取消原因至少需要 10 个字符")
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            workflow = self._workflow(cursor, workflow_id, lock=True)
            if workflow["status"] in {"COMPLETED", "CANCELLED"}:
                raise WorkflowError("WORKFLOW_TERMINAL", "终态流程不能取消")
            if any(
                item["status"] in {"RUNNING", "WAITING"}
                for item in self._steps(cursor, workflow_id, lock=True)
            ):
                raise WorkflowError(
                    "WORKFLOW_ACTION_IN_PROGRESS", "存在进行中的 Action，不能取消流程"
                )
            previous_status = workflow["status"]
            cursor.execute(
                """
                UPDATE mvp_control.personal_credit_workflows
                SET status='CANCELLED',version=version+1,updated_at=now(),completed_at=now()
                WHERE workflow_id=%s RETURNING *
                """,
                (workflow_id,),
            )
            workflow = cursor.fetchone()
            self._insert_event(
                cursor,
                workflow_id=workflow_id,
                event_type="workflow_cancelled",
                action_name=None,
                from_status=previous_status,
                to_status="CANCELLED",
                actor_id=actor_id,
                details={"reason": _safe_message(normalized_reason)},
            )
            return self._detail(cursor, workflow)

    def validate_step_scope(
        self,
        workflow_id: str,
        action_name: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            workflow = self._workflow(cursor, workflow_id)
            application_id = payload.get("application_id")
            if application_id is not None and application_id != workflow["application_id"]:
                raise WorkflowError(
                    "WORKFLOW_APPLICATION_MISMATCH",
                    "Action application_id 与流程申请不一致",
                )
            if action_name == "record_phone_verification":
                cursor.execute(
                    """
                    SELECT 1 FROM mvp_control.phone_verification_tasks
                    WHERE verification_id=%s AND application_id=%s
                    """,
                    (payload.get("verification_id"), workflow["application_id"]),
                )
                if not cursor.fetchone():
                    raise WorkflowError(
                        "WORKFLOW_APPLICATION_MISMATCH",
                        "电核任务不属于当前流程申请",
                    )
            if action_name == "trigger_manual_review":
                cursor.execute(
                    """
                    SELECT 1 FROM mvp_control.personal_credit_workflow_steps
                    WHERE workflow_id=%s AND execution_id=%s
                      AND action_name IN (
                        'generate_risk_assessment','request_fraud_report',
                        'record_phone_verification'
                      )
                    """,
                    (workflow_id, payload.get("source_execution_id")),
                )
                if not cursor.fetchone():
                    raise WorkflowError(
                        "WORKFLOW_SOURCE_EXECUTION_INVALID",
                        "人工复核来源执行不属于当前流程",
                    )

    def reconcile(self, workflow_id: str, *, actor_id: str) -> dict[str, Any]:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            workflow = self._workflow(cursor, workflow_id, lock=True)
            if workflow["status"] in {"COMPLETED", "CANCELLED"}:
                return self._detail(cursor, workflow)
            steps = self._steps(cursor, workflow_id, lock=True)
            now = datetime.now(timezone.utc)
            for step in steps:
                if step["status"] not in {"RUNNING", "WAITING"}:
                    continue
                execution = None
                if step.get("execution_id"):
                    cursor.execute(
                        "SELECT * FROM mvp_control.action_executions WHERE execution_id=%s",
                        (step["execution_id"],),
                    )
                    execution = cursor.fetchone()
                elif step.get("dispatch_request_id"):
                    cursor.execute(
                        """
                        SELECT * FROM mvp_control.action_executions
                        WHERE correlation_id=%s ORDER BY created_at DESC LIMIT 1
                        """,
                        (step["dispatch_request_id"],),
                    )
                    execution = cursor.fetchone()
                if execution:
                    action_status = execution["status"]
                    target = (
                        "COMPLETED"
                        if action_status == "SUCCEEDED"
                        else "FAILED"
                        if action_status in ACTION_TERMINAL_FAILURES
                        else "WAITING"
                    )
                    if (
                        target != step["status"]
                        or step.get("execution_id") != execution["execution_id"]
                    ):
                        cursor.execute(
                            """
                            UPDATE mvp_control.personal_credit_workflow_steps
                            SET status=%s,execution_id=%s,result_summary=%s,
                                last_error_code=%s,last_error_message=%s,
                                lease_expires_at=NULL,
                                completed_at=CASE WHEN %s IN ('COMPLETED','FAILED')
                                                  THEN now() ELSE completed_at END,
                                updated_at=now()
                            WHERE workflow_id=%s AND action_name=%s
                            """,
                            (
                                target,
                                execution["execution_id"],
                                Jsonb(_safe_mapping(execution.get("result_summary"))),
                                execution.get("error_code"),
                                _safe_message(execution.get("error_message")) or None,
                                target,
                                workflow_id,
                                step["action_name"],
                            ),
                        )
                        self._insert_event(
                            cursor,
                            workflow_id=workflow_id,
                            event_type="workflow_step_reconciled",
                            action_name=step["action_name"],
                            from_status=step["status"],
                            to_status=target,
                            actor_id=actor_id,
                            details={
                                "execution_id": execution["execution_id"],
                                "action_status": action_status,
                            },
                        )
                elif (
                    step["status"] == "RUNNING"
                    and step.get("lease_expires_at")
                    and step["lease_expires_at"] <= now
                ):
                    cursor.execute(
                        """
                        UPDATE mvp_control.personal_credit_workflow_steps
                        SET status='READY',dispatch_request_id=NULL,request_digest=NULL,
                            lease_expires_at=NULL,updated_at=now()
                        WHERE workflow_id=%s AND action_name=%s
                        """,
                        (workflow_id, step["action_name"]),
                    )
                    self._insert_event(
                        cursor,
                        workflow_id=workflow_id,
                        event_type="workflow_dispatch_lease_recovered",
                        action_name=step["action_name"],
                        from_status="RUNNING",
                        to_status="READY",
                        actor_id=actor_id,
                        details={"attempt_count": step["attempt_count"]},
                    )
            workflow = self._recompute(cursor, workflow, actor_id=actor_id)
            return self._detail(cursor, workflow)

    def reconcile_active(self, *, actor_id: str, limit: int = 100) -> int:
        self.ensure_schema()
        limit = max(1, min(int(limit), 500))
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT workflow_id FROM mvp_control.personal_credit_workflows
                WHERE status IN ('WAITING_INPUT','RUNNING','FAILED')
                ORDER BY updated_at LIMIT %s
                """,
                (limit,),
            )
            workflow_ids = [item["workflow_id"] for item in cursor.fetchall()]
        for workflow_id in workflow_ids:
            self.reconcile(workflow_id, actor_id=actor_id)
        return len(workflow_ids)

    def get(self, workflow_id: str) -> dict[str, Any]:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            workflow = self._workflow(cursor, workflow_id)
            return self._detail(cursor, workflow)

    def list(
        self,
        *,
        status: str | None = None,
        application_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        limit = max(1, min(int(limit), 500))
        if status is not None and status not in WORKFLOW_STATUSES:
            raise WorkflowError("WORKFLOW_REQUEST_INVALID", "流程状态筛选值无效")
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("workflow.status=%s")
            values.append(status)
        if application_id:
            clauses.append("workflow.application_id=%s")
            values.append(application_id.strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT workflow.*,
                    count(step.action_name) FILTER (WHERE step.status='COMPLETED') AS completed_steps,
                    count(step.action_name) FILTER (WHERE step.status='READY') AS ready_steps,
                    count(step.action_name) FILTER (WHERE step.status='FAILED') AS failed_steps
                FROM mvp_control.personal_credit_workflows AS workflow
                LEFT JOIN mvp_control.personal_credit_workflow_steps AS step
                  ON step.workflow_id=workflow.workflow_id
                {where}
                GROUP BY workflow.workflow_id
                ORDER BY workflow.updated_at DESC LIMIT %s
                """,
                values,
            )
            return [dict(item) for item in cursor.fetchall()]

    def summary(self) -> dict[str, Any]:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status,count(*) AS count
                FROM mvp_control.personal_credit_workflows GROUP BY status
                """
            )
            counts = {item["status"]: int(item["count"]) for item in cursor.fetchall()}
            cursor.execute(
                """
                SELECT count(*) AS count
                FROM mvp_control.personal_credit_workflow_steps WHERE status='READY'
                """
            )
            ready = int(cursor.fetchone()["count"])
        return {
            "total": sum(counts.values()),
            "by_status": {status: counts.get(status, 0) for status in WORKFLOW_STATUSES},
            "active": sum(
                counts.get(status, 0)
                for status in ("WAITING_INPUT", "RUNNING", "FAILED")
            ),
            "ready_steps": ready,
        }

    @staticmethod
    def _detail(cursor, workflow: Mapping[str, Any]) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT * FROM mvp_control.personal_credit_workflow_steps
            WHERE workflow_id=%s ORDER BY position
            """,
            (workflow["workflow_id"],),
        )
        steps = [dict(item) for item in cursor.fetchall()]
        cursor.execute(
            """
            SELECT * FROM mvp_control.personal_credit_workflow_events
            WHERE workflow_id=%s ORDER BY sequence_no
            """,
            (workflow["workflow_id"],),
        )
        events = [dict(item) for item in cursor.fetchall()]
        return {"workflow": dict(workflow), "steps": steps, "events": events}
