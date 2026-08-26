from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


class PhoneVerificationResultPersistenceError(RuntimeError):
    pass


class PhoneVerificationTaskNotFound(PhoneVerificationResultPersistenceError):
    pass


class PhoneVerificationTaskNotAssigned(PhoneVerificationResultPersistenceError):
    pass


class PhoneVerificationTerminalConflict(PhoneVerificationResultPersistenceError):
    pass


@dataclass(frozen=True)
class PhoneVerificationAssignment:
    verification_id: str
    application_id: str
    verification_target: str
    callback_actor_id: str
    callback_actor_role: str
    dispatch_status: str
    task_created_at: datetime
    dispatched_at: datetime


RESULT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS mvp_control.phone_verification_callbacks (
        callback_id varchar(128) PRIMARY KEY,
        verification_id varchar(32) NOT NULL UNIQUE
            REFERENCES mvp_control.phone_verification_tasks(verification_id) ON DELETE RESTRICT,
        payload_digest char(64) NOT NULL,
        actor_id varchar(128) NOT NULL,
        actor_role varchar(64) NOT NULL,
        execution_id varchar(36) NOT NULL
            REFERENCES mvp_control.action_executions(execution_id) ON DELETE RESTRICT,
        committed_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS phone_verification_callbacks_execution_idx
    ON mvp_control.phone_verification_callbacks (execution_id)
    """,
)


def phone_result_digest(result: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(result), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_terminal(row: Mapping[str, Any]) -> bool:
    return any(
        row.get(field) is not None
        for field in (
            "verifier_name", "call_time", "call_duration_seconds",
            "conclusion", "doubts", "recording_file_id",
        )
    )


class PostgresPhoneVerificationResultRepository:
    """Atomically writes a terminal phone result and a graph-sync outbox event."""

    _schema_lock = threading.Lock()
    _schema_ready = False

    def __init__(self, postgres_service, task_repository):
        self.postgres = postgres_service
        self.tasks = task_repository

    def ensure_schema(self) -> None:
        self.tasks.ensure_schema()
        if type(self)._schema_ready:
            return
        with type(self)._schema_lock:
            if type(self)._schema_ready:
                return
            with self.postgres.conn() as connection, connection.cursor() as cursor:
                for statement in RESULT_SCHEMA:
                    cursor.execute(statement)
            type(self)._schema_ready = True

    @staticmethod
    def _assignment_from_row(row: Mapping[str, Any]) -> PhoneVerificationAssignment:
        return PhoneVerificationAssignment(
            verification_id=str(row["verification_id"]),
            application_id=str(row["application_id"]),
            verification_target=str(row["verification_target"]),
            callback_actor_id=str(row["callback_actor_id"]),
            callback_actor_role=str(row["callback_actor_role"]),
            dispatch_status=str(row["dispatch_status"]),
            task_created_at=row["task_created_at"],
            dispatched_at=row["dispatched_at"],
        )

    @staticmethod
    def _validate_assignment(row: Mapping[str, Any], actor_id: str, actor_role: str) -> None:
        if (
            row.get("dispatch_status") != "DISPATCHED"
            or not row.get("dispatched_at")
            or not row.get("callback_actor_id")
            or not row.get("callback_actor_role")
            or row.get("callback_actor_id") != actor_id
            or row.get("callback_actor_role") != actor_role
        ):
            raise PhoneVerificationTaskNotAssigned("当前回调身份未被分配到该电核任务")

    def inspect_assignment(
        self, verification_id: str, *, actor_id: str, actor_role: str
    ) -> PhoneVerificationAssignment:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT task.verification_id,task.application_id,task.dispatch_status,
                       task.callback_actor_id,task.callback_actor_role,
                       task.created_at AS task_created_at,task.dispatched_at,
                       record.verification_target,record.verifier_name,record.call_time,
                       record.call_duration_seconds,record.conclusion,record.doubts,
                       record.recording_file_id
                FROM mvp_control.phone_verification_tasks AS task
                JOIN public.phone_verification_record AS record
                  ON record.verification_id=task.verification_id
                 AND record.application_id=task.application_id
                WHERE task.verification_id=%s
                """,
                (verification_id,),
            )
            row = cursor.fetchone()
        if not row:
            raise PhoneVerificationTaskNotFound("电核任务不存在")
        self._validate_assignment(row, actor_id, actor_role)
        if _is_terminal(row):
            raise PhoneVerificationTerminalConflict("电核任务已经写入终态结果")
        return self._assignment_from_row(row)

    def persist_and_enqueue(
        self,
        *,
        assignment: PhoneVerificationAssignment,
        callback_id: str,
        verifier_identity: str,
        call_time: datetime,
        duration_seconds: int,
        conclusion: str,
        doubts: str | None,
        recording_file_id: str | None,
        payload_digest: str,
        actor_id: str,
        actor_role: str,
        execution_id: str,
        ontology_version: str,
        outbox_repository,
        max_attempts: int,
    ):
        self.ensure_schema()
        outbox_repository.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT task.verification_id,task.application_id,task.dispatch_status,
                       task.callback_actor_id,task.callback_actor_role,
                       task.created_at AS task_created_at,task.dispatched_at,
                       record.verification_target,record.verifier_name,record.call_time,
                       record.call_duration_seconds,record.conclusion,record.doubts,
                       record.recording_file_id
                FROM mvp_control.phone_verification_tasks AS task
                JOIN public.phone_verification_record AS record
                  ON record.verification_id=task.verification_id
                 AND record.application_id=task.application_id
                WHERE task.verification_id=%s
                FOR UPDATE OF task,record
                """,
                (assignment.verification_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise PhoneVerificationTaskNotFound("电核任务不存在")
            self._validate_assignment(row, actor_id, actor_role)
            current_assignment = self._assignment_from_row(row)
            if current_assignment != assignment:
                raise PhoneVerificationTaskNotAssigned("提交时电核任务分配状态已变化")
            if _is_terminal(row):
                raise PhoneVerificationTerminalConflict("电核任务已经写入终态结果")
            cursor.execute(
                "SELECT * FROM mvp_control.phone_verification_callbacks WHERE callback_id=%s",
                (callback_id,),
            )
            existing_callback = cursor.fetchone()
            if existing_callback:
                raise PhoneVerificationTerminalConflict("callback_id 已被使用")
            cursor.execute(
                """
                UPDATE public.phone_verification_record
                SET verifier_name=%s,call_time=%s,call_duration_seconds=%s,
                    conclusion=%s,doubts=%s,recording_file_id=%s
                WHERE verification_id=%s AND application_id=%s
                  AND verifier_name IS NULL AND call_time IS NULL
                  AND call_duration_seconds IS NULL AND conclusion IS NULL
                  AND doubts IS NULL AND recording_file_id IS NULL
                RETURNING *
                """,
                (
                    verifier_identity, call_time, duration_seconds, conclusion,
                    doubts, recording_file_id, assignment.verification_id,
                    assignment.application_id,
                ),
            )
            stored = cursor.fetchone()
            if not stored:
                raise PhoneVerificationTerminalConflict("电核终态已被并发写入")
            cursor.execute(
                """
                INSERT INTO mvp_control.phone_verification_callbacks (
                    callback_id,verification_id,payload_digest,actor_id,actor_role,execution_id
                ) VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (
                    callback_id, assignment.verification_id, payload_digest,
                    actor_id, actor_role, execution_id,
                ),
            )
            cursor.execute(
                """
                UPDATE mvp_control.phone_verification_tasks
                SET result_sync_status='PENDING',result_callback_id=%s,updated_at=now()
                WHERE verification_id=%s
                """,
                (callback_id, assignment.verification_id),
            )
            event = outbox_repository.enqueue(
                cursor,
                execution_id=execution_id,
                event_type="PHONE_VERIFICATION_RESULT_GRAPH_SYNC",
                aggregate_type="PhoneVerificationRecord",
                aggregate_id=assignment.verification_id,
                ontology_version=ontology_version,
                deduplication_key=execution_id,
                payload={
                    "actor_id": actor_id,
                    "application_id": assignment.application_id,
                    "verification_id": assignment.verification_id,
                    "callback_id": callback_id,
                    "conclusion": conclusion,
                    "record_status": "COMPLETED",
                    "retention_policy": "retain_terminal_result_on_graph_failure",
                },
                max_attempts=max_attempts,
            )
        return dict(stored), event

    def mark_graph_sync(self, verification_id: str, *, synchronized: bool) -> None:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE mvp_control.phone_verification_tasks
                SET result_sync_status=%s,updated_at=now()
                WHERE verification_id=%s AND result_callback_id IS NOT NULL
                """,
                ("SYNCHRONIZED" if synchronized else "DEAD_LETTER", verification_id),
            )
            if cursor.rowcount != 1:
                raise PhoneVerificationResultPersistenceError("电核结果图同步状态更新失败")
