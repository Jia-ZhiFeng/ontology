from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from enum import Enum
from typing import Annotated, Any, Literal, Mapping, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)


MAX_DECISION_BYTES = 32 * 1024
MAX_DECISION_DEPTH = 12
MAX_DECISION_NODES = 2048
MAX_READ_ACTIONS_PER_DECISION = 4
MAX_LOOKUP_CAPABILITIES = 12

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

CapabilityName: TypeAlias = Annotated[
    StrictStr,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]{0,127}$",
    ),
]
ObservationId: TypeAlias = Annotated[
    StrictStr,
    StringConstraints(
        min_length=1,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$",
    ),
]


class OntologyAgentDecisionKind(str, Enum):
    LOOKUP_ONTOLOGY = "LOOKUP_ONTOLOGY"
    CALL_READ_ACTIONS = "CALL_READ_ACTIONS"
    PROPOSE_WRITE_ACTION = "PROPOSE_WRITE_ACTION"
    ASK_USER = "ASK_USER"
    FINAL = "FINAL"


class OntologyAgentDecisionParseError(ValueError):
    """Safe protocol error that never retains or echoes the model payload."""

    _MESSAGES = {
        "ONTOLOGY_DECISION_INVALID_JSON": "模型决策不是严格 JSON 对象",
        "ONTOLOGY_DECISION_DUPLICATE_KEY": "模型决策包含重复字段",
        "ONTOLOGY_DECISION_NON_FINITE_NUMBER": "模型决策包含非有限数值",
        "ONTOLOGY_DECISION_TOO_LARGE": "模型决策超过大小限制",
        "ONTOLOGY_DECISION_TOO_DEEP": "模型决策超过嵌套限制",
        "ONTOLOGY_DECISION_TOO_COMPLEX": "模型决策超过结构复杂度限制",
        "ONTOLOGY_DECISION_UNSAFE_TEXT": "模型决策包含不安全文本",
        "ONTOLOGY_DECISION_CONTRACT_INVALID": "模型决策不符合单步协议",
    }

    def __init__(self, code: str):
        self.code = code
        self.message = self._MESSAGES.get(
            code, "模型决策未通过安全协议校验"
        )
        super().__init__(self.message)

    def public(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _nonempty_safe_text(value: str, *, max_length: int) -> str:
    if not value.strip() or len(value) > max_length or _CONTROL_RE.search(value):
        raise ValueError("文本为空、过长或包含控制字符")
    return value


class _DecisionBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReadActionCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_name: CapabilityName
    arguments: dict[str, Any]

    @field_validator("arguments")
    @classmethod
    def validate_argument_keys(cls, value: dict[str, Any]) -> dict[str, Any]:
        if any(
            not isinstance(key, str)
            or not key
            or len(key) > 128
            or _CONTROL_RE.search(key)
            for key in value
        ):
            raise ValueError("arguments 字段名无效")
        return value


class LookupOntologyDecision(_DecisionBase):
    decision: Literal[OntologyAgentDecisionKind.LOOKUP_ONTOLOGY]
    capability_names: tuple[CapabilityName, ...] = Field(
        min_length=1,
        max_length=MAX_LOOKUP_CAPABILITIES,
    )

    @model_validator(mode="after")
    def validate_unique_capabilities(self):
        if len(self.capability_names) != len(set(self.capability_names)):
            raise ValueError("ontology lookup capability_names 必须唯一")
        return self


class CallReadActionsDecision(_DecisionBase):
    decision: Literal[OntologyAgentDecisionKind.CALL_READ_ACTIONS]
    calls: tuple[ReadActionCall, ...] = Field(
        min_length=1,
        max_length=MAX_READ_ACTIONS_PER_DECISION,
    )

    @model_validator(mode="after")
    def validate_unique_capabilities(self):
        names = tuple(item.capability_name for item in self.calls)
        if len(names) != len(set(names)):
            raise ValueError("单次 READ 批次不得重复 capability")
        return self


class ProposeWriteActionDecision(_DecisionBase):
    decision: Literal[OntologyAgentDecisionKind.PROPOSE_WRITE_ACTION]
    capability_name: CapabilityName
    arguments: dict[str, Any]

    @field_validator("arguments")
    @classmethod
    def validate_argument_keys(cls, value: dict[str, Any]) -> dict[str, Any]:
        if any(
            not isinstance(key, str)
            or not key
            or len(key) > 128
            or _CONTROL_RE.search(key)
            for key in value
        ):
            raise ValueError("arguments 字段名无效")
        return value


class AskUserDecision(_DecisionBase):
    decision: Literal[OntologyAgentDecisionKind.ASK_USER]
    question: StrictStr = Field(min_length=1, max_length=1000)

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        return _nonempty_safe_text(value, max_length=1000)


class FinalDecision(_DecisionBase):
    decision: Literal[OntologyAgentDecisionKind.FINAL]
    answer: StrictStr = Field(min_length=1, max_length=4000)
    observation_ids: tuple[ObservationId, ...] = Field(max_length=16)
    boundary: Literal["NOT_FINAL_APPROVAL"]

    @field_validator("answer")
    @classmethod
    def validate_answer(cls, value: str) -> str:
        return _nonempty_safe_text(value, max_length=4000)

    @model_validator(mode="after")
    def validate_unique_observations(self):
        if len(self.observation_ids) != len(set(self.observation_ids)):
            raise ValueError("FINAL observation_ids 必须唯一")
        return self


OntologyAgentDecision: TypeAlias = Annotated[
    LookupOntologyDecision
    | CallReadActionsDecision
    | ProposeWriteActionDecision
    | AskUserDecision
    | FinalDecision,
    Field(discriminator="decision"),
]

_DECISION_ADAPTER = TypeAdapter(OntologyAgentDecision)


class _DuplicateKeyError(ValueError):
    pass


class _NonFiniteNumberError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError()
        result[key] = value
    return result


def _reject_non_finite(_value: str) -> None:
    raise _NonFiniteNumberError()


def _validate_json_tree(value: Any, *, depth: int = 0) -> int:
    if depth > MAX_DECISION_DEPTH:
        raise OntologyAgentDecisionParseError("ONTOLOGY_DECISION_TOO_DEEP")
    nodes = 1
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 128
                or _CONTROL_RE.search(key)
            ):
                raise OntologyAgentDecisionParseError(
                    "ONTOLOGY_DECISION_UNSAFE_TEXT"
                )
            nodes += _validate_json_tree(nested, depth=depth + 1)
            if nodes > MAX_DECISION_NODES:
                raise OntologyAgentDecisionParseError(
                    "ONTOLOGY_DECISION_TOO_COMPLEX"
                )
        return nodes
    if isinstance(value, list):
        for nested in value:
            nodes += _validate_json_tree(nested, depth=depth + 1)
            if nodes > MAX_DECISION_NODES:
                raise OntologyAgentDecisionParseError(
                    "ONTOLOGY_DECISION_TOO_COMPLEX"
                )
        return nodes
    if isinstance(value, str):
        if len(value) > 8192 or _CONTROL_RE.search(value):
            raise OntologyAgentDecisionParseError("ONTOLOGY_DECISION_UNSAFE_TEXT")
        return nodes
    if isinstance(value, float) and not math.isfinite(value):
        raise OntologyAgentDecisionParseError(
            "ONTOLOGY_DECISION_NON_FINITE_NUMBER"
        )
    if value is None or isinstance(value, (bool, int, float)):
        return nodes
    raise OntologyAgentDecisionParseError("ONTOLOGY_DECISION_INVALID_JSON")


