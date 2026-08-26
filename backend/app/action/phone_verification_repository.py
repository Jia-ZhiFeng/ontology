from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

try:
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:  # Unit tests do not require the PostgreSQL driver.
    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, value):
            self.obj = value

from .phone_dispatch import (
    PhoneTaskDispatchRequest,
    PhoneTaskDispatchResponse,
    deterministic_phone_dispatch_id,
)


class PhoneVerificationPersistenceError(RuntimeError):
    pass


class PhoneVerificationPrerequisiteFailed(PhoneVerificationPersistenceError):
    pass


class PhoneVerificationTriggerNotMet(PhoneVerificationPersistenceError):
    pass


class PhoneVerificationRoundAlreadyExists(PhoneVerificationPersistenceError):
    pass


@dataclass(frozen=True)
class PhoneVerificationContext:
    application_id: str
    application_status: str
    borrower_name: str
    risk_assessment_id: str
    risk_level: str
    risk_factors: str
    risk_check_result: str
    fraud_report_id: str
    fraud_score: Decimal
    fraud_result: str
    fraud_hit_details: str
    verified_trigger_signals: tuple[str, ...]


@dataclass(frozen=True)
class PhoneVerificationTaskEffect:
    operation: str
    verification_id: str
    application_id: str
    verification_round: int
    dispatch_id: str


