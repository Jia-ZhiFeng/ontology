from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Mapping

try:
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:
    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, value):
            self.obj = value


class ManualReviewPersistenceError(RuntimeError):
    pass


class ManualReviewApplicationNotFound(ManualReviewPersistenceError):
    pass


class ManualReviewRuleNotMet(ManualReviewPersistenceError):
    pass


class ManualReviewEvidenceIncomplete(ManualReviewPersistenceError):
    pass


class ManualReviewAlreadyExists(ManualReviewPersistenceError):
    pass


@dataclass(frozen=True)
class ManualReviewContext:
    application_id: str
    application_status: str
    decision_result: str | None
    risk_assessment_id: str | None
    risk_level: str | None
    risk_check_result: str | None
    fraud_report_id: str | None
    fraud_result: str | None
    fraud_hit: bool
    phone_verification_id: str | None
    phone_conclusion: str | None
    phone_has_doubts: bool
    source_execution_id: str
    source_action_name: str
    source_execution_status: str
    matched_rules: tuple[str, ...]


@dataclass(frozen=True)
class ManualReviewEffect:
    review_id: str
    application_id: str
    previous_application_status: str
    previous_decision_result: str | None
    applied_application_status: str = "待人工复核"
    applied_decision_result: str = "转人工"


MANUAL_REVIEW_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS mvp_control.manual_review_records (
        review_id varchar(32) PRIMARY KEY,
        application_id varchar(32) NOT NULL
            REFERENCES public.loan_application(application_id) ON DELETE RESTRICT,
        review_reason varchar(200) NOT NULL,
        source_execution_id varchar(36) NOT NULL
            REFERENCES mvp_control.action_executions(execution_id) ON DELETE RESTRICT,
        trigger_execution_id varchar(36) NOT NULL UNIQUE
            REFERENCES mvp_control.action_executions(execution_id) ON DELETE RESTRICT,
        matched_rules jsonb NOT NULL,
        recommended_decision varchar(20) NOT NULL,
        evidence_reference varchar(40) NOT NULL UNIQUE,
        evidence_digest char(64) NOT NULL,
        evidence_lineage jsonb NOT NULL,
        previous_application_status varchar(20) NOT NULL,
        previous_decision_result varchar(20),
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (application_id,review_reason)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS manual_review_records_application_idx
    ON mvp_control.manual_review_records (application_id,created_at)
    """,
)


ALLOWED_SOURCE_ACTIONS = frozenset(
    {
        "generate_risk_assessment",
        "request_fraud_report",
        "create_phone_verification_task",
        "record_phone_verification",
    }
)


def deterministic_manual_review_id(application_id: str, review_reason: str) -> str:
    digest = hashlib.sha256(
        f"{application_id}|{review_reason}".encode("utf-8")
    ).hexdigest()[:28].upper()
    return f"MR{digest}"


def manual_review_evidence_reference(
    application_id: str, review_reason: str, source_execution_id: str,
    evidence_digest: str,
) -> str:
    digest = hashlib.sha256(
        f"{application_id}|{review_reason}|{source_execution_id}|{evidence_digest}".encode(
            "utf-8"
        )
    ).hexdigest()[:28].upper()
    return f"EVD{digest}"


def evidence_digest(evidence: Mapping[str, Any]) -> str:
    raw = json.dumps(
        dict(evidence), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _fraud_hit(value: Any) -> bool:
    compact = str(value or "").replace(" ", "")
    return bool(compact) and not any(
        token in compact for token in ("未命中", "无命中", "未发现")
    )


def _source_application_id(result_summary: Any) -> str | None:
    if not isinstance(result_summary, Mapping):
        return None
    value = result_summary.get("application_id")
    return str(value) if value else None


class PostgresManualReviewRepository:
    """Persist a review-routing fact and status change in one transaction."""

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
                for statement in MANUAL_REVIEW_SCHEMA:
                    cursor.execute(statement)
            type(self)._schema_ready = True

    @staticmethod
    def _load_context(
        cursor, application_id: str, source_execution_id: str, *, lock: bool = False
    ) -> ManualReviewContext:
        cursor.execute(
            f"""
            SELECT application_id,application_status,decision_result
            FROM public.loan_application
            WHERE application_id=%s {"FOR UPDATE" if lock else ""}
            """,
            (application_id,),
        )
        application = cursor.fetchone()
        if not application:
            raise ManualReviewApplicationNotFound("贷款申请不存在")
        if application.get("application_status") == "已完成" or application.get(
            "decision_result"
        ) in {"通过", "拒绝"}:
            raise ManualReviewRuleNotMet("已完成或已有最终结果的申请不能转入人工复核")
        cursor.execute(
            """
            SELECT execution_id,action_name,status,result_summary
            FROM mvp_control.action_executions WHERE execution_id=%s
            """,
            (source_execution_id,),
        )
        source = cursor.fetchone()
        if not source:
            raise ManualReviewEvidenceIncomplete("来源 Action 执行不存在")
        retained_phone_result = (
            source.get("action_name") == "record_phone_verification"
            and source.get("status") == "FAILED"
            and isinstance(source.get("result_summary"), Mapping)
            and source["result_summary"].get("retained_terminal_result") is True
        )
        if (
            source.get("action_name") not in ALLOWED_SOURCE_ACTIONS
            or (source.get("status") != "SUCCEEDED" and not retained_phone_result)
            or _source_application_id(source.get("result_summary")) != application_id
        ):
            raise ManualReviewEvidenceIncomplete(
                "来源 Action 未成功、类型不受支持或不属于当前申请"
            )
        cursor.execute(
            """
            SELECT assessment_id,risk_level,check_result
            FROM public.risk_assessment_report
            WHERE application_id=%s
            ORDER BY assessment_time DESC NULLS LAST,assessment_id DESC LIMIT 1
            """,
            (application_id,),
        )
        risk = cursor.fetchone() or {}
        cursor.execute(
            """
            SELECT fraud_report_id,result,hit_details
            FROM public.anti_fraud_report
            WHERE application_id=%s ORDER BY fraud_report_id DESC LIMIT 1
            """,
            (application_id,),
        )
        fraud = cursor.fetchone() or {}
        cursor.execute(
            """
            SELECT verification_id,conclusion,doubts
            FROM public.phone_verification_record
            WHERE application_id=%s
            ORDER BY call_time DESC NULLS LAST,verification_id DESC LIMIT 1
            """,
            (application_id,),
        )
        phone = cursor.fetchone() or {}
        matched: list[str] = []
        if risk.get("risk_level") in {"中", "高", "拒绝"} or risk.get(
            "check_result"
        ) in {"待人工复核", "不通过"}:
            matched.append("RISK_REVIEW_REQUIRED")
        if fraud.get("result") in {"人工复核", "拒绝"}:
            matched.append("FRAUD_REVIEW_REQUIRED")
        fraud_hit = _fraud_hit(fraud.get("hit_details"))
        if fraud_hit:
            matched.append("FRAUD_RULE_HIT")
        phone_has_doubts = bool(str(phone.get("doubts") or "").strip())
        if phone.get("conclusion") == "存疑" or phone_has_doubts:
            matched.append("PHONE_DOUBT")
        if not matched:
            raise ManualReviewRuleNotMet("当前申请未命中任何转人工规则")
        return ManualReviewContext(
            application_id=str(application["application_id"]),
            application_status=str(application["application_status"]),
            decision_result=(
                str(application["decision_result"])
                if application.get("decision_result") is not None
                else None
            ),
            risk_assessment_id=(str(risk["assessment_id"]) if risk.get("assessment_id") else None),
            risk_level=(str(risk["risk_level"]) if risk.get("risk_level") else None),
            risk_check_result=(str(risk["check_result"]) if risk.get("check_result") else None),
            fraud_report_id=(str(fraud["fraud_report_id"]) if fraud.get("fraud_report_id") else None),
            fraud_result=(str(fraud["result"]) if fraud.get("result") else None),
            fraud_hit=fraud_hit,
            phone_verification_id=(str(phone["verification_id"]) if phone.get("verification_id") else None),
            phone_conclusion=(str(phone["conclusion"]) if phone.get("conclusion") else None),
            phone_has_doubts=phone_has_doubts,
            source_execution_id=str(source["execution_id"]),
            source_action_name=str(source["action_name"]),
            source_execution_status=str(source["status"]),
            matched_rules=tuple(matched),
        )

    def inspect_target(
        self, application_id: str, source_execution_id: str
    ) -> ManualReviewContext:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            return self._load_context(cursor, application_id, source_execution_id)

    def persist_and_enqueue(
        self,
        *,
        context: ManualReviewContext,
        review_reason: str,
        decision_support: Mapping[str, Any],
        evidence_lineage: Mapping[str, Any],
        outbox_repository,
        execution_id: str,
        ontology_version: str,
        actor_id: str,
        request_id: str,
        max_attempts: int,
    ):
        self.ensure_schema()
        outbox_repository.ensure_schema()
        digest = evidence_digest(evidence_lineage)
        normalized_evidence = json.loads(
            json.dumps(dict(evidence_lineage), ensure_ascii=False, default=str)
        )
        review_id = deterministic_manual_review_id(context.application_id, review_reason)
        reference = manual_review_evidence_reference(
            context.application_id, review_reason, context.source_execution_id, digest
        )
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            current = self._load_context(
                cursor, context.application_id, context.source_execution_id, lock=True
            )
            if current != context:
                raise ManualReviewEvidenceIncomplete(
                    "提交时申请、风险或证据上下文已经变化"
                )
            cursor.execute(
                """
                SELECT review_id FROM mvp_control.manual_review_records
                WHERE application_id=%s AND review_reason=%s FOR UPDATE
                """,
                (context.application_id, review_reason),
            )
            if cursor.fetchone():
                raise ManualReviewAlreadyExists("相同申请和原因的人工复核记录已存在")
            cursor.execute(
                """
                UPDATE public.loan_application
                SET application_status='待人工复核',decision_result='转人工'
                WHERE application_id=%s
                  AND application_status=%s
                  AND decision_result IS NOT DISTINCT FROM %s
                RETURNING *
                """,
                (
                    context.application_id, context.application_status,
                    context.decision_result,
                ),
            )
            stored = cursor.fetchone()
            if not stored:
                raise ManualReviewPersistenceError("申请人工复核状态更新失败")
            cursor.execute(
                """
                INSERT INTO mvp_control.manual_review_records (
                    review_id,application_id,review_reason,source_execution_id,
                    trigger_execution_id,matched_rules,recommended_decision,
                    evidence_reference,evidence_digest,evidence_lineage,
                    previous_application_status,previous_decision_result
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (
                    review_id, context.application_id, review_reason,
                    context.source_execution_id, execution_id,
                    Jsonb(list(context.matched_rules)),
                    str(decision_support["recommended_decision"]), reference, digest,
                    Jsonb(normalized_evidence), context.application_status,
                    context.decision_result,
                ),
            )
            review = cursor.fetchone()
            if not review:
                raise ManualReviewPersistenceError("人工复核原因写入后回读失败")
            effect = ManualReviewEffect(
                review_id=review_id,
                application_id=context.application_id,
                previous_application_status=context.application_status,
                previous_decision_result=context.decision_result,
            )
            event = outbox_repository.enqueue(
                cursor,
                execution_id=execution_id,
                event_type="MANUAL_REVIEW_GRAPH_SYNC",
                aggregate_type="LoanApplication",
                aggregate_id=context.application_id,
                ontology_version=ontology_version,
                deduplication_key=execution_id,
                payload={
                    "actor_id": actor_id,
                    "request_id": request_id,
                    "application_id": context.application_id,
                    "review_id": review_id,
                    "review_reason": review_reason,
                    "source_execution_id": context.source_execution_id,
                    "matched_rules": list(context.matched_rules),
                    "recommended_decision": decision_support["recommended_decision"],
                    "application_status": "待人工复核",
                    "routing_result": "转人工",
                    "review_evidence_reference": reference,
                    "effect": {
                        "review_id": effect.review_id,
                        "application_id": effect.application_id,
                        "previous_application_status": effect.previous_application_status,
                        "previous_decision_result": effect.previous_decision_result,
                        "applied_application_status": effect.applied_application_status,
                        "applied_decision_result": effect.applied_decision_result,
                    },
                },
                max_attempts=max_attempts,
            )
        return dict(stored), dict(review), effect, event

    def compensate(self, effect: ManualReviewEffect) -> None:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM mvp_control.manual_review_records
                WHERE review_id=%s AND application_id=%s FOR UPDATE
                """,
                (effect.review_id, effect.application_id),
            )
            review = cursor.fetchone()
            if not review:
                return
            cursor.execute(
                "SELECT application_status,decision_result FROM public.loan_application WHERE application_id=%s FOR UPDATE",
                (effect.application_id,),
            )
            application = cursor.fetchone()
            cursor.execute(
                "SELECT count(*) AS count FROM mvp_control.manual_review_records WHERE application_id=%s AND review_id<>%s",
                (effect.application_id, effect.review_id),
            )
            other = int(cursor.fetchone()["count"])
            if (
                not application
                or application.get("application_status") != effect.applied_application_status
                or application.get("decision_result") != effect.applied_decision_result
                or other
            ):
                raise ManualReviewPersistenceError(
                    "人工复核状态已被后续业务使用，拒绝自动补偿覆盖"
                )
            cursor.execute(
                "DELETE FROM mvp_control.manual_review_records WHERE review_id=%s",
                (effect.review_id,),
            )
            cursor.execute(
                """
                UPDATE public.loan_application
                SET application_status=%s,decision_result=%s
                WHERE application_id=%s
                """,
                (
                    effect.previous_application_status,
                    effect.previous_decision_result,
                    effect.application_id,
                ),
            )


def manual_review_effect_from_payload(payload: Mapping[str, Any]) -> ManualReviewEffect:
    raw = payload.get("effect")
    if not isinstance(raw, Mapping):
        raise ManualReviewPersistenceError("outbox 缺少人工复核补偿信息")
    return ManualReviewEffect(
        review_id=str(raw["review_id"]),
        application_id=str(raw["application_id"]),
        previous_application_status=str(raw["previous_application_status"]),
        previous_decision_result=(
            str(raw["previous_decision_result"])
            if raw.get("previous_decision_result") is not None
            else None
        ),
        applied_application_status=str(raw.get("applied_application_status") or "待人工复核"),
        applied_decision_result=str(raw.get("applied_decision_result") or "转人工"),
    )
