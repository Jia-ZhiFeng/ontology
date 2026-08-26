from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .execution import (
    ActionExecutionRecord,
    ActionExecutionService,
    ActionRequestError,
    ActionStateMachine,
)
from .loan_application_repository import (
    LoanApplicationAlreadyExists,
    LoanApplicationAxiomViolation,
    LoanApplicationMasterDataConflict,
    LoanApplicationPersistenceError,
    PostgresLoanApplicationRepository,
)
from .outbox import LoanApplicationOutboxWorker, PostgresActionOutboxRepository


ACTION_NAME = "create_loan_application"
ID_PATTERN = re.compile(r"^[0-9]{17}[0-9X]$")
PHONE_PATTERN = re.compile(r"^1[0-9]{10}$")
VIN_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
APPLICATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")
CHANNELS = frozenset({"线上H5", "线下门店", "经销商APP"})
INITIAL_STATUSES = frozenset({"资料待补充", "评估中"})

PERSON_FIELDS = frozenset(
    {
        "id_number",
        "name",
        "phone",
        "gender",
        "address",
        "marital_status",
        "monthly_income",
        "employer",
        "guarantee_rating",
        "guarantee_count",
        "guarantee_total",
    }
)
VEHICLE_FIELDS = frozenset(
    {"vin", "brand", "model", "invoice_price", "guide_price", "pledge_status"}
)
DEALER_FIELDS = frozenset({"credit_code", "dealer_name", "location"})
APPLICATION_FIELDS = frozenset(
    {
        "submit_date",
        "channel",
        "loan_amount",
        "loan_term",
        "interest_rate",
        "application_status",
    }
)


class CreateLoanApplicationError(RuntimeError):
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
class ValidatedLoanApplicationRequest:
    application_id: str
    application: Mapping[str, Any]
    borrower: Mapping[str, Any]
    vehicle: Mapping[str, Any]
    dealer: Mapping[str, Any] | None
    guarantor: Mapping[str, Any] | None
    co_borrower: Mapping[str, Any] | None
    request_id: str

    def as_mapping(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "application": dict(self.application),
            "borrower": dict(self.borrower),
            "vehicle": dict(self.vehicle),
            "dealer": dict(self.dealer) if self.dealer else None,
            "guarantor": dict(self.guarantor) if self.guarantor else None,
            "co_borrower": dict(self.co_borrower) if self.co_borrower else None,
            "request_id": self.request_id,
        }


