from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from .action_design_catalog import ACTION_RUNTIME_DESIGNS, ActionRuntimeDesign


ONTOLOGY_PERSONAL_CREDIT = "personal-credit"
ONTOLOGY_VERSION_WILDCARD = "*"

REUSE_MODE_RUNTIME_CORE = "runtime_core"
REUSE_MODE_ORCHESTRATION_TEMPLATE = "orchestration_template"
REUSE_MODES = frozenset(
    {REUSE_MODE_RUNTIME_CORE, REUSE_MODE_ORCHESTRATION_TEMPLATE}
)

# These steps have the same semantics in every ontology. The remaining steps are
# deliberately kept behind a business adapter because their data, rules or effects
# are domain-specific.
PORTABLE_STEP_KINDS = frozenset({"authorize", "idempotency", "verify", "audit"})


class ReusableActionError(RuntimeError):
    """可复用 Action 目录或适配器注册失败。"""


class ReusableActionNotFound(ReusableActionError):
    pass


class ReusableActionAlreadyRegistered(ReusableActionError):
    pass


class ReusableActionRequestInvalid(ReusableActionError):
    def __init__(self, message: str, *, errors: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.errors = errors or []


@dataclass(frozen=True)
class ReusableActionArchetype:
    name: str
    label: str
    description: str
    generic_inputs: tuple[str, ...]
    generic_outputs: tuple[str, ...]


@dataclass(frozen=True)
class ActionReuseProfile:
    action_name: str
    archetype: str
    reuse_mode: str
    reusable_capabilities: tuple[str, ...]
    business_binding_points: tuple[str, ...]
    audit_conclusion: str


ACTION_ARCHETYPES: dict[str, ReusableActionArchetype] = {
    "create_business_case": ReusableActionArchetype(
        name="create_business_case",
        label="创建业务案件",
        description="创建业务主对象、参与对象及受控关系。",
        generic_inputs=("case", "participants", "targets", "request_id"),
        generic_outputs=("case_id", "created_objects", "created_relations"),
    ),
    "request_external_report": ReusableActionArchetype(
        name="request_external_report",
        label="获取外部报告",
        description="在授权和幂等边界内请求外部系统并登记报告。",
        generic_inputs=("subject", "context", "authorization", "request_id"),
        generic_outputs=("report", "provider_request_id", "lineage"),
    ),
    "generate_assessment": ReusableActionArchetype(
        name="generate_assessment",
        label="生成业务评估",
        description="读取一致性快照、执行本体 Function 并登记评估结果。",
        generic_inputs=("subject", "assessment_version", "snapshot_policy"),
        generic_outputs=("assessment", "evidence", "lineage"),
    ),
    "create_verification_task": ReusableActionArchetype(
        name="create_verification_task",
        label="创建核验任务",
        description="根据风险或业务信号生成问题并创建外部核验任务。",
        generic_inputs=("subject", "round", "trigger_reason"),
        generic_outputs=("task_id", "questions", "dispatch_status"),
    ),
    "record_verification_result": ReusableActionArchetype(
        name="record_verification_result",
        label="登记核验结果",
        description="校验任务归属和回调幂等性后登记核验结果。",
        generic_inputs=("task_id", "result", "evidence", "callback_id"),
        generic_outputs=("task_id", "record_status", "lineage"),
    ),
    "route_human_review": ReusableActionArchetype(
        name="route_human_review",
        label="路由人工复核",
        description="在业务规则和证据成立时将案件路由至人工复核。",
        generic_inputs=("subject", "reason", "source_execution_id"),
        generic_outputs=("subject_id", "routing_status", "evidence_reference"),
    ),
    "record_decision_support": ReusableActionArchetype(
        name="record_decision_support",
        label="登记决策建议",
        description="登记带证据和人工确认的建议，不执行最终业务审批。",
        generic_inputs=("subject", "recommendation_version", "approval_assertion"),
        generic_outputs=("recommendation", "decision_authority", "evidence_reference"),
    ),
}


PERSONAL_CREDIT_ACTION_REUSE_PROFILES: dict[str, ActionReuseProfile] = {
    "create_loan_application": ActionReuseProfile(
        action_name="create_loan_application",
        archetype="create_business_case",
        reuse_mode=REUSE_MODE_ORCHESTRATION_TEMPLATE,
        reusable_capabilities=("状态机", "鉴权", "幂等", "审计", "事务/补偿合同"),
        business_binding_points=("对象字段映射", "进件公理", "关系写入", "图同步"),
        audit_conclusion="可复用创建案件模板；贷款申请模型和关系必须由本体适配器提供。",
    ),
    "request_credit_report": ActionReuseProfile(
        action_name="request_credit_report",
        archetype="request_external_report",
        reuse_mode=REUSE_MODE_ORCHESTRATION_TEMPLATE,
        reusable_capabilities=("状态机", "鉴权", "幂等", "外部调用保护", "审计"),
        business_binding_points=("授权规则", "查询主体", "征信供应商", "报告映射"),
        audit_conclusion="与反欺诈查询共用外部报告模板；征信授权及报告语义保持独立。",
    ),
    "request_fraud_report": ActionReuseProfile(
        action_name="request_fraud_report",
        archetype="request_external_report",
        reuse_mode=REUSE_MODE_ORCHESTRATION_TEMPLATE,
        reusable_capabilities=("状态机", "鉴权", "幂等", "外部调用保护", "审计"),
        business_binding_points=("申请上下文", "关联风险 Function", "反欺诈供应商", "报告映射"),
        audit_conclusion="与征信查询共用外部报告模板；关联风险和报告规则由业务适配。",
    ),
    "generate_risk_assessment": ActionReuseProfile(
        action_name="generate_risk_assessment",
        archetype="generate_assessment",
        reuse_mode=REUSE_MODE_ORCHESTRATION_TEMPLATE,
        reusable_capabilities=("一致性快照", "Function 编排", "幂等", "审计", "补偿"),
        business_binding_points=("数据快照", "Function 集合", "评估公理", "结果仓储"),
        audit_conclusion="可复用评估生成模板；指标、模型和门禁必须按本体绑定。",
    ),
    "create_phone_verification_task": ActionReuseProfile(
        action_name="create_phone_verification_task",
        archetype="create_verification_task",
        reuse_mode=REUSE_MODE_ORCHESTRATION_TEMPLATE,
        reusable_capabilities=("触发门禁", "任务幂等", "外部投递", "审计"),
        business_binding_points=("核验渠道", "问题生成 Function", "任务字段", "调度器"),
        audit_conclusion="可复用核验任务模板；电话只是当前信审本体的一种渠道绑定。",
    ),
    "record_phone_verification": ActionReuseProfile(
        action_name="record_phone_verification",
        archetype="record_verification_result",
        reuse_mode=REUSE_MODE_RUNTIME_CORE,
        reusable_capabilities=("任务归属校验", "回调幂等", "结果登记", "审计"),
        business_binding_points=("结果枚举", "证据字段", "任务仓储"),
        audit_conclusion="核心结果登记流程可直接复用；电核字段通过业务适配器约束。",
    ),
    "trigger_manual_review": ActionReuseProfile(
        action_name="trigger_manual_review",
        archetype="route_human_review",
        reuse_mode=REUSE_MODE_RUNTIME_CORE,
        reusable_capabilities=("规则门禁", "证据校验", "状态路由", "审计"),
        business_binding_points=("转人工规则", "业务状态映射", "证据来源"),
        audit_conclusion="人工复核路由核心可复用；触发规则和状态值由业务本体定义。",
    ),
    "record_decision_recommendation": ActionReuseProfile(
        action_name="record_decision_recommendation",
        archetype="record_decision_support",
        reuse_mode=REUSE_MODE_RUNTIME_CORE,
        reusable_capabilities=("证据聚合", "人工确认门禁", "建议登记", "审计"),
        business_binding_points=("建议枚举", "高风险约束", "证据 Function", "结果仓储"),
        audit_conclusion="建议登记核心可复用；始终保持 recommendation_only，不泛化为最终审批。",
    ),
}


def validate_action_reuse_profiles(
    designs: Mapping[str, ActionRuntimeDesign] | None = None,
    profiles: Mapping[str, ActionReuseProfile] | None = None,
) -> list[str]:
    active_designs = ACTION_RUNTIME_DESIGNS if designs is None else designs
    active_profiles = (
        PERSONAL_CREDIT_ACTION_REUSE_PROFILES if profiles is None else profiles
    )
    errors: list[str] = []
    if set(active_designs) != set(active_profiles):
        errors.append("Action 复用审核必须完整覆盖运行设计")
    for action_name, profile in active_profiles.items():
        design = active_designs.get(action_name)
        if design is None:
            continue
        if profile.action_name != action_name:
            errors.append(f"Action 复用审核键与名称不一致：{action_name}")
        if profile.archetype not in ACTION_ARCHETYPES:
            errors.append(f"Action 引用了未知复用原型：{action_name}.{profile.archetype}")
        if profile.reuse_mode not in REUSE_MODES:
            errors.append(f"Action 复用模式非法：{action_name}.{profile.reuse_mode}")
        step_kinds = {step.kind for step in design.steps}
        missing_core = {"authorize", "idempotency", "audit"} - step_kinds
        if missing_core:
            errors.append(
                f"Action 缺少可复用运行步骤：{action_name}.{','.join(sorted(missing_core))}"
            )
        if not profile.reusable_capabilities or not profile.business_binding_points:
            errors.append(f"Action 复用能力或业务绑定点为空：{action_name}")
    return errors


def build_action_reuse_audit(
    designs: Mapping[str, ActionRuntimeDesign] | None = None,
    profiles: Mapping[str, ActionReuseProfile] | None = None,
) -> dict[str, Any]:
    active_designs = ACTION_RUNTIME_DESIGNS if designs is None else designs
    active_profiles = (
        PERSONAL_CREDIT_ACTION_REUSE_PROFILES if profiles is None else profiles
    )
    errors = validate_action_reuse_profiles(active_designs, active_profiles)
    actions = []
    for action_name, design in active_designs.items():
        profile = active_profiles.get(action_name)
        if profile is None:
            continue
        actions.append(
            {
                "action_name": action_name,
                "archetype": profile.archetype,
                "archetype_label": ACTION_ARCHETYPES[profile.archetype].label,
                "reuse_mode": profile.reuse_mode,
                "reusable_capabilities": list(profile.reusable_capabilities),
                "business_binding_points": list(profile.business_binding_points),
                "portable_steps": [
                    step.name for step in design.steps if step.kind in PORTABLE_STEP_KINDS
                ],
                "adapter_steps": [
                    step.name for step in design.steps if step.kind not in PORTABLE_STEP_KINDS
                ],
                "audit_conclusion": profile.audit_conclusion,
            }
        )
    return {
        "status": "passed" if not errors else "failed",
        "action_count": len(actions),
        "archetype_count": len({item["archetype"] for item in actions}),
        "errors": errors,
        "actions": actions,
    }


ActionExecutor = Callable[[Any], Awaitable[Any] | Any]


@dataclass(frozen=True)
class ActionAdapterBinding:
    ontology_id: str
    ontology_version: str
    action_name: str
    request_model: Any
    executor: ActionExecutor
    profile: ActionReuseProfile


class ReusableActionAdapterRegistry:
    """按本体注册业务适配器，并通过统一入口分发 Action。"""

    def __init__(self):
        self._bindings: dict[tuple[str, str, str], ActionAdapterBinding] = {}

    @staticmethod
    def _key(
        ontology_id: str,
        ontology_version: str,
        action_name: str,
    ) -> tuple[str, str, str]:
        ontology = str(ontology_id or "").strip().lower()
        version = str(ontology_version or "").strip()
        action = str(action_name or "").strip()
        if not ontology or not version or not action:
            raise ReusableActionError(
                "ontology_id、ontology_version 和 action_name 不能为空"
            )
        return ontology, version, action

    def register(
        self,
        *,
        ontology_id: str,
        ontology_version: str = ONTOLOGY_VERSION_WILDCARD,
        action_name: str,
        request_model: Any,
        executor: ActionExecutor,
        profile: ActionReuseProfile | None = None,
    ) -> None:
        key = self._key(ontology_id, ontology_version, action_name)
        if key in self._bindings:
            raise ReusableActionAlreadyRegistered(
                f"Action 适配器已注册：{key[0]}.{key[1]}.{key[2]}"
            )
        active_profile = profile or PERSONAL_CREDIT_ACTION_REUSE_PROFILES.get(key[2])
        if active_profile is None or active_profile.action_name != key[2]:
            raise ReusableActionError(f"Action 缺少复用审核档案：{key[2]}")
        if not hasattr(request_model, "model_validate") or not hasattr(
            request_model, "model_fields"
        ):
            raise ReusableActionError(f"Action 请求模型不支持 Pydantic 校验：{key[2]}")
        if not callable(executor):
            raise ReusableActionError(f"Action 执行器不可调用：{key[2]}")
        self._bindings[key] = ActionAdapterBinding(
            ontology_id=key[0],
            ontology_version=key[1],
            action_name=key[2],
            request_model=request_model,
            executor=executor,
            profile=active_profile,
        )

    def resolve(
        self,
        ontology_id: str,
        action_name: str,
        *,
        ontology_version: str = ONTOLOGY_VERSION_WILDCARD,
    ) -> ActionAdapterBinding:
        key = self._key(ontology_id, ontology_version, action_name)
        binding = self._bindings.get(key)
        if binding is None and key[1] != ONTOLOGY_VERSION_WILDCARD:
            binding = self._bindings.get(
                (key[0], ONTOLOGY_VERSION_WILDCARD, key[2])
            )
        if binding is None:
            raise ReusableActionNotFound(
                f"Action 适配器不存在：{key[0]}.{key[1]}.{key[2]}"
            )
        return binding

    def request_model(
        self,
        ontology_id: str,
        action_name: str,
        *,
        ontology_version: str = ONTOLOGY_VERSION_WILDCARD,
    ) -> Any:
        return self.resolve(
            ontology_id,
            action_name,
            ontology_version=ontology_version,
        ).request_model

    def validate_payload(
        self,
        ontology_id: str,
        action_name: str,
        payload: Mapping[str, Any],
        *,
        ontology_version: str = ONTOLOGY_VERSION_WILDCARD,
    ) -> Any:
        if not isinstance(payload, Mapping):
            raise ReusableActionRequestInvalid("Action payload 必须是对象")
        model = self.request_model(
            ontology_id,
            action_name,
            ontology_version=ontology_version,
        )
        try:
            return model.model_validate(dict(payload))
        except Exception as error:
            validation_errors: list[dict[str, Any]] = []
            if hasattr(error, "errors"):
                validation_errors = [
                    {
                        "loc": list(item.get("loc", ())),
                        "type": item.get("type"),
                        "msg": item.get("msg"),
                    }
                    for item in error.errors()
                ]
            raise ReusableActionRequestInvalid(
                f"{action_name} 请求字段校验失败",
                errors=validation_errors,
            ) from error

    async def execute_model(
        self,
        ontology_id: str,
        action_name: str,
        request_model: Any,
        *,
        ontology_version: str = ONTOLOGY_VERSION_WILDCARD,
    ) -> Any:
        binding = self.resolve(
            ontology_id,
            action_name,
            ontology_version=ontology_version,
        )
        if not isinstance(request_model, binding.request_model):
            raise ReusableActionRequestInvalid(
                f"{action_name} 请求模型类型不匹配"
            )
        result = binding.executor(request_model)
        return await result if inspect.isawaitable(result) else result

    async def execute(
        self,
        ontology_id: str,
        action_name: str,
        payload: Mapping[str, Any],
        *,
        ontology_version: str = ONTOLOGY_VERSION_WILDCARD,
    ) -> Any:
        model = self.validate_payload(
            ontology_id,
            action_name,
            payload,
            ontology_version=ontology_version,
        )
        return await self.execute_model(
            ontology_id,
            action_name,
            model,
            ontology_version=ontology_version,
        )

    def describe(
        self,
        ontology_id: str | None = None,
        *,
        ontology_version: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(ontology_id or "").strip().lower()
        normalized_version = str(ontology_version or "").strip()
        bindings = [
            binding
            for (registered_ontology, registered_version, _), binding
            in sorted(self._bindings.items())
            if not normalized or registered_ontology == normalized
            if not normalized_version
            or registered_version in {
                normalized_version,
                ONTOLOGY_VERSION_WILDCARD,
            }
        ]
        if normalized_version:
            selected: dict[tuple[str, str], ActionAdapterBinding] = {}
            for binding in bindings:
                key = (binding.ontology_id, binding.action_name)
                current = selected.get(key)
                if current is None or binding.ontology_version == normalized_version:
                    selected[key] = binding
            bindings = [selected[key] for key in sorted(selected)]
        if normalized and not bindings:
            raise ReusableActionNotFound(f"本体 Action 适配器不存在：{normalized}")
        return {
            "ontology_id": normalized or None,
            "ontology_version": normalized_version or None,
            "action_count": len(bindings),
            "actions": [
                {
                    "action_name": item.action_name,
                    "adapter_version": item.ontology_version,
                    "archetype": item.profile.archetype,
                    "archetype_label": ACTION_ARCHETYPES[item.profile.archetype].label,
                    "reuse_mode": item.profile.reuse_mode,
                    "request_fields": list(item.request_model.model_fields),
                    "business_binding_points": list(item.profile.business_binding_points),
                }
                for item in bindings
            ],
        }
