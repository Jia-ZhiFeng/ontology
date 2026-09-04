from __future__ import annotations

from enum import Enum


class PersonalCreditAgentMode(str, Enum):
    LEGACY = "legacy"
    ONTOLOGY_AGENT = "ontology_agent"


class AgentModeConfigurationError(ValueError):
    """The configured Agent mode is not one of the controlled runtime modes."""


def parse_personal_credit_agent_mode(value: object) -> PersonalCreditAgentMode:
    if isinstance(value, PersonalCreditAgentMode):
        return value
    normalized = str(value or "").strip().lower()
    try:
        return PersonalCreditAgentMode(normalized)
    except ValueError as error:
        allowed = ", ".join(mode.value for mode in PersonalCreditAgentMode)
        raise AgentModeConfigurationError(
            f"PERSONAL_CREDIT_AGENT_MODE 必须是：{allowed}"
        ) from error
