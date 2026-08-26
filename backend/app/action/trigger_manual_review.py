from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

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
from .manual_review_repository import (
    ManualReviewAlreadyExists,
    ManualReviewApplicationNotFound,
    ManualReviewEvidenceIncomplete,
    ManualReviewPersistenceError,
    ManualReviewRuleNotMet,
    PostgresManualReviewRepository,
)
from .outbox import ManualReviewOutboxWorker, PostgresActionOutboxRepository


ACTION_NAME = "trigger_manual_review"
DECLARED_FUNCTIONS = (
    "aggregate_decision_support",
    "build_evidence_explanation",
)
APPLICATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")
EXECUTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")


class TriggerManualReviewError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, execution_id: str | None = None,
        status: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.execution_id = execution_id
        self.status = status


@dataclass(frozen=True)
class ValidatedManualReviewRequest:
    application_id: str
    review_reason: str
    source_execution_id: str
    request_id: str

    def as_mapping(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "review_reason": self.review_reason,
            "source_execution_id": self.source_execution_id,
            "request_id": self.request_id,
        }


def _text(value: Any, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionRequestError(f"{field_name} 不能为空")
    result = " ".join(value.strip().split()) if field_name == "review_reason" else value.strip()
    if len(result) > max_length:
        raise ActionRequestError(f"{field_name} 超出长度限制")
    return result


def validate_manual_review_request(
    request: Mapping[str, Any],
) -> ValidatedManualReviewRequest:
    if not isinstance(request, Mapping):
        raise ActionRequestError("Action request 必须是映射")
    allowed = {"application_id", "review_reason", "source_execution_id", "request_id"}
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise ActionRequestError(f"Action 请求包含未声明字段：{','.join(unknown)}")
    application_id = _text(request.get("application_id"), "application_id", 32)
    source_execution_id = _text(
        request.get("source_execution_id"), "source_execution_id", 64
    )
    if not APPLICATION_ID_PATTERN.fullmatch(application_id):
        raise ActionRequestError("application_id 格式无效")
    if not EXECUTION_ID_PATTERN.fullmatch(source_execution_id):
        raise ActionRequestError("source_execution_id 格式无效")
    return ValidatedManualReviewRequest(
        application_id=application_id,
        review_reason=_text(request.get("review_reason"), "review_reason", 200),
        source_execution_id=source_execution_id,
        request_id=_text(request.get("request_id"), "request_id", 128),
    )


def _validate_decision_run(
    run: Mapping[str, Any], *, application_id: str, execution_id: str,
    ontology_version: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if run.get("status") != "success" or run.get("read_only") is not True:
        raise TriggerManualReviewError(
            "ACTION_EVIDENCE_INCOMPLETE", "决策支持 Function 未返回只读成功状态"
        )
    if tuple(run.get("execution_order") or ()) != DECLARED_FUNCTIONS:
        raise TriggerManualReviewError(
            "ACTION_EVIDENCE_INCOMPLETE", "Function 执行顺序与 Action 声明不一致"
        )
    functions = run.get("functions")
    if not isinstance(functions, Mapping) or tuple(functions) != DECLARED_FUNCTIONS:
        raise TriggerManualReviewError(
            "ACTION_EVIDENCE_INCOMPLETE", "Function 集合与 Action 声明不一致"
        )
    for name in DECLARED_FUNCTIONS:
        envelope = functions[name]
        if (
            not isinstance(envelope, Mapping)
            or envelope.get("status") != "success"
            or envelope.get("complete") is not True
            or not envelope.get("implementation_version")
            or not envelope.get("evidence")
            or envelope.get("data_snapshot_time") != run.get("postgres_snapshot_time")
        ):
            raise TriggerManualReviewError(
                "ACTION_EVIDENCE_INCOMPLETE", f"{name} 输出不完整或快照不一致"
            )
    decision = functions["aggregate_decision_support"].get("result")
    if not isinstance(decision, Mapping):
        raise TriggerManualReviewError(
            "ACTION_EVIDENCE_INCOMPLETE", "决策支持结果不是对象"
        )
    reasons = decision.get("decision_reasons")
    if (
        decision.get("recommended_decision") not in {"转人工", "拒绝"}
        or decision.get("decision_boundary") != "decision_support_only_not_final_approval"
        or not isinstance(reasons, list)
        or not reasons
    ):
        raise TriggerManualReviewError(
            "ACTION_MANUAL_REVIEW_RULE_NOT_MET",
            "Function 未形成可转人工的受控建议",
        )
    explanation = functions["build_evidence_explanation"].get("result")
    if not isinstance(explanation, Mapping):
        raise TriggerManualReviewError(
            "ACTION_EVIDENCE_INCOMPLETE", "证据说明结果不是对象"
        )
    chain = explanation.get("evidence_chain")
    lineage = explanation.get("lineage")
    if (
        not isinstance(chain, list)
        or len(chain) < 5
        or not isinstance(lineage, Mapping)
        or lineage.get("application_id") != application_id
        or lineage.get("ontology_version") != ontology_version
        or lineage.get("action_execution_id") != execution_id
        or lineage.get("missing_sources")
        or not lineage.get("source_references")
    ):
        raise TriggerManualReviewError(
            "ACTION_EVIDENCE_INCOMPLETE", "证据链、血缘或来源引用不完整"
        )
    summary = {
        "recommended_decision": decision["recommended_decision"],
        "reason_codes": [str(item.get("code")) for item in reasons if isinstance(item, Mapping)],
        "function_versions": {
            name: str(functions[name]["implementation_version"])
            for name in DECLARED_FUNCTIONS
        },
        "postgres_snapshot_id": run.get("postgres_snapshot_id"),
        "postgres_snapshot_time": run.get("postgres_snapshot_time"),
    }
    return dict(decision), dict(explanation), summary


def _error_code(exc: Exception) -> str:
    if isinstance(exc, TriggerManualReviewError):
        return exc.code
    if isinstance(exc, ManualReviewRuleNotMet):
        return "ACTION_MANUAL_REVIEW_RULE_NOT_MET"
    if isinstance(
        exc, (ManualReviewEvidenceIncomplete, ManualReviewApplicationNotFound)
    ):
        return "ACTION_EVIDENCE_INCOMPLETE"
    if isinstance(exc, ManualReviewAlreadyExists):
        return "ACTION_IDEMPOTENCY_CONFLICT"
    if isinstance(exc, FunctionNotBoundError):
        return "ACTION_FUNCTION_NOT_BOUND"
    if isinstance(exc, (FunctionDataAdapterError, FunctionRegistryError)):
        return "ACTION_EVIDENCE_INCOMPLETE"
    if isinstance(exc, ActionRequestError):
        return "ACTION_REQUEST_INVALID"
    if isinstance(exc, ManualReviewPersistenceError):
        return "ACTION_WRITE_FAILED"
    return "ACTION_WRITE_FAILED"


class TriggerManualReviewActionService:
    """Route an eligible application to human review without final approval."""

    def __init__(
        self,
        *,
        postgres_service,
        nebula_factory: Callable[[], Any],
        proposal,
        execution_repository,
        review_repository: PostgresManualReviewRepository | None = None,
        function_adapter: PersonalCreditFunctionDataAdapter | None = None,
        outbox_repository: PostgresActionOutboxRepository | None = None,
        outbox_worker: ManualReviewOutboxWorker | None = None,
        outbox_max_attempts: int = 5,
    ):
        self.postgres = postgres_service
        self.nebula_factory = nebula_factory
        self.proposal = proposal
        self.execution_repository = execution_repository
        self.executions = ActionExecutionService(execution_repository, proposal=proposal)
        self.reviews = review_repository or PostgresManualReviewRepository(postgres_service)
        self.reviews.ensure_schema()
        self.functions = function_adapter or PersonalCreditFunctionDataAdapter(
            postgres_service, nebula_factory
        )
        self.outbox = outbox_repository or PostgresActionOutboxRepository(postgres_service)
        self.outbox.ensure_schema()
        self.outbox_max_attempts = max(1, int(outbox_max_attempts))
        self.outbox_worker = outbox_worker or ManualReviewOutboxWorker(
            postgres_service=postgres_service,
            nebula_factory=nebula_factory,
            proposal_loader=self._proposal_for_version,
            outbox_repository=self.outbox,
            execution_repository=execution_repository,
            review_repository=self.reviews,
        )

    def _proposal_for_version(self, version: str):
        if version != self.proposal.version:
            raise ActionRequestError(f"outbox 本体版本不可用：{version}")
        return self.proposal

    @staticmethod
    def _existing_response(record: ActionExecutionRecord) -> dict[str, Any]:
        if record.status == "SUCCEEDED":
            return {
                "execution_id": record.execution_id, "action_name": record.action_name,
                "status": record.status, "ontology_version": record.ontology_version,
                "idempotent_replay": True, "result": dict(record.result_summary or {}),
                "error_code": record.error_code, "error_message": record.error_message,
                "audit_reference": record.execution_id,
            }
        if record.status in {"FAILED", "COMPENSATED", "COMPENSATION_FAILED"}:
            raise TriggerManualReviewError(
                record.error_code or "ACTION_WRITE_FAILED",
                record.error_message or "既有人工复核执行未成功，未重复变更申请",
                execution_id=record.execution_id, status=record.status,
            )
        raise TriggerManualReviewError(
            "ACTION_EXECUTION_IN_PROGRESS", "人工复核路由正在提交，请查询原执行状态",
            execution_id=record.execution_id, status=record.status,
        )

    def execute(
        self, request: Mapping[str, Any], *, ontology_version: str,
        actor_id: str, actor_role: str, correlation_id: str | None = None,
    ) -> dict[str, Any]:
        validated = validate_manual_review_request(request)
        if ontology_version != self.proposal.version:
            raise ActionRequestError("ontology_version 与活动本体版本不一致")
        reservation = self.executions.start(
            ACTION_NAME, validated.as_mapping(), ontology_version=ontology_version,
            actor_id=actor_id, actor_role=actor_role,
            correlation_id=correlation_id or validated.request_id,
        )
        if not reservation.created:
            return self._existing_response(reservation.record)
        record = reservation.record
        outbox_event = None
        try:
            record = self.executions.advance(
                record.execution_id, "AUTHORIZING", expected_version=record.version,
                event_type="authorization_started", actor_id=actor_id,
            )
            self.executions.record_event(
                record.execution_id, event_type="authorization_checked", actor_id=actor_id,
                details={"actor_role": actor_role, "allowed": True},
            )
            record = self.executions.advance(
                record.execution_id, "VALIDATING", expected_version=record.version,
                event_type="validation_started", actor_id=actor_id,
            )
            try:
                source_audit_valid = self.execution_repository.verify_audit_chain(
                    validated.source_execution_id
                )
            except Exception as exc:
                raise ManualReviewEvidenceIncomplete(
                    "来源 Action 审计链不存在或不可验证"
                ) from exc
            if not source_audit_valid:
                raise ManualReviewEvidenceIncomplete("来源 Action 审计链校验失败")
            context = self.reviews.inspect_target(
                validated.application_id, validated.source_execution_id
            )
            self.executions.record_event(
                record.execution_id, event_type="manual_review_rule_matched",
                actor_id=actor_id,
                details={
                    "application_id": validated.application_id,
                    "matched_rules": list(context.matched_rules),
                    "source_execution_id": validated.source_execution_id,
                    "source_action_name": context.source_action_name,
                },
            )
            record = self.executions.advance(
                record.execution_id, "RUNNING", expected_version=record.version,
                event_type="decision_support_started", actor_id=actor_id,
            )
            run = self.functions.execute_decision_support(
                validated.application_id,
                ontology_version=ontology_version,
                action_execution_id=record.execution_id,
                actor=actor_id,
            )
            decision, explanation, evidence_summary = _validate_decision_run(
                run, application_id=validated.application_id,
                execution_id=record.execution_id, ontology_version=ontology_version,
            )
            self.executions.record_event(
                record.execution_id, event_type="review_evidence_validated",
                actor_id=actor_id, details=evidence_summary,
            )
            record = self.executions.advance(
                record.execution_id, "VERIFYING", expected_version=record.version,
                event_type="manual_review_output_verified", actor_id=actor_id,
                details={
                    "recommended_decision": decision["recommended_decision"],
                    "matched_rules": list(context.matched_rules),
                },
            )
            record = self.executions.advance(
                record.execution_id, "COMMITTING", expected_version=record.version,
                event_type="persistence_started", actor_id=actor_id,
            )
            stored, review, effect, outbox_event = self.reviews.persist_and_enqueue(
                context=context,
                review_reason=validated.review_reason,
                decision_support=decision,
                evidence_lineage=explanation,
                outbox_repository=self.outbox,
                execution_id=record.execution_id,
                ontology_version=ontology_version,
                actor_id=actor_id,
                request_id=validated.request_id,
                max_attempts=self.outbox_max_attempts,
            )
            self.executions.record_event(
                record.execution_id, event_type="manual_review_committed", actor_id=actor_id,
                details={
                    "application_id": validated.application_id,
                    "review_id": review["review_id"],
                    "application_status": stored["application_status"],
                    "routing_result": stored["decision_result"],
                    "outbox_event_id": outbox_event.event_id,
                },
            )
            pending_result = {
                "application_id": validated.application_id,
                "application_status": "待人工复核",
                "routing_result": "转人工",
                "review_id": review["review_id"],
                "review_evidence_reference": review["evidence_reference"],
                "matched_rules": list(context.matched_rules),
                "source_execution_id": validated.source_execution_id,
                "persistence": {
                    "postgres": "manual_review_status_and_reason_recorded",
                    "nebulagraph": "pending", "outbox_event_id": outbox_event.event_id,
                    "outbox_status": outbox_event.status,
                },
                "decision_boundary": "manual_review_routing_only_not_final_approval",
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
                raise TriggerManualReviewError(
                    record.error_code or summary.get("original_error_code")
                    or "ACTION_GRAPH_SYNC_FAILED",
                    record.error_message or worker_error or "人工复核图同步重试耗尽",
                    execution_id=record.execution_id, status=record.status,
                )
            result = dict(record.result_summary or {}) if record.status == "SUCCEEDED" else pending_result
            if record.status != "SUCCEEDED":
                result["persistence"]["outbox_status"] = outbox_event.status
                result["warning"] = "人工复核状态已提交，图同步转入持久化 outbox 重试"
            return {
                "execution_id": record.execution_id, "action_name": ACTION_NAME,
                "status": record.status, "ontology_version": ontology_version,
                "idempotent_replay": False, "result": result,
                "error_code": None, "error_message": None,
                "audit_reference": record.execution_id,
                "audit_chain_valid": self.execution_repository.verify_audit_chain(record.execution_id),
                "outbox": {
                    "event_id": outbox_event.event_id, "status": outbox_event.status,
                    "attempt_count": outbox_event.attempt_count,
                    "max_attempts": outbox_event.max_attempts,
                },
            }
        except Exception as exc:
            code = _error_code(exc)
            message = str(exc) or type(exc).__name__
            if outbox_event is not None:
                current = self.execution_repository.get(record.execution_id)
                raise TriggerManualReviewError(
                    code, message, execution_id=current.execution_id, status=current.status
                ) from exc
            current = self.execution_repository.get(record.execution_id)
            if not ActionStateMachine.is_terminal(current.status):
                current = self.executions.advance(
                    current.execution_id, "FAILED", expected_version=current.version,
                    event_type="action_failed", actor_id=actor_id,
                    error_code=code, error_message=message,
                )
            raise TriggerManualReviewError(
                code, message, execution_id=current.execution_id, status=current.status
            ) from exc
