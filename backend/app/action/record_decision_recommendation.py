from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .decision_recommendation_repository import (
    DECISION_AUTHORITY,
    DecisionRecommendationAlreadyExists,
    DecisionRecommendationApplicationNotFound,
    DecisionRecommendationContext,
    DecisionRecommendationFinalized,
    DecisionRecommendationPersistenceError,
    DecisionRecommendationPrerequisiteMissing,
    PostgresDecisionRecommendationRepository,
)
from .execution import (
    ActionExecutionRecord,
    ActionExecutionService,
    ActionRequestError,
    ActionStateMachine,
)
from .function_data_adapter import (
    FunctionDataAdapterError,
    PersonalCreditFunctionDataAdapter,
)
from .function_registry import FunctionNotBoundError, FunctionRegistryError
from .outbox import (
    DecisionRecommendationOutboxWorker,
    PostgresActionOutboxRepository,
)


ACTION_NAME = "record_decision_recommendation"
DECLARED_FUNCTIONS = (
    "aggregate_decision_support",
    "build_evidence_explanation",
)
RECOMMENDATION_ONLY_ASSERTION = "我确认该记录仅为决策建议，不代表最终审批"
HUMAN_APPROVER_ROLES = frozenset({"信审员", "风控专员", "风险经理"})
APPLICATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
APPROVER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:-]{2,127}$")


class RecordDecisionRecommendationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        execution_id: str | None = None,
        status: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.execution_id = execution_id
        self.status = status


@dataclass(frozen=True)
class ValidatedDecisionRecommendationRequest:
    application_id: str
    recommendation_version: str
    approver_id: str
    approval_assertion: str
    request_id: str

    def as_mapping(self) -> dict[str, str]:
        return {
            "application_id": self.application_id,
            "recommendation_version": self.recommendation_version,
            "approver_id": self.approver_id,
            "approval_assertion": self.approval_assertion,
            "request_id": self.request_id,
        }


