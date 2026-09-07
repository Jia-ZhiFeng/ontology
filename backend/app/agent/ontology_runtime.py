from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Awaitable, Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..action.capabilities import CapabilityAccess, OntologyCapabilityReference
from .capability_registry import (
    AgentCapabilityDecision,
    AgentCapabilityManifest,
    AgentCapabilityRegistry,
    AgentCapabilityRegistryError,
    AgentPrincipalContext,
    AuthorizedCapabilityDecision,
)
from .conversation import (
    AgentConversationStore,
    ConversationLease,
    sanitize_conversation_text,
)
from .ontology_context import (
    OntologyContextError,
    OntologyContextProvider,
    PublishedOntologyContext,
    PublishedOntologyPublication,
    assert_model_safe_payload,
    safe_ontology_text,
)
from .personal_credit import PersonalCreditAgentService


ONTOLOGY_AGENT_MAX_ACTION_CALLS = 4
ONTOLOGY_AGENT_MAX_MODEL_CALLS = 3
ONTOLOGY_AGENT_MAX_LOOKUPS = 1
ONTOLOGY_AGENT_SOFT_TIMEOUT_SECONDS = 30.0
ONTOLOGY_AGENT_HARD_TIMEOUT_SECONDS = 45.0


# Ontology-agent conversations must never resume a legacy-mode lease, even when
# a caller happens to reuse the same opaque conversation id.
DEFAULT_ONTOLOGY_AGENT_CONVERSATION_STORE = AgentConversationStore()


class OntologyAgentRuntimeError(RuntimeError):
    _PUBLIC_MESSAGES = {
        "ONTOLOGY_AGENT_CONTEXT_FAILED": "当前案件上下文不可用于本体 Agent",
        "ONTOLOGY_AGENT_MODEL_UNAVAILABLE": "当前未配置可用的模型服务",
        "ONTOLOGY_AGENT_MODEL_FAILED": "模型响应未通过安全校验",
        "ONTOLOGY_AGENT_BUDGET_EXHAUSTED": "本轮 Agent 已达到安全预算",
        "ONTOLOGY_AGENT_READ_FAILED": "只读能力调用失败",
        "ONTOLOGY_AGENT_OUTPUT_REJECTED": "Agent 输出未通过安全校验",
    }

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        self.message = self._PUBLIC_MESSAGES.get(code, "本体 Agent 请求失败")
        super().__init__(self.message)

    def public(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class OntologyAgentBudgetLimits:
    action_calls: int = ONTOLOGY_AGENT_MAX_ACTION_CALLS
    model_calls: int = ONTOLOGY_AGENT_MAX_MODEL_CALLS
    ontology_lookups: int = ONTOLOGY_AGENT_MAX_LOOKUPS
    soft_timeout_seconds: float = ONTOLOGY_AGENT_SOFT_TIMEOUT_SECONDS
    hard_timeout_seconds: float = ONTOLOGY_AGENT_HARD_TIMEOUT_SECONDS

    def __post_init__(self):
        if not 1 <= int(self.action_calls) <= ONTOLOGY_AGENT_MAX_ACTION_CALLS:
            raise ValueError("action_calls 必须在 1 到 4 之间")
        if not 1 <= int(self.model_calls) <= ONTOLOGY_AGENT_MAX_MODEL_CALLS:
            raise ValueError("model_calls 必须在 1 到 3 之间")
        if not 1 <= int(self.ontology_lookups) <= ONTOLOGY_AGENT_MAX_LOOKUPS:
            raise ValueError("ontology_lookups 必须为 1")
        if self.soft_timeout_seconds <= 0 or self.hard_timeout_seconds <= 0:
            raise ValueError("Agent 时限必须为正数")
        if self.soft_timeout_seconds > self.hard_timeout_seconds:
            raise ValueError("软时限不得超过硬时限")


@dataclass
class _Budget:
    limits: OntologyAgentBudgetLimits
    action_limit: int
    clock: Callable[[], float]
    started_at: float
    action_calls: int = 0
    model_calls: int = 0
    ontology_lookups: int = 0

    def elapsed(self) -> float:
        return max(0.0, float(self.clock()) - self.started_at)

    def hard_remaining(self) -> float:
        return max(0.0, self.limits.hard_timeout_seconds - self.elapsed())

    def hard_expired(self) -> bool:
        return self.elapsed() >= self.limits.hard_timeout_seconds

    def soft_expired(self) -> bool:
        return self.elapsed() >= self.limits.soft_timeout_seconds

    def can_call_model(self) -> bool:
        return (
            self.model_calls < self.limits.model_calls
            and not self.soft_expired()
            and not self.hard_expired()
        )

    def consume_model(self) -> None:
        if self.model_calls >= self.limits.model_calls or self.hard_expired():
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_BUDGET_EXHAUSTED", "模型调用预算耗尽"
            )
        self.model_calls += 1

    def reserve_actions(self, count: int) -> None:
        if (
            count < 1
            or self.action_calls + count > self.action_limit
            or self.soft_expired()
            or self.hard_expired()
        ):
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_BUDGET_EXHAUSTED", "Action 调用预算耗尽"
            )
        self.action_calls += count

    def consume_lookup(self) -> None:
        if (
            self.ontology_lookups >= self.limits.ontology_lookups
            or self.soft_expired()
            or self.hard_expired()
        ):
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_BUDGET_EXHAUSTED", "本体查询预算耗尽"
            )
        self.ontology_lookups += 1

    def public(self) -> dict[str, Any]:
        return {
            "limits": {
                "action_calls": self.action_limit,
                "model_calls": self.limits.model_calls,
                "ontology_lookups": self.limits.ontology_lookups,
                "soft_timeout_seconds": self.limits.soft_timeout_seconds,
                "hard_timeout_seconds": self.limits.hard_timeout_seconds,
            },
            "used": {
                "action_calls": self.action_calls,
                "model_calls": self.model_calls,
                "ontology_lookups": self.ontology_lookups,
            },
            "elapsed_ms": int(round(self.elapsed() * 1000)),
        }


class AgentObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str
    kind: str
    status: str
    capability_name: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None

    def model_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        assert_model_safe_payload(payload)
        return payload


class InMemoryWriteProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str
    action_name: str
    capability_id: str
    source_step_id: str
    status: str = "PENDING_CONFIRMATION"
    requires_confirmation: bool = True
    auto_executable: bool = False
    proposal_only: bool = True
    model_arguments: dict[str, str] = Field(default_factory=dict)
    final_approval: bool = False


