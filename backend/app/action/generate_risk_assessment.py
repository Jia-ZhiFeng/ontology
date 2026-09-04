from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import Decimal
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
from .function_registry import FunctionRegistryError
from .outbox import (
    PostgresActionOutboxRepository,
    RiskAssessmentOutboxWorker,
)
from .risk_assessment_repository import (
    PostgresRiskAssessmentRepository,
    RiskAssessmentAlreadyExists,
    RiskAssessmentApplicationNotFound,
    RiskAssessmentEffect,
    RiskAssessmentPersistenceError,
)
from .risk_rules import PD_PROXY_RULES, PERSONAL_CREDIT_RULESET_VERSION


ACTION_NAME = "generate_risk_assessment"
DECLARED_FUNCTIONS = (
    "calculate_dti",
    "calculate_ltv",
    "evaluate_credit_risk",
    "evaluate_fraud_risk",
    "assess_application_risk",
)
ACCEPTED_SNAPSHOT_POLICIES = frozenset(
    {"repeatable-read", "repeatable_read_read_only"}
)


class GenerateRiskAssessmentError(RuntimeError):
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
class _ValidatedRequest:
    application_id: str
    assessment_version: str
    data_snapshot_policy: str
    request_id: str

    def as_mapping(self) -> dict[str, str]:
        return {
            "application_id": self.application_id,
            "assessment_version": self.assessment_version,
            "data_snapshot_policy": self.data_snapshot_policy,
            "request_id": self.request_id,
        }


def _required_text(value: Any, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionRequestError(f"{field_name} 不能为空")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ActionRequestError(f"{field_name} 超出长度限制")
    return normalized


def _validated_request(request: Mapping[str, Any]) -> _ValidatedRequest:
    if not isinstance(request, Mapping):
        raise ActionRequestError("Action request 必须是映射")
    allowed = {
        "application_id",
        "assessment_version",
        "data_snapshot_policy",
        "request_id",
    }
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise ActionRequestError(f"Action 请求包含未声明字段：{','.join(unknown)}")
    policy = _required_text(
        request.get("data_snapshot_policy"), "data_snapshot_policy", 64
    )
    if policy not in ACCEPTED_SNAPSHOT_POLICIES:
        raise ActionRequestError(
            "data_snapshot_policy 仅支持 repeatable-read 或 "
            "repeatable_read_read_only"
        )
    return _ValidatedRequest(
        application_id=_required_text(
            request.get("application_id"), "application_id", 32
        ),
        assessment_version=_required_text(
            request.get("assessment_version"), "assessment_version", 64
        ),
        data_snapshot_policy=policy,
        request_id=_required_text(request.get("request_id"), "request_id", 128),
    )


def _function_summary(functions: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "implementation_version": functions[name]["implementation_version"],
            "complete": functions[name]["complete"],
            "duration_ms": functions[name]["duration_ms"],
        }
        for name in DECLARED_FUNCTIONS
    ]


