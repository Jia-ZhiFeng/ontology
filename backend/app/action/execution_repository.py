from __future__ import annotations

import re
import threading
from datetime import datetime
from typing import Any, Mapping
from uuid import uuid4

from psycopg.types.json import Jsonb

from ..core.security import mask_payload
from .action_design_catalog import ACTION_RUNTIME_DESIGNS, ACTION_STATE_TRANSITIONS
from .execution import (
    ActionAuditIntegrityError,
    ActionConcurrentUpdateError,
    ActionExecutionNotFound,
    ActionExecutionRecord,
    ActionIdempotencyConflict,
    ActionRequestError,
    ActionReservation,
    ActionStateMachine,
    sha256_digest,
)


_STATUS_SQL = ",".join(f"'{status}'" for status in ACTION_STATE_TRANSITIONS)

ACTION_CONTROL_SCHEMA_STATEMENTS = (
    "CREATE SCHEMA IF NOT EXISTS mvp_control",
    f"""
    CREATE TABLE IF NOT EXISTS mvp_control.action_executions (
        execution_id varchar(36) PRIMARY KEY,
        action_name varchar(64) NOT NULL,
        ontology_version varchar(64) NOT NULL,
        status varchar(32) NOT NULL CHECK (status IN ({_STATUS_SQL})),
        actor_id varchar(128) NOT NULL,
        actor_role varchar(64) NOT NULL,
        idempotency_key_hash char(64) NOT NULL CHECK (char_length(idempotency_key_hash)=64),
        request_digest char(64) NOT NULL CHECK (char_length(request_digest)=64),
        correlation_id varchar(128),
        result_summary jsonb,
        error_code varchar(64),
        error_message varchar(500),
        version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        started_at timestamptz,
        completed_at timestamptz,
        UNIQUE (action_name,idempotency_key_hash)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS action_executions_status_created_idx
    ON mvp_control.action_executions (status,created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS mvp_control.action_audit_events (
        event_id bigserial PRIMARY KEY,
        execution_id varchar(36) NOT NULL
            REFERENCES mvp_control.action_executions(execution_id) ON DELETE RESTRICT,
        sequence_no integer NOT NULL CHECK (sequence_no > 0),
        event_type varchar(64) NOT NULL,
        from_status varchar(32),
        to_status varchar(32) NOT NULL,
        actor_id varchar(128) NOT NULL,
        details jsonb NOT NULL DEFAULT '{}'::jsonb,
        previous_event_digest char(64),
        event_digest char(64) NOT NULL CHECK (char_length(event_digest)=64),
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (execution_id,sequence_no),
        UNIQUE (execution_id,event_digest)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS action_audit_events_execution_idx
    ON mvp_control.action_audit_events (execution_id,sequence_no)
    """,
    """
    CREATE OR REPLACE FUNCTION mvp_control.prevent_action_audit_mutation()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'action_audit_events is append-only';
    END;
    $$
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgname='action_audit_events_append_only'
              AND tgrelid='mvp_control.action_audit_events'::regclass
        ) THEN
            EXECUTE 'CREATE TRIGGER action_audit_events_append_only
                BEFORE UPDATE OR DELETE ON mvp_control.action_audit_events
                FOR EACH ROW EXECUTE FUNCTION mvp_control.prevent_action_audit_mutation()';
        END IF;
    END;
    $$
    """,
)


def _record_from_row(row: Mapping[str, Any]) -> ActionExecutionRecord:
    return ActionExecutionRecord(
        execution_id=row["execution_id"],
        action_name=row["action_name"],
        ontology_version=row["ontology_version"],
        status=row["status"],
        actor_id=row["actor_id"],
        actor_role=row["actor_role"],
        idempotency_key_hash=row["idempotency_key_hash"],
        request_digest=row["request_digest"],
        correlation_id=row.get("correlation_id"),
        result_summary=row.get("result_summary"),
        error_code=row.get("error_code"),
        error_message=row.get("error_message"),
        version=row["version"],
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
    )


def _safe_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ActionRequestError("Action 审计或结果摘要必须是映射")
    masked = mask_payload(dict(value))
    if not isinstance(masked, dict):
        raise ActionRequestError("Action 摘要脱敏结果必须是映射")
    return masked


def _audit_digest(
    *,
    execution_id: str,
    sequence_no: int,
    event_type: str,
    from_status: str | None,
    to_status: str,
    actor_id: str,
    details: Mapping[str, Any],
    previous_event_digest: str | None,
) -> str:
    return sha256_digest(
        {
            "execution_id": execution_id,
            "sequence_no": sequence_no,
            "event_type": event_type,
            "from_status": from_status,
            "to_status": to_status,
            "actor_id": actor_id,
            "details": dict(details),
            "previous_event_digest": previous_event_digest,
        }
    )