def _required_text(value: Any, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionRequestError(f"{field_name} 不能为空")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ActionRequestError(f"{field_name} 超出长度限制")
    return normalized


def validate_decision_recommendation_request(
    request: Mapping[str, Any],
) -> ValidatedDecisionRecommendationRequest:
    if not isinstance(request, Mapping):
        raise ActionRequestError("Action request 必须是映射")
    allowed = {
        "application_id",
        "recommendation_version",
        "approver_id",
        "approval_assertion",
        "request_id",
    }
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise ActionRequestError(f"Action 请求包含未声明字段：{','.join(unknown)}")
    application_id = _required_text(request.get("application_id"), "application_id", 32)
    recommendation_version = _required_text(
        request.get("recommendation_version"), "recommendation_version", 64
    )
    approver_id = _required_text(request.get("approver_id"), "approver_id", 128)
    assertion = _required_text(
        request.get("approval_assertion"), "approval_assertion", 80
    )
    if not APPLICATION_ID_PATTERN.fullmatch(application_id):
        raise ActionRequestError("application_id 格式无效")
    if not VERSION_PATTERN.fullmatch(recommendation_version):
        raise ActionRequestError("recommendation_version 格式无效")
    if not APPROVER_ID_PATTERN.fullmatch(approver_id):
        raise ActionRequestError("approver_id 格式无效")
    if assertion != RECOMMENDATION_ONLY_ASSERTION:
        raise ActionRequestError(
            f"approval_assertion 必须为：{RECOMMENDATION_ONLY_ASSERTION}"
        )
    return ValidatedDecisionRecommendationRequest(
        application_id=application_id,
        recommendation_version=recommendation_version,
        approver_id=approver_id,
        approval_assertion=assertion,
        request_id=_required_text(request.get("request_id"), "request_id", 128),
    )


def _reference_map(chain: list[Any]) -> dict[str, str]:
    references: dict[str, str] = {}
    for item in chain:
        if isinstance(item, Mapping) and item.get("source_type") and item.get(
            "reference_id"
        ):
            references[str(item["source_type"])] = str(item["reference_id"])
    return references


def _validate_decision_run(
    run: Mapping[str, Any],
    *,
    context: DecisionRecommendationContext,
    execution_id: str,
    ontology_version: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], dict[str, Any]]:
    if run.get("status") != "success" or run.get("read_only") is not True:
        raise RecordDecisionRecommendationError(
            "ACTION_RECOMMENDATION_INVALID",
            "决策支持 Function 未返回只读成功状态",
        )
    if tuple(run.get("execution_order") or ()) != DECLARED_FUNCTIONS:
        raise RecordDecisionRecommendationError(
            "ACTION_RECOMMENDATION_INVALID", "Function 执行顺序与声明不一致"
        )
    functions = run.get("functions")
    if not isinstance(functions, Mapping) or tuple(functions) != DECLARED_FUNCTIONS:
        raise RecordDecisionRecommendationError(
            "ACTION_RECOMMENDATION_INVALID", "Function 集合与声明不一致"
        )
    snapshot_time = run.get("postgres_snapshot_time")
    versions: dict[str, str] = {}
    for name in DECLARED_FUNCTIONS:
        envelope = functions[name]
        if (
            not isinstance(envelope, Mapping)
            or envelope.get("status") != "success"
            or envelope.get("complete") is not True
            or not envelope.get("implementation_version")
            or not envelope.get("evidence")
            or envelope.get("data_snapshot_time") != snapshot_time
        ):
            raise RecordDecisionRecommendationError(
                "ACTION_RECOMMENDATION_INVALID",
                f"{name} 输出不完整或快照不一致",
            )
        versions[name] = str(envelope["implementation_version"])
    decision = functions["aggregate_decision_support"].get("result")
    if not isinstance(decision, Mapping):
        raise RecordDecisionRecommendationError(
            "ACTION_RECOMMENDATION_INVALID", "决策建议结果不是对象"
        )
    recommendation = decision.get("recommended_decision")
    reasons = decision.get("decision_reasons")
    if (
        recommendation not in {"通过", "拒绝", "转人工", "补充材料"}
        or decision.get("decision_boundary")
        != "decision_support_only_not_final_approval"
        or not isinstance(reasons, list)
        or not reasons
        or any(
            not isinstance(item, Mapping)
            or not item.get("code")
            or not item.get("description")
            for item in reasons
        )
    ):
        raise RecordDecisionRecommendationError(
            "ACTION_RECOMMENDATION_INVALID", "决策建议、原因或安全边界无效"
        )
    if recommendation == "通过" and not context.direct_pass_allowed:
        raise RecordDecisionRecommendationError(
            "ACTION_HIGH_RISK_PASS_FORBIDDEN",
            "风险、反欺诈或电核上下文不允许记录通过建议",
        )
    explanation = functions["build_evidence_explanation"].get("result")
    if not isinstance(explanation, Mapping):
        raise RecordDecisionRecommendationError(
            "ACTION_RECOMMENDATION_INVALID", "证据说明结果不是对象"
        )
    chain = explanation.get("evidence_chain")
    lineage = explanation.get("lineage")
    if (
        not isinstance(chain, list)
        or len(chain) < 5
        or not isinstance(lineage, Mapping)
        or lineage.get("application_id") != context.application_id
        or lineage.get("ontology_version") != ontology_version
        or lineage.get("action_execution_id") != execution_id
        or lineage.get("missing_sources")
        or not lineage.get("source_references")
    ):
        raise RecordDecisionRecommendationError(
            "ACTION_RECOMMENDATION_INVALID", "证据链、血缘或来源引用不完整"
        )
    references = _reference_map(chain)
    expected_references = {
        "LoanApplication": context.application_id,
        "RiskAssessmentReport": context.risk_assessment_id,
        "AntiFraudReport": context.fraud_report_id,
        "PhoneVerificationRecord": context.phone_verification_id,
    }
    if any(references.get(name) != value for name, value in expected_references.items()):
        raise RecordDecisionRecommendationError(
            "ACTION_RECOMMENDATION_INVALID", "证据来源与当前报告上下文不一致"
        )
    summary = {
        "recommended_decision": recommendation,
        "reason_codes": [str(item["code"]) for item in reasons],
        "function_versions": versions,
        "postgres_snapshot_id": run.get("postgres_snapshot_id"),
        "postgres_snapshot_time": snapshot_time,
    }
    return dict(decision), dict(explanation), versions, summary