def _validate_function_run(run: Mapping[str, Any]) -> Mapping[str, Any]:
    if run.get("status") != "success" or run.get("read_only") is not True:
        raise GenerateRiskAssessmentError(
            "ACTION_FUNCTION_OUTPUT_INVALID", "Function 链未返回只读成功状态"
        )
    functions = run.get("functions")
    if not isinstance(functions, Mapping) or tuple(functions) != DECLARED_FUNCTIONS:
        raise GenerateRiskAssessmentError(
            "ACTION_FUNCTION_OUTPUT_INVALID", "Function 执行集合或顺序与 Action 声明不一致"
        )
    snapshot_time = run.get("postgres_snapshot_time")
    for name in DECLARED_FUNCTIONS:
        envelope = functions[name]
        if (
            envelope.get("status") != "success"
            or envelope.get("complete") is not True
            or not envelope.get("implementation_version")
            or not envelope.get("evidence")
            or envelope.get("data_snapshot_time") != snapshot_time
        ):
            raise GenerateRiskAssessmentError(
                "ACTION_FUNCTION_OUTPUT_INVALID",
                f"Function 输出不完整或快照不一致：{name}",
            )
    assessment = functions["assess_application_risk"].get("result")
    if not isinstance(assessment, Mapping):
        raise GenerateRiskAssessmentError(
            "ACTION_FUNCTION_OUTPUT_INVALID", "综合风险结果不是对象"
        )
    required = {
        "pd_value",
        "risk_level",
        "risk_factors",
        "check_result",
        "model",
        "decision_boundary",
    }
    if required - set(assessment):
        raise GenerateRiskAssessmentError(
            "ACTION_FUNCTION_OUTPUT_INVALID", "综合风险结果缺少必需字段"
        )
    pd_value = assessment.get("pd_value")
    if (
        isinstance(pd_value, bool)
        or not isinstance(pd_value, (int, float, Decimal))
        or not math.isfinite(float(pd_value))
        or not 0 <= float(pd_value) <= 1
    ):
        raise GenerateRiskAssessmentError(
            "ACTION_FUNCTION_OUTPUT_INVALID", "pd_value 不是 0 到 1 的有限数"
        )
    if assessment.get("risk_level") not in {"低", "中", "高", "拒绝"}:
        raise GenerateRiskAssessmentError(
            "ACTION_FUNCTION_OUTPUT_INVALID", "风险等级不在受控枚举中"
        )
    if assessment.get("check_result") not in {"通过", "不通过", "待人工复核"}:
        raise GenerateRiskAssessmentError(
            "ACTION_FUNCTION_OUTPUT_INVALID", "核验结果不在受控枚举中"
        )
    if not isinstance(assessment.get("risk_factors"), list):
        raise GenerateRiskAssessmentError(
            "ACTION_FUNCTION_OUTPUT_INVALID", "risk_factors 必须是列表"
        )
    model = assessment.get("model")
    if not isinstance(model, Mapping) or model.get("calibrated_pd") is not False:
        raise GenerateRiskAssessmentError(
            "ACTION_FUNCTION_OUTPUT_INVALID", "MVP PD 代理值不得标记为已校准"
        )
    if assessment.get("decision_boundary") != "risk_assessment_only_not_final_approval":
        raise GenerateRiskAssessmentError(
            "ACTION_FUNCTION_OUTPUT_INVALID", "Function 越过风险评估边界"
        )
    return assessment


def _error_code(exc: Exception) -> str:
    if isinstance(exc, GenerateRiskAssessmentError):
        return exc.code
    if isinstance(exc, RiskAssessmentAlreadyExists):
        return "ACTION_ASSESSMENT_ALREADY_EXISTS"
    if isinstance(exc, RiskAssessmentApplicationNotFound):
        return "ACTION_ASSESSMENT_PREREQUISITE_FAILED"
    if isinstance(exc, (FunctionDataAdapterError, FunctionRegistryError)):
        return "ACTION_FUNCTION_FAILED"
    if isinstance(exc, RiskAssessmentPersistenceError):
        return "ACTION_WRITE_FAILED"
    if isinstance(exc, ActionRequestError):
        return "ACTION_REQUEST_INVALID"
    return "ACTION_WRITE_FAILED"


