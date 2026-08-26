from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from .execution import (
    ActionExecutionRecord,
    ActionExecutionService,
    ActionRequestError,
    ActionStateMachine,
)
from .outbox import (
    PhoneVerificationResultOutboxWorker,
    PostgresActionOutboxRepository,
)
from .phone_verification_repository import PostgresPhoneVerificationTaskRepository
from .phone_verification_result_repository import (
    PhoneVerificationResultPersistenceError,
    PhoneVerificationTaskNotAssigned,
    PhoneVerificationTaskNotFound,
    PhoneVerificationTerminalConflict,
    PostgresPhoneVerificationResultRepository,
    phone_result_digest,
)


ACTION_NAME = "record_phone_verification"
CONCLUSIONS = frozenset({"通过", "信息基本一致", "存疑", "未接通"})
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$")


class RecordPhoneVerificationError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, execution_id: str | None = None,
        status: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.execution_id = execution_id
        self.status = status


@dataclass(frozen=True)
class ValidatedPhoneVerificationResultRequest:
    verification_id: str
    callback_id: str
    verifier_identity: str
    call_time: datetime
    duration_seconds: int
    conclusion: str
    doubts: str | None
    recording_file_id: str | None

    def as_mapping(self) -> dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            "callback_id": self.callback_id,
            "verifier_identity": self.verifier_identity,
            "call_time": self.call_time.isoformat(),
            "duration_seconds": self.duration_seconds,
            "conclusion": self.conclusion,
            "doubts": self.doubts,
            "recording_file_id": self.recording_file_id,
        }