PHONE_TASK_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS mvp_control.phone_verification_tasks (
        verification_id varchar(32) PRIMARY KEY
            REFERENCES public.phone_verification_record(verification_id) ON DELETE RESTRICT,
        application_id varchar(32) NOT NULL
            REFERENCES public.loan_application(application_id) ON DELETE RESTRICT,
        verification_round integer NOT NULL CHECK (verification_round BETWEEN 1 AND 99),
        trigger_reason varchar(200) NOT NULL,
        verified_trigger_signals jsonb NOT NULL,
        questions jsonb NOT NULL,
        question_set_digest char(64) NOT NULL,
        question_count integer NOT NULL CHECK (question_count > 0),
        template_version varchar(64) NOT NULL,
        function_version varchar(64) NOT NULL,
        data_snapshot_id varchar(160) NOT NULL,
        data_snapshot_time timestamptz NOT NULL,
        dispatch_id varchar(64) NOT NULL UNIQUE,
        dispatch_status varchar(24) NOT NULL
            CHECK (dispatch_status IN ('PENDING','DISPATCHED','DEAD_LETTER')),
        provider_name varchar(80),
        provider_mode varchar(24),
        synthetic boolean NOT NULL DEFAULT false,
        external_task_id varchar(64),
        dispatched_at timestamptz,
        callback_actor_id varchar(128),
        callback_actor_role varchar(64),
        result_sync_status varchar(24)
            CHECK (result_sync_status IS NULL OR result_sync_status IN ('PENDING','SYNCHRONIZED','DEAD_LETTER')),
        result_callback_id varchar(128),
        last_error_code varchar(64),
        last_error_message varchar(500),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (application_id,verification_round)
    )
    """,
    """
    ALTER TABLE mvp_control.phone_verification_tasks
    ADD COLUMN IF NOT EXISTS callback_actor_id varchar(128)
    """,
    """
    ALTER TABLE mvp_control.phone_verification_tasks
    ADD COLUMN IF NOT EXISTS callback_actor_role varchar(64)
    """,
    """
    ALTER TABLE mvp_control.phone_verification_tasks
    ADD COLUMN IF NOT EXISTS result_sync_status varchar(24)
    """,
    """
    ALTER TABLE mvp_control.phone_verification_tasks
    ADD COLUMN IF NOT EXISTS result_callback_id varchar(128)
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname='phone_verification_tasks_result_sync_status_check'
              AND conrelid='mvp_control.phone_verification_tasks'::regclass
        ) THEN
            ALTER TABLE mvp_control.phone_verification_tasks
            ADD CONSTRAINT phone_verification_tasks_result_sync_status_check
            CHECK (
                result_sync_status IS NULL OR
                result_sync_status IN ('PENDING','SYNCHRONIZED','DEAD_LETTER')
            );
        END IF;
    END
    $$
    """,
    """
    CREATE INDEX IF NOT EXISTS phone_verification_tasks_dispatch_idx
    ON mvp_control.phone_verification_tasks (dispatch_status,updated_at)
    """,
)


def deterministic_verification_id(
    application_id: str, verification_round: int
) -> str:
    digest = hashlib.sha256(
        f"{application_id}|{verification_round}".encode("utf-8")
    ).hexdigest()[:28].upper()
    return f"PV{digest}"


def _question_digest(questions: tuple[Mapping[str, Any], ...]) -> str:
    encoded = json.dumps(
        [dict(item) for item in questions],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fraud_hit(hit_details: str) -> bool:
    compact = hit_details.replace(" ", "")
    return bool(compact) and not any(
        token in compact for token in ("未命中", "无命中", "未发现")
    )


class PostgresPhoneVerificationTaskRepository:
    """Create a pending phone task while keeping task metadata outside the ABox."""

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
                for statement in PHONE_TASK_SCHEMA:
                    cursor.execute(statement)
            type(self)._schema_ready = True

    @staticmethod
    def _load_context(cursor, application_id: str, *, lock: bool = False):
        if lock:
            cursor.execute(
                """
                SELECT application_id FROM public.loan_application
                WHERE application_id=%s FOR UPDATE
                """,
                (application_id,),
            )
            if not cursor.fetchone():
                raise PhoneVerificationPrerequisiteFailed(
                    f"贷款申请不存在：{application_id}"
                )
        cursor.execute(
            """
            SELECT application.application_id,application.application_status,
                   person.name AS borrower_name
            FROM public.loan_application AS application
            LEFT JOIN public.natural_person AS person
              ON person.id_number=application.borrower_id
            WHERE application.application_id=%s
            """,
            (application_id,),
        )
        application = cursor.fetchone()
        if not application:
            raise PhoneVerificationPrerequisiteFailed(
                f"贷款申请不存在：{application_id}"
            )
        if not application.get("application_status") or not application.get(
            "borrower_name"
        ):
            raise PhoneVerificationPrerequisiteFailed(
                "申请状态或借款人主体不完整，不能创建电核任务"
            )
        cursor.execute(
            """
            SELECT assessment_id,risk_level,risk_factors,check_result
            FROM public.risk_assessment_report
            WHERE application_id=%s
            ORDER BY assessment_time DESC NULLS LAST,assessment_id DESC
            LIMIT 1
            """,
            (application_id,),
        )
        risk = cursor.fetchone()
        cursor.execute(
            """
            SELECT fraud_report_id,fraud_score,result,hit_details
            FROM public.anti_fraud_report
            WHERE application_id=%s
            ORDER BY fraud_report_id DESC LIMIT 1
            """,
            (application_id,),
        )
        fraud = cursor.fetchone()
        if not risk or not all(
            risk.get(field)
            for field in ("assessment_id", "risk_level", "risk_factors", "check_result")
        ):
            raise PhoneVerificationPrerequisiteFailed(
                "缺少完整风险评估报告，不能创建电核任务"
            )
        if (
            not fraud
            or fraud.get("fraud_score") is None
            or not fraud.get("result")
            or not fraud.get("hit_details")
        ):
            raise PhoneVerificationPrerequisiteFailed(
                "缺少完整反欺诈报告，不能创建电核任务"
            )
        signals: list[str] = []
        if risk["risk_level"] in {"中", "高", "拒绝"}:
            signals.append(f"RISK_LEVEL_{risk['risk_level']}")
        if risk["check_result"] in {"待人工复核", "不通过"}:
            signals.append(f"RISK_CHECK_{risk['check_result']}")
        if fraud["result"] in {"人工复核", "拒绝"}:
            signals.append(f"FRAUD_RESULT_{fraud['result']}")
        if _fraud_hit(str(fraud["hit_details"])):
            signals.append("FRAUD_RULE_HIT")
        if not signals:
            raise PhoneVerificationTriggerNotMet(
                "当前风险评估与反欺诈结果未命中电核触发条件"
            )
        return PhoneVerificationContext(
            application_id=str(application["application_id"]),
            application_status=str(application["application_status"]),
            borrower_name=str(application["borrower_name"]),
            risk_assessment_id=str(risk["assessment_id"]),
            risk_level=str(risk["risk_level"]),
            risk_factors=str(risk["risk_factors"]),
            risk_check_result=str(risk["check_result"]),
            fraud_report_id=str(fraud["fraud_report_id"]),
            fraud_score=Decimal(str(fraud["fraud_score"])),
            fraud_result=str(fraud["result"]),
            fraud_hit_details=str(fraud["hit_details"]),
            verified_trigger_signals=tuple(signals),
        )

    def inspect_target(self, application_id: str) -> PhoneVerificationContext:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            return self._load_context(cursor, application_id)

    def persist_and_enqueue(
        self,
        *,
        context: PhoneVerificationContext,
        verification_round: int,
        trigger_reason: str,
        question_set: Mapping[str, Any],
        function_version: str,
        snapshot_id: str,
        snapshot_time: str,
        outbox_repository,
        execution_id: str,
        ontology_version: str,
        actor_id: str,
        request_id: str,
        callback_actor_id: str,
        callback_actor_role: str,
        max_attempts: int,
    ):
        self.ensure_schema()
        outbox_repository.ensure_schema()
        questions = tuple(dict(item) for item in question_set["phone_questions"])
        question_count = int(question_set["question_count"])
        template_version = str(question_set["template_version"])
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            current = self._load_context(cursor, context.application_id, lock=True)
            if current != context:
                raise PhoneVerificationPrerequisiteFailed(
                    "提交时风险或反欺诈上下文已变化，拒绝创建电核任务"
                )
            cursor.execute(
                """
                SELECT verification_id FROM mvp_control.phone_verification_tasks
                WHERE application_id=%s AND verification_round=%s FOR UPDATE
                """,
                (context.application_id, verification_round),
            )
            if cursor.fetchone():
                raise PhoneVerificationRoundAlreadyExists(
                    "当前申请的指定电核轮次已经存在"
                )
            cursor.execute(
                """
                SELECT record.*
                FROM public.phone_verification_record AS record
                LEFT JOIN mvp_control.phone_verification_tasks AS task
                  ON task.verification_id=record.verification_id
                WHERE record.application_id=%s
                  AND task.verification_id IS NULL
                  AND record.verifier_name IS NULL
                  AND record.call_time IS NULL
                  AND record.call_duration_seconds IS NULL
                  AND record.conclusion IS NULL
                  AND record.doubts IS NULL
                  AND record.recording_file_id IS NULL
                ORDER BY record.verification_id
                LIMIT 1 FOR UPDATE OF record
                """,
                (context.application_id,),
            )
            placeholder = cursor.fetchone()
            if placeholder:
                verification_id = str(placeholder["verification_id"])
                cursor.execute(
                    """
                    UPDATE public.phone_verification_record
                    SET verification_target=%s
                    WHERE verification_id=%s AND application_id=%s
                    RETURNING *
                    """,
                    (
                        context.borrower_name,
                        verification_id,
                        context.application_id,
                    ),
                )
                operation = "materialized_placeholder"
            else:
                verification_id = deterministic_verification_id(
                    context.application_id, verification_round
                )
                cursor.execute(
                    """
                    INSERT INTO public.phone_verification_record (
                        verification_id,application_id,verification_target
                    ) VALUES (%s,%s,%s) RETURNING *
                    """,
                    (
                        verification_id,
                        context.application_id,
                        context.borrower_name,
                    ),
                )
                operation = "created"
            record = cursor.fetchone()
            if not record or record.get("verification_target") != context.borrower_name:
                raise PhoneVerificationPersistenceError("电核记录写入后回读失败")
            dispatch_id = deterministic_phone_dispatch_id(
                context.application_id, verification_round
            )
            cursor.execute(
                """
                INSERT INTO mvp_control.phone_verification_tasks (
                    verification_id,application_id,verification_round,
                    trigger_reason,verified_trigger_signals,questions,
                    question_set_digest,question_count,template_version,
                    function_version,data_snapshot_id,data_snapshot_time,
                    dispatch_id,dispatch_status,callback_actor_id,callback_actor_role
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s,%s)
                RETURNING *
                """,
                (
                    verification_id,
                    context.application_id,
                    verification_round,
                    trigger_reason,
                    Jsonb(list(context.verified_trigger_signals)),
                    Jsonb(list(questions)),
                    _question_digest(questions),
                    question_count,
                    template_version,
                    function_version,
                    snapshot_id,
                    snapshot_time,
                    dispatch_id,
                    callback_actor_id,
                    callback_actor_role,
                ),
            )
            task = cursor.fetchone()
            if not task:
                raise PhoneVerificationPersistenceError("电核任务元数据写入后回读失败")
            effect = PhoneVerificationTaskEffect(
                operation=operation,
                verification_id=verification_id,
                application_id=context.application_id,
                verification_round=verification_round,
                dispatch_id=dispatch_id,
            )
            event = outbox_repository.enqueue(
                cursor,
                execution_id=execution_id,
                event_type="PHONE_VERIFICATION_TASK_DISPATCH",
                aggregate_type="PhoneVerificationRecord",
                aggregate_id=verification_id,
                ontology_version=ontology_version,
                deduplication_key=execution_id,
                payload={
                    "actor_id": actor_id,
                    "request_id": request_id,
                    "application_id": context.application_id,
                    "verification_id": verification_id,
                    "verification_round": verification_round,
                    "dispatch_id": dispatch_id,
                    "question_count": question_count,
                    "template_version": template_version,
                    "function_version": function_version,
                    "verified_trigger_signals": list(
                        context.verified_trigger_signals
                    ),
                    "created_relations": ["HAS_PHONE_VERIFICATION"],
                    "effect": {
                        "operation": operation,
                        "verification_id": verification_id,
                        "application_id": context.application_id,
                        "verification_round": verification_round,
                        "dispatch_id": dispatch_id,
                        "retention_policy": "retain_on_dispatch_failure",
                    },
                },
                max_attempts=max_attempts,
            )
            return dict(record), dict(task), effect, event

    def load_dispatch_request(self, verification_id: str) -> PhoneTaskDispatchRequest:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT task.*,record.verification_target
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
            raise PhoneVerificationPersistenceError("待投递电核任务不存在")
        questions = row.get("questions")
        if not isinstance(questions, list) or not questions:
            raise PhoneVerificationPersistenceError("待投递电核任务的问题集无效")
        return PhoneTaskDispatchRequest(
            dispatch_id=str(row["dispatch_id"]),
            verification_id=str(row["verification_id"]),
            application_id=str(row["application_id"]),
            verification_round=int(row["verification_round"]),
            verification_target=str(row["verification_target"]),
            trigger_reason=str(row["trigger_reason"]),
            questions=tuple(dict(item) for item in questions),
        )

    def mark_dispatched(
        self,
        response: PhoneTaskDispatchResponse,
        *,
        response_cached: bool,
    ) -> Mapping[str, Any]:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE mvp_control.phone_verification_tasks
                SET dispatch_status='DISPATCHED',provider_name=%s,provider_mode=%s,
                    synthetic=%s,external_task_id=%s,dispatched_at=%s,
                    last_error_code=NULL,last_error_message=NULL,updated_at=now()
                WHERE verification_id=%s AND dispatch_id=%s
                RETURNING *
                """,
                (
                    response.provider_name,
                    response.provider_mode,
                    response.synthetic,
                    response.external_task_id,
                    response.accepted_at,
                    response.verification_id,
                    response.dispatch_id,
                ),
            )
            row = cursor.fetchone()
        if not row:
            raise PhoneVerificationPersistenceError("电核投递成功后状态更新失败")
        result = dict(row)
        result["provider_response_cached"] = response_cached
        return result

    def mark_dispatch_failure(
        self,
        verification_id: str,
        *,
        error_code: str,
        error_message: str,
        dead_letter: bool,
    ) -> None:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE mvp_control.phone_verification_tasks
                SET dispatch_status=%s,last_error_code=%s,last_error_message=%s,
                    updated_at=now()
                WHERE verification_id=%s
                """,
                (
                    "DEAD_LETTER" if dead_letter else "PENDING",
                    error_code[:64],
                    error_message[:500],
                    verification_id,
                ),
            )
