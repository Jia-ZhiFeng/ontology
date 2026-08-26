from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .credit_provider import (
    CreditConsentMissing,
    CreditConsentVerifier,
    CreditProviderNotConfigured,
    CreditProviderRequest,
    CreditProviderResponseInvalid,
    CreditProviderTimeout,
    PostgresCreditProviderCoordinator,
    deterministic_provider_request_id,
)
from .operations import ExternalCircuitOpenError
from .credit_report_repository import (
    CreditReportAlreadyExists,
    CreditReportPersistenceError,
    CreditReportSubjectNotFound,
    PostgresCreditReportRepository,
)
from .execution import (
    ActionExecutionRecord,
    ActionExecutionService,
    ActionRequestError,
    ActionStateMachine,
)
from .outbox import CreditReportOutboxWorker, PostgresActionOutboxRepository


ACTION_NAME = "request_credit_report"
ID_PATTERN = re.compile(r"^[0-9]{17}[0-9X]$")
APPLICATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")


class RequestCreditReportError(RuntimeError):
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
class ValidatedCreditReportRequest:
    application_id: str
    subject_id_number: str
    authorization_reference: str
    query_purpose: str
    request_id: str

    def as_mapping(self) -> dict[str, str]:
        return {
            "application_id": self.application_id,
            "subject_id_number": self.subject_id_number,
            "authorization_reference": self.authorization_reference,
            "query_purpose": self.query_purpose,
            "request_id": self.request_id,
        }