def _required_text(value: Any, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionRequestError(f"{field_name} 不能为空")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ActionRequestError(f"{field_name} 超出长度限制")
    return normalized


def _optional_text(value: Any, field_name: str, max_length: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name, max_length)


def _decimal(
    value: Any,
    field_name: str,
    *,
    required: bool = True,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal | None:
    if value is None and not required:
        return None
    if isinstance(value, bool) or value is None:
        raise ActionRequestError(f"{field_name} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ActionRequestError(f"{field_name} 必须是数值") from exc
    if not math.isfinite(float(result)):
        raise ActionRequestError(f"{field_name} 必须是有限数")
    if minimum is not None and result < minimum:
        raise ActionRequestError(f"{field_name} 小于允许下限")
    if maximum is not None and result > maximum:
        raise ActionRequestError(f"{field_name} 超出允许上限")
    return result


def _integer(
    value: Any,
    field_name: str,
    *,
    required: bool = True,
    minimum: int = 0,
    maximum: int = 2_147_483_647,
) -> int | None:
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ActionRequestError(f"{field_name} 必须是整数")
    if not minimum <= value <= maximum:
        raise ActionRequestError(f"{field_name} 超出允许范围")
    return value


def _timestamp(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ActionRequestError(f"{field_name} 必须是 ISO-8601 时间") from exc
    else:
        raise ActionRequestError(f"{field_name} 必须是 ISO-8601 时间")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ActionRequestError(f"{field_name} 必须包含时区")
    return result


def _mapping(value: Any, field_name: str, allowed: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ActionRequestError(f"{field_name} 必须是对象")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ActionRequestError(
            f"{field_name} 包含未声明字段：{','.join(unknown)}"
        )
    return value


def _person(value: Any, role: str, application_status: str) -> dict[str, Any]:
    data = _mapping(value, role, PERSON_FIELDS)
    identifier = _required_text(data.get("id_number"), f"{role}.id_number", 18).upper()
    if not ID_PATTERN.fullmatch(identifier):
        raise ActionRequestError(f"{role}.id_number 不是18位身份证格式")
    phone = _optional_text(data.get("phone"), f"{role}.phone", 11)
    if phone and not PHONE_PATTERN.fullmatch(phone):
        raise ActionRequestError(f"{role}.phone 不是11位大陆手机号")
    if role == "borrower" and phone is None:
        raise ActionRequestError("borrower.phone 在申请提交前必填")
    address = _optional_text(data.get("address"), f"{role}.address", 200)
    if role == "borrower" and address is None:
        raise ActionRequestError("borrower.address 在申请提交前必填")
    gender = _optional_text(data.get("gender"), f"{role}.gender", 2)
    if gender and gender not in {"男", "女"}:
        raise ActionRequestError(f"{role}.gender 不在受控枚举中")
    marital_status = _optional_text(
        data.get("marital_status"), f"{role}.marital_status", 10
    )
    if marital_status and marital_status not in {"未婚", "已婚", "离异", "丧偶"}:
        raise ActionRequestError(f"{role}.marital_status 不在受控枚举中")
    monthly_required = role == "borrower" and application_status == "评估中"
    monthly_income = _decimal(
        data.get("monthly_income"),
        f"{role}.monthly_income",
        required=monthly_required,
        minimum=Decimal("0.01") if monthly_required else Decimal("0"),
        maximum=Decimal("9999999999.99"),
    )
    guarantee_rating = _optional_text(
        data.get("guarantee_rating"), f"{role}.guarantee_rating", 10
    )
    if guarantee_rating and guarantee_rating not in {"优", "良", "中", "受限"}:
        raise ActionRequestError(f"{role}.guarantee_rating 不在受控枚举中")
    guarantee_count = _integer(
        data.get("guarantee_count"),
        f"{role}.guarantee_count",
        required=role == "guarantor",
        minimum=0,
    )
    guarantee_total = _decimal(
        data.get("guarantee_total"),
        f"{role}.guarantee_total",
        required=role == "guarantor",
        minimum=Decimal("0"),
        maximum=Decimal("9999999999999.99"),
    )
    if role == "guarantor" and guarantee_rating is None:
        raise ActionRequestError("guarantor.guarantee_rating 在担保场景必填且必须合法")
    return {
        "id_number": identifier,
        "name": _required_text(data.get("name"), f"{role}.name", 50),
        "phone": phone,
        "gender": gender,
        "address": address,
        "marital_status": marital_status,
        "monthly_income": monthly_income,
        "employer": _optional_text(data.get("employer"), f"{role}.employer", 255),
        "guarantee_rating": guarantee_rating,
        "guarantee_count": guarantee_count,
        "guarantee_total": guarantee_total,
    }


def _vehicle(value: Any) -> dict[str, Any]:
    data = _mapping(value, "vehicle", VEHICLE_FIELDS)
    vin = _required_text(data.get("vin"), "vehicle.vin", 17).upper()
    if not VIN_PATTERN.fullmatch(vin):
        raise ActionRequestError("vehicle.vin 不是合法17位VIN")
    pledge_status = _optional_text(
        data.get("pledge_status"), "vehicle.pledge_status", 10
    )
    if pledge_status and pledge_status not in {"未抵押", "抵押中", "已解押"}:
        raise ActionRequestError("vehicle.pledge_status 不在受控枚举中")
    return {
        "vin": vin,
        "brand": _required_text(data.get("brand"), "vehicle.brand", 30),
        "model": _required_text(data.get("model"), "vehicle.model", 50),
        "invoice_price": _decimal(
            data.get("invoice_price"),
            "vehicle.invoice_price",
            minimum=Decimal("0.01"),
            maximum=Decimal("9999999999.99"),
        ),
        "guide_price": _decimal(
            data.get("guide_price"),
            "vehicle.guide_price",
            required=False,
            minimum=Decimal("0.01"),
            maximum=Decimal("9999999999.99"),
        ),
        "pledge_status": pledge_status,
    }


def _dealer(value: Any) -> dict[str, Any]:
    data = _mapping(value, "dealer", DEALER_FIELDS)
    return {
        "credit_code": _required_text(data.get("credit_code"), "dealer.credit_code", 32),
        "dealer_name": _required_text(data.get("dealer_name"), "dealer.dealer_name", 100),
        "location": _required_text(data.get("location"), "dealer.location", 200),
    }


def validate_loan_application_request(
    request: Mapping[str, Any],
) -> ValidatedLoanApplicationRequest:
    if not isinstance(request, Mapping):
        raise ActionRequestError("Action request 必须是映射")
    allowed = {
        "application_id",
        "application",
        "borrower",
        "vehicle",
        "dealer",
        "guarantor",
        "co_borrower",
        "request_id",
    }
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise ActionRequestError(f"Action 请求包含未声明字段：{','.join(unknown)}")
    application_id = _required_text(
        request.get("application_id"), "application_id", 32
    )
    if not APPLICATION_ID_PATTERN.fullmatch(application_id):
        raise ActionRequestError("application_id 只能包含字母、数字、下划线或连字符")
    raw_application = _mapping(
        request.get("application"), "application", APPLICATION_FIELDS
    )
    channel = _required_text(raw_application.get("channel"), "application.channel", 20)
    if channel not in CHANNELS:
        raise ActionRequestError("application.channel 不在受控枚举中")
    status = _required_text(
        raw_application.get("application_status", "资料待补充"),
        "application.application_status",
        20,
    )
    if status not in INITIAL_STATUSES:
        raise ActionRequestError("新申请状态只允许资料待补充或评估中")
    application = {
        "application_id": application_id,
        "submit_date": _timestamp(
            raw_application.get("submit_date"), "application.submit_date"
        ),
        "channel": channel,
        "loan_amount": _decimal(
            raw_application.get("loan_amount"),
            "application.loan_amount",
            minimum=Decimal("0.01"),
            maximum=Decimal("9999999999.99"),
        ),
        "loan_term": _integer(
            raw_application.get("loan_term"),
            "application.loan_term",
            minimum=1,
            maximum=600,
        ),
        "interest_rate": _decimal(
            raw_application.get("interest_rate"),
            "application.interest_rate",
            minimum=Decimal("0.0001"),
            maximum=Decimal("0.9999"),
        ),
        "application_status": status,
    }
    borrower = _person(request.get("borrower"), "borrower", status)
    vehicle = _vehicle(request.get("vehicle"))
    dealer = _dealer(request["dealer"]) if request.get("dealer") is not None else None
    guarantor = (
        _person(request["guarantor"], "guarantor", status)
        if request.get("guarantor") is not None
        else None
    )
    co_borrower = (
        _person(request["co_borrower"], "co_borrower", status)
        if request.get("co_borrower") is not None
        else None
    )
    if channel in {"线下门店", "经销商APP"} and dealer is None:
        raise ActionRequestError("线下门店或经销商APP申请必须提供 dealer")
    roles = [borrower["id_number"]]
    roles.extend(
        person["id_number"] for person in (guarantor, co_borrower) if person
    )
    if len(roles) != len(set(roles)):
        raise ActionRequestError("借款人、担保人和共借人必须互不相同")
    return ValidatedLoanApplicationRequest(
        application_id=application_id,
        application=application,
        borrower=borrower,
        vehicle=vehicle,
        dealer=dealer,
        guarantor=guarantor,
        co_borrower=co_borrower,
        request_id=_required_text(request.get("request_id"), "request_id", 128),
    )


def _error_code(exc: Exception) -> str:
    if isinstance(exc, CreateLoanApplicationError):
        return exc.code
    if isinstance(exc, LoanApplicationAlreadyExists):
        return "ACTION_APPLICATION_ALREADY_EXISTS"
    if isinstance(exc, LoanApplicationMasterDataConflict):
        return "ACTION_MASTER_DATA_CONFLICT"
    if isinstance(exc, LoanApplicationAxiomViolation):
        return "ACTION_AXIOM_VIOLATION"
    if isinstance(exc, ActionRequestError):
        return "ACTION_REQUEST_INVALID"
    if isinstance(exc, LoanApplicationPersistenceError):
        return "ACTION_WRITE_FAILED"
    return "ACTION_WRITE_FAILED"


class CreateLoanApplicationActionService:
    """Controlled origination entry point; it never writes a credit decision."""

    def __init__(
        self,
        *,
        postgres_service,
        nebula_factory,
        proposal,
        execution_repository,
        application_repository: PostgresLoanApplicationRepository | None = None,
        outbox_repository: PostgresActionOutboxRepository | None = None,
        outbox_worker: LoanApplicationOutboxWorker | None = None,
        outbox_max_attempts: int = 5,
    ):
        self.postgres = postgres_service
        self.nebula_factory = nebula_factory
        self.proposal = proposal
        self.execution_repository = execution_repository
        self.executions = ActionExecutionService(execution_repository, proposal=proposal)
        self.applications = application_repository or PostgresLoanApplicationRepository(
            postgres_service
        )
        self.outbox = outbox_repository or PostgresActionOutboxRepository(postgres_service)
        self.outbox.ensure_schema()
        self.outbox_max_attempts = max(1, int(outbox_max_attempts))
        self.outbox_worker = outbox_worker or LoanApplicationOutboxWorker(
            postgres_service=postgres_service,
            nebula_factory=nebula_factory,
            proposal_loader=self._proposal_for_version,
            outbox_repository=self.outbox,
            execution_repository=execution_repository,
            application_repository=self.applications,
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
        if record.status in {"FAILED", "COMPENSATED", "COMPENSATION_FAILED"}:
            summary = dict(record.result_summary or {})
            raise CreateLoanApplicationError(
                record.error_code
                or summary.get("original_error_code")
                or "ACTION_WRITE_FAILED",
                record.error_message or "既有幂等执行未成功，未重复产生业务副作用",
                execution_id=record.execution_id,
                status=record.status,
            )
        raise CreateLoanApplicationError(
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
        validated = validate_loan_application_request(request)
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
            self.executions.record_event(
                record.execution_id,
                event_type="idempotency_checked",
                actor_id=actor_id,
                details={"created": True},
            )
            relations = ["SUBMITS", "TARGETS"]
            if validated.dealer:
                relations.append("SOLD_BY")
            if validated.guarantor:
                relations.append("GUARANTEES")
            if validated.co_borrower:
                relations.append("CO_BORROWS")
            record = self.executions.advance(
                record.execution_id,
                "RUNNING",
                expected_version=record.version,
                event_type="application_axioms_validated",
                actor_id=actor_id,
                details={
                    "validated_axioms": [
                        "AX-PER-001",
                        "AX-PER-002",
                        "AX-PER-003",
                        "AX-VEH-001",
                        "AX-VEH-002",
                        "AX-APP-001",
                        "AX-APP-002",
                        "AX-APP-003",
                        "AX-APP-004",
                    ],
                    "relations": relations,
                },
            )
            record = self.executions.advance(
                record.execution_id,
                "VERIFYING",
                expected_version=record.version,
                event_type="application_payload_verified",
                actor_id=actor_id,
                details={"initial_status": validated.application["application_status"]},
            )
            record = self.executions.advance(
                record.execution_id,
                "COMMITTING",
                expected_version=record.version,
                event_type="persistence_started",
                actor_id=actor_id,
            )
            stored, effect, outbox_event = self.applications.persist_and_enqueue(
                application=validated.application,
                borrower=validated.borrower,
                vehicle=validated.vehicle,
                dealer=validated.dealer,
                guarantor=validated.guarantor,
                co_borrower=validated.co_borrower,
                outbox_repository=self.outbox,
                execution_id=record.execution_id,
                ontology_version=ontology_version,
                actor_id=actor_id,
                request_id=validated.request_id,
                max_attempts=self.outbox_max_attempts,
            )
            self.executions.record_event(
                record.execution_id,
                event_type="graph_sync_enqueued",
                actor_id=actor_id,
                details={
                    "application_id": stored["application_id"],
                    "created_relations": relations,
                    "outbox_event_id": outbox_event.event_id,
                },
            )
            pending_result = {
                "application_id": stored["application_id"],
                "application_status": stored["application_status"],
                "created_relations": relations,
                "entity_operations": outbox_event.payload.get("entity_operations") or {},
                "persistence": {
                    "postgres": "created",
                    "nebulagraph": "pending",
                    "outbox_event_id": outbox_event.event_id,
                    "outbox_status": outbox_event.status,
                },
                "decision_boundary": "origination_only_no_credit_decision",
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
                raise CreateLoanApplicationError(
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
                raise CreateLoanApplicationError(
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
            raise CreateLoanApplicationError(
                code,
                message,
                execution_id=current.execution_id,
                status=current.status,
            ) from exc