def _canonical_sha256(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _decision_api():
    # Kept local to avoid an import cycle while the strict DTO module evolves.
    from .ontology_decisions import (
        AskUserDecision,
        CallReadActionsDecision,
        FinalDecision,
        LookupOntologyDecision,
        ProposeWriteActionDecision,
        parse_ontology_agent_decision,
    )

    return (
        parse_ontology_agent_decision,
        LookupOntologyDecision,
        CallReadActionsDecision,
        ProposeWriteActionDecision,
        AskUserDecision,
        FinalDecision,
    )


def _maybe_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OntologyAgentRuntimeError(
            "ONTOLOGY_AGENT_READ_FAILED", "READ executor 返回值不是 object"
        )
    return value


def _project_read_result(
    capability_name: str, raw: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the only READ result shape allowed into model or public output."""

    if capability_name == "inspect_case_readiness":
        payload = {
            "status": raw.get("status"),
            "available_evidence": list(raw.get("available_evidence") or ())[:32],
            "missing_evidence": list(raw.get("missing_evidence") or ())[:32],
            "available_tools": list(raw.get("available_tools") or ())[:16],
            "blocked_tools": {
                str(name): list(missing)[:32]
                for name, missing in (raw.get("blocked_tools") or {}).items()
                if isinstance(missing, (list, tuple))
            },
        }
    elif capability_name == "preview_risk_assessment":
        payload = {
            "risk_level": raw.get("risk_level"),
            "pd_value": raw.get("pd_value"),
            "check_result": raw.get("check_result"),
            "risk_factors": [
                {
                    "factor": item.get("factor"),
                    "contribution": item.get("contribution"),
                }
                for item in (raw.get("risk_factors") or ())
                if isinstance(item, Mapping)
            ][:10],
            "decision_boundary": raw.get("decision_boundary"),
        }
    elif capability_name == "generate_verification_questions":
        payload = {
            "question_count": raw.get("question_count"),
            "phone_questions": [
                {
                    "category": item.get("category"),
                    "question": sanitize_conversation_text(
                        item.get("question"), max_chars=200
                    ),
                    "required": bool(item.get("required")),
                }
                for item in (raw.get("phone_questions") or ())
                if isinstance(item, Mapping)
            ][:20],
        }
    elif capability_name == "generate_decision_support":
        payload = {
            "recommended_decision": raw.get("recommended_decision"),
            "decision_reasons": [
                {
                    "code": item.get("code"),
                    "description": sanitize_conversation_text(
                        item.get("description"), max_chars=200
                    ),
                }
                for item in (raw.get("decision_reasons") or ())
                if isinstance(item, Mapping)
            ][:10],
            "decision_boundary": raw.get("decision_boundary"),
        }
    else:
        raise OntologyAgentRuntimeError(
            "ONTOLOGY_AGENT_READ_FAILED", "未知 READ observation 投影"
        )
    try:
        assert_model_safe_payload(payload)
    except OntologyContextError as exc:
        raise OntologyAgentRuntimeError(
            "ONTOLOGY_AGENT_READ_FAILED",
            f"READ observation 未通过安全扫描: {exc.code}",
        ) from exc
    return payload


def _read_answer(capability_name: str, payload: Mapping[str, Any]) -> str:
    if capability_name == "inspect_case_readiness":
        missing = "、".join(str(item) for item in payload.get("missing_evidence", []))
        return (
            f"案件就绪度为{payload.get('status') or '待确认'}；"
            f"当前缺少：{missing or '无'}。"
        )
    if capability_name == "preview_risk_assessment":
        pd_value = payload.get("pd_value")
        pd_text = (
            f"{float(pd_value) * 100:.2f}%"
            if isinstance(pd_value, (int, float)) and not isinstance(pd_value, bool)
            else "待确认"
        )
        return (
            f"风险评估预览：风险等级为{payload.get('risk_level') or '待确认'}，"
            f"检查结论为{payload.get('check_result') or '待确认'}，"
            f"违约概率为{pd_text}；结果未写入业务记录。"
        )
    if capability_name == "generate_verification_questions":
        return (
            f"已生成{len(payload.get('phone_questions') or [])}个电核问题；"
            "未创建或派发电核任务。"
        )
    return (
        f"只读决策建议为{payload.get('recommended_decision') or '待确认'}；"
        "该建议不代表最终审批，也未写入业务记录。"
    )


_UNSAFE_FINAL_CLAIMS = (
    "审批通过",
    "终审通过",
    "最终审批通过",
    "已审批通过",
    "审批已经通过",
    "正式批准",
    "最终批准",
    "批准申请",
    "批准该申请",
    "申请获批",
    "已获批",
    "放款批准",
    "批准放款",
    "同意放款",
    "可以放款",
    "终审拒绝",
    "最终审批拒绝",
    "最终拒绝",
    "正式拒绝",
    "已写入业务记录",
    "已创建电核任务",
    "已派发电核任务",
    "已提交审批",
    "已触发放款",
)
_CREDENTIAL_REQUEST_TERMS = (
    "密码",
    "口令",
    "密钥",
    "password",
    "passwd",
    "api key",
    "api_key",
    "token",
    "authorization",
    "cookie",
    "secret",
)
_RISK_LEVEL_ALIASES = {
    "LOW": ("low", "低风险"),
    "MEDIUM": ("medium", "中风险"),
    "HIGH": ("high", "高风险"),
}
_READINESS_ALIASES = {
    "READY": ("ready", "就绪"),
    "PARTIAL": ("partial", "部分就绪"),
    "BLOCKED": ("blocked", "阻断", "未就绪"),
}
_RECOMMENDATION_ALIASES = {
    "APPROVE": ("approve", "建议通过"),
    "PASS": ("pass", "建议通过"),
    "REJECT": ("reject", "建议拒绝"),
    "MANUAL_REVIEW": ("manualreview", "人工复核"),
}


class OntologyAgentRuntime:
    """Bounded ontology-driven runtime with no write execution dependency."""

    def __init__(
        self,
        *,
        context_provider: OntologyContextProvider,
        capability_registry: AgentCapabilityRegistry,
        principal: AgentPrincipalContext | Callable[
            [PublishedOntologyContext], AgentPrincipalContext
        ],
        snapshot_loader: Callable[[str], Any],
        read_executor: Callable[..., Any],
        published_ontology: PublishedOntologyPublication | None = None,
        requested_ontology_version: str | None = None,
        llm_client: Any = None,
        conversation_store: AgentConversationStore | None = None,
        budget_limits: OntologyAgentBudgetLimits | None = None,
        clock: Callable[[], float] | None = None,
    ):
        if not isinstance(context_provider, OntologyContextProvider):
            raise TypeError("context_provider 类型无效")
        if not isinstance(capability_registry, AgentCapabilityRegistry):
            raise TypeError("capability_registry 类型无效")
        if not isinstance(principal, AgentPrincipalContext) and not callable(principal):
            raise TypeError("principal 必须是安全主体或主体工厂")
        if not callable(snapshot_loader) or not callable(read_executor):
            raise TypeError("snapshot_loader 和 read_executor 必须可调用")
        self.context_provider = context_provider
        self.registry = capability_registry
        self._principal = principal
        self.snapshot_loader = snapshot_loader
        self.read_executor = read_executor
        if (
            published_ontology is not None
            and not isinstance(published_ontology, PublishedOntologyPublication)
        ):
            raise TypeError("published_ontology 类型无效")
        self.published_ontology = published_ontology
        self.requested_ontology_version = (
            str(requested_ontology_version).strip()
            if requested_ontology_version is not None
            else None
        )
        self.llm_client = llm_client
        self.conversations = (
            conversation_store or DEFAULT_ONTOLOGY_AGENT_CONVERSATION_STORE
        )
        self.limits = budget_limits or OntologyAgentBudgetLimits()
        self.clock = clock or perf_counter

    async def _await_dependency(
        self,
        value: Any,
        budget: _Budget,
    ) -> Any:
        if not inspect.isawaitable(value):
            if budget.hard_expired():
                raise OntologyAgentRuntimeError(
                    "ONTOLOGY_AGENT_BUDGET_EXHAUSTED", "硬时限已到"
                )
            return value
        remaining = budget.hard_remaining()
        if remaining <= 0:
            if inspect.iscoroutine(value):
                value.close()
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_BUDGET_EXHAUSTED", "硬时限已到"
            )
        try:
            return await asyncio.wait_for(value, timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_BUDGET_EXHAUSTED", "异步调用超过硬时限"
            ) from exc

    async def _invoke_dependency(
        self,
        dependency: Callable[..., Any],
        budget: _Budget,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run synchronous I/O off-loop and bound every awaited dependency."""

        async_callable = inspect.iscoroutinefunction(dependency) or (
            inspect.iscoroutinefunction(getattr(dependency, "__call__", None))
        )
        if async_callable:
            value = dependency(*args, **kwargs)
        else:
            value = await self._await_dependency(
                asyncio.to_thread(dependency, *args, **kwargs),
                budget,
            )
        if inspect.isawaitable(value):
            return await self._await_dependency(value, budget)
        return value

    async def _snapshot(
        self, application_id: str, budget: _Budget
    ) -> Any:
        try:
            return await self._invoke_dependency(
                self.snapshot_loader,
                budget,
                application_id,
            )
        except OntologyAgentRuntimeError:
            raise
        except Exception as exc:
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_CONTEXT_FAILED", "一致性快照加载失败"
            ) from exc

    def _resolve_principal(
        self, context: PublishedOntologyContext
    ) -> AgentPrincipalContext:
        value = self._principal(context) if callable(self._principal) else self._principal
        if not isinstance(value, AgentPrincipalContext):
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_CONTEXT_FAILED", "主体工厂返回类型无效"
            )
        return value

    @staticmethod
    def _principal_conversation_scope(principal: AgentPrincipalContext) -> str:
        """Stable scope that deliberately excludes case and snapshot values."""

        return _canonical_sha256({
            "authentication_source": principal.authentication_source.value,
            "principal_id": principal.principal_id,
            "authenticated": principal.authenticated,
            "iam_roles": sorted(principal.iam_roles),
        })

    @staticmethod
    def _trace(
        trace: list[dict[str, Any]],
        *,
        stage: str,
        status: str,
        source: str,
        started_at: float,
        clock: Callable[[], float],
        capability_name: str | None = None,
        observation_id: str | None = None,
        decision: str | None = None,
    ) -> None:
        item: dict[str, Any] = {
            "stage": stage,
            "status": status,
            "source": source,
            "duration_ms": max(0, int(round((clock() - started_at) * 1000))),
        }
        if capability_name:
            item["capability_name"] = capability_name
        if observation_id:
            item["observation_id"] = observation_id
        if decision:
            item["decision"] = decision
        trace.append(item)

    @staticmethod
    def _next_observation_id(observations: Sequence[AgentObservation]) -> str:
        return f"obs-{len(observations) + 1}"

    @staticmethod
    def _call_signature(
        capability_id: str, arguments: Mapping[str, Any]
    ) -> str:
        return _canonical_sha256({
            "capability_id": capability_id,
            "arguments": dict(arguments),
        })

    @staticmethod
    def _lookup_references(
        context: PublishedOntologyContext,
        manifest: AgentCapabilityManifest,
        capability_names: Sequence[str],
    ) -> tuple[OntologyCapabilityReference, ...]:
        by_name = manifest.by_name()
        if (
            not capability_names
            or len(capability_names) != len(set(capability_names))
            or any(name not in by_name for name in capability_names)
        ):
            raise AgentCapabilityRegistryError(
                "AGENT_CAPABILITY_NOT_AVAILABLE", "本体查询能力名不在 manifest"
            )
        values = {
            (reference.kind, reference.name)
            for name in capability_names
            for reference in by_name[name].ontology_references
        }
        return tuple(
            OntologyCapabilityReference(
                ontology_id=context.ontology_id,
                ontology_version=context.ontology_version,
                declaration_kind=kind,
                declaration_name=name,
            )
            for kind, name in sorted(values)
        )

    @staticmethod
    def _model_observations(
        observations: Sequence[AgentObservation],
    ) -> list[dict[str, Any]]:
        return [item.model_payload() for item in observations]

    async def _decide(
        self,
        *,
        llm_client: Any,
        instruction: str,
        history: Sequence[Mapping[str, Any]],
        manifest: AgentCapabilityManifest,
        observations: Sequence[AgentObservation],
        budget: _Budget,
    ) -> Any:
        if llm_client is None:
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_MODEL_UNAVAILABLE", "llm_client is None"
            )
        budget.consume_model()
        system = (
            "你是个人汽车信审本体 Agent 的单步决策器。每次只能返回一个严格 JSON "
            "decision：LOOKUP_ONTOLOGY、CALL_READ_ACTIONS、"
            "PROPOSE_WRITE_ACTION、ASK_USER 或 FINAL。不得输出思维过程、理由、SQL、"
            "Cypher、数据库字段、案件标识或未声明参数。只能选择 capability manifest "
            "中的能力。READ 的系统参数由后端注入；WRITE 永远只能产生待确认 Proposal，"
            "不得声称已执行写入或最终审批。已有 Observation 时必须据此 Replan，"
            "不得补造事实。"
        )
        from .ontology_decisions import ontology_agent_decision_instruction

        system += "\n" + ontology_agent_decision_instruction()
        user_payload = {
            "query": sanitize_conversation_text(instruction, max_chars=500),
            "history": [dict(item) for item in history][-8:],
            "capability_manifest": manifest.model_payload(),
            "observations": self._model_observations(observations),
            "remaining_budget": {
                "action_calls": max(0, budget.action_limit - budget.action_calls),
                "model_calls": max(
                    0, budget.limits.model_calls - budget.model_calls
                ),
                "ontology_lookups": max(
                    0,
                    budget.limits.ontology_lookups - budget.ontology_lookups,
                ),
            },
        }
        try:
            raw = await self._await_dependency(
                llm_client.chat_json(
                    system,
                    json.dumps(user_payload, ensure_ascii=False),
                    temperature=0,
                    max_tokens=1200,
                ),
                budget,
            )
            parser = _decision_api()[0]
            return parser(raw)
        except asyncio.CancelledError:
            raise
        except OntologyAgentRuntimeError:
            raise
        except Exception as exc:
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_MODEL_FAILED", "模型请求或严格决策解析失败"
            ) from exc

    async def _execute_read(
        self,
        *,
        authorization: AuthorizedCapabilityDecision,
        application_id: str,
        snapshot: Any,
        budget: _Budget,
    ) -> Mapping[str, Any]:
        try:
            value = await self._invoke_dependency(
                self.read_executor,
                budget,
                authorization=authorization,
                application_id=application_id,
                snapshot=snapshot,
            )
            return _maybe_mapping(value)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_READ_FAILED", "READ executor 调用失败"
            ) from exc

    @staticmethod
    def _answer_contradicts_observation(
        answer: str,
        observation: AgentObservation,
    ) -> bool:
        compact="".join(str(answer or "").lower().split())
        payload=observation.payload
        if observation.capability_name=="preview_risk_assessment":
            expected=str(payload.get("risk_level") or "").upper()
            stated={
                level
                for level,aliases in _RISK_LEVEL_ALIASES.items()
                if any(alias in compact for alias in aliases)
            }
            # Merely repeating the observed value before overturning it must
            # not make a mixed LOW/HIGH answer pass the grounding gate.
            return bool(stated - {expected})
        if observation.capability_name=="inspect_case_readiness":
            expected=str(payload.get("status") or "").upper()
            stated={
                value
                for value,aliases in _READINESS_ALIASES.items()
                if any(alias in compact for alias in aliases)
            }
            return bool(stated - {expected})
        if observation.capability_name=="generate_decision_support":
            expected=str(payload.get("recommended_decision") or "").upper()
            stated={
                value
                for value,aliases in _RECOMMENDATION_ALIASES.items()
                if any(alias in compact for alias in aliases)
            }
            expected_group=(
                {"APPROVE","PASS"} if expected in {"APPROVE","PASS"}
                else {expected}
            )
            return bool(stated - expected_group)
        if observation.kind=="WRITE_PROPOSAL":
            return any(
                marker in compact
                for marker in (
                    "已经执行",
                    "已执行",
                    "执行完成",
                    "已经生效",
                    "已生效",
                    "alreadyexecuted",
                    "executed successfully",
                )
            )
        return False

    @staticmethod
    def _deterministic_observation_answer(
        observations: Sequence[AgentObservation],
    ) -> str:
        """Render business facts only from backend-produced observations.

        The model still decides when to finish and must cite the observations,
        but its prose is never used as the authoritative credit conclusion.
        This removes the class of attacks that first repeat a true value and
        then overturn it later in the same sentence.
        """

        lines: list[str] = []
        for observation in observations:
            if observation.kind=="READ_ACTION" and observation.status=="COMPLETED":
                if observation.capability_name:
                    lines.append(
                        _read_answer(
                            observation.capability_name,
                            observation.payload,
                        )
                    )
            elif (
                observation.kind=="WRITE_PROPOSAL"
                and observation.status=="PROPOSED"
                and observation.capability_name
            ):
                lines.append(
                    "已生成待人工确认的写 Action Proposal："
                    f"{observation.capability_name}；"
                    "尚未执行，也未产生业务副作用。"
                )
        answer="\n".join(lines)
        if any(
            item.kind=="READ_ACTION" and item.status=="COMPLETED"
            for item in observations
        ):
            answer += (
                " 以上仅为只读信审支持，不代表最终审批，也未写入业务记录。"
            )
        return sanitize_conversation_text(answer,max_chars=2000)

    @staticmethod
    def _safe_final_answer(
        answer: Any,
        *,
        observations: Sequence[AgentObservation],
        cited_observation_ids: Sequence[str],
    ) -> str:
        text = safe_ontology_text(
            answer,
            field="final_answer",
            max_length=2000,
        )
        compact = "".join(text.lower().split())
        if (
            any(claim in compact for claim in _UNSAFE_FINAL_CLAIMS)
            or PersonalCreditAgentService._has_unsafe_business_claim(text)
            or any(
                marker in compact
                for marker in (
                    "approved",
                    "loanapproved",
                    "finalapproval",
                    "finaldecision:approve",
                )
            )
        ):
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_OUTPUT_REJECTED", "FINAL 越过写入或审批边界"
            )
        has_business_observation = any(
            item.kind in {"READ_ACTION", "WRITE_PROPOSAL"}
            and item.status in {"COMPLETED", "PROPOSED"}
            for item in observations
        )
        if not has_business_observation:
            # A free-form model answer cannot establish any case outcome.  The
            # model must first obtain a backend READ/Proposal observation, or
            # use ASK_USER when it needs clarification.  Rejecting every such
            # FINAL avoids an inevitably incomplete synonym blacklist for
            # approval/rejection claims (for example "approval granted").
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_OUTPUT_REJECTED",
                "FINAL 缺少成功 READ 或 Proposal Observation",
            )
        known_ids = {item.observation_id for item in observations}
        cited = tuple(cited_observation_ids)
        if (
            len(cited) != len(set(cited))
            or not set(cited).issubset(known_ids)
            or (known_ids and not cited)
        ):
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_OUTPUT_REJECTED", "FINAL Observation 引用无效"
            )
        business_observations=[
            item for item in observations
            if item.observation_id in set(cited)
            and item.kind in {"READ_ACTION","WRITE_PROPOSAL"}
            and item.status in {"COMPLETED","PROPOSED"}
        ]
        if any(
            OntologyAgentRuntime._answer_contradicts_observation(text,item)
            for item in business_observations
        ):
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_OUTPUT_REJECTED", "FINAL 与 Observation 结论冲突"
            )
        all_business_observation_ids={
            item.observation_id for item in observations
            if item.kind in {"READ_ACTION","WRITE_PROPOSAL"}
            and item.status in {"COMPLETED","PROPOSED"}
        }
        if has_business_observation and not all_business_observation_ids.issubset(
            set(cited)
        ):
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_OUTPUT_REJECTED", "FINAL 未引用全部业务 Observation"
            )
        if has_business_observation:
            # Credit facts and proposal state are always rendered by the
            # backend. Model text is only a termination signal and cannot
            # replace, selectively omit, or reverse an Observation.
            text=OntologyAgentRuntime._deterministic_observation_answer(
                observations
            )
            if not text:
                raise OntologyAgentRuntimeError(
                    "ONTOLOGY_AGENT_OUTPUT_REJECTED", "FINAL 无可呈现业务 Observation"
                )
        return text

    @staticmethod
    def _deterministic_answer(
        executions: Sequence[Mapping[str, Any]],
        proposals: Sequence[InMemoryWriteProposal],
        *,
        question: str | None = None,
        no_result_reason: str | None = None,
    ) -> str:
        lines = [
            str(item.get("answer") or "").strip()
            for item in executions
            if item.get("status") == "EXECUTED" and item.get("answer")
        ]
        if proposals:
            names = "、".join(item.action_name for item in proposals)
            lines.append(
                f"已生成待人工确认的写 Action Proposal：{names}；"
                "尚未执行，也未产生业务副作用。"
            )
        if not lines:
            lines.append(
                no_result_reason
                or "本轮没有形成可用的信审 Observation，请补充目标后重试。"
            )
        if question:
            lines.append(question)
        answer = "\n".join(lines)
        return sanitize_conversation_text(answer, max_chars=2000)

    @staticmethod
    def _execution_summary(executions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        executed = sum(item.get("status") == "EXECUTED" for item in executions)
        failed = sum(item.get("status") == "FAILED" for item in executions)
        blocked = sum(
            item.get("status") in {"BLOCKED", "REJECTED"}
            for item in executions
        )
        proposed = sum(item.get("status") == "PROPOSED" for item in executions)
        planned = len(executions)
        if failed and executed:
            status = "PARTIAL"
        elif failed:
            status = "FAILED"
        elif blocked and executed:
            status = "PARTIAL"
        elif proposed and not executed:
            status = "PROPOSED"
        elif executed:
            status = "COMPLETED"
        elif planned:
            status = "PLANNED"
        else:
            status = "NEEDS_CLARIFICATION"
        return {
            "planned": planned,
            "executed": executed,
            "blocked": blocked,
            "failed": failed,
            "status": status,
        }

    @staticmethod
    def _plan_payload(
        *,
        application_id: str,
        ontology_version: str,
        instruction: str,
        manifest: AgentCapabilityManifest,
        executions: Sequence[Mapping[str, Any]],
        proposals: Sequence[InMemoryWriteProposal],
        max_steps: int,
        model_calls: int,
        degraded: bool,
        degradation_reason: str | None,
        clarification_question: str | None,
    ) -> dict[str, Any]:
        first = executions[0] if executions else None
        labels = manifest.by_name()
        selected = str(first.get("tool_name")) if first else None
        selected_item = labels.get(selected) if selected else None
        steps = [
            {
                "step_id": item.get("step_id"),
                "position": position,
                "tool_name": item.get("tool_name"),
                "tool_label": item.get("tool_label"),
                "status": item.get("status"),
                "executable": item.get("status") in {"EXECUTED", "PROPOSED"},
                "read_only": item.get("status") != "PROPOSED",
                "missing_evidence": [],
            }
            for position, item in enumerate(executions, start=1)
        ]
        return {
            "application_id": application_id,
            "ontology_version": ontology_version,
            "goal": sanitize_conversation_text(instruction, max_chars=500),
            "planner_mode": "ontology_agent",
            "selected_tool": selected,
            "tool_label": (
                selected_item.label if selected_item is not None else None
            ),
            "reason": "模型基于已发布本体能力选择，并通过后端授权校验。",
            "executable": bool(first and first.get("status") == "EXECUTED"),
            "status": str(first.get("status")) if first else "NO_ACTION",
            "response_type": "ACTION_PLAN" if executions else "NO_ACTION",
            "missing_evidence": [],
            "read_only": not bool(proposals),
            "requires_confirmation": bool(proposals),
            "final_approval": False,
            "suggested_write_action": None,
            "steps": steps,
            "overall_status": OntologyAgentRuntime._execution_summary(
                executions
            )["status"],
            "max_steps": max_steps,
            "readiness": None,
            "clarification_required": bool(clarification_question),
            "clarification_question": clarification_question,
            "chat_answer": None,
            "model_calls": model_calls,
            "degraded": degraded,
            "degradation_reason": degradation_reason,
            "execution_semantics": "same_snapshot_observe_replan",
        }

    def _response(
        self,
        *,
        application_id: str,
        ontology_version: str,
        instruction: str,
        manifest: AgentCapabilityManifest,
        executions: list[dict[str, Any]],
        observations: list[AgentObservation],
        proposals: list[InMemoryWriteProposal],
        answer: str,
        reason_code: str | None,
        final_source: str,
        budget: _Budget,
        max_steps: int,
        trace: list[dict[str, Any]],
        degraded: bool,
        clarification_question: str | None,
    ) -> dict[str, Any]:
        summary = self._execution_summary(executions)
        first_result = next((
            item.get("result")
            for item in executions
            if item.get("status") == "EXECUTED"
        ), None)
        response_type = (
            "ACTION_RUN"
            if summary["executed"] > 0
            else "ACTION_PLAN"
            if executions or proposals
            else "NO_ACTION" if reason_code else "CHAT"
        )
        synthesis_status = "FALLBACK" if degraded else "COMPLETED"
        result = {
            "response_type": response_type,
            "reason_code": reason_code,
            "plan": self._plan_payload(
                application_id=application_id,
                ontology_version=ontology_version,
                instruction=instruction,
                manifest=manifest,
                executions=executions,
                proposals=proposals,
                max_steps=max_steps,
                model_calls=budget.model_calls,
                degraded=degraded,
                degradation_reason=reason_code if degraded else None,
                clarification_question=clarification_question,
            ),
            "executed": summary["executed"] > 0,
            "answer": answer,
            "final_answer": answer,
            "result": first_result,
            "executions": executions,
            "execution_summary": summary,
            "proposed_actions": [item.model_dump(mode="json") for item in proposals],
            "synthesis": {
                "mode": final_source,
                "status": synthesis_status,
                "reason_code": reason_code if degraded else None,
                "degraded": degraded,
                "quality_gate": "REJECTED" if degraded else "PASSED",
                "model_called": budget.model_calls > 0,
            },
            "model_calls": budget.model_calls,
            "trace": trace,
            "observations": self._model_observations(observations),
            "manifest_id": manifest.manifest_id,
            "budget": budget.public(),
            "degraded": degraded,
        }
        return result

    async def _run_loop(
        self,
        *,
        application_id: str,
        instruction: str,
        execute: bool,
        max_steps: int,
        llm_client: Any,
        context: PublishedOntologyContext,
        principal: AgentPrincipalContext,
        snapshot: Any,
        manifest: AgentCapabilityManifest,
        history: Sequence[Mapping[str, Any]],
        budget: _Budget,
    ) -> dict[str, Any]:
        trace: list[dict[str, Any]] = []
        observations: list[AgentObservation] = []
        executions: list[dict[str, Any]] = []
        proposals: list[InMemoryWriteProposal] = []
        signatures: set[str] = set()
        degraded = False
        reason_code: str | None = None
        final_source = "llm"
        final_answer: str | None = None
        terminal_question: str | None = None

        while final_answer is None and terminal_question is None:
            if not budget.can_call_model():
                degraded = True
                reason_code = "AGENT_BUDGET_FALLBACK"
                final_source = "deterministic_fallback"
                break
            decision_started = self.clock()
            try:
                decision = await self._decide(
                    llm_client=llm_client,
                    instruction=instruction,
                    history=history,
                    manifest=manifest,
                    observations=observations,
                    budget=budget,
                )
            except asyncio.CancelledError:
                raise
            except OntologyAgentRuntimeError as exc:
                degraded = True
                reason_code = exc.code
                final_source = "deterministic_fallback"
                self._trace(
                    trace,
                    stage="replan" if observations else "plan",
                    status="FAILED",
                    source="model",
                    started_at=decision_started,
                    clock=self.clock,
                )
                break

            (
                _parser,
                LookupOntologyDecision,
                CallReadActionsDecision,
                ProposeWriteActionDecision,
                AskUserDecision,
                FinalDecision,
            ) = _decision_api()
            raw_decision_name = getattr(decision, "decision", "")
            decision_name = str(
                getattr(raw_decision_name, "value", raw_decision_name)
            )
            self._trace(
                trace,
                stage="replan" if observations else "plan",
                status="COMPLETED",
                source="model",
                started_at=decision_started,
                clock=self.clock,
                decision=decision_name,
            )

            if isinstance(decision, LookupOntologyDecision):
                lookup_started = self.clock()
                try:
                    budget.consume_lookup()
                    references = self._lookup_references(
                        context,
                        manifest,
                        tuple(decision.capability_names),
                    )
                    ontology_slice = self.context_provider.build_model_slice(
                        context, references
                    )
                    observation = AgentObservation(
                        observation_id=self._next_observation_id(observations),
                        kind="ONTOLOGY_LOOKUP",
                        status="COMPLETED",
                        payload=ontology_slice.model_payload(),
                    )
                    observations.append(observation)
                    self._trace(
                        trace,
                        stage="ontology_lookup",
                        status="COMPLETED",
                        source="backend",
                        started_at=lookup_started,
                        clock=self.clock,
                        observation_id=observation.observation_id,
                    )
                except (OntologyAgentRuntimeError, OntologyContextError,
                        AgentCapabilityRegistryError):
                    degraded = True
                    reason_code = "ONTOLOGY_LOOKUP_REJECTED"
                    final_source = "deterministic_fallback"
                    self._trace(
                        trace,
                        stage="ontology_lookup",
                        status="REJECTED",
                        source="backend",
                        started_at=lookup_started,
                        clock=self.clock,
                    )
                    break
                continue

            if isinstance(decision, CallReadActionsDecision):
                calls = tuple(decision.calls)
                action_started = self.clock()
                if not calls or len(calls) > budget.action_limit - budget.action_calls:
                    degraded = True
                    reason_code = "ACTION_BUDGET_REJECTED"
                    final_source = "deterministic_fallback"
                    self._trace(
                        trace,
                        stage="action",
                        status="REJECTED",
                        source="backend",
                        started_at=action_started,
                        clock=self.clock,
                    )
                    break

                # Phase one: authorize the complete batch and reject duplicates.
                authorized: list[AuthorizedCapabilityDecision] = []
                batch_signatures: list[str] = []
                try:
                    for call in calls:
                        authorization = self.registry.authorize_decision(
                            context=context,
                            principal=principal,
                            manifest=manifest,
                            decision=AgentCapabilityDecision.CALL_READ_ACTIONS,
                            capability_name=call.capability_name,
                            arguments=call.arguments,
                        )
                        signature = self._call_signature(
                            authorization.capability_id,
                            authorization.model_arguments,
                        )
                        if signature in signatures or signature in batch_signatures:
                            raise AgentCapabilityRegistryError(
                                "AGENT_CAPABILITY_DECISION_REJECTED",
                                "同轮 Action 签名重复",
                            )
                        authorized.append(authorization)
                        batch_signatures.append(signature)
                    budget.reserve_actions(len(authorized))
                except (AgentCapabilityRegistryError, OntologyAgentRuntimeError):
                    degraded = True
                    reason_code = "READ_BATCH_AUTHORIZATION_REJECTED"
                    final_source = "deterministic_fallback"
                    self._trace(
                        trace,
                        stage="action",
                        status="REJECTED",
                        source="backend",
                        started_at=action_started,
                        clock=self.clock,
                    )
                    break

                if not execute:
                    for authorization in authorized:
                        manifest_item = manifest.by_name()[
                            authorization.capability_name
                        ]
                        executions.append({
                            "step_id": f"step-{len(executions) + 1}",
                            "tool_name": authorization.capability_name,
                            "tool_label": manifest_item.label,
                            "status": "PLANNED",
                            "executed": False,
                            "answer": f"已完成“{manifest_item.label}”的调用规划。",
                            "result": None,
                        })
                    reason_code = "EXECUTION_DISABLED_BY_REQUEST"
                    final_source = "deterministic"
                    break

                # Phase two: all authorizations succeeded; now execute in order.
                signatures.update(batch_signatures)
                for authorization in authorized:
                    item_started = self.clock()
                    manifest_item = manifest.by_name()[authorization.capability_name]
                    execution = {
                        "step_id": f"step-{len(executions) + 1}",
                        "tool_name": authorization.capability_name,
                        "tool_label": manifest_item.label,
                        "status": "PLANNED",
                        "executed": False,
                        "answer": None,
                        "result": None,
                    }
                    try:
                        raw_result = await self._execute_read(
                            authorization=authorization,
                            application_id=application_id,
                            snapshot=snapshot,
                            budget=budget,
                        )
                        projected = _project_read_result(
                            authorization.capability_name, raw_result
                        )
                        execution.update({
                            "status": "EXECUTED",
                            "executed": True,
                            "answer": _read_answer(
                                authorization.capability_name, projected
                            ),
                            "result": projected,
                        })
                        observation = AgentObservation(
                            observation_id=self._next_observation_id(observations),
                            kind="READ_ACTION",
                            status="COMPLETED",
                            capability_name=authorization.capability_name,
                            payload=projected,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        execution.update({
                            "status": "FAILED",
                            "answer": (
                                f"“{manifest_item.label}”调用失败；"
                                "已完成的其他只读结果仍然有效。"
                            ),
                            "error_code": "ACTION_EXECUTION_FAILED",
                        })
                        observation = AgentObservation(
                            observation_id=self._next_observation_id(observations),
                            kind="READ_ACTION",
                            status="FAILED",
                            capability_name=authorization.capability_name,
                            payload={},
                            error_code="ACTION_EXECUTION_FAILED",
                        )
                    executions.append(execution)
                    observations.append(observation)
                    self._trace(
                        trace,
                        stage="action",
                        status=execution["status"],
                        source="backend_read_only",
                        started_at=item_started,
                        clock=self.clock,
                        capability_name=authorization.capability_name,
                    )
                    self._trace(
                        trace,
                        stage="observation",
                        status=observation.status,
                        source="backend",
                        started_at=item_started,
                        clock=self.clock,
                        capability_name=authorization.capability_name,
                        observation_id=observation.observation_id,
                    )
                continue

            if isinstance(decision, ProposeWriteActionDecision):
                proposal_started = self.clock()
                try:
                    authorization = self.registry.authorize_decision(
                        context=context,
                        principal=principal,
                        manifest=manifest,
                        decision=AgentCapabilityDecision.PROPOSE_WRITE_ACTION,
                        capability_name=decision.capability_name,
                        arguments=decision.arguments,
                    )
                    signature = self._call_signature(
                        authorization.capability_id,
                        authorization.model_arguments,
                    )
                    if signature in signatures:
                        raise AgentCapabilityRegistryError(
                            "AGENT_CAPABILITY_DECISION_REJECTED",
                            "同轮 Proposal 签名重复",
                        )
                    budget.reserve_actions(1)
                    signatures.add(signature)
                    step_id = f"step-{len(executions) + 1}"
                    proposal = InMemoryWriteProposal(
                        proposal_id="wprop_" + _canonical_sha256({
                            "manifest_id": manifest.manifest_id,
                            "capability_id": authorization.capability_id,
                            "arguments": authorization.model_arguments,
                            "step_id": step_id,
                        }),
                        action_name=authorization.capability_name,
                        capability_id=authorization.capability_id,
                        source_step_id=step_id,
                        model_arguments=authorization.model_arguments,
                        final_approval=False,
                    )
                    proposal_payload = {
                        "proposal_id": proposal.proposal_id,
                        "action_name": proposal.action_name,
                        "status": proposal.status,
                        "requires_confirmation": True,
                        "auto_executable": False,
                        "proposal_only": True,
                        "final_approval": False,
                    }
                    assert_model_safe_payload(proposal_payload)
                    proposals.append(proposal)
                    manifest_item = manifest.by_name()[proposal.action_name]
                    executions.append({
                        "step_id": step_id,
                        "tool_name": proposal.action_name,
                        "tool_label": manifest_item.label,
                        "status": "PROPOSED",
                        "executed": False,
                        "answer": (
                            f"已生成“{manifest_item.label}”待确认 Proposal；"
                            "尚未执行。"
                        ),
                        "result": None,
                    })
                    observation = AgentObservation(
                        observation_id=self._next_observation_id(observations),
                        kind="WRITE_PROPOSAL",
                        status="PROPOSED",
                        capability_name=proposal.action_name,
                        payload=proposal_payload,
                    )
                    observations.append(observation)
                    self._trace(
                        trace,
                        stage="action",
                        status="PROPOSED",
                        source="backend_proposal_only",
                        started_at=proposal_started,
                        clock=self.clock,
                        capability_name=proposal.action_name,
                    )
                    self._trace(
                        trace,
                        stage="observation",
                        status="PROPOSED",
                        source="backend",
                        started_at=proposal_started,
                        clock=self.clock,
                        capability_name=proposal.action_name,
                        observation_id=observation.observation_id,
                    )
                except (AgentCapabilityRegistryError, OntologyAgentRuntimeError,
                        OntologyContextError):
                    degraded = True
                    reason_code = "WRITE_PROPOSAL_REJECTED"
                    final_source = "deterministic_fallback"
                    self._trace(
                        trace,
                        stage="action",
                        status="REJECTED",
                        source="backend_proposal_only",
                        started_at=proposal_started,
                        clock=self.clock,
                    )
                    break
                continue

            if isinstance(decision, AskUserDecision):
                try:
                    compact_question="".join(decision.question.lower().split())
                    if any(
                        "".join(term.lower().split()) in compact_question
                        for term in _CREDENTIAL_REQUEST_TERMS
                    ):
                        raise OntologyContextError(
                            "ONTOLOGY_CONTEXT_UNSAFE_TEXT",
                            "澄清问题不得索取凭据或秘密",
                        )
                    terminal_question = safe_ontology_text(
                        decision.question,
                        field="clarification_question",
                        max_length=500,
                    )
                    reason_code = "NEEDS_CLARIFICATION"
                    final_source = "llm"
                    self._trace(
                        trace,
                        stage="clarification",
                        status="COMPLETED",
                        source="model",
                        started_at=decision_started,
                        clock=self.clock,
                    )
                except OntologyContextError:
                    degraded = True
                    reason_code = "MODEL_CLARIFICATION_REJECTED"
                    final_source = "deterministic_fallback"
                break

            if isinstance(decision, FinalDecision):
                try:
                    if decision.boundary != "NOT_FINAL_APPROVAL":
                        raise OntologyAgentRuntimeError(
                            "ONTOLOGY_AGENT_OUTPUT_REJECTED",
                            "FINAL decision boundary 无效",
                        )
                    final_answer = self._safe_final_answer(
                        decision.answer,
                        observations=observations,
                        cited_observation_ids=decision.observation_ids,
                    )
                    if any(
                        item.kind in {"READ_ACTION","WRITE_PROPOSAL"}
                        and item.status in {"COMPLETED","PROPOSED"}
                        for item in observations
                    ):
                        final_source = "deterministic_observation"
                    self._trace(
                        trace,
                        stage="answer",
                        status="COMPLETED",
                        source=(
                            "backend_observation"
                            if final_source=="deterministic_observation"
                            else "model"
                        ),
                        started_at=decision_started,
                        clock=self.clock,
                    )
                except (OntologyAgentRuntimeError, OntologyContextError):
                    degraded = True
                    reason_code = "MODEL_FINAL_REJECTED"
                    final_source = "deterministic_fallback"
                    final_answer = None
                break

            degraded = True
            reason_code = "MODEL_DECISION_UNSUPPORTED"
            final_source = "deterministic_fallback"
            break

        if final_answer is None:
            final_answer = self._deterministic_answer(
                executions,
                proposals,
                question=terminal_question,
                no_result_reason=(
                    "当前模型不可用或决策未通过校验；本轮未调用任何业务 Action。"
                    if not observations else None
                ),
            )
            if final_source == "llm" and terminal_question:
                final_source = "llm_clarification"
            elif terminal_question is None:
                degraded = True
            fallback_started = self.clock()
            self._trace(
                trace,
                stage="fallback" if degraded else "answer",
                status="COMPLETED",
                source=final_source,
                started_at=fallback_started,
                clock=self.clock,
            )

        return self._response(
            application_id=application_id,
            ontology_version=context.ontology_version,
            instruction=instruction,
            manifest=manifest,
            executions=executions,
            observations=observations,
            proposals=proposals,
            answer=final_answer,
            reason_code=reason_code,
            final_source=final_source,
            budget=budget,
            max_steps=max_steps,
            trace=trace,
            degraded=degraded,
            clarification_question=terminal_question,
        )

    async def run(
        self,
        application_id: str,
        instruction: str,
        *,
        llm_client: Any = None,
        execute: bool = True,
        max_steps: int = 1,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(application_id, str) or not application_id.strip():
            raise ValueError("application_id 不能为空")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("instruction 不能为空")
        if not 1 <= int(max_steps) <= ONTOLOGY_AGENT_MAX_ACTION_CALLS:
            raise ValueError("max_steps 必须在 1 到 4 之间")
        budget = _Budget(
            limits=self.limits,
            action_limit=min(int(max_steps), self.limits.action_calls),
            clock=self.clock,
            started_at=self.clock(),
        )
        try:
            publication = self.published_ontology
            if publication is None:
                publication = await self._invoke_dependency(
                    self.context_provider.resolve_published_ontology,
                    budget,
                    self.requested_ontology_version,
                )
            elif (
                self.requested_ontology_version is not None
                and publication.ontology_version != self.requested_ontology_version
            ):
                raise OntologyContextError(
                    "ONTOLOGY_CONTEXT_VERSION_MISMATCH",
                    "钉住的发布本体版本与请求版本不一致",
                )
        except OntologyAgentRuntimeError:
            raise
        except OntologyContextError as exc:
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_CONTEXT_FAILED", "published ontology 固定失败"
            ) from exc
        snapshot = await self._snapshot(application_id.strip(), budget)
        try:
            def bind_authorized_context():
                bound_context = self.context_provider.bind_snapshot(
                    publication,
                    application_id.strip(),
                    snapshot,
                )
                bound_principal = self._resolve_principal(bound_context)
                bound_manifest = self.registry.build_manifest(
                    context=bound_context, principal=bound_principal
                )
                return bound_context,bound_principal,bound_manifest

            context,principal,manifest = await self._invoke_dependency(
                bind_authorized_context,
                budget,
            )
        except OntologyAgentRuntimeError:
            raise
        except (OntologyContextError, AgentCapabilityRegistryError) as exc:
            raise OntologyAgentRuntimeError(
                "ONTOLOGY_AGENT_CONTEXT_FAILED", "context/manifest 构建失败"
            ) from exc

        # Reuse existing leases while binding them to the exact ontology digest.
        lease: ConversationLease = self.conversations.begin(
            conversation_id,
            application_id=application_id.strip(),
            ontology_version=(
                f"ontology_agent:{context.ontology_id}:"
                f"{context.ontology_version}:{context.ontology_digest}:"
                f"{self._principal_conversation_scope(principal)}"
            ),
        )
        committed = False
        try:
            selected_llm = llm_client if llm_client is not None else self.llm_client
            result = await self._run_loop(
                application_id=application_id.strip(),
                instruction=instruction,
                execute=bool(execute),
                max_steps=int(max_steps),
                llm_client=selected_llm,
                context=context,
                principal=principal,
                snapshot=snapshot,
                manifest=manifest,
                history=lease.history,
                budget=budget,
            )
            executed_tools = [
                str(item.get("tool_name"))
                for item in result["executions"]
                if item.get("status") == "EXECUTED"
            ]
            context_window = self.conversations.commit(
                lease,
                user=instruction,
                assistant=sanitize_conversation_text(
                    result["final_answer"], max_chars=300
                ),
                response_type=result["response_type"],
                tool_names=executed_tools,
            )
            committed = True
            result.update({
                "conversation_id": lease.conversation_id,
                "turn_index": lease.turn_index,
                "context_window": context_window,
            })
            return result
        finally:
            if not committed:
                self.conversations.abort(lease)


__all__ = (
    "ONTOLOGY_AGENT_HARD_TIMEOUT_SECONDS",
    "ONTOLOGY_AGENT_MAX_ACTION_CALLS",
    "ONTOLOGY_AGENT_MAX_LOOKUPS",
    "ONTOLOGY_AGENT_MAX_MODEL_CALLS",
    "ONTOLOGY_AGENT_SOFT_TIMEOUT_SECONDS",
    "DEFAULT_ONTOLOGY_AGENT_CONVERSATION_STORE",
    "AgentObservation",
    "InMemoryWriteProposal",
    "OntologyAgentBudgetLimits",
    "OntologyAgentRuntime",
    "OntologyAgentRuntimeError",
)