class PostgresActionExecutionRepository:
    _schema_lock = threading.Lock()
    _schema_ready = False
    _digest_pattern = re.compile(r"^[0-9a-f]{64}$")

    def __init__(self, postgres_service):
        self.postgres = postgres_service

    def ensure_schema(self) -> None:
        if type(self)._schema_ready:
            return
        with type(self)._schema_lock:
            if type(self)._schema_ready:
                return
            with self.postgres.conn() as connection, connection.cursor() as cursor:
                for statement in ACTION_CONTROL_SCHEMA_STATEMENTS:
                    cursor.execute(statement)
            type(self)._schema_ready = True

    @classmethod
    def _validate_digest(cls, value: str, field_name: str) -> None:
        if not isinstance(value, str) or not cls._digest_pattern.fullmatch(value):
            raise ActionRequestError(f"{field_name} 必须是 64 位 SHA-256")

    @staticmethod
    def _validate_identity(value: str, field_name: str, max_length: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ActionRequestError(f"{field_name} 不能为空")
        normalized = value.strip()
        if len(normalized) > max_length:
            raise ActionRequestError(f"{field_name} 超出长度限制")
        return normalized

    @staticmethod
    def _insert_audit_event(
        cursor,
        *,
        execution_id: str,
        event_type: str,
        from_status: str | None,
        to_status: str,
        actor_id: str,
        details: Mapping[str, Any] | None,
    ) -> None:
        safe_details = _safe_mapping(details)
        cursor.execute(
            """
            SELECT sequence_no,event_digest
            FROM mvp_control.action_audit_events
            WHERE execution_id=%s
            ORDER BY sequence_no DESC LIMIT 1
            """,
            (execution_id,),
        )
        previous = cursor.fetchone()
        sequence_no = previous["sequence_no"] + 1 if previous else 1
        previous_digest = previous["event_digest"] if previous else None
        digest = _audit_digest(
            execution_id=execution_id,
            sequence_no=sequence_no,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            actor_id=actor_id,
            details=safe_details,
            previous_event_digest=previous_digest,
        )
        cursor.execute(
            """
            INSERT INTO mvp_control.action_audit_events (
                execution_id,sequence_no,event_type,from_status,to_status,actor_id,
                details,previous_event_digest,event_digest
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                execution_id,
                sequence_no,
                event_type,
                from_status,
                to_status,
                actor_id,
                Jsonb(safe_details),
                previous_digest,
                digest,
            ),
        )

    def reserve(
        self,
        *,
        action_name: str,
        ontology_version: str,
        actor_id: str,
        actor_role: str,
        idempotency_key_hash: str,
        request_digest: str,
        correlation_id: str | None = None,
    ) -> ActionReservation:
        self.ensure_schema()
        if action_name not in ACTION_RUNTIME_DESIGNS:
            raise ActionRequestError(f"Action 设计不存在：{action_name}")
        actor_id = self._validate_identity(actor_id, "actor_id", 128)
        actor_role = self._validate_identity(actor_role, "actor_role", 64)
        ontology_version = self._validate_identity(
            ontology_version, "ontology_version", 64
        )
        if correlation_id is not None:
            correlation_id = self._validate_identity(
                correlation_id, "correlation_id", 128
            )
        self._validate_digest(idempotency_key_hash, "idempotency_key_hash")
        self._validate_digest(request_digest, "request_digest")
        execution_id = str(uuid4())

        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO mvp_control.action_executions (
                    execution_id,action_name,ontology_version,status,actor_id,actor_role,
                    idempotency_key_hash,request_digest,correlation_id
                ) VALUES (%s,%s,%s,'RECEIVED',%s,%s,%s,%s,%s)
                ON CONFLICT (action_name,idempotency_key_hash) DO NOTHING
                RETURNING *
                """,
                (
                    execution_id,
                    action_name,
                    ontology_version,
                    actor_id,
                    actor_role,
                    idempotency_key_hash,
                    request_digest,
                    correlation_id,
                ),
            )
            row = cursor.fetchone()
            if row:
                self._insert_audit_event(
                    cursor,
                    execution_id=execution_id,
                    event_type="action_received",
                    from_status=None,
                    to_status="RECEIVED",
                    actor_id=actor_id,
                    details={"actor_role": actor_role, "correlation_id": correlation_id},
                )
                return ActionReservation(_record_from_row(row), True)

            cursor.execute(
                """
                SELECT * FROM mvp_control.action_executions
                WHERE action_name=%s AND idempotency_key_hash=%s
                FOR UPDATE
                """,
                (action_name, idempotency_key_hash),
            )
            existing = cursor.fetchone()
            if not existing:
                raise ActionConcurrentUpdateError("Action 幂等记录并发读取失败")
            if existing["request_digest"] != request_digest:
                raise ActionIdempotencyConflict("ACTION_IDEMPOTENCY_CONFLICT")
            return ActionReservation(_record_from_row(existing), False)

    def get(self, execution_id: str) -> ActionExecutionRecord:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM mvp_control.action_executions WHERE execution_id=%s",
                (execution_id,),
            )
            row = cursor.fetchone()
        if not row:
            raise ActionExecutionNotFound(f"Action 执行不存在：{execution_id}")
        return _record_from_row(row)

    def transition(
        self,
        execution_id: str,
        target_status: str,
        *,
        expected_version: int,
        event_type: str,
        actor_id: str,
        details: Mapping[str, Any] | None = None,
        result_summary: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ActionExecutionRecord:
        self.ensure_schema()
        actor_id = self._validate_identity(actor_id, "actor_id", 128)
        event_type = self._validate_identity(event_type, "event_type", 64)
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ActionRequestError("expected_version 必须是正整数")
        if expected_version <= 0:
            raise ActionRequestError("expected_version 必须是正整数")
        if error_code is not None:
            if not isinstance(error_code, str) or not error_code.startswith("ACTION_"):
                raise ActionRequestError("error_code 必须使用 ACTION_* 受控错误码")
            error_code = error_code[:64]
        if target_status in {"FAILED", "COMPENSATION_FAILED"} and not error_code:
            raise ActionRequestError(f"进入 {target_status} 必须提供受控错误码")
        if target_status not in {"FAILED", "COMPENSATION_FAILED"} and error_code:
            raise ActionRequestError("非失败状态不得记录 error_code")
        safe_result = _safe_mapping(result_summary) if result_summary is not None else None
        safe_error = mask_payload(error_message or "")[:500] or None

        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM mvp_control.action_executions
                WHERE execution_id=%s FOR UPDATE
                """,
                (execution_id,),
            )
            current = cursor.fetchone()
            if not current:
                raise ActionExecutionNotFound(f"Action 执行不存在：{execution_id}")
            if current["version"] != expected_version:
                raise ActionConcurrentUpdateError("ACTION_EXECUTION_VERSION_CONFLICT")
            ActionStateMachine.require_transition(current["status"], target_status)
            terminal = ActionStateMachine.is_terminal(target_status)
            cursor.execute(
                """
                UPDATE mvp_control.action_executions
                SET status=%s,
                    result_summary=COALESCE(%s,result_summary),
                    error_code=%s,
                    error_message=%s,
                    version=version+1,
                    started_at=COALESCE(started_at,now()),
                    completed_at=CASE WHEN %s THEN now() ELSE NULL END,
                    updated_at=now()
                WHERE execution_id=%s AND version=%s
                RETURNING *
                """,
                (
                    target_status,
                    Jsonb(safe_result) if safe_result is not None else None,
                    error_code,
                    safe_error,
                    terminal,
                    execution_id,
                    expected_version,
                ),
            )
            updated = cursor.fetchone()
            if not updated:
                raise ActionConcurrentUpdateError("ACTION_EXECUTION_VERSION_CONFLICT")
            audit_details = dict(_safe_mapping(details))
            if error_code:
                audit_details["error_code"] = error_code
            self._insert_audit_event(
                cursor,
                execution_id=execution_id,
                event_type=event_type,
                from_status=current["status"],
                to_status=target_status,
                actor_id=actor_id,
                details=audit_details,
            )
            return _record_from_row(updated)

    def record_event(
        self,
        execution_id: str,
        *,
        event_type: str,
        actor_id: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.ensure_schema()
        actor_id = self._validate_identity(actor_id, "actor_id", 128)
        event_type = self._validate_identity(event_type, "event_type", 64)
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status FROM mvp_control.action_executions
                WHERE execution_id=%s FOR UPDATE
                """,
                (execution_id,),
            )
            current = cursor.fetchone()
            if not current:
                raise ActionExecutionNotFound(f"Action 执行不存在：{execution_id}")
            self._insert_audit_event(
                cursor,
                execution_id=execution_id,
                event_type=event_type,
                from_status=current["status"],
                to_status=current["status"],
                actor_id=actor_id,
                details=details,
            )

    def list_audit_events(self, execution_id: str) -> list[dict[str, Any]]:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT event_id,execution_id,sequence_no,event_type,from_status,to_status,
                       actor_id,details,previous_event_digest,event_digest,created_at
                FROM mvp_control.action_audit_events
                WHERE execution_id=%s ORDER BY sequence_no
                """,
                (execution_id,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def verify_audit_chain(self, execution_id: str) -> bool:
        events = self.list_audit_events(execution_id)
        if not events:
            raise ActionAuditIntegrityError("Action 执行没有审计事件")
        previous_digest = None
        for expected_sequence, event in enumerate(events, start=1):
            if event["sequence_no"] != expected_sequence:
                raise ActionAuditIntegrityError("Action 审计序号不连续")
            if event["previous_event_digest"] != previous_digest:
                raise ActionAuditIntegrityError("Action 审计前序摘要不一致")
            expected_digest = _audit_digest(
                execution_id=event["execution_id"],
                sequence_no=event["sequence_no"],
                event_type=event["event_type"],
                from_status=event["from_status"],
                to_status=event["to_status"],
                actor_id=event["actor_id"],
                details=event["details"],
                previous_event_digest=event["previous_event_digest"],
            )
            if event["event_digest"] != expected_digest:
                raise ActionAuditIntegrityError("Action 审计事件摘要不一致")
            previous_digest = event["event_digest"]
        return True
