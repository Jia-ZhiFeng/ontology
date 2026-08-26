from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

try:
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:
    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, value):
            self.obj = value


DECISION_AUTHORITY = "recommendation_only"
APPROVAL_ASSERTION_VERSION = "recommendation-only-v1"
ALLOWED_RECOMMENDATIONS = frozenset({"通过", "拒绝", "转人工", "补充材料"})


class DecisionRecommendationPersistenceError(RuntimeError):
    pass


class DecisionRecommendationApplicationNotFound(
    DecisionRecommendationPersistenceError
):
    pass


class DecisionRecommendationPrerequisiteMissing(
    DecisionRecommendationPersistenceError
):
    pass


class DecisionRecommendationAlreadyExists(DecisionRecommendationPersistenceError):
    pass


class DecisionRecommendationFinalized(DecisionRecommendationPersistenceError):
    pass


@dataclass(frozen=True)
class DecisionRecommendationContext:
    application_id: str
    application_status: str
    decision_result: str | None
    risk_assessment_id: str
    risk_level: str
    pd_value: Decimal
    threshold: Decimal | None
    risk_check_result: str | None
    fraud_report_id: str
    fraud_result: str
    phone_verification_id: str
    phone_conclusion: str | None
    phone_doubts_digest: str | None

    @property
    def direct_pass_allowed(self) -> bool:
        below_threshold = self.threshold is None or self.pd_value < self.threshold
        return (
            self.risk_level == "低"
            and below_threshold
            and self.fraud_result == "通过"
            and self.phone_conclusion in {"通过", "信息基本一致"}
            and self.phone_doubts_digest is None
        )


@dataclass(frozen=True)
class DecisionRecommendationEffect:
    recommendation_id: str
    application_id: str
    previous_application_status: str
    previous_decision_result: str | None
    applied_application_status: str
    applied_decision_result: str