def _text(value: Any, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionRequestError(f"{field_name} 不能为空")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ActionRequestError(f"{field_name} 超出长度限制")
    return normalized


def validate_credit_report_request(
    request: Mapping[str, Any],
) -> ValidatedCreditReportRequest:
    if not isinstance(request, Mapping):
        raise ActionRequestError("Action request 必须是映射")
    allowed = {
        "application_id",
        "subject_id_number",
        "authorization_reference",
        "query_purpose",
        "request_id",
    }
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise ActionRequestError(f"Action 请求包含未声明字段：{','.join(unknown)}")
    application_id = _text(request.get("application_id"), "application_id", 32)
    if not APPLICATION_ID_PATTERN.fullmatch(application_id):
        raise ActionRequestError("application_id 只能包含字母、数字、下划线或连字符")
    subject_id_number = _text(
        request.get("subject_id_number"), "subject_id_number", 18
    ).upper()
    if not ID_PATTERN.fullmatch(subject_id_number):
        raise ActionRequestError("subject_id_number 不是18位身份证格式")
    query_purpose = _text(request.get("query_purpose"), "query_purpose", 32)
    if query_purpose != "贷款审批":
        raise ActionRequestError("当前 Action 仅允许贷款审批用途")
    return ValidatedCreditReportRequest(
        application_id=application_id,
        subject_id_number=subject_id_number,
        authorization_reference=_text(
            request.get("authorization_reference"), "authorization_reference", 160
        ),
        query_purpose=query_purpose,
        request_id=_text(request.get("request_id"), "request_id", 128),
    )


def _error_code(exc: Exception) -> str:
    if isinstance(exc, RequestCreditReportError):
        return exc.code
    if isinstance(exc, CreditProviderNotConfigured):
        return "ACTION_CREDIT_PROVIDER_NOT_CONFIGURED"
    if isinstance(exc, ExternalCircuitOpenError):
        return "ACTION_EXTERNAL_CIRCUIT_OPEN"
    if isinstance(exc, CreditConsentMissing):
        return "ACTION_CREDIT_CONSENT_MISSING"
    if isinstance(exc, CreditReportSubjectNotFound):
        return "ACTION_SUBJECT_NOT_FOUND"
    if isinstance(exc, CreditProviderTimeout):
        return "ACTION_EXTERNAL_TIMEOUT"
    if isinstance(exc, CreditProviderResponseInvalid):
        return "ACTION_EXTERNAL_RESPONSE_INVALID"
    if isinstance(exc, CreditReportAlreadyExists):
        return "ACTION_CREDIT_REPORT_ALREADY_EXISTS"
    if isinstance(exc, ActionRequestError):
        return "ACTION_REQUEST_INVALID"
    if isinstance(exc, CreditReportPersistenceError):
        return "ACTION_WRITE_FAILED"
    return "ACTION_WRITE_FAILED"


class RequestCreditReportActionService:
    """Authorized, provider-idempotent CreditReport registration entry point."""

    def __init__(
        self,
        *,
        postgres_service,
        nebula_factory,
        proposal,
        execution_repository,
        consent_verifier: CreditConsentVerifier,
        provider_coordinator: PostgresCreditProviderCoordinator,
        report_repository: PostgresCreditReportRepository | None = None,
        outbox_repository: PostgresActionOutboxRepository | None = None,
        outbox_worker: CreditReportOutboxWorker | None = None,
        outbox_max_attempts: int = 5,
        local_write_max_attempts: int = 2,
    ):
        self.postgres = postgres_service
        self.nebula_factory = nebula_factory
        self.proposal = proposal
        self.execution_repository = execution_repository
        self.executions = ActionExecutionService(execution_repository, proposal=proposal)
        self.consent = consent_verifier
        self.provider = provider_coordinator
        self.reports = report_repository or PostgresCreditReportRepository(
            postgres_service
        )
        self.outbox = outbox_repository or PostgresActionOutboxRepository(postgres_service)
        self.outbox.ensure_schema()
        self.outbox_max_attempts = max(1, int(outbox_max_attempts))
        self.local_write_max_attempts = max(1, int(local_write_max_attempts))
        self.outbox_worker = outbox_worker or CreditReportOutboxWorker(
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
            raise RequestCreditReportError(
                record.error_code
                or summary.get("original_error_code")
                or "ACTION_WRITE_FAILED",
                record.error_message or "既有幂等执行未成功，未重复发起征信查询",
                execution_id=record.execution_id,
                status=record.status,
            )
        raise RequestCreditReportError(
            "ACTION_EXECUTION_IN_PROGRESS",
            "相同申请与主体已有未完成查询，请查询原执行状态",
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
        validated = validate_credit_report_request(request)
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
            context = self.reports.load_application_subject(
                validated.application_id, validated.subject_id_number
            )
            consent = self.consent.verify(
                application_id=validated.application_id,
                subject_id_number=validated.subject_id_number,
                authorization_reference=validated.authorization_reference,
                query_purpose=validated.query_purpose,
            )
            self.executions.record_event(
                record.execution_id,
                event_type="credit_consent_validated",
                actor_id=actor_id,
                details={
                    "verified": bool(consent.get("verified")),
                    "verifier": consent.get("verifier"),
                    "authorization_reference_hash": consent.get(
                        "authorization_reference_hash"
                    ),
                    "query_purpose": validated.query_purpose,
                    "subject_role": context.subject_role,
                },
            )
            record = self.executions.advance(
                record.execution_id,
                "RUNNING",
                expected_version=record.version,
                event_type="credit_query_prerequisites_validated",
                actor_id=actor_id,
                details={"subject_role": context.subject_role},
            )
            provider_request_id = deterministic_provider_request_id(
                validated.application_id, validated.subject_id_number
            )
            provider_request = CreditProviderRequest(
                provider_request_id=provider_request_id,
                application_id=validated.application_id,
                subject_id_number=validated.subject_id_number,
                query_purpose=validated.query_purpose,
                application_channel=context.application_channel,
            )
            record = self.executions.advance(
                record.execution_id,
                "WAITING_EXTERNAL",
                expected_version=record.version,
                event_type="credit_query_dispatched",
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
                event_type="provider_response_validated",
                actor_id=actor_id,
                details={
                    "provider_request_id": provider_request_id,
                    "provider_name": response.provider_name,
                    "provider_mode": response.provider_mode,
                    "synthetic": response.synthetic,
                    "cached": provider_response_cached,
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
                        outbox_repository=self.outbox,
                        execution_id=record.execution_id,
                        ontology_version=ontology_version,
                        actor_id=actor_id,
                        request_id=validated.request_id,
                        provider_response_cached=provider_response_cached or attempt > 1,
                        max_attempts=self.outbox_max_attempts,
                    )
                    break
                except (CreditReportSubjectNotFound, CreditReportAlreadyExists):
                    raise
                except Exception as exc:
                    last_write_error = exc
                    if attempt == self.local_write_max_attempts:
                        raise
            if outbox_event is None:
                raise last_write_error or CreditReportPersistenceError(
                    "征信报告未产生持久化事件"
                )
            self.executions.record_event(
                record.execution_id,
                event_type="graph_sync_enqueued",
                actor_id=actor_id,
                details={
                    "report_id": stored["report_id"],
                    "relation": "HOLDS",
                    "outbox_event_id": outbox_event.event_id,
                },
            )
            pending_result = {
                "application_id": validated.application_id,
                "report_id": stored["report_id"],
                "provider_request_id": provider_request_id,
                "provider_name": response.provider_name,
                "provider_mode": response.provider_mode,
                "synthetic": response.synthetic,
                "subject_role": context.subject_role,
                "created_relations": ["HOLDS"],
                "persistence": {
                    "postgres": effect.operation,
                    "nebulagraph": "pending",
                    "outbox_event_id": outbox_event.event_id,
                    "outbox_status": outbox_event.status,
                },
                "decision_boundary": "credit_report_registration_only_no_decision",
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
                raise RequestCreditReportError(
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
                raise RequestCreditReportError(
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
            raise RequestCreditReportError(
                code,
                message,
                execution_id=current.execution_id,
                status=current.status,
            ) from exc