def _load_strict_json(raw: str) -> Any:
    try:
        encoded = raw.encode("utf-8")
    except (AttributeError, UnicodeEncodeError):
        raise OntologyAgentDecisionParseError(
            "ONTOLOGY_DECISION_INVALID_JSON"
        ) from None
    if len(encoded) > MAX_DECISION_BYTES:
        raise OntologyAgentDecisionParseError("ONTOLOGY_DECISION_TOO_LARGE")
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_finite,
        )
    except _DuplicateKeyError:
        raise OntologyAgentDecisionParseError(
            "ONTOLOGY_DECISION_DUPLICATE_KEY"
        ) from None
    except _NonFiniteNumberError:
        raise OntologyAgentDecisionParseError(
            "ONTOLOGY_DECISION_NON_FINITE_NUMBER"
        ) from None
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise OntologyAgentDecisionParseError(
            "ONTOLOGY_DECISION_INVALID_JSON"
        ) from None


def _normalize_mapping(payload: Mapping[str, Any]) -> Any:
    _validate_json_tree(payload)
    try:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError):
        raise OntologyAgentDecisionParseError(
            "ONTOLOGY_DECISION_INVALID_JSON"
        ) from None
    return _load_strict_json(raw)


def parse_ontology_agent_decision(
    payload: str | Mapping[str, Any],
) -> OntologyAgentDecision:
    """Parse one model decision without retaining or echoing the raw response."""

    if isinstance(payload, str):
        parsed = _load_strict_json(payload)
    elif isinstance(payload, Mapping):
        parsed = _normalize_mapping(payload)
    else:
        raise OntologyAgentDecisionParseError(
            "ONTOLOGY_DECISION_INVALID_JSON"
        )
    if not isinstance(parsed, Mapping):
        raise OntologyAgentDecisionParseError(
            "ONTOLOGY_DECISION_INVALID_JSON"
        )
    _validate_json_tree(parsed)
    try:
        return _DECISION_ADAPTER.validate_python(parsed)
    except ValidationError:
        raise OntologyAgentDecisionParseError(
            "ONTOLOGY_DECISION_CONTRACT_INVALID"
        ) from None