def _error_code(exc: Exception) -> str:
    if isinstance(exc, RecordDecisionRecommendationError):
        return exc.code
    if isinstance(exc, DecisionRecommendationPrerequisiteMissing):
        return "ACTION_REQUIRED_REPORT_MISSING"
    if isinstance(exc, DecisionRecommendationApplicationNotFound):
        return "ACTION_REQUIRED_REPORT_MISSING"
    if isinstance(exc, DecisionRecommendationFinalized):
        return "ACTION_RECOMMENDATION_INVALID"
    if isinstance(exc, DecisionRecommendationAlreadyExists):
        return "ACTION_IDEMPOTENCY_CONFLICT"
    if isinstance(exc, FunctionNotBoundError):
        return "ACTION_FUNCTION_NOT_BOUND"
    if isinstance(exc, (FunctionDataAdapterError, FunctionRegistryError)):
        return "ACTION_RECOMMENDATION_INVALID"
    if isinstance(exc, ActionRequestError):
        return "ACTION_REQUEST_INVALID"
    if isinstance(exc, DecisionRecommendationPersistenceError):
        return "ACTION_WRITE_FAILED"
    return "ACTION_WRITE_FAILED"


class RecordDecisionRecommendationActionService:
    """Persist evidence-backed advice while preserving the human approval boundary."""

    def __init__(
        self,
        *,
        postgres_service,
        nebula_factory: Callable[[], Any],
        proposal,
        execution_repository,
        recommendation_repository: PostgresDecisionRecommendationRepository | None = None,
        function_adapter: PersonalCreditFunctionDataAdapter | None = None,
        outbox_repository: PostgresActionOutboxRepository | None = None,
        outbox_worker: DecisionRecommendationOutboxWorker | None = None,
        outbox_max_attempts: int = 5,
    ):
        self.postgres = postgres_service
        self.nebula_factory = nebula_factory
        self.proposal = proposal
        self.execution_repository = execution_repository
        self.executions = ActionExecutionService(execution_repository, proposal=proposal)
        self.recommendations = recommendation_repository or (
            PostgresDecisionRecommendationRepository(postgres_service)
        )
        self.recommendations.ensure_schema()
        self.functions = function_adapter or PersonalCreditFunctionDataAdapter(
            postgres_service, nebula_factory, proposal=proposal
        )
        self.outbox = outbox_repository or PostgresActionOutboxRepository(
            postgres_service
        )
        self.outbox.ensure_schema()
        self.outbox_max_attempts = max(1, int(outbox_max_attempts))
        self.outbox_worker = outbox_worker or DecisionRecommendationOutboxWorker(
            postgres_service=postgres_service,
            nebula_factory=nebula_factory,
            proposal_loader=self._proposal_for_version,
            outbox_repository=self.outbox,
            execution_repository=execution_repository,
            recommendation_repository=self.recommendations,
        )

    def _proposal_for_version(self, version: str):
        if version != self.proposal.version:
            raise ActionRequestError(f"outbox 本体版本不可用：{version}")
        return self.proposal

    @staticmethod
    def _existing_response(record: ActionExecutionRecord) -> dict[str, Any]:
        if record.status == "SUCCEEDED":
            return {
                "execution_id": record.execution_id,
                "action_name": record.action_name,
                "status": record.status,
                "ontology_version": record.ontology_version,
                "idempotent_replay": True,
                "result": dict(record.result_summary or {}),
                "error_code": record.error_code,
                "error_message": record.error_message,
                "audit_reference": record.execution_id,
            }
        if record.status in {"FAILED", "COMPENSATED", "COMPENSATION_FAILED"}:
            raise RecordDecisionRecommendationError(
                record.error_code or "ACTION_WRITE_FAILED",
                record.error_message or "既有决策建议执行未成功，未重复写入",
                execution_id=record.execution_id,
                status=record.status,
            )
        raise RecordDecisionRecommendationError(
            "ACTION_EXECUTION_IN_PROGRESS",
            "决策建议正在提交，请查询原执行状态",
            execution_id=record.execution_id,
            status=record.status,
        )

    @staticmethod
    def _confirm_human_gate(
        validated: ValidatedDecisionRecommendationRequest,
        *,
        actor_id: str,
        actor_role: str,
    ) -> None:
        if actor_role not in HUMAN_APPROVER_ROLES:
            raise RecordDecisionRecommendationError(
                "ACTION_APPROVAL_REQUIRED",
                "Agent 或非有权角色不能作为决策建议确认人",
            )
        if validated.approver_id != actor_id:
            raise RecordDecisionRecommendationError(
                "ACTION_APPROVAL_REQUIRED",
                "approver_id 必须与服务端认证身份一致",
            )

    def execute(
        self,
        request: Mapping[str, Any],
        *,
        ontology_version: str,
        actor_id: str,
        actor_role: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        validated = validate_decision_recommendation_request(request)
        if ontology_version != self.proposal.version:
            raise ActionRequestError("ontology_version 与活动本体版本不一致")
        reservation = self.executions.start(
            ACTION_NAME,
            validated.as_mapping(),
            ontology_version=ontology_version,
            actor_id=actor_id,
            actor_role=actor_role,
            correlation_id=correlation_id or validated.request_id,
        )
        if not reservation.created:
            return self._existing_response(reservation.record)
        record = reservation.record
        outbox_event = None
        try:
            record = self.executions.advance(
                record.execution_id,
                "AUTHORIZING",
                expected_version=record.version,
                event_type="authorization_started",
                actor_id=actor_id,
            )
            self.executions.record_event(
                record.execution_id,
                event_type="authorization_checked",
                actor_id=actor_id,
                details={"actor_role": actor_role, "allowed": True},
            )
            record = self.executions.advance(
                record.execution_id,
                "VALIDATING",
                expected_version=record.version,
                event_type="validation_started",
                actor_id=actor_id,
            )
            context = self.recommendations.inspect_target(validated.application_id)
            record = self.executions.advance(
                record.execution_id,
                "RUNNING",
                expected_version=record.version,
                event_type="decision_support_started",
                actor_id=actor_id,
            )
            run = self.functions.execute_decision_support(
                validated.application_id,
                ontology_version=ontology_version,
                action_execution_id=record.execution_id,
                actor=actor_id,
            )
            decision, explanation, function_versions, evidence_summary = (
                _validate_decision_run(
                    run,
                    context=context,
                    execution_id=record.execution_id,
                    ontology_version=ontology_version,
                )
            )
            self.executions.record_event(
                record.execution_id,
                event_type="decision_support_aggregated",
                actor_id=actor_id,
                details=evidence_summary,
            )
            record = self.executions.advance(
                record.execution_id,
                "VERIFYING",
                expected_version=record.version,
                event_type="recommendation_validated",
                actor_id=actor_id,
                details={
                    "recommended_decision": decision["recommended_decision"],
                    "decision_authority": DECISION_AUTHORITY,
                    "direct_pass_allowed": context.direct_pass_allowed,
                },
            )
            self._confirm_human_gate(
                validated, actor_id=actor_id, actor_role=actor_role
            )
            self.executions.record_event(
                record.execution_id,
                event_type="human_confirmation_recorded",
                actor_id=actor_id,
                details={
                    "approver_id": validated.approver_id,
                    "approver_role": actor_role,
                    "assertion_version": "recommendation-only-v1",
                    "decision_authority": DECISION_AUTHORITY,
                },
            )
            record = self.executions.advance(
                record.execution_id,
                "COMMITTING",
                expected_version=record.version,
                event_type="persistence_started",
                actor_id=actor_id,
            )
            stored_application, stored_recommendation, effect, outbox_event = (
                self.recommendations.persist_and_enqueue(
                    context=context,
                    recommendation_version=validated.recommendation_version,
                    decision_support=decision,
                    evidence_explanation=explanation,
                    function_versions=function_versions,
                    approver_id=validated.approver_id,
                    approver_role=actor_role,
                    approval_assertion=validated.approval_assertion,
                    outbox_repository=self.outbox,
                    execution_id=record.execution_id,
                    ontology_version=ontology_version,
                    actor_id=actor_id,
                    request_id=validated.request_id,
                    max_attempts=self.outbox_max_attempts,
                )
            )
            self.executions.record_event(
                record.execution_id,
                event_type="recommendation_committed",
                actor_id=actor_id,
                details={
                    "application_id": validated.application_id,
                    "recommendation_id": stored_recommendation["recommendation_id"],
                    "recommendation_version": validated.recommendation_version,
                    "recommended_decision": stored_application["decision_result"],
                    "decision_authority": DECISION_AUTHORITY,
                    "outbox_event_id": outbox_event.event_id,
                },
            )
            pending_result = {
                "application_id": validated.application_id,
                "recommendation_id": stored_recommendation["recommendation_id"],
                "recommendation_version": validated.recommendation_version,
                "recommended_decision": decision["recommended_decision"],
                "decision_authority": DECISION_AUTHORITY,
                "evidence_reference": stored_recommendation["evidence_reference"],
                "reason_codes": evidence_summary["reason_codes"],
                "approver_id": validated.approver_id,
                "approver_role": actor_role,
                "application_status": context.application_status,
                "persistence": {
                    "postgres": "recommendation_evidence_and_confirmation_recorded",
                    "nebulagraph": "pending",
                    "outbox_event_id": outbox_event.event_id,
                    "outbox_status": outbox_event.status,
                },
                "decision_boundary": "recommendation_only_not_final_approval",
            }
            worker_error = None
            try:
                outbox_event = self.outbox_worker.process_event(outbox_event.event_id)
            except Exception as exc:
                worker_error = str(exc) or type(exc).__name__
                outbox_event = self.outbox.get(outbox_event.event_id)
            record = self.execution_repository.get(record.execution_id)
            if record.status in {"COMPENSATED", "COMPENSATION_FAILED"}:
                summary = dict(record.result_summary or {})
                raise RecordDecisionRecommendationError(
                    record.error_code
                    or summary.get("original_error_code")
                    or "ACTION_GRAPH_SYNC_FAILED",
                    record.error_message
                    or worker_error
                    or "决策建议图同步重试耗尽",
                    execution_id=record.execution_id,
                    status=record.status,
                )
            result = (
                dict(record.result_summary or {})
                if record.status == "SUCCEEDED"
                else pending_result
            )
            if record.status != "SUCCEEDED":
                result["persistence"]["outbox_status"] = outbox_event.status
                result["warning"] = "决策建议已提交，图同步转入持久化 outbox 重试"
            return {
                "execution_id": record.execution_id,
                "action_name": ACTION_NAME,
                "status": record.status,
                "ontology_version": ontology_version,
                "idempotent_replay": False,
                "result": result,
                "error_code": None,
                "error_message": None,
                "audit_reference": record.execution_id,
                "audit_chain_valid": self.execution_repository.verify_audit_chain(
                    record.execution_id
                ),
                "outbox": {
                    "event_id": outbox_event.event_id,
                    "status": outbox_event.status,
                    "attempt_count": outbox_event.attempt_count,
                    "max_attempts": outbox_event.max_attempts,
                },
            }
        except Exception as exc:
            code = _error_code(exc)
            message = str(exc) or type(exc).__name__
            if outbox_event is not None:
                current = self.execution_repository.get(record.execution_id)
                raise RecordDecisionRecommendationError(
                    code,
                    message,
                    execution_id=current.execution_id,
                    status=current.status,
                ) from exc
            current = self.execution_repository.get(record.execution_id)
            if not ActionStateMachine.is_terminal(current.status):
                current = self.executions.advance(
                    current.execution_id,
                    "FAILED",
                    expected_version=current.version,
                    event_type="action_failed",
                    actor_id=actor_id,
                    error_code=code,
                    error_message=message,
                )
            raise RecordDecisionRecommendationError(
                code,
                message,
                execution_id=current.execution_id,
                status=current.status,
            ) from exc