DECISION_RECOMMENDATION_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS mvp_control.decision_recommendations (
        recommendation_id varchar(32) PRIMARY KEY,
        application_id varchar(32) NOT NULL
            REFERENCES public.loan_application(application_id) ON DELETE RESTRICT,
        recommendation_version varchar(64) NOT NULL,
        execution_id varchar(36) NOT NULL UNIQUE
            REFERENCES mvp_control.action_executions(execution_id) ON DELETE RESTRICT,
        recommended_decision varchar(20) NOT NULL
            CHECK (recommended_decision IN ('通过','拒绝','转人工','补充材料')),
        decision_authority varchar(32) NOT NULL
            CHECK (decision_authority='recommendation_only'),
        approver_id varchar(128) NOT NULL,
        approver_role varchar(64) NOT NULL,
        approval_assertion_version varchar(32) NOT NULL,
        approval_assertion_digest char(64) NOT NULL,
        decision_reasons jsonb NOT NULL,
        evidence_reference varchar(40) NOT NULL UNIQUE,
        evidence_digest char(64) NOT NULL,
        evidence_chain jsonb NOT NULL,
        evidence_lineage jsonb NOT NULL,
        function_versions jsonb NOT NULL,
        previous_application_status varchar(20) NOT NULL,
        previous_decision_result varchar(20),
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (application_id,recommendation_version)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS decision_recommendations_application_idx
    ON mvp_control.decision_recommendations (application_id,created_at DESC)
    """,
)


def deterministic_recommendation_id(
    application_id: str, recommendation_version: str
) -> str:
    digest = hashlib.sha256(
        f"{application_id}|{recommendation_version}".encode("utf-8")
    ).hexdigest()[:28].upper()
    return f"DR{digest}"


def _json_digest(value: Mapping[str, Any]) -> str:
    raw = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def recommendation_evidence_reference(
    application_id: str,
    recommendation_version: str,
    evidence_digest: str,
) -> str:
    digest = hashlib.sha256(
        f"{application_id}|{recommendation_version}|{evidence_digest}".encode("utf-8")
    ).hexdigest()[:28].upper()
    return f"EVD{digest}"


def approval_assertion_digest(assertion: str) -> str:
    return hashlib.sha256(assertion.encode("utf-8")).hexdigest()


def _optional_digest(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else None


class PostgresDecisionRecommendationRepository:
    """Record a recommendation and its evidence without finalizing an application."""

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
                for statement in DECISION_RECOMMENDATION_SCHEMA:
                    cursor.execute(statement)
            type(self)._schema_ready = True

    @staticmethod
    def _load_context(cursor, application_id: str, *, lock: bool = False):
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
            raise DecisionRecommendationApplicationNotFound("贷款申请不存在")
        if application.get("application_status") == "已完成":
            raise DecisionRecommendationFinalized(
                "已完成申请不能再记录决策建议"
            )
        cursor.execute(
            """
            SELECT recommended_decision
            FROM mvp_control.decision_recommendations
            WHERE application_id=%s
            ORDER BY created_at DESC,recommendation_id DESC LIMIT 1
            """,
            (application_id,),
        )
        latest_recommendation = cursor.fetchone()
        current_decision = application.get("decision_result")
        if current_decision in {"通过", "拒绝"} and (
            not latest_recommendation
            or latest_recommendation.get("recommended_decision") != current_decision
        ):
            raise DecisionRecommendationFinalized(
                "申请已有非本 Action 记录的通过或拒绝结果，禁止覆盖"
            )
        cursor.execute(
            """
            SELECT assessment_id,risk_level,pd_value,threshold,check_result
            FROM public.risk_assessment_report
            WHERE application_id=%s
            ORDER BY assessment_time DESC NULLS LAST,assessment_id DESC LIMIT 1
            """,
            (application_id,),
        )
        risk = cursor.fetchone() or {}
        cursor.execute(
            """
            SELECT fraud_report_id,result
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
        if (
            not risk.get("assessment_id")
            or not risk.get("risk_level")
            or risk.get("pd_value") is None
            or not fraud.get("fraud_report_id")
            or not fraud.get("result")
            or not phone.get("verification_id")
        ):
            raise DecisionRecommendationPrerequisiteMissing(
                "缺少完整风险评估、反欺诈或电核记录"
            )
        return DecisionRecommendationContext(
            application_id=str(application["application_id"]),
            application_status=str(application["application_status"]),
            decision_result=(
                str(current_decision) if current_decision is not None else None
            ),
            risk_assessment_id=str(risk["assessment_id"]),
            risk_level=str(risk["risk_level"]),
            pd_value=Decimal(str(risk["pd_value"])),
            threshold=(
                Decimal(str(risk["threshold"]))
                if risk.get("threshold") is not None
                else None
            ),
            risk_check_result=(
                str(risk["check_result"])
                if risk.get("check_result") is not None
                else None
            ),
            fraud_report_id=str(fraud["fraud_report_id"]),
            fraud_result=str(fraud["result"]),
            phone_verification_id=str(phone["verification_id"]),
            phone_conclusion=(
                str(phone["conclusion"])
                if phone.get("conclusion") is not None
                else None
            ),
            phone_doubts_digest=_optional_digest(phone.get("doubts")),
        )

    def inspect_target(self, application_id: str) -> DecisionRecommendationContext:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            return self._load_context(cursor, application_id)

    def persist_and_enqueue(
        self,
        *,
        context: DecisionRecommendationContext,
        recommendation_version: str,
        decision_support: Mapping[str, Any],
        evidence_explanation: Mapping[str, Any],
        function_versions: Mapping[str, str],
        approver_id: str,
        approver_role: str,
        approval_assertion: str,
        outbox_repository,
        execution_id: str,
        ontology_version: str,
        actor_id: str,
        request_id: str,
        max_attempts: int,
    ):
        self.ensure_schema()
        outbox_repository.ensure_schema()
        recommendation = str(decision_support.get("recommended_decision") or "")
        if recommendation not in ALLOWED_RECOMMENDATIONS:
            raise DecisionRecommendationPersistenceError("决策建议值无效")
        normalized_explanation = json.loads(
            json.dumps(dict(evidence_explanation), ensure_ascii=False, default=str)
        )
        normalized_versions = {
            str(name): str(version) for name, version in function_versions.items()
        }
        digest = _json_digest(normalized_explanation)
        recommendation_id = deterministic_recommendation_id(
            context.application_id, recommendation_version
        )
        reference = recommendation_evidence_reference(
            context.application_id, recommendation_version, digest
        )
        assertion_digest = approval_assertion_digest(approval_assertion)
        reasons = decision_support.get("decision_reasons")
        if not isinstance(reasons, list) or not reasons:
            raise DecisionRecommendationPersistenceError("决策建议缺少原因")
        lineage = normalized_explanation.get("lineage")
        chain = normalized_explanation.get("evidence_chain")
        if not isinstance(lineage, Mapping) or not isinstance(chain, list) or not chain:
            raise DecisionRecommendationPersistenceError("决策证据或血缘无效")
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            current = self._load_context(cursor, context.application_id, lock=True)
            if current != context:
                raise DecisionRecommendationPrerequisiteMissing(
                    "提交时申请、报告或电核上下文已经变化"
                )
            cursor.execute(
                """
                SELECT recommendation_id
                FROM mvp_control.decision_recommendations
                WHERE application_id=%s AND recommendation_version=%s FOR UPDATE
                """,
                (context.application_id, recommendation_version),
            )
            if cursor.fetchone():
                raise DecisionRecommendationAlreadyExists(
                    "相同申请和版本的决策建议已存在"
                )
            cursor.execute(
                """
                UPDATE public.loan_application
                SET decision_result=%s
                WHERE application_id=%s
                  AND application_status=%s
                  AND decision_result IS NOT DISTINCT FROM %s
                RETURNING *
                """,
                (
                    recommendation,
                    context.application_id,
                    context.application_status,
                    context.decision_result,
                ),
            )
            stored_application = cursor.fetchone()
            if not stored_application:
                raise DecisionRecommendationPersistenceError(
                    "贷款申请决策建议更新失败"
                )
            cursor.execute(
                """
                INSERT INTO mvp_control.decision_recommendations (
                    recommendation_id,application_id,recommendation_version,
                    execution_id,recommended_decision,decision_authority,
                    approver_id,approver_role,approval_assertion_version,
                    approval_assertion_digest,decision_reasons,evidence_reference,
                    evidence_digest,evidence_chain,evidence_lineage,function_versions,
                    previous_application_status,previous_decision_result
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                ) RETURNING *
                """,
                (
                    recommendation_id,
                    context.application_id,
                    recommendation_version,
                    execution_id,
                    recommendation,
                    DECISION_AUTHORITY,
                    approver_id,
                    approver_role,
                    APPROVAL_ASSERTION_VERSION,
                    assertion_digest,
                    Jsonb(reasons),
                    reference,
                    digest,
                    Jsonb(chain),
                    Jsonb(dict(lineage)),
                    Jsonb(normalized_versions),
                    context.application_status,
                    context.decision_result,
                ),
            )
            stored_recommendation = cursor.fetchone()
            if not stored_recommendation:
                raise DecisionRecommendationPersistenceError(
                    "决策建议写入后回读失败"
                )
            effect = DecisionRecommendationEffect(
                recommendation_id=recommendation_id,
                application_id=context.application_id,
                previous_application_status=context.application_status,
                previous_decision_result=context.decision_result,
                applied_application_status=context.application_status,
                applied_decision_result=recommendation,
            )
            event = outbox_repository.enqueue(
                cursor,
                execution_id=execution_id,
                event_type="DECISION_RECOMMENDATION_GRAPH_SYNC",
                aggregate_type="LoanApplication",
                aggregate_id=context.application_id,
                ontology_version=ontology_version,
                deduplication_key=execution_id,
                payload={
                    "actor_id": actor_id,
                    "request_id": request_id,
                    "application_id": context.application_id,
                    "recommendation_id": recommendation_id,
                    "recommendation_version": recommendation_version,
                    "recommended_decision": recommendation,
                    "decision_authority": DECISION_AUTHORITY,
                    "approver_id": approver_id,
                    "approver_role": approver_role,
                    "evidence_reference": reference,
                    "reason_codes": [
                        str(item.get("code"))
                        for item in reasons
                        if isinstance(item, Mapping) and item.get("code")
                    ],
                    "effect": effect.__dict__,
                },
                max_attempts=max_attempts,
            )
        return (
            dict(stored_application),
            dict(stored_recommendation),
            effect,
            event,
        )

    def compensate(self, effect: DecisionRecommendationEffect) -> None:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM mvp_control.decision_recommendations
                WHERE recommendation_id=%s AND application_id=%s FOR UPDATE
                """,
                (effect.recommendation_id, effect.application_id),
            )
            recommendation = cursor.fetchone()
            if not recommendation:
                return
            cursor.execute(
                """
                SELECT application_status,decision_result
                FROM public.loan_application WHERE application_id=%s FOR UPDATE
                """,
                (effect.application_id,),
            )
            application = cursor.fetchone()
            cursor.execute(
                """
                SELECT count(*) AS count
                FROM mvp_control.decision_recommendations
                WHERE application_id=%s AND recommendation_id<>%s
                  AND created_at>= %s
                """,
                (
                    effect.application_id,
                    effect.recommendation_id,
                    recommendation["created_at"],
                ),
            )
            newer = int(cursor.fetchone()["count"])
            if (
                not application
                or application.get("application_status")
                != effect.applied_application_status
                or application.get("decision_result") != effect.applied_decision_result
                or newer
            ):
                raise DecisionRecommendationPersistenceError(
                    "决策建议已被后续业务使用，拒绝自动补偿覆盖"
                )
            cursor.execute(
                """
                DELETE FROM mvp_control.decision_recommendations
                WHERE recommendation_id=%s
                """,
                (effect.recommendation_id,),
            )
            cursor.execute(
                """
                UPDATE public.loan_application
                SET decision_result=%s
                WHERE application_id=%s AND application_status=%s
                """,
                (
                    effect.previous_decision_result,
                    effect.application_id,
                    effect.previous_application_status,
                ),
            )


def decision_recommendation_effect_from_payload(
    payload: Mapping[str, Any],
) -> DecisionRecommendationEffect:
    raw = payload.get("effect")
    if not isinstance(raw, Mapping):
        raise DecisionRecommendationPersistenceError(
            "outbox 缺少决策建议补偿信息"
        )
    return DecisionRecommendationEffect(
        recommendation_id=str(raw["recommendation_id"]),
        application_id=str(raw["application_id"]),
        previous_application_status=str(raw["previous_application_status"]),
        previous_decision_result=(
            str(raw["previous_decision_result"])
            if raw.get("previous_decision_result") is not None
            else None
        ),
        applied_application_status=str(raw["applied_application_status"]),
        applied_decision_result=str(raw["applied_decision_result"]),
    )
