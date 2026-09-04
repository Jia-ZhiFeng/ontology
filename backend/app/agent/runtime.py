from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

from ..core.agent_runtime import (
    PersonalCreditAgentMode,
    parse_personal_credit_agent_mode,
)


RuntimeT = TypeVar("RuntimeT")


class AgentRuntimeUnavailableError(RuntimeError):
    code = "ONTOLOGY_AGENT_NOT_READY"

    def __init__(self, mode: PersonalCreditAgentMode):
        self.mode = mode
        super().__init__(
            "ontology_agent 运行时尚未启用；请将 "
            "PERSONAL_CREDIT_AGENT_MODE 切回 legacy"
        )


@dataclass(frozen=True)
class AgentRuntimeStatus:
    mode: PersonalCreditAgentMode
    available: bool

    def public(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "available": self.available,
            "rollback_mode": PersonalCreditAgentMode.LEGACY.value,
            "ontology_agent_status": (
                "pending_runtime" if not self.available else "not_selected"
            ),
        }


def personal_credit_agent_runtime_status(mode: object) -> AgentRuntimeStatus:
    selected = parse_personal_credit_agent_mode(mode)
    return AgentRuntimeStatus(
        mode=selected,
        available=selected is PersonalCreditAgentMode.LEGACY,
    )


def require_personal_credit_agent_runtime(
    mode: object,
) -> PersonalCreditAgentMode:
    status = personal_credit_agent_runtime_status(mode)
    if not status.available:
        raise AgentRuntimeUnavailableError(status.mode)
    return status.mode


def build_personal_credit_agent_runtime(
    *,
    mode: object,
    legacy_factory: Callable[[], RuntimeT],
) -> RuntimeT:
    selected = parse_personal_credit_agent_mode(mode)
    if selected is PersonalCreditAgentMode.LEGACY:
        return legacy_factory()
    # Phase 1 deliberately has no ontology runtime factory. A later phase must
    # add the implementation and its safety gates explicitly instead of making
    # this rollout switch silently reuse the legacy runtime.
    raise AgentRuntimeUnavailableError(selected)