ONTOLOGY_AGENT_DECISION_INSTRUCTION = """\
只返回一个 JSON 对象，不要使用 Markdown 或附加文字。每次只选择一个 decision：
LOOKUP_ONTOLOGY 仅提交 capability_names；
CALL_READ_ACTIONS 提交 calls，每项仅含 capability_name 和 arguments；
PROPOSE_WRITE_ACTION 仅提交 capability_name 和 arguments，它不会执行写入；
ASK_USER 仅提交 question；
FINAL 仅提交 answer、唯一 observation_ids 和固定 boundary=NOT_FINAL_APPROVAL。
不要提交分析、思维过程、理由、执行器、身份、案件、快照、确认或数据库查询字段。\
"""


def ontology_agent_decision_json_schema() -> dict[str, Any]:
    """Return a defensive copy of the strict model-facing decision schema."""

    return deepcopy(_DECISION_ADAPTER.json_schema())


def ontology_agent_decision_instruction() -> str:
    return ONTOLOGY_AGENT_DECISION_INSTRUCTION


__all__ = (
    "AskUserDecision",
    "CallReadActionsDecision",
    "FinalDecision",
    "LookupOntologyDecision",
    "MAX_DECISION_BYTES",
    "MAX_DECISION_DEPTH",
    "MAX_DECISION_NODES",
    "MAX_LOOKUP_CAPABILITIES",
    "MAX_READ_ACTIONS_PER_DECISION",
    "ONTOLOGY_AGENT_DECISION_INSTRUCTION",
    "OntologyAgentDecision",
    "OntologyAgentDecisionKind",
    "OntologyAgentDecisionParseError",
    "ProposeWriteActionDecision",
    "ReadActionCall",
    "ontology_agent_decision_instruction",
    "ontology_agent_decision_json_schema",
    "parse_ontology_agent_decision",
)
