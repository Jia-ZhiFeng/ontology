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
            "ontology_agent 运行时依赖尚未就绪；请完成安全配置或将 "
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
                "ready"
                if self.mode is PersonalCreditAgentMode.ONTOLOGY_AGENT
                and self.available
                else "pending_runtime"
                if self.mode is PersonalCreditAgentMode.ONTOLOGY_AGENT
                else "not_selected"
            ),
        }


def personal_credit_agent_runtime_status(
    mode: object,
    *,
    ontology_available: bool = False,
) -> AgentRuntimeStatus:
    selected = parse_personal_credit_agent_mode(mode)
    return AgentRuntimeStatus(
        mode=selected,
        available=(
            selected is PersonalCreditAgentMode.LEGACY
            or bool(ontology_available)
        ),
    )


def require_personal_credit_agent_runtime(
    mode: object,
    *,
    ontology_available: bool = False,
) -> PersonalCreditAgentMode:
    status = personal_credit_agent_runtime_status(
        mode,
        ontology_available=ontology_available,
    )
    if not status.available:
        raise AgentRuntimeUnavailableError(status.mode)
    return status.mode


def build_personal_credit_agent_runtime(
    *,
    mode: object,
    legacy_factory: Callable[[], RuntimeT],
    ontology_factory: Callable[[], RuntimeT] | None = None,
) -> RuntimeT:
    selected = parse_personal_credit_agent_mode(mode)
    if selected is PersonalCreditAgentMode.LEGACY:
        return legacy_factory()
    if ontology_factory is None:
        raise AgentRuntimeUnavailableError(selected)
    return ontology_factory()
