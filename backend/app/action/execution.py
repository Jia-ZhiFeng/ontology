from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Protocol
from uuid import UUID

from .action_design_catalog import (
    ACTION_RUNTIME_DESIGNS,
    ACTION_STATE_TRANSITIONS,
    TERMINAL_ACTION_STATES,
)
from ..ontology.baseline import build_personal_credit_baseline


class ActionExecutionError(RuntimeError):
    """Action 执行控制层受控错误基类。"""


class UnknownActionError(ActionExecutionError):
    pass


class ActionAuthorizationError(ActionExecutionError):
    pass


class ActionRequestError(ActionExecutionError):
    pass


class ActionIdempotencyConflict(ActionExecutionError):
    pass


class ActionTransitionError(ActionExecutionError):
    pass


class ActionConcurrentUpdateError(ActionExecutionError):
    pass


class ActionExecutionNotFound(ActionExecutionError):
    pass


class ActionAuditIntegrityError(ActionExecutionError):
    pass


@dataclass(frozen=True)
class ActionExecutionRecord:
    execution_id: str
    action_name: str
    ontology_version: str
    status: str
    actor_id: str
    actor_role: str
    idempotency_key_hash: str
    request_digest: str
    correlation_id: str | None
    result_summary: Mapping[str, Any] | None
    error_code: str | None
    error_message: str | None
    version: int
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class ActionReservation:
    record: ActionExecutionRecord
    created: bool


class ActionExecutionRepository(Protocol):
    def reserve(
        self,
        *,
        action_name: str,
        ontology_version: str,
        actor_id: str,
        actor_role: str,
        idempotency_key_hash: str,
        request_digest: str,
        correlation_id: str | None = None,
    ) -> ActionReservation: ...

    def get(self, execution_id: str) -> ActionExecutionRecord: ...

    def transition(
        self,
        execution_id: str,
        target_status: str,
        *,
        expected_version: int,
        event_type: str,
        actor_id: str,
        details: Mapping[str, Any] | None = None,
        result_summary: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ActionExecutionRecord: ...

    def record_event(
        self,
        execution_id: str,
        *,
        event_type: str,
        actor_id: str,
        details: Mapping[str, Any] | None = None,
    ) -> None: ...


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"不支持摘要的数据类型：{type(value).__name__}")


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
    except (TypeError, ValueError) as exc:
        raise ActionRequestError("Action 请求包含不可摘要的数据类型") from exc


def sha256_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def action_request_digest(
    action_name: str,
    ontology_version: str,
    request: Mapping[str, Any],
) -> str:
    if not isinstance(request, Mapping):
        raise ActionRequestError("Action request 必须是映射")
    business_request = {
        key: value
        for key, value in request.items()
        if key not in {"request_id", "correlation_id"}
    }
    return sha256_digest(
        {
            "action_name": action_name,
            "ontology_version": ontology_version,
            "request": business_request,
        }
    )


def action_idempotency_key_hash(
    action_name: str,
    request: Mapping[str, Any],
) -> str:
    design = ACTION_RUNTIME_DESIGNS.get(action_name)
    if design is None:
        raise UnknownActionError(f"Action 设计不存在：{action_name}")
    if not isinstance(request, Mapping):
        raise ActionRequestError("Action request 必须是映射")
    values: list[tuple[str, Any]] = []
    for field_name in design.idempotency_fields:
        value = request.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ActionRequestError(f"Action 请求缺少幂等字段：{field_name}")
        values.append((field_name, value))
    return sha256_digest({"action_name": action_name, "fields": values})


class ActionStateMachine:
    @staticmethod
    def is_terminal(status: str) -> bool:
        return status in TERMINAL_ACTION_STATES

    @staticmethod
    def can_transition(current_status: str, target_status: str) -> bool:
        return target_status in ACTION_STATE_TRANSITIONS.get(current_status, ())

    @classmethod
    def require_transition(cls, current_status: str, target_status: str) -> None:
        if current_status not in ACTION_STATE_TRANSITIONS:
            raise ActionTransitionError(f"未知 Action 状态：{current_status}")
        if target_status not in ACTION_STATE_TRANSITIONS:
            raise ActionTransitionError(f"未知 Action 目标状态：{target_status}")
        if not cls.can_transition(current_status, target_status):
            raise ActionTransitionError(
                f"Action 状态不允许从 {current_status} 转为 {target_status}"
            )


class ActionExecutionService:
    """编排身份边界、幂等摘要和状态流转，不执行业务步骤。"""

    def __init__(
        self,
        repository: ActionExecutionRepository,
        *,
        proposal=None,
    ):
        self.repository = repository
        self.proposal = proposal or build_personal_credit_baseline()
        self.actions = {item.name: item for item in self.proposal.action_types}
        self.security = {item.action: item for item in self.proposal.dynamic_security}

    def start(
        self,
        action_name: str,
        request: Mapping[str, Any],
        *,
        ontology_version: str,
        actor_id: str,
        actor_role: str,
        correlation_id: str | None = None,
    ) -> ActionReservation:
        if action_name not in self.actions or action_name not in ACTION_RUNTIME_DESIGNS:
            raise UnknownActionError(f"Action 不存在：{action_name}")
        if not isinstance(ontology_version, str) or not ontology_version.strip():
            raise ActionRequestError("ontology_version 不能为空")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise ActionRequestError("actor_id 不能为空")
        if not isinstance(actor_role, str) or not actor_role.strip():
            raise ActionRequestError("actor_role 不能为空")

        policy = self.security.get(action_name)
        if (
            policy is None
            or actor_role in policy.denied_roles
            or actor_role not in policy.allowed_roles
        ):
            raise ActionAuthorizationError("ACTION_ROLE_DENIED")

        return self.repository.reserve(
            action_name=action_name,
            ontology_version=ontology_version.strip(),
            actor_id=actor_id.strip(),
            actor_role=actor_role.strip(),
            idempotency_key_hash=action_idempotency_key_hash(action_name, request),
            request_digest=action_request_digest(
                action_name,
                ontology_version.strip(),
                request,
            ),
            correlation_id=(correlation_id.strip() if correlation_id else None),
        )

    def advance(
        self,
        execution_id: str,
        target_status: str,
        *,
        expected_version: int,
        event_type: str,
        actor_id: str,
        details: Mapping[str, Any] | None = None,
        result_summary: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ActionExecutionRecord:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ActionRequestError("event_type 不能为空")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise ActionRequestError("actor_id 不能为空")
        return self.repository.transition(
            execution_id,
            target_status,
            expected_version=expected_version,
            event_type=event_type.strip(),
            actor_id=actor_id.strip(),
            details=details,
            result_summary=result_summary,
            error_code=error_code,
            error_message=error_message,
        )

    def record_event(
        self,
        execution_id: str,
        *,
        event_type: str,
        actor_id: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ActionRequestError("event_type 不能为空")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise ActionRequestError("actor_id 不能为空")
        self.repository.record_event(
            execution_id,
            event_type=event_type.strip(),
            actor_id=actor_id.strip(),
            details=details,
        )