class GenerateRiskAssessmentActionService:
    """First end-to-end Action; it never updates a final approval decision."""

    def __init__(
        self,
        *,
        postgres_service,
        nebula_factory: Callable[[], Any],
        proposal,
        execution_repository,
        report_repository: PostgresRiskAssessmentRepository | None = None,
        function_adapter: PersonalCreditFunctionDataAdapter | None = None,
        outbox_repository: PostgresActionOutboxRepository | None = None,
        outbox_worker: RiskAssessmentOutboxWorker | None = None,
        outbox_max_attempts: int = 5,
    ):
        self.postgres = postgres_service
        self.nebula_factory = nebula_factory
        self.proposal = proposal
        self.execution_repository = execution_repository
        self.executions = ActionExecutionService(
            execution_repository, proposal=proposal
        )
        self.reports = report_repository or PostgresRiskAssessmentRepository(
            postgres_service
        )
        self.functions = function_adapter or PersonalCreditFunctionDataAdapter(
            postgres_service, nebula_factory, proposal=proposal
        )
        self.outbox = outbox_repository or PostgresActionOutboxRepository(
            postgres_service
        )
        self.outbox.ensure_schema()
        self.outbox_max_attempts = max(1, int(outbox_max_attempts))
        self.outbox_worker = outbox_worker or RiskAssessmentOutboxWorker(
            postgres_service=postgres_service,
            nebula_factory=nebula_factory,
            proposal_loader=self._proposal_for_version,
            outbox_repository=self.outbox,
            execution_repository=execution_repository,
            report_repository=self.reports,
        )

    def _proposal_for_version(self, version: str):
        if version != self.proposal.version:
            raise ActionRequestError(f"outbox 本体版本不可用：{version}")
        return self.proposal

    @staticmethod
    def _existing_response(record: ActionExecutionRecord) -> dict[str, Any]:
        response = {
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
        if record.status == "SUCCEEDED":
            return response
        if record.status == "FAILED":
            raise GenerateRiskAssessmentError(
                record.error_code or "ACTION_WRITE_FAILED",
                record.error_message or "既有幂等执行已失败",
                execution_id=record.execution_id,
                status=record.status,
            )
        if record.status in {"COMPENSATED", "COMPENSATION_FAILED"}:
            summary = dict(record.result_summary or {})
            code = record.error_code or summary.get("original_error_code")
            raise GenerateRiskAssessmentError(
                code or "ACTION_COMPENSATION_FAILED",
                "既有幂等执行已完成补偿，未重复产生业务副作用",
                execution_id=record.execution_id,
                status=record.status,
            )
        raise GenerateRiskAssessmentError(
            "ACTION_EXECUTION_IN_PROGRESS",
            "相同幂等键已有未完成执行，请查询原执行状态",
            execution_id=record.execution_id,
            status=record.status,
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
        validated = _validated_request(request)
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
        effect: RiskAssessmentEffect | None = None
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
            self.executions.record_event(
                record.execution_id,
                event_type="idempotency_checked",
                actor_id=actor_id,
                details={"created": True, "assessment_version": validated.assessment_version},
            )
            placeholder = self.reports.inspect_target(validated.application_id)
            record = self.executions.advance(
                record.execution_id,
                "RUNNING",
                expected_version=record.version,
                event_type="assessment_prerequisites_validated",
                actor_id=actor_id,
                details={"placeholder_available": placeholder is not None},
            )
            run = self.functions.execute_risk_assessment(
                validated.application_id,
                ontology_version=ontology_version,
                action_execution_id=record.execution_id,
                actor=actor_id,
            )
            self.executions.record_event(
                record.execution_id,
                event_type="assessment_snapshot_loaded",
                actor_id=actor_id,
                details={
                    "snapshot_id": run["postgres_snapshot_id"],
                    "snapshot_time": run["postgres_snapshot_time"],
                    "snapshot_policy": "repeatable_read_read_only",
                },
            )
            assessment = _validate_function_run(run)
            functions_summary = _function_summary(run["functions"])
            self.executions.record_event(
                record.execution_id,
                event_type="assessment_functions_completed",
                actor_id=actor_id,
                details={"functions": functions_summary},
            )
            record = self.executions.advance(
                record.execution_id,
                "VERIFYING",
                expected_version=record.version,
                event_type="assessment_output_validated",
                actor_id=actor_id,
                details={
                    "risk_level": assessment["risk_level"],
                    "pd_value": assessment["pd_value"],
                    "decision_boundary": assessment["decision_boundary"],
                },
            )
            record = self.executions.advance(
                record.execution_id,
                "COMMITTING",
                expected_version=record.version,
                event_type="persistence_started",
                actor_id=actor_id,
            )
            risk_factors = json.dumps(
                {
                    "assessment_version": validated.assessment_version,
                    "rule_version": PERSONAL_CREDIT_RULESET_VERSION,
                    "calculation_policy": assessment.get("calculation_policy"),
                    "factors": assessment["risk_factors"],
                    "function_versions": {
                        item["name"]: item["implementation_version"]
                        for item in functions_summary
                    },
                    "action_execution_id": record.execution_id,
                    "decision_boundary": assessment["decision_boundary"],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            evidence_summary = {
                "function_count": len(functions_summary),
                "functions": functions_summary,
                "snapshot_id": run["postgres_snapshot_id"],
                "rule_version": PERSONAL_CREDIT_RULESET_VERSION,
                "calibrated_pd": False,
            }
            warnings = run["functions"]["assess_application_risk"].get(
                "warnings", []
            )
            stored, effect, outbox_event = self.reports.persist_and_enqueue(
                application_id=validated.application_id,
                assessment_version=validated.assessment_version,
                report={
                    "risk_level": assessment["risk_level"],
                    "assessment_channel": "自动决策",
                    "pd_value": assessment["pd_value"],
                    "threshold": PD_PROXY_RULES["high_threshold"],
                    "risk_factors": risk_factors,
                    "check_item": "MVP规则综合风险评估（非最终审批）",
                    "check_result": assessment["check_result"],
                    "checker": None,
                },
                outbox_repository=self.outbox,
                execution_id=record.execution_id,
                ontology_version=ontology_version,
                outbox_payload={
                    "actor_id": actor_id,
                    "request_id": validated.request_id,
                    "evidence_summary": evidence_summary,
                    "warnings": warnings,
                    "decision_boundary": assessment["decision_boundary"],
                },
                max_attempts=self.outbox_max_attempts,
            )
            self.executions.record_event(
                record.execution_id,
                event_type="graph_sync_enqueued",
                actor_id=actor_id,
                details={
                    "assessment_id": stored["assessment_id"],
                    "operation": effect.operation,
                    "outbox_event_id": outbox_event.event_id,
                },
            )
            pending_result = {
                "application_id": validated.application_id,
                "assessment_version": validated.assessment_version,
                "assessment_id": stored["assessment_id"],
                "risk_level": stored["risk_level"],
                "pd_value": float(stored["pd_value"]),
                "check_result": stored["check_result"],
                "evidence_summary": evidence_summary,
                "persistence": {
                    "postgres": effect.operation,
                    "nebulagraph": "pending",
                    "outbox_event_id": outbox_event.event_id,
                    "outbox_status": outbox_event.status,
                },
                "decision_boundary": "risk_assessment_only_not_final_approval",
                "warnings": warnings,
            }
            worker_error = None
            try:
                outbox_event = self.outbox_worker.process_event(outbox_event.event_id)
            except Exception as exc:
                # The durable event/lease remains the source of truth. A background
                # worker can retry or recover it after this request returns.
                worker_error = str(exc) or type(exc).__name__
                outbox_event = self.outbox.get(outbox_event.event_id)
            record = self.execution_repository.get(record.execution_id)
            if record.status in {"COMPENSATED", "COMPENSATION_FAILED"}:
                summary = dict(record.result_summary or {})
                raise GenerateRiskAssessmentError(
                    record.error_code
                    or summary.get("original_error_code")
                    or "ACTION_GRAPH_SYNC_RETRY_EXHAUSTED",
                    record.error_message or worker_error or "图同步重试耗尽",
                    execution_id=record.execution_id,
                    status=record.status,
                )
            if record.status == "SUCCEEDED":
                result = dict(record.result_summary or {})
            else:
                pending_result["persistence"]["outbox_status"] = outbox_event.status
                if outbox_event.available_at:
                    pending_result["persistence"]["next_attempt_at"] = (
                        outbox_event.available_at.isoformat()
                    )
                if worker_error:
                    pending_result["warnings"] = list(pending_result["warnings"]) + [
                        "NebulaGraph 同步已转入持久化 outbox 重试"
                    ]
                result = pending_result
            audit_valid = self.execution_repository.verify_audit_chain(record.execution_id)
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
                "audit_chain_valid": audit_valid,
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
                raise GenerateRiskAssessmentError(
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
            raise GenerateRiskAssessmentError(
                code,
                message,
                execution_id=current.execution_id,
                status=current.status,
            ) from exc