def _required_text(value: Any, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionRequestError(f"{field_name} 不能为空")
    result = value.strip()
    if len(result) > max_length:
        raise ActionRequestError(f"{field_name} 超出长度限制")
    return result


def _optional_text(value: Any, field_name: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ActionRequestError(f"{field_name} 必须是字符串或 null")
    result = value.strip()
    if not result:
        return None
    if len(result) > max_length:
        raise ActionRequestError(f"{field_name} 超出长度限制")
    return result


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ActionRequestError("call_time 不能为空")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ActionRequestError("call_time 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ActionRequestError("call_time 必须包含时区")
    return parsed.astimezone(timezone.utc)


def validate_phone_verification_result_request(
    request: Mapping[str, Any], *, now: datetime | None = None,
) -> ValidatedPhoneVerificationResultRequest:
    if not isinstance(request, Mapping):
        raise ActionRequestError("Action request 必须是映射")
    allowed = {
        "verification_id", "callback_id", "verifier_identity", "call_time",
        "duration_seconds", "conclusion", "doubts", "recording_file_id",
    }
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise ActionRequestError(f"Action 请求包含未声明字段：{','.join(unknown)}")
    verification_id = _required_text(request.get("verification_id"), "verification_id", 32)
    callback_id = _required_text(request.get("callback_id"), "callback_id", 128)
    if not IDENTIFIER_PATTERN.fullmatch(verification_id):
        raise ActionRequestError("verification_id 格式无效")
    if not IDENTIFIER_PATTERN.fullmatch(callback_id):
        raise ActionRequestError("callback_id 格式无效")
    call_time = _timestamp(request.get("call_time"))
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if call_time > current + timedelta(minutes=5):
        raise ActionRequestError("call_time 不能晚于当前时间超过 5 分钟")
    duration = request.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, int) or not 0 <= duration <= 86400:
        raise ActionRequestError("duration_seconds 必须是 0 到 86400 的整数")
    conclusion = _required_text(request.get("conclusion"), "conclusion", 100)
    if conclusion not in CONCLUSIONS:
        raise ActionRequestError("conclusion 不在受控枚举中")
    if conclusion != "未接通" and duration == 0:
        raise ActionRequestError("已完成通话的 duration_seconds 必须大于 0")
    doubts = _optional_text(request.get("doubts"), "doubts", 2000)
    if conclusion == "存疑" and not doubts:
        raise ActionRequestError("conclusion 为存疑时必须填写 doubts")
    return ValidatedPhoneVerificationResultRequest(
        verification_id=verification_id,
        callback_id=callback_id,
        verifier_identity=_required_text(
            request.get("verifier_identity"), "verifier_identity", 50
        ),
        call_time=call_time,
        duration_seconds=duration,
        conclusion=conclusion,
        doubts=doubts,
        recording_file_id=_optional_text(
            request.get("recording_file_id"), "recording_file_id", 64
        ),
    )


def _error_code(exc: Exception) -> str:
    if isinstance(exc, RecordPhoneVerificationError):
        return exc.code
    if isinstance(exc, PhoneVerificationTaskNotFound):
        return "ACTION_PHONE_TASK_NOT_FOUND"
    if isinstance(exc, PhoneVerificationTaskNotAssigned):
        return "ACTION_PHONE_TASK_NOT_ASSIGNED"
    if isinstance(exc, PhoneVerificationTerminalConflict):
        return "ACTION_TERMINAL_STATE_CONFLICT"
    if isinstance(exc, ActionRequestError):
        return "ACTION_PHONE_RESULT_INVALID"
    if isinstance(exc, PhoneVerificationResultPersistenceError):
        return "ACTION_WRITE_FAILED"
    return "ACTION_WRITE_FAILED"


class RecordPhoneVerificationActionService:
    """Record one immutable terminal result for a server-assigned phone task."""

    def __init__(
        self,
        *,
        postgres_service,
        nebula_factory: Callable[[], Any],
        proposal,
        execution_repository,
        task_repository: PostgresPhoneVerificationTaskRepository | None = None,
        result_repository: PostgresPhoneVerificationResultRepository | None = None,
        outbox_repository: PostgresActionOutboxRepository | None = None,
        outbox_worker: PhoneVerificationResultOutboxWorker | None = None,
        outbox_max_attempts: int = 5,
    ):
        self.postgres = postgres_service
        self.nebula_factory = nebula_factory
        self.proposal = proposal
        self.execution_repository = execution_repository
        self.executions = ActionExecutionService(execution_repository, proposal=proposal)
        self.tasks = task_repository or PostgresPhoneVerificationTaskRepository(postgres_service)
        self.results = result_repository or PostgresPhoneVerificationResultRepository(
            postgres_service, self.tasks
        )
        self.results.ensure_schema()
        self.outbox = outbox_repository or PostgresActionOutboxRepository(postgres_service)
        self.outbox.ensure_schema()
        self.outbox_max_attempts = max(1, int(outbox_max_attempts))
        self.outbox_worker = outbox_worker or PhoneVerificationResultOutboxWorker(
            postgres_service=postgres_service,
            nebula_factory=nebula_factory,
            proposal_loader=self._proposal_for_version,
            outbox_repository=self.outbox,
            execution_repository=execution_repository,
            result_repository=self.results,
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
            raise RecordPhoneVerificationError(
                record.error_code or "ACTION_WRITE_FAILED",
                record.error_message or "既有幂等执行未成功，未覆盖电核终态",
                execution_id=record.execution_id, status=record.status,
            )
        raise RecordPhoneVerificationError(
            "ACTION_EXECUTION_IN_PROGRESS", "电核结果正在提交，请查询原执行状态",
            execution_id=record.execution_id, status=record.status,
        )

    def execute(
        self, request: Mapping[str, Any], *, ontology_version: str,
        actor_id: str, actor_role: str, correlation_id: str | None = None,
    ) -> dict[str, Any]:
        validated = validate_phone_verification_result_request(request)
        if ontology_version != self.proposal.version:
            raise ActionRequestError("ontology_version 与活动本体版本不一致")
        reservation = self.executions.start(
            ACTION_NAME, validated.as_mapping(), ontology_version=ontology_version,
            actor_id=actor_id, actor_role=actor_role,
            correlation_id=correlation_id or validated.callback_id,
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
                event_type="phone_callback_received", actor_id=actor_id,
                details={"callback_id": validated.callback_id},
            )
            assignment = self.results.inspect_assignment(
                validated.verification_id, actor_id=actor_id, actor_role=actor_role
            )
            if validated.call_time < assignment.task_created_at - timedelta(minutes=5):
                raise ActionRequestError("call_time 早于电核任务创建时间")
            self.executions.record_event(
                record.execution_id, event_type="phone_assignment_validated",
                actor_id=actor_id,
                details={
                    "verification_id": validated.verification_id,
                    "application_id": assignment.application_id,
                    "assigned_actor_id": actor_id,
                    "assigned_actor_role": actor_role,
                },
            )
            record = self.executions.advance(
                record.execution_id, "RUNNING", expected_version=record.version,
                event_type="phone_result_validated", actor_id=actor_id,
                details={
                    "verification_id": validated.verification_id,
                    "conclusion": validated.conclusion,
                    "has_doubts": bool(validated.doubts),
                    "has_recording_reference": bool(validated.recording_file_id),
                },
            )
            record = self.executions.advance(
                record.execution_id, "VERIFYING", expected_version=record.version,
                event_type="terminal_state_checked", actor_id=actor_id,
            )
            record = self.executions.advance(
                record.execution_id, "COMMITTING", expected_version=record.version,
                event_type="persistence_started", actor_id=actor_id,
            )
            normalized = validated.as_mapping()
            stored, outbox_event = self.results.persist_and_enqueue(
                assignment=assignment,
                callback_id=validated.callback_id,
                verifier_identity=validated.verifier_identity,
                call_time=validated.call_time,
                duration_seconds=validated.duration_seconds,
                conclusion=validated.conclusion,
                doubts=validated.doubts,
                recording_file_id=validated.recording_file_id,
                payload_digest=phone_result_digest(normalized),
                actor_id=actor_id,
                actor_role=actor_role,
                execution_id=record.execution_id,
                ontology_version=ontology_version,
                outbox_repository=self.outbox,
                max_attempts=self.outbox_max_attempts,
            )
            self.executions.record_event(
                record.execution_id, event_type="phone_result_committed", actor_id=actor_id,
                details={
                    "verification_id": validated.verification_id,
                    "callback_id": validated.callback_id,
                    "record_status": "COMPLETED",
                    "outbox_event_id": outbox_event.event_id,
                },
            )
            pending_result = {
                "application_id": assignment.application_id,
                "verification_id": validated.verification_id,
                "callback_id": validated.callback_id,
                "record_status": "COMPLETED",
                "conclusion": stored["conclusion"],
                "persistence": {
                    "postgres": "terminal_result_recorded", "nebulagraph": "pending",
                    "outbox_event_id": outbox_event.event_id,
                    "outbox_status": outbox_event.status,
                },
                "decision_boundary": "phone_result_only_no_correction_or_credit_decision",
            }
            worker_error = None
            try:
                outbox_event = self.outbox_worker.process_event(outbox_event.event_id)
            except Exception as exc:
                worker_error = str(exc) or type(exc).__name__
                outbox_event = self.outbox.get(outbox_event.event_id)
            record = self.execution_repository.get(record.execution_id)
            if record.status == "FAILED":
                raise RecordPhoneVerificationError(
                    record.error_code or "ACTION_GRAPH_SYNC_FAILED",
                    record.error_message or worker_error or "电核结果图同步进入死信",
                    execution_id=record.execution_id, status=record.status,
                )
            result = dict(record.result_summary or {}) if record.status == "SUCCEEDED" else pending_result
            if record.status != "SUCCEEDED":
                result["persistence"]["outbox_status"] = outbox_event.status
                result["warning"] = "电核终态已保存，图同步转入持久化 outbox 重试"
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
                raise RecordPhoneVerificationError(
                    code, message, execution_id=current.execution_id, status=current.status
                ) from exc
            current = self.execution_repository.get(record.execution_id)
            if not ActionStateMachine.is_terminal(current.status):
                current = self.executions.advance(
                    current.execution_id, "FAILED", expected_version=current.version,
                    event_type="action_failed", actor_id=actor_id,
                    error_code=code, error_message=message,
                )
            raise RecordPhoneVerificationError(
                code, message, execution_id=current.execution_id, status=current.status
            ) from exc
