from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .anti_fraud_report_repository import (
    AntiFraudApplicationIncomplete,
    AntiFraudPersistenceError,
    AntiFraudReportAlreadyExists,
    PostgresAntiFraudReportRepository,
)
from .execution import (
    ActionExecutionRecord,
    ActionExecutionService,
    ActionRequestError,
    ActionStateMachine,
)
from .fraud_provider import (
    FraudProviderNotConfigured,
    FraudProviderRequest,
    FraudProviderResponseInvalid,
    FraudProviderTimeout,
    PostgresFraudProviderCoordinator,
    deterministic_fraud_provider_request_id,
)
from .operations import ExternalCircuitOpenError
from .function_data_adapter import (
    FunctionDataAdapterError,
    PersonalCreditFunctionDataAdapter,
)
from .function_registry import FunctionNotBoundError, FunctionRegistryError
from .outbox import AntiFraudReportOutboxWorker, PostgresActionOutboxRepository


ACTION_NAME = "request_fraud_report"
DECLARED_FUNCTIONS = ("detect_association_risk",)
APPLICATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")
RISK_SEVERITY = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}


class RequestFraudReportError(RuntimeError):
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
class ValidatedFraudReportRequest:
    application_id: str
    request_id: str

    def as_mapping(self) -> dict[str, str]:
        return {
            "application_id": self.application_id,
            "request_id": self.request_id,
        }


