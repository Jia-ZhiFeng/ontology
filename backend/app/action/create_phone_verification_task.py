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
from .outbox import (
    PhoneVerificationTaskOutboxWorker,
    PostgresActionOutboxRepository,
)
from .phone_dispatch import PostgresPhoneDispatchCoordinator
from .phone_verification_repository import (
    PhoneVerificationPersistenceError,
    PhoneVerificationPrerequisiteFailed,
    PhoneVerificationRoundAlreadyExists,
    PhoneVerificationTriggerNotMet,
    PostgresPhoneVerificationTaskRepository,
)


ACTION_NAME = "create_phone_verification_task"
DECLARED_FUNCTIONS = ("generate_phone_questions",)
APPLICATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")


class CreatePhoneVerificationTaskError(RuntimeError):
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
class ValidatedPhoneVerificationTaskRequest:
    application_id: str
    verification_round: int
    trigger_reason: str
    request_id: str

    def as_mapping(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "verification_round": self.verification_round,
            "trigger_reason": self.trigger_reason,
            "request_id": self.request_id,
        }


def _text(value: Any, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionRequestError(f"{field_name} 不能为空")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ActionRequestError(f"{field_name} 超出长度限制")
    return normalized


def validate_phone_verification_task_request(
    request: Mapping[str, Any],
) -> ValidatedPhoneVerificationTaskRequest:
    if not isinstance(request, Mapping):
        raise ActionRequestError("Action request 必须是映射")
    allowed = {
        "application_id",
        "verification_round",
        "trigger_reason",
        "request_id",
    }
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise ActionRequestError(f"Action 请求包含未声明字段：{','.join(unknown)}")
    application_id = _text(request.get("application_id"), "application_id", 32)
    if not APPLICATION_ID_PATTERN.fullmatch(application_id):
        raise ActionRequestError("application_id 只能包含字母、数字、下划线或连字符")
    verification_round = request.get("verification_round")
    if (
        isinstance(verification_round, bool)
        or not isinstance(verification_round, int)
        or not 1 <= verification_round <= 99
    ):
        raise ActionRequestError("verification_round 必须是 1 到 99 的整数")
    return ValidatedPhoneVerificationTaskRequest(
        application_id=application_id,
        verification_round=verification_round,
        trigger_reason=_text(request.get("trigger_reason"), "trigger_reason", 200),
        request_id=_text(request.get("request_id"), "request_id", 128),
    )


def _validate_question_run(run: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if run.get("status") != "success" or run.get("read_only") is not True:
        raise CreatePhoneVerificationTaskError(
            "ACTION_QUESTION_SET_INVALID", "电核问题 Function 未返回只读成功状态"
        )
    functions = run.get("functions")
    if not isinstance(functions, Mapping) or tuple(functions) != DECLARED_FUNCTIONS:
        raise CreatePhoneVerificationTaskError(
            "ACTION_QUESTION_SET_INVALID", "Function 集合与 Action 声明不一致"
        )
    envelope = functions["generate_phone_questions"]
    if (
        envelope.get("status") != "success"
        or envelope.get("complete") is not True
        or not envelope.get("implementation_version")
        or not envelope.get("evidence")
        or envelope.get("data_snapshot_time") != run.get("postgres_snapshot_time")
    ):
        raise CreatePhoneVerificationTaskError(
            "ACTION_QUESTION_SET_INVALID", "电核问题集不完整或快照不一致"
        )
    result = envelope.get("result")
    if not isinstance(result, Mapping):
        raise CreatePhoneVerificationTaskError(
            "ACTION_QUESTION_SET_INVALID", "电核问题结果不是对象"
        )
    questions = result.get("phone_questions")
    count = result.get("question_count")
    template_version = result.get("template_version")
    if (
        not isinstance(questions, list)
        or not questions
        or len(questions) > 20
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count != len(questions)
        or not isinstance(template_version, str)
        or not template_version.strip()
    ):
        raise CreatePhoneVerificationTaskError(
            "ACTION_QUESTION_SET_INVALID", "电核问题数量或模板版本无效"
        )
    seen_ids = set()
    categories = []
    for question in questions:
        if not isinstance(question, Mapping):
            raise CreatePhoneVerificationTaskError(
                "ACTION_QUESTION_SET_INVALID", "电核问题项不是对象"
            )
        question_id = question.get("question_id")
        source_factors = question.get("source_factors")
        if (
            not isinstance(question_id, str)
            or not question_id
            or question_id in seen_ids
            or not isinstance(question.get("category"), str)
            or not question.get("category")
            or not isinstance(question.get("question"), str)
            or not question.get("question")
            or question.get("required") is not True
            or not isinstance(source_factors, list)
            or not source_factors
            or not all(isinstance(item, str) and item for item in source_factors)
        ):
            raise CreatePhoneVerificationTaskError(
                "ACTION_QUESTION_SET_INVALID", "电核问题项字段或来源无效"
            )
        seen_ids.add(question_id)
        categories.append(question["category"])
    safe_summary = {
        "question_count": count,
        "categories": sorted(set(categories)),
        "template_version": template_version,
        "function_version": envelope["implementation_version"],
        "postgres_snapshot_id": run.get("postgres_snapshot_id"),
        "postgres_snapshot_time": run.get("postgres_snapshot_time"),
    }
    return dict(result), safe_summary


def _error_code(exc: Exception) -> str:
    if isinstance(exc, CreatePhoneVerificationTaskError):
        return exc.code
    if isinstance(exc, PhoneVerificationTriggerNotMet):
        return "ACTION_PHONE_TRIGGER_NOT_MET"
    if isinstance(exc, PhoneVerificationPrerequisiteFailed):
        return "ACTION_PHONE_PREREQUISITE_FAILED"
    if isinstance(exc, PhoneVerificationRoundAlreadyExists):
        return "ACTION_PHONE_ROUND_ALREADY_EXISTS"
    if isinstance(exc, FunctionNotBoundError):
        return "ACTION_FUNCTION_NOT_BOUND"
    if isinstance(exc, (FunctionDataAdapterError, FunctionRegistryError)):
        return "ACTION_FUNCTION_FAILED"
    if isinstance(exc, ActionRequestError):
        return "ACTION_REQUEST_INVALID"
    if isinstance(exc, PhoneVerificationPersistenceError):
        return "ACTION_WRITE_FAILED"
    return "ACTION_WRITE_FAILED"


class CreatePhoneVerificationTaskActionService:
    """Create and dispatch a phone task; never records a call result or decision."""

    def __init__(
        self,
        *,
        postgres_service,
        nebula_factory: Callable[[], Any],
        proposal,
        execution_repository,
        dispatch_coordinator: PostgresPhoneDispatchCoordinator,
        task_repository: PostgresPhoneVerificationTaskRepository | None = None,
        function_adapter: PersonalCreditFunctionDataAdapter | None = None,
        outbox_repository: PostgresActionOutboxRepository | None = None,
        outbox_worker: PhoneVerificationTaskOutboxWorker | None = None,
        outbox_max_attempts: int = 5,
        callback_actor_id: str = "mvp-phone-result-adapter",
        callback_actor_role: str = "数字人适配器",
    ):
        self.postgres = postgres_service
        self.nebula_factory = nebula_factory
        self.proposal = proposal
        self.execution_repository = execution_repository
        self.executions = ActionExecutionService(execution_repository, proposal=proposal)
        self.tasks = task_repository or PostgresPhoneVerificationTaskRepository(
            postgres_service
        )
        self.tasks.ensure_schema()
        self.functions = function_adapter or PersonalCreditFunctionDataAdapter(
            postgres_service, nebula_factory
        )
        self.outbox = outbox_repository or PostgresActionOutboxRepository(postgres_service)
        self.outbox.ensure_schema()
        self.outbox_max_attempts = max(1, int(outbox_max_attempts))
        self.callback_actor_id = callback_actor_id.strip()
        self.callback_actor_role = callback_actor_role.strip()
        if not self.callback_actor_id or not self.callback_actor_role:
            raise ActionRequestError("电核结果回调分配身份不能为空")
        self.outbox_worker = outbox_worker or PhoneVerificationTaskOutboxWorker(
            postgres_service=postgres_service,
            nebula_factory=nebula_factory,
            proposal_loader=self._proposal_for_version,
            outbox_repository=self.outbox,
            execution_repository=execution_repository,
            task_repository=self.tasks,
            dispatch_coordinator=dispatch_coordinator,
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
            raise CreatePhoneVerificationTaskError(
                record.error_code or "ACTION_WRITE_FAILED",
                record.error_message or "既有幂等执行未成功，未重复创建电核任务",
                execution_id=record.execution_id,
                status=record.status,
            )
        raise CreatePhoneVerificationTaskError(
            "ACTION_EXECUTION_IN_PROGRESS",
            "当前轮次已有未完成电核任务，请查询原执行状态",
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
        validated = validate_phone_verification_task_request(request)
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
            context = self.tasks.inspect_target(validated.application_id)
            self.executions.record_event(
                record.execution_id,
                event_type="idempotency_checked",
                actor_id=actor_id,
                details={"created": True, "verification_round": validated.verification_round},
            )
            record = self.executions.advance(
                record.execution_id,
                "RUNNING",
                expected_version=record.version,
                event_type="phone_trigger_validated",
                actor_id=actor_id,
                details={
                    "risk_assessment_id": context.risk_assessment_id,
                    "fraud_report_id": context.fraud_report_id,
                    "verified_trigger_signals": list(
                        context.verified_trigger_signals
                    ),
                },
            )
            run = self.functions.execute_phone_questions(
                validated.application_id,
                ontology_version=ontology_version,
                action_execution_id=record.execution_id,
                actor=actor_id,
            )
            question_set, question_summary = _validate_question_run(run)
            self.executions.record_event(
                record.execution_id,
                event_type="phone_questions_generated",
                actor_id=actor_id,
                details=question_summary,
            )
            record = self.executions.advance(
                record.execution_id,
                "VERIFYING",
                expected_version=record.version,
                event_type="phone_question_set_validated",
                actor_id=actor_id,
                details={
                    "question_count": question_summary["question_count"],
                    "template_version": question_summary["template_version"],
                },
            )
            record = self.executions.advance(
                record.execution_id,
                "COMMITTING",
                expected_version=record.version,
                event_type="persistence_started",
                actor_id=actor_id,
            )
            stored, task, effect, outbox_event = self.tasks.persist_and_enqueue(
                context=context,
                verification_round=validated.verification_round,
                trigger_reason=validated.trigger_reason,
                question_set=question_set,
                function_version=question_summary["function_version"],
                snapshot_id=str(question_summary["postgres_snapshot_id"]),
                snapshot_time=str(question_summary["postgres_snapshot_time"]),
                outbox_repository=self.outbox,
                execution_id=record.execution_id,
                ontology_version=ontology_version,
                actor_id=actor_id,
                request_id=validated.request_id,
                callback_actor_id=self.callback_actor_id,
                callback_actor_role=self.callback_actor_role,
                max_attempts=self.outbox_max_attempts,
            )
            self.executions.record_event(
                record.execution_id,
                event_type="phone_task_created",
                actor_id=actor_id,
                details={
                    "verification_id": stored["verification_id"],
                    "verification_round": validated.verification_round,
                    "dispatch_status": task["dispatch_status"],
                    "relation": "HAS_PHONE_VERIFICATION",
                    "outbox_event_id": outbox_event.event_id,
                },
            )
            pending_result = {
                "application_id": validated.application_id,
                "verification_id": stored["verification_id"],
                "verification_round": validated.verification_round,
                "question_count": question_summary["question_count"],
                "template_version": question_summary["template_version"],
                "dispatch_id": effect.dispatch_id,
                "dispatch_status": "PENDING",
                "verified_trigger_signals": list(context.verified_trigger_signals),
                "created_relations": ["HAS_PHONE_VERIFICATION"],
                "persistence": {
                    "postgres": effect.operation,
                    "nebulagraph": "pending",
                    "outbox_event_id": outbox_event.event_id,
                    "outbox_status": outbox_event.status,
                },
                "decision_boundary": "phone_task_creation_only_no_call_result_or_decision",
            }
            worker_error = None
            try:
                outbox_event = self.outbox_worker.process_event(outbox_event.event_id)
            except Exception as exc:
                worker_error = str(exc) or type(exc).__name__
                outbox_event = self.outbox.get(outbox_event.event_id)
            record = self.execution_repository.get(record.execution_id)
            if record.status == "FAILED":
                raise CreatePhoneVerificationTaskError(
                    record.error_code or "ACTION_PHONE_DISPATCH_FAILED",
                    record.error_message or worker_error or "电核任务投递进入死信",
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
                if outbox_event.last_error_code:
                    result["warning"] = "电核任务已保留并转入持久化 outbox 重试"
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
                raise CreatePhoneVerificationTaskError(
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
            raise CreatePhoneVerificationTaskError(
                code,
                message,
                execution_id=current.execution_id,
                status=current.status,
            ) from exc