def _text(value: Any, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionRequestError(f"{field_name} 不能为空")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ActionRequestError(f"{field_name} 超出长度限制")
    return normalized


def validate_fraud_report_request(
    request: Mapping[str, Any],
) -> ValidatedFraudReportRequest:
    if not isinstance(request, Mapping):
        raise ActionRequestError("Action request 必须是映射")
    allowed = {"application_id", "request_id"}
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise ActionRequestError(f"Action 请求包含未声明字段：{','.join(unknown)}")
    application_id = _text(request.get("application_id"), "application_id", 32)
    if not APPLICATION_ID_PATTERN.fullmatch(application_id):
        raise ActionRequestError("application_id 只能包含字母、数字、下划线或连字符")
    return ValidatedFraudReportRequest(
        application_id=application_id,
        request_id=_text(request.get("request_id"), "request_id", 128),
    )


def _validate_association_run(run: Mapping[str, Any]) -> dict[str, Any]:
    if run.get("status") != "success" or run.get("read_only") is not True:
        raise RequestFraudReportError(
            "ACTION_FUNCTION_OUTPUT_INVALID", "关联风险 Function 未返回只读成功状态"
        )
    functions = run.get("functions")
    if not isinstance(functions, Mapping) or tuple(functions) != DECLARED_FUNCTIONS:
        raise RequestFraudReportError(
            "ACTION_FUNCTION_OUTPUT_INVALID", "Function 集合与 Action 声明不一致"
        )
    envelope = functions["detect_association_risk"]
    if (
        envelope.get("status") != "success"
        or envelope.get("complete") is not True
        or not envelope.get("implementation_version")
        or not envelope.get("evidence")
        or envelope.get("data_snapshot_time") != run.get("postgres_snapshot_time")
    ):
        raise RequestFraudReportError(
            "ACTION_FUNCTION_OUTPUT_INVALID",
            "关联风险结果不完整、被截断或快照不一致",
        )
    result = envelope.get("result")
    if not isinstance(result, Mapping):
        raise RequestFraudReportError(
            "ACTION_FUNCTION_OUTPUT_INVALID", "关联风险结果不是对象"
        )
    paths = result.get("association_paths")
    signals = result.get("association_risk_signals")
    coverage = result.get("query_coverage")
    if not isinstance(paths, list) or not isinstance(signals, list):
        raise RequestFraudReportError(
            "ACTION_FUNCTION_OUTPUT_INVALID", "关联路径或风险信号不是列表"
        )
    if not isinstance(coverage, Mapping) or coverage.get("truncated") is not False:
        raise RequestFraudReportError(
            "ACTION_FUNCTION_OUTPUT_INVALID", "关联图查询覆盖不完整"
        )
    levels = []
    for signal in signals:
        if not isinstance(signal, Mapping) or signal.get("risk_level") not in RISK_SEVERITY:
            raise RequestFraudReportError(
                "ACTION_FUNCTION_OUTPUT_INVALID", "关联风险信号等级无效"
            )
        levels.append(str(signal["risk_level"]))
    max_level = max(levels, key=RISK_SEVERITY.get) if levels else "NONE"
    return {
        "path_count": len(paths),
        "signal_count": len(signals),
        "max_risk_level": max_level,
        "hard_stop_count": sum(
            bool(signal.get("hard_stop"))
            for signal in signals
            if isinstance(signal, Mapping)
        ),
        "query_coverage": dict(coverage),
        "rule_version": result.get("rule_version"),
        "function_version": envelope["implementation_version"],
        "postgres_snapshot_id": run.get("postgres_snapshot_id"),
        "postgres_snapshot_time": run.get("postgres_snapshot_time"),
        "graph_snapshot_time": run.get("graph_snapshot_time"),
        "cross_store_atomic": False,
    }


def _error_code(exc: Exception) -> str:
    if isinstance(exc, RequestFraudReportError):
        return exc.code
    if isinstance(exc, AntiFraudApplicationIncomplete):
        return "ACTION_APPLICATION_INCOMPLETE"
    if isinstance(exc, AntiFraudReportAlreadyExists):
        return "ACTION_FRAUD_REPORT_ALREADY_EXISTS"
    if isinstance(exc, FunctionNotBoundError):
        return "ACTION_FUNCTION_NOT_BOUND"
    if isinstance(exc, (FunctionDataAdapterError, FunctionRegistryError)):
        return "ACTION_FUNCTION_FAILED"
    if isinstance(exc, FraudProviderNotConfigured):
        return "ACTION_FRAUD_PROVIDER_NOT_CONFIGURED"
    if isinstance(exc, ExternalCircuitOpenError):
        return "ACTION_EXTERNAL_CIRCUIT_OPEN"
    if isinstance(exc, FraudProviderTimeout):
        return "ACTION_EXTERNAL_TIMEOUT"
    if isinstance(exc, FraudProviderResponseInvalid):
        return "ACTION_EXTERNAL_RESPONSE_INVALID"
    if isinstance(exc, ActionRequestError):
        return "ACTION_REQUEST_INVALID"
    if isinstance(exc, AntiFraudPersistenceError):
        return "ACTION_WRITE_FAILED"
    return "ACTION_WRITE_FAILED"


class RequestFraudReportActionService:
    """Association-aware AntiFraudReport registration; never writes a decision."""

    def __init__(
        self,
        *,
        postgres_service,
        nebula_factory: Callable[[], Any],
        proposal,
        execution_repository,
        provider_coordinator: PostgresFraudProviderCoordinator,
        report_repository: PostgresAntiFraudReportRepository | None = None,
        function_adapter: PersonalCreditFunctionDataAdapter | None = None,
        outbox_repository: PostgresActionOutboxRepository | None = None,
        outbox_worker: AntiFraudReportOutboxWorker | None = None,
        outbox_max_attempts: int = 5,
        local_write_max_attempts: int = 2,
    ):
        self.postgres = postgres_service
        self.nebula_factory = nebula_factory
        self.proposal = proposal
        self.execution_repository = execution_repository
        self.executions = ActionExecutionService(execution_repository, proposal=proposal)
        self.provider = provider_coordinator
        self.reports = report_repository or PostgresAntiFraudReportRepository(
            postgres_service
        )
        self.functions = function_adapter or PersonalCreditFunctionDataAdapter(
            postgres_service, nebula_factory
        )
        self.outbox = outbox_repository or PostgresActionOutboxRepository(postgres_service)
        self.outbox.ensure_schema()
        self.outbox_max_attempts = max(1, int(outbox_max_attempts))
        self.local_write_max_attempts = max(1, int(local_write_max_attempts))
        self.outbox_worker = outbox_worker or AntiFraudReportOutboxWorker(
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
            summary = dict(record.result_summary or {})
            raise RequestFraudReportError(
                record.error_code
                or summary.get("original_error_code")
                or "ACTION_WRITE_FAILED",
                record.error_message or "既有幂等执行未成功，未重复发起反欺诈查询",
                execution_id=record.execution_id,
                status=record.status,
            )
        raise RequestFraudReportError(
            "ACTION_EXECUTION_IN_PROGRESS",
            "当前申请已有未完成反欺诈查询，请查询原执行状态",
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
        validated = validate_fraud_report_request(request)
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
            context = self.reports.inspect_target(validated.application_id)
            self.executions.record_event(
                record.execution_id,
                event_type="idempotency_checked",
                actor_id=actor_id,
                details={"created": True},
            )
            record = self.executions.advance(
                record.execution_id,
                "RUNNING",
                expected_version=record.version,
                event_type="fraud_prerequisites_validated",
                actor_id=actor_id,
                details={
                    "core_relations": ["SUBMITS", "TARGETS"],
                    "application_status": context.application_status,
                },
            )
            association_run = self.functions.execute_association_risk(
                validated.application_id,
                ontology_version=ontology_version,
                action_execution_id=record.execution_id,
                actor=actor_id,
            )
            association_summary = _validate_association_run(association_run)
            self.executions.record_event(
                record.execution_id,
                event_type="association_risk_evaluated",
                actor_id=actor_id,
                details=association_summary,
            )
            provider_request_id = deterministic_fraud_provider_request_id(
                validated.application_id
            )
            provider_request = FraudProviderRequest(
                provider_request_id=provider_request_id,
                application_id=validated.application_id,
                borrower_id_number=context.borrower_id_number,
                borrower_phone=context.borrower_phone,
                borrower_address=context.borrower_address,
                vehicle_vin=context.vehicle_vin,
                application_channel=context.application_channel,
            )
            record = self.executions.advance(
                record.execution_id,
                "WAITING_EXTERNAL",
                expected_version=record.version,
                event_type="fraud_query_dispatched",
                actor_id=actor_id,
                details={"provider_request_id": provider_request_id},
            )
            response, provider_response_cached = self.provider.query(
                provider_request, timeout_seconds=30, max_attempts=2
            )
            record = self.executions.advance(
                record.execution_id,
                "VERIFYING",
                expected_version=record.version,
                event_type="combined_fraud_result_validated",
                actor_id=actor_id,
                details={
                    "provider_request_id": provider_request_id,
                    "provider_name": response.provider_name,
                    "provider_mode": response.provider_mode,
                    "synthetic": response.synthetic,
                    "provider_response_cached": provider_response_cached,
                    "association_complete": True,
                },
            )
            record = self.executions.advance(
                record.execution_id,
                "COMMITTING",
                expected_version=record.version,
                event_type="persistence_started",
                actor_id=actor_id,
            )
            last_write_error = None
            for attempt in range(1, self.local_write_max_attempts + 1):
                try:
                    stored, effect, outbox_event = self.reports.persist_and_enqueue(
                        context=context,
                        response=response,
                        association_risk_summary=association_summary,
                        outbox_repository=self.outbox,
                        execution_id=record.execution_id,
                        ontology_version=ontology_version,
                        actor_id=actor_id,
                        request_id=validated.request_id,
                        provider_response_cached=provider_response_cached or attempt > 1,
                        max_attempts=self.outbox_max_attempts,
                    )
                    break
                except (
                    AntiFraudApplicationIncomplete,
                    AntiFraudReportAlreadyExists,
                ):
                    raise
                except Exception as exc:
                    last_write_error = exc
                    if attempt == self.local_write_max_attempts:
                        raise
            if outbox_event is None:
                raise last_write_error or AntiFraudPersistenceError(
                    "反欺诈报告未产生持久化事件"
                )
            self.executions.record_event(
                record.execution_id,
                event_type="graph_sync_enqueued",
                actor_id=actor_id,
                details={
                    "fraud_report_id": stored["fraud_report_id"],
                    "relation": "HAS_FRAUD_REPORT",
                    "outbox_event_id": outbox_event.event_id,
                },
            )
            pending_result = {
                "application_id": validated.application_id,
                "fraud_report_id": stored["fraud_report_id"],
                "provider_request_id": provider_request_id,
                "provider_name": response.provider_name,
                "provider_mode": response.provider_mode,
                "synthetic": response.synthetic,
                "association_risk_summary": association_summary,
                "created_relations": ["HAS_FRAUD_REPORT"],
                "persistence": {
                    "postgres": effect.operation,
                    "nebulagraph": "pending",
                    "outbox_event_id": outbox_event.event_id,
                    "outbox_status": outbox_event.status,
                },
                "decision_boundary": "fraud_report_registration_only_no_decision",
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
                raise RequestFraudReportError(
                    record.error_code
                    or summary.get("original_error_code")
                    or "ACTION_GRAPH_SYNC_RETRY_EXHAUSTED",
                    record.error_message or worker_error or "图同步重试耗尽",
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
                if outbox_event.available_at:
                    result["persistence"]["next_attempt_at"] = (
                        outbox_event.available_at.isoformat()
                    )
                if worker_error:
                    result["warning"] = "NebulaGraph 同步已转入持久化 outbox 重试"
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
                raise RequestFraudReportError(
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
            raise RequestFraudReportError(
                code,
                message,
                execution_id=current.execution_id,
                status=current.status,
            ) from exc
