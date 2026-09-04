from __future__ import annotations

from copy import deepcopy
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from ..ontology.audit import proposal_digest
from ..ontology.models import OntologyProposal
from .action_design_catalog import (
    ACTION_RUNTIME_DESIGNS,
    ActionRuntimeDesign,
    validate_action_design_catalog,
)
from .function_registry import FunctionRegistry, FunctionRegistryError
from .reusable_actions import (
    PERSONAL_CREDIT_ACTION_REUSE_PROFILES,
    ReusableActionAdapterRegistry,
    ReusableActionNotFound,
)


ACTION_CAPABILITY_CONTRACT_VERSION = "1.0"
JSON_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"


class CapabilityAccess(str, Enum):
    READ = "READ"
    WRITE = "WRITE"


class CapabilityOrigin(str, Enum):
    ONTOLOGY_ACTION = "ONTOLOGY_ACTION"
    LEGACY_READ_TOOL = "LEGACY_READ_TOOL"


class OntologyDeclarationStatus(str, Enum):
    DECLARED = "DECLARED"
    PROJECTED = "PROJECTED"


class CapabilityRisk(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ConfirmationMode(str, Enum):
    NONE = "NONE"
    PROPOSAL_ONLY = "PROPOSAL_ONLY"


class CapabilityParameterSource(str, Enum):
    SERVER_CONTEXT = "SERVER_CONTEXT"
    MODEL_PROPOSAL = "MODEL_PROPOSAL"
    TRUSTED_USER = "TRUSTED_USER"
    TRUSTED_CONFIRMATION = "TRUSTED_CONFIRMATION"


class RuntimeBindingKind(str, Enum):
    REUSABLE_ACTION_ADAPTER = "REUSABLE_ACTION_ADAPTER"
    LEGACY_READ_SERVICE = "LEGACY_READ_SERVICE"


class OntologyCapabilityReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ontology_id: str
    ontology_version: str
    declaration_kind: str
    declaration_name: str


class CapabilityParameterPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    source: CapabilityParameterSource
    sensitive: bool = False
    model_writable: bool = False

    @model_validator(mode="after")
    def validate_source_boundary(self):
        if self.source is not CapabilityParameterSource.MODEL_PROPOSAL:
            if self.model_writable:
                raise ValueError("受信参数不得由模型写入")
        elif not self.model_writable:
            raise ValueError("MODEL_PROPOSAL 参数必须显式允许模型写入")
        if self.sensitive and self.model_writable:
            raise ValueError("敏感参数不得由模型写入")
        return self


class EvidenceRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    condition: str
    ontology_references: tuple[OntologyCapabilityReference, ...] = ()


class CapabilityPreconditionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    precondition: str
    evidence_keys: tuple[str, ...]
    runtime_guard_ids: tuple[str, ...]


class CapabilityConfirmationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: ConfirmationMode
    required: bool
    proposal_only: bool
    agent_execution_enabled: bool
    revalidate_on_execute: bool
    ontology_approval_required: bool
    approval_condition: str | None = None

    @model_validator(mode="after")
    def validate_mode(self):
        if self.mode is ConfirmationMode.NONE:
            if (
                self.required
                or self.proposal_only
                or self.revalidate_on_execute
                or self.ontology_approval_required
                or self.approval_condition
                or not self.agent_execution_enabled
            ):
                raise ValueError("NONE 确认策略与字段组合不一致")
        else:
            if (
                not self.required
                or not self.proposal_only
                or self.agent_execution_enabled
                or not self.revalidate_on_execute
            ):
                raise ValueError("PROPOSAL_ONLY 必须禁止 Agent 直接执行并要求重校验")
            if self.ontology_approval_required and not str(
                self.approval_condition or ""
            ).strip():
                raise ValueError("本体人工审批能力必须保留 approval_condition")
        return self


class FunctionRuntimeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    function_name: str
    bound: bool
    implementation_version: str


class CapabilityRuntimeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: RuntimeBindingKind
    binding_id: str
    bound: bool
    implementation_version: str
    request_model: str
    executor: str
    archetype: str
    reuse_mode: str
    function_bindings: tuple[FunctionRuntimeBinding, ...] = ()


class CapabilitySecurityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    allowed_roles: tuple[str, ...] = ()
    denied_roles: tuple[str, ...] = ()
    execution_boundary: str
    sensitive_fields: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    audit_required: bool


class CapabilityStepContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: str
    depends_on: tuple[str, ...] = ()
    function_name: str | None = None
    effect: str | None = None
    external_system: str | None = None
    timeout_seconds: int
    max_attempts: int
    failure_policy: str


class CapabilityExecutionSafety(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: tuple[CapabilityStepContract, ...]
    completion_rule: str
    consistency_boundary: str
    compensation: tuple[str, ...]
    audit_events: tuple[str, ...]
    error_codes: tuple[str, ...]


class ActionCapability(BaseModel):
    """Backend-only normalized contract; it is not a model-facing manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = ACTION_CAPABILITY_CONTRACT_VERSION
    capability_id: str
    name: str
    label: str
    description: str
    tags: tuple[str, ...] = ()
    origin: CapabilityOrigin
    access: CapabilityAccess
    declaration_status: OntologyDeclarationStatus
    ontology_id: str
    ontology_version: str
    ontology_digest: str
    ontology_references: tuple[OntologyCapabilityReference, ...]
    ontology_input_types: tuple[str, ...] = ()
    ontology_output_types: tuple[str, ...] = ()
    invokes_functions: tuple[str, ...] = ()
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    runtime_design_response_fields: tuple[str, ...] = ()
    parameter_policies: tuple[CapabilityParameterPolicy, ...]
    runtime_context_policies: tuple[CapabilityParameterPolicy, ...] = ()
    required_evidence: tuple[EvidenceRequirement, ...] = ()
    preconditions: tuple[str, ...] = ()
    precondition_bindings: tuple[CapabilityPreconditionBinding, ...] = ()
    effects: tuple[str, ...] = ()
    external_systems: tuple[str, ...] = ()
    side_effects: bool
    risk: CapabilityRisk
    confirmation_policy: CapabilityConfirmationPolicy
    idempotency_fields: tuple[str, ...] = ()
    runtime_binding: CapabilityRuntimeBinding
    security_policy: CapabilitySecurityPolicy
    execution_safety: CapabilityExecutionSafety | None = None
    suggested_write_action: str | None = None
    decision_support_only: bool = False
    final_approval: bool = False

    @model_validator(mode="after")
    def validate_security_invariants(self):
        if self.contract_version != ACTION_CAPABILITY_CONTRACT_VERSION:
            raise ValueError("ActionCapability contract_version 不受支持")
        if not all(
            value.strip()
            for value in (
                self.capability_id,
                self.name,
                self.label,
                self.description,
                self.ontology_id,
                self.ontology_version,
                self.ontology_digest,
            )
        ):
            raise ValueError("ActionCapability 身份、名称、标签或描述不能为空")
        if len(self.ontology_digest) != 64:
            raise ValueError("ontology_digest 必须是 SHA-256")
        try:
            int(self.ontology_digest, 16)
        except ValueError as error:
            raise ValueError("ontology_digest 必须是十六进制 SHA-256") from error
        declaration_kind = (
            "ActionType"
            if self.origin is CapabilityOrigin.ONTOLOGY_ACTION
            else "LegacyReadTool"
        )
        expected_capability_id = (
            f"{self.ontology_id}:{self.ontology_version}:{self.ontology_digest}:"
            f"{declaration_kind}:{self.name}"
        )
        if self.capability_id != expected_capability_id:
            raise ValueError("capability_id 未绑定准确的本体版本与 digest")
        if not self.runtime_binding.bound:
            raise ValueError("ActionCapability runtime binding 必须已绑定")
        if not self.ontology_references:
            raise ValueError("ActionCapability 必须包含同版本本体引用")
        if any(
            item.ontology_id != self.ontology_id
            or item.ontology_version != self.ontology_version
            for item in self.ontology_references
        ):
            raise ValueError("ActionCapability 本体引用越过目录版本边界")
        for schema_name, schema in (
            ("input", self.input_schema),
            ("output", self.output_schema),
        ):
            if schema.get("$schema") != JSON_SCHEMA_DRAFT:
                raise ValueError(f"{schema_name} Schema 必须声明 Draft 2020-12")
            if schema.get("type") != "object":
                raise ValueError(f"{schema_name} Schema 必须是 object")
            if schema.get("additionalProperties") is not False:
                raise ValueError(
                    f"{schema_name} Schema 顶层必须禁止 additionalProperties"
                )
        properties = set((self.input_schema.get("properties") or {}).keys())
        policy_fields = [item.field for item in self.parameter_policies]
        if properties != set(policy_fields) or len(policy_fields) != len(set(policy_fields)):
            raise ValueError("参数来源策略必须精确覆盖输入 Schema")
        context_fields = [item.field for item in self.runtime_context_policies]
        if len(context_fields) != len(set(context_fields)) or any(
            item.model_writable for item in self.runtime_context_policies
        ):
            raise ValueError("运行上下文参数必须唯一且禁止模型写入")
        function_names = [
            item.function_name for item in self.runtime_binding.function_bindings
        ]
        if (
            set(function_names) != set(self.invokes_functions)
            or len(function_names) != len(set(function_names))
            or any(
                not item.bound or not item.implementation_version.strip()
                for item in self.runtime_binding.function_bindings
            )
        ):
            raise ValueError("Function runtime binding 必须精确覆盖 invokes_functions")
        forbidden = {"sql", "cypher", "query", "query_text", "raw_query"}
        if {name.lower() for name in properties} & forbidden:
            raise ValueError("ActionCapability 不得接收自由数据库查询参数")

        if self.access is CapabilityAccess.READ:
            if self.origin is not CapabilityOrigin.LEGACY_READ_TOOL:
                raise ValueError("READ capability 必须标记为 legacy read 投影")
            if self.declaration_status is not OntologyDeclarationStatus.PROJECTED:
                raise ValueError("legacy READ 尚未发布为本体 capability")
            if (
                self.side_effects
                or self.effects
                or self.external_systems
                or self.idempotency_fields
                or self.execution_safety is not None
                or self.precondition_bindings
            ):
                raise ValueError("READ capability 不得携带写入合同")
            if self.risk in {CapabilityRisk.CRITICAL}:
                raise ValueError("READ capability 风险级别非法")
            if self.confirmation_policy.mode is not ConfirmationMode.NONE:
                raise ValueError("READ capability 不得要求写确认")
            if self.suggested_write_action and self.final_approval:
                raise ValueError("READ capability 不得成为最终审批入口")
        else:
            if self.origin is not CapabilityOrigin.ONTOLOGY_ACTION:
                raise ValueError("WRITE capability 必须来自 ActionType")
            if self.declaration_status is not OntologyDeclarationStatus.DECLARED:
                raise ValueError("WRITE capability 必须由本体明确声明")
            if not self.side_effects or not self.effects or not self.idempotency_fields:
                raise ValueError("WRITE capability 必须声明副作用和幂等字段")
            if self.risk in {CapabilityRisk.LOW, CapabilityRisk.MEDIUM}:
                raise ValueError("WRITE capability 风险不得低于 HIGH")
            if self.confirmation_policy.mode is not ConfirmationMode.PROPOSAL_ONLY:
                raise ValueError("WRITE capability 对 Agent 只能生成 Proposal")
            if self.execution_safety is None or not self.security_policy.audit_required:
                raise ValueError("WRITE capability 必须保留执行安全和审计合同")
            if self.suggested_write_action:
                raise ValueError("WRITE capability 不得再建议另一个写 Action")
            bound_preconditions = [
                item.precondition for item in self.precondition_bindings
            ]
            if (
                set(bound_preconditions) != set(self.preconditions)
                or len(bound_preconditions) != len(set(bound_preconditions))
            ):
                raise ValueError("本体前置条件必须被 runtime guard 精确覆盖")
            evidence_keys = {item.key for item in self.required_evidence}
            steps = {
                item.name: item for item in self.execution_safety.steps
            }
            for binding in self.precondition_bindings:
                if (
                    not binding.evidence_keys
                    or not binding.runtime_guard_ids
                    or not set(binding.evidence_keys).issubset(evidence_keys)
                ):
                    raise ValueError("前置条件绑定了未知证据或空 runtime guard")
                if any(
                    guard not in steps
                    or steps[guard].kind not in {"validate", "manual_gate"}
                    for guard in binding.runtime_guard_ids
                ):
                    raise ValueError("前置条件 runtime guard 不存在或不是校验门禁")
        if self.final_approval:
            raise ValueError("个人信审 capability 不得执行最终审批")
        if self.name == "record_decision_recommendation" and not (
            self.decision_support_only
            and self.confirmation_policy.ontology_approval_required
            and self.risk is CapabilityRisk.CRITICAL
        ):
            raise ValueError("决策建议写入必须保留人工确认和非最终审批边界")
        return self


class ActionCapabilityCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = ACTION_CAPABILITY_CONTRACT_VERSION
    ontology_id: str
    ontology_version: str
    ontology_digest: str
    ontology_action_count: int
    legacy_read_capability_count: int
    capability_count: int
    capabilities: tuple[ActionCapability, ...]

    @model_validator(mode="after")
    def validate_catalog(self):
        if self.contract_version != ACTION_CAPABILITY_CONTRACT_VERSION:
            raise ValueError("ActionCapabilityCatalog contract_version 不受支持")
        names = [item.name for item in self.capabilities]
        identifiers = [item.capability_id for item in self.capabilities]
        write_count = sum(
            item.access is CapabilityAccess.WRITE for item in self.capabilities
        )
        read_count = sum(
            item.access is CapabilityAccess.READ for item in self.capabilities
        )
        if len(names) != len(set(names)) or len(identifiers) != len(set(identifiers)):
            raise ValueError("ActionCapability 名称或 ID 重复")
        if (
            self.ontology_action_count != write_count
            or self.legacy_read_capability_count != read_count
            or self.capability_count != len(self.capabilities)
        ):
            raise ValueError("ActionCapability 目录计数不一致")
        if any(
            item.ontology_id != self.ontology_id
            or item.ontology_version != self.ontology_version
            or item.ontology_digest != self.ontology_digest
            for item in self.capabilities
        ):
            raise ValueError("ActionCapability 目录混入其他本体版本")
        writes = {item.name for item in self.capabilities if item.access is CapabilityAccess.WRITE}
        for item in self.capabilities:
            if item.suggested_write_action and item.suggested_write_action not in writes:
                raise ValueError(
                    f"只读 capability 建议了不存在的 WRITE：{item.name}"
                )
        return self

    def by_name(self) -> dict[str, ActionCapability]:
        return {item.name: item.model_copy(deep=True) for item in self.capabilities}

    def safe_summary(self) -> dict[str, Any]:
        """Additive operational summary; deliberately excludes schemas/bindings."""

        return {
            "contract_version": self.contract_version,
            "ontology_id": self.ontology_id,
            "ontology_version": self.ontology_version,
            "ontology_digest": self.ontology_digest,
            "ontology_action_count": self.ontology_action_count,
            "legacy_read_capability_count": self.legacy_read_capability_count,
            "capability_count": self.capability_count,
            "read_count": self.legacy_read_capability_count,
            "write_count": self.ontology_action_count,
            "agent_write_execution_enabled": False,
        }


class ActionCapabilityContractError(RuntimeError):
    def __init__(self, errors: list[str]):
        self.errors = list(dict.fromkeys(errors))
        super().__init__("；".join(self.errors) or "ActionCapability 合同无效")


PERSONAL_CREDIT_READ_CAPABILITY_NAMES = (
    "inspect_case_readiness",
    "preview_risk_assessment",
    "generate_verification_questions",
    "generate_decision_support",
)

_EVIDENCE_OBJECT_TYPES = {
    "loan_application": "LoanApplication",
    "borrower_income": "NaturalPerson",
    "vehicle_valuation": "Vehicle",
    "credit_report": "CreditReport",
    "external_debts": "ExternalDebt",
    "anti_fraud_report": "AntiFraudReport",
    "risk_assessment": "RiskAssessmentReport",
    "phone_verification": "PhoneVerificationRecord",
}

_READ_ONTOLOGY_REFERENCES: dict[str, tuple[tuple[str, str], ...]] = {
    "inspect_case_readiness": tuple(
        ("ObjectType", name) for name in _EVIDENCE_OBJECT_TYPES.values()
    ),
    "preview_risk_assessment": (
        ("Function", "calculate_dti"),
        ("Function", "calculate_ltv"),
        ("Function", "evaluate_credit_risk"),
        ("Function", "evaluate_fraud_risk"),
        ("Function", "assess_application_risk"),
    ),
    "generate_verification_questions": (
        ("Function", "generate_phone_questions"),
    ),
    "generate_decision_support": (
        ("Function", "aggregate_decision_support"),
        ("Function", "build_evidence_explanation"),
    ),
}

_READ_RISK = {
    "inspect_case_readiness": CapabilityRisk.LOW,
    "preview_risk_assessment": CapabilityRisk.MEDIUM,
    "generate_verification_questions": CapabilityRisk.MEDIUM,
    "generate_decision_support": CapabilityRisk.HIGH,
}

_READ_SUGGESTED_WRITE_ACTION = {
    "inspect_case_readiness": None,
    "preview_risk_assessment": "generate_risk_assessment",
    "generate_verification_questions": "create_phone_verification_task",
    "generate_decision_support": "record_decision_recommendation",
}

_WRITE_EVIDENCE: dict[str, tuple[tuple[str, tuple[str, ...], str], ...]] = {
    "create_loan_application": (
        ("validated_application_payload", ("NaturalPerson", "LoanApplication", "Vehicle", "Dealer"), "申请、主体、车辆和经销商载荷满足进件公理"),
    ),
    "request_credit_report": (
        ("borrower_consent", ("NaturalPerson", "LoanApplication"), "存在当前申请借款人的有效征信授权"),
        ("subject_identity", ("NaturalPerson",), "主体证件格式有效且与当前申请一致"),
    ),
    "request_fraud_report": (
        ("complete_application_core_relations", ("LoanApplication", "NaturalPerson"), "当前申请核心关系完整"),
    ),
    "generate_risk_assessment": tuple(
        (key, (object_type,), f"{key} 证据完整")
        for key, object_type in _EVIDENCE_OBJECT_TYPES.items()
        if key not in {"risk_assessment", "phone_verification"}
    ),
    "create_phone_verification_task": (
        ("loan_application", ("LoanApplication",), "当前申请存在"),
        ("risk_assessment", ("RiskAssessmentReport",), "风险评估信号触发电核"),
        ("anti_fraud_report", ("AntiFraudReport",), "反欺诈信号已纳入问题生成"),
    ),
    "record_phone_verification": (
        ("assigned_phone_verification_task", ("PhoneVerificationRecord",), "电核任务存在且分配给当前回调主体"),
        ("validated_phone_result", ("PhoneVerificationRecord",), "结果字段满足电核约束"),
    ),
    "trigger_manual_review": (
        ("loan_application", ("LoanApplication",), "当前申请存在"),
        ("risk_assessment", ("RiskAssessmentReport",), "风险证据完整"),
        ("anti_fraud_report", ("AntiFraudReport",), "反欺诈证据完整"),
        ("phone_verification", ("PhoneVerificationRecord",), "电核证据已纳入判断"),
        ("manual_review_rule_match", ("LoanApplication",), "至少命中一条转人工公理"),
    ),
    "record_decision_recommendation": (
        ("loan_application", ("LoanApplication",), "当前申请存在"),
        ("risk_assessment", ("RiskAssessmentReport",), "风险评估报告完整"),
        ("anti_fraud_report", ("AntiFraudReport",), "反欺诈报告完整"),
        ("phone_verification", ("PhoneVerificationRecord",), "电核记录完整"),
        ("human_confirmation", ("LoanApplication",), "风险经理或有权信审人员完成可信确认"),
    ),
}

_WRITE_PRECONDITION_BINDINGS: dict[
    str, dict[str, tuple[tuple[str, ...], tuple[str, ...]]]
] = {
    "create_loan_application": {
        "PersonalIdentity、AssessableVehicle和ApplicationIntegrity校验通过": (
            ("validated_application_payload",),
            ("validate_axioms",),
        ),
    },
    "request_credit_report": {
        "存在有效借款人授权": (
            ("borrower_consent",),
            ("validate_consent",),
        ),
        "身份证号格式有效": (
            ("subject_identity",),
            ("validate_consent",),
        ),
    },
    "request_fraud_report": {
        "申请核心关系完整": (
            ("complete_application_core_relations",),
            ("validate_core_relations",),
        ),
    },
    "generate_risk_assessment": {
        "申请、征信、反欺诈及车辆数据满足公理": (
            (
                "loan_application",
                "borrower_income",
                "vehicle_valuation",
                "credit_report",
                "external_debts",
                "anti_fraud_report",
            ),
            ("validate_assessment_axioms",),
        ),
    },
    "create_phone_verification_task": {
        "风险信号触发电核条件": (
            ("loan_application", "risk_assessment", "anti_fraud_report"),
            ("validate_phone_trigger",),
        ),
    },
    "record_phone_verification": {
        "电核任务存在": (
            ("assigned_phone_verification_task",),
            ("validate_assignment",),
        ),
        "结果字段满足电核约束": (
            ("validated_phone_result",),
            ("validate_phone_result",),
        ),
    },
    "trigger_manual_review": {
        "至少命中一条转人工公理": (
            (
                "loan_application",
                "risk_assessment",
                "anti_fraud_report",
                "phone_verification",
                "manual_review_rule_match",
            ),
            ("validate_manual_review_rule", "validate_review_evidence"),
        ),
    },
    "record_decision_recommendation": {
        "必要报告和校验已完成": (
            (
                "loan_application",
                "risk_assessment",
                "anti_fraud_report",
                "phone_verification",
            ),
            ("validate_required_reports",),
        ),
        "高风险规则不得直接通过": (
            ("risk_assessment",),
            ("validate_recommendation",),
        ),
    },
}

_WRITE_PARAMETER_SOURCES: dict[
    str, dict[str, CapabilityParameterSource]
] = {
    "create_loan_application": {
        "application_id": CapabilityParameterSource.SERVER_CONTEXT,
        "application": CapabilityParameterSource.TRUSTED_USER,
        "borrower": CapabilityParameterSource.TRUSTED_USER,
        "vehicle": CapabilityParameterSource.TRUSTED_USER,
        "dealer": CapabilityParameterSource.TRUSTED_USER,
        "guarantor": CapabilityParameterSource.TRUSTED_USER,
        "co_borrower": CapabilityParameterSource.TRUSTED_USER,
        "request_id": CapabilityParameterSource.SERVER_CONTEXT,
    },
    "request_credit_report": {
        "application_id": CapabilityParameterSource.SERVER_CONTEXT,
        "subject_id_number": CapabilityParameterSource.TRUSTED_USER,
        "authorization_reference": CapabilityParameterSource.TRUSTED_USER,
        "query_purpose": CapabilityParameterSource.SERVER_CONTEXT,
        "request_id": CapabilityParameterSource.SERVER_CONTEXT,
    },
    "request_fraud_report": {
        "application_id": CapabilityParameterSource.SERVER_CONTEXT,
        "request_id": CapabilityParameterSource.SERVER_CONTEXT,
    },
    "generate_risk_assessment": {
        "application_id": CapabilityParameterSource.SERVER_CONTEXT,
        "assessment_version": CapabilityParameterSource.SERVER_CONTEXT,
        "data_snapshot_policy": CapabilityParameterSource.SERVER_CONTEXT,
        "request_id": CapabilityParameterSource.SERVER_CONTEXT,
    },
    "create_phone_verification_task": {
        "application_id": CapabilityParameterSource.SERVER_CONTEXT,
        "verification_round": CapabilityParameterSource.SERVER_CONTEXT,
        "trigger_reason": CapabilityParameterSource.MODEL_PROPOSAL,
        "request_id": CapabilityParameterSource.SERVER_CONTEXT,
    },
    "record_phone_verification": {
        "verification_id": CapabilityParameterSource.SERVER_CONTEXT,
        "callback_id": CapabilityParameterSource.SERVER_CONTEXT,
        "verifier_identity": CapabilityParameterSource.TRUSTED_CONFIRMATION,
        "call_time": CapabilityParameterSource.TRUSTED_USER,
        "duration_seconds": CapabilityParameterSource.TRUSTED_USER,
        "conclusion": CapabilityParameterSource.TRUSTED_USER,
        "doubts": CapabilityParameterSource.TRUSTED_USER,
        "recording_file_id": CapabilityParameterSource.TRUSTED_USER,
    },
    "trigger_manual_review": {
        "application_id": CapabilityParameterSource.SERVER_CONTEXT,
        "review_reason": CapabilityParameterSource.MODEL_PROPOSAL,
        "source_execution_id": CapabilityParameterSource.SERVER_CONTEXT,
        "request_id": CapabilityParameterSource.SERVER_CONTEXT,
    },
    "record_decision_recommendation": {
        "application_id": CapabilityParameterSource.SERVER_CONTEXT,
        "recommendation_version": CapabilityParameterSource.SERVER_CONTEXT,
        "approver_id": CapabilityParameterSource.TRUSTED_CONFIRMATION,
        "approval_assertion": CapabilityParameterSource.TRUSTED_CONFIRMATION,
        "request_id": CapabilityParameterSource.SERVER_CONTEXT,
    },
}

_READ_PARAMETER_SOURCES = {
    "application_id": CapabilityParameterSource.SERVER_CONTEXT,
    "ontology_version": CapabilityParameterSource.SERVER_CONTEXT,
}

def _string(*, nullable: bool = False) -> dict[str, Any]:
    base = {"type": "string"}
    return {"anyOf": [base, {"type": "null"}]} if nullable else base


def _number(*, nullable: bool = False) -> dict[str, Any]:
    base = {"type": "number"}
    return {"anyOf": [base, {"type": "null"}]} if nullable else base


def _integer(*, nullable: bool = False) -> dict[str, Any]:
    base = {"type": "integer"}
    return {"anyOf": [base, {"type": "null"}]} if nullable else base


def _boolean(*, nullable: bool = False) -> dict[str, Any]:
    base = {"type": "boolean"}
    return {"anyOf": [base, {"type": "null"}]} if nullable else base


def _scalar(*, nullable: bool = False) -> dict[str, Any]:
    choices = [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]
    if nullable:
        choices.append({"type": "null"})
    return {"anyOf": choices}


def _array(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": deepcopy(items)}


def _object(
    properties: Mapping[str, dict[str, Any]],
    *,
    required: tuple[str, ...] | list[str] | None = None,
    additional: Any = False,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": deepcopy(dict(properties)),
        "required": list(required if required is not None else properties.keys()),
        "additionalProperties": deepcopy(additional),
    }


def _root_object(
    properties: Mapping[str, dict[str, Any]],
    *,
    required: tuple[str, ...] | list[str] | None = None,
    additional: Any = False,
) -> dict[str, Any]:
    return {
        "$schema": JSON_SCHEMA_DRAFT,
        **_object(properties, required=required, additional=additional),
    }


_LINEAGE_SCHEMA = _object(
    {
        "postgres_snapshot_id": _string(nullable=True),
        "postgres_snapshot_time": _string(nullable=True),
        "data_binding_version": _string(nullable=True),
        "data_binding_digest": _string(nullable=True),
        "read_only": _boolean(),
    }
)

_READ_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "inspect_case_readiness": _root_object({
        "application_id": _string(),
        "ontology_version": _string(),
        "status": _string(),
        "snapshot_available": _boolean(),
        "snapshot_error": _string(nullable=True),
        "available_evidence": _array(_string()),
        "missing_evidence": _array(_string()),
        "evidence": _object({}, required=(), additional={
            "type": "object",
            "properties": {
                "available": _boolean(),
                "label": _string(),
            },
            "required": ["available", "label"],
            "additionalProperties": False,
        }),
        "available_tools": _array(_string()),
        "blocked_tools": _object({}, required=(), additional={
            "type": "array",
            "items": _string(),
        }),
        "data_binding_version": _string(),
        "data_binding_digest": _string(nullable=True),
        "postgres_snapshot_id": _string(nullable=True),
        "postgres_snapshot_time": _string(nullable=True),
    }),
    "preview_risk_assessment": _root_object({
        "risk_level": _string(nullable=True),
        "pd_value": _number(nullable=True),
        "check_result": _string(nullable=True),
        "risk_factors": _array(_object({
            "factor": _string(nullable=True),
            "value": _scalar(nullable=True),
            "contribution": _number(nullable=True),
            "evidence_source": _string(nullable=True),
        }, required=("factor", "contribution", "evidence_source"))),
        "model": _object({
            "name": _string(),
            "version": _string(),
            "calibrated_pd": _boolean(),
        }),
        "calculation_policy": _string(nullable=True),
        "decision_boundary": _string(nullable=True),
        "data_lineage": _LINEAGE_SCHEMA,
    }),
    "generate_verification_questions": _root_object({
        "phone_questions": _array(_object({
            "question_id": _string(),
            "category": _string(),
            "question": _string(),
            "source_factors": _array(_string()),
            "required": _boolean(),
        })),
        "question_count": _integer(),
        "template_version": _string(nullable=True),
        "data_lineage": _LINEAGE_SCHEMA,
    }),
    "generate_decision_support": _root_object({
        "recommended_decision": _string(nullable=True),
        "decision_reasons": _array(_object({
            "code": _string(nullable=True),
            "description": _string(nullable=True),
        })),
        "decision_boundary": _string(nullable=True),
        "rule_version": _string(nullable=True),
        "evidence_chain": _array(_object({}, required=(), additional=True)),
        "lineage": _object({}, required=(), additional=True),
        "data_lineage": _LINEAGE_SCHEMA,
    }),
}


_PERSISTENCE_SCHEMA = _object(
    {
        "postgres": _string(nullable=True),
        "nebulagraph": _string(nullable=True),
        "outbox_event_id": _string(),
        "outbox_status": _string(nullable=True),
        "next_attempt_at": _string(nullable=True),
    },
    required=("postgres", "nebulagraph", "outbox_event_id"),
    additional=True,
)

_WRITE_RESULT_PROPERTIES: dict[str, dict[str, dict[str, Any]]] = {
    "create_loan_application": {
        "application_id": _string(),
        "application_status": _string(),
        "created_relations": _array(_string()),
        "entity_operations": _object({}, required=(), additional=True),
        "persistence": _PERSISTENCE_SCHEMA,
        "decision_boundary": _string(),
        "warning": _string(nullable=True),
    },
    "request_credit_report": {
        "application_id": _string(),
        "report_id": _string(),
        "provider_request_id": _string(),
        "provider_name": _string(nullable=True),
        "provider_mode": _string(nullable=True),
        "synthetic": _boolean(),
        "subject_role": _string(nullable=True),
        "created_relations": _array(_string()),
        "persistence": _PERSISTENCE_SCHEMA,
        "decision_boundary": _string(),
        "warning": _string(nullable=True),
    },
    "request_fraud_report": {
        "application_id": _string(),
        "fraud_report_id": _string(),
        "provider_request_id": _string(),
        "provider_name": _string(nullable=True),
        "provider_mode": _string(nullable=True),
        "synthetic": _boolean(),
        "association_risk_summary": _object({}, required=(), additional=True),
        "created_relations": _array(_string()),
        "persistence": _PERSISTENCE_SCHEMA,
        "decision_boundary": _string(),
        "warning": _string(nullable=True),
    },
    "generate_risk_assessment": {
        "application_id": _string(),
        "assessment_version": _string(),
        "assessment_id": _string(),
        "risk_level": _string(),
        "pd_value": _number(),
        "check_result": _string(),
        "evidence_summary": _object({}, required=(), additional=True),
        "persistence": _PERSISTENCE_SCHEMA,
        "decision_boundary": _string(),
        "warnings": _array(_string()),
    },
    "create_phone_verification_task": {
        "application_id": _string(),
        "verification_id": _string(),
        "verification_round": _integer(),
        "question_count": _integer(),
        "template_version": _string(nullable=True),
        "dispatch_id": _string(nullable=True),
        "dispatch_status": _string(),
        "external_task_id": _string(nullable=True),
        "provider_name": _string(nullable=True),
        "provider_mode": _string(nullable=True),
        "synthetic": _boolean(),
        "provider_response_cached": _boolean(),
        "verified_trigger_signals": _array(_string()),
        "created_relations": _array(_string()),
        "persistence": _PERSISTENCE_SCHEMA,
        "decision_boundary": _string(),
        "warning": _string(nullable=True),
    },
    "record_phone_verification": {
        "application_id": _string(),
        "verification_id": _string(),
        "callback_id": _string(),
        "record_status": _string(),
        "conclusion": _string(nullable=True),
        "persistence": _PERSISTENCE_SCHEMA,
        "decision_boundary": _string(),
        "warning": _string(nullable=True),
    },
    "trigger_manual_review": {
        "application_id": _string(),
        "application_status": _string(),
        "routing_result": _string(),
        "review_id": _string(),
        "review_evidence_reference": _string(),
        "matched_rules": _array(_string()),
        "source_execution_id": _string(),
        "persistence": _PERSISTENCE_SCHEMA,
        "decision_boundary": _string(),
        "warning": _string(nullable=True),
    },
    "record_decision_recommendation": {
        "application_id": _string(),
        "recommendation_id": _string(),
        "recommendation_version": _string(),
        "recommended_decision": _string(),
        "decision_authority": _string(),
        "evidence_reference": _string(),
        "reason_codes": _array(_string()),
        "approver_id": _string(),
        "approver_role": _string(),
        "application_status": _string(nullable=True),
        "persistence": _PERSISTENCE_SCHEMA,
        "decision_boundary": _string(),
        "warning": _string(nullable=True),
    },
}

_WRITE_RESULT_REQUIRED: dict[str, tuple[str, ...]] = {
    name: tuple(field for field in properties if field != "warning")
    for name, properties in _WRITE_RESULT_PROPERTIES.items()
}
_WRITE_RESULT_REQUIRED["create_phone_verification_task"] = tuple(
    field
    for field in _WRITE_RESULT_REQUIRED["create_phone_verification_task"]
    if field not in {
        "external_task_id",
        "provider_name",
        "provider_mode",
        "synthetic",
        "provider_response_cached",
    }
)


def _write_output_schema(action_name: str) -> dict[str, Any]:
    result_schema = _object(
        _WRITE_RESULT_PROPERTIES[action_name],
        required=_WRITE_RESULT_REQUIRED[action_name],
    )
    outbox_schema = _object({
        "event_id": _string(),
        "status": _string(),
        "attempt_count": _integer(),
        "max_attempts": _integer(),
    })
    return _root_object(
        {
            "execution_id": _string(),
            "action_name": {"const": action_name, "type": "string"},
            "status": _string(),
            "ontology_version": _string(),
            "idempotent_replay": _boolean(),
            "result": result_schema,
            "error_code": _string(nullable=True),
            "error_message": _string(nullable=True),
            "audit_reference": _string(),
            "audit_chain_valid": _boolean(),
            "outbox": outbox_schema,
        },
        required=(
            "execution_id",
            "action_name",
            "status",
            "ontology_version",
            "idempotent_replay",
            "result",
            "error_code",
            "error_message",
            "audit_reference",
        ),
    )


def _callable_name(value: Any) -> str:
    module = str(getattr(value, "__module__", "")).strip()
    qualname = str(
        getattr(value, "__qualname__", getattr(value, "__name__", ""))
    ).strip()
    return f"{module}.{qualname}".strip(".")


def _ontology_names(proposal: OntologyProposal) -> dict[str, set[str]]:
    return {
        "ObjectType": {item.name for item in proposal.object_types},
        "ActionType": {item.name for item in proposal.action_types},
        "Function": {item.name for item in proposal.functions},
        "DynamicSecurity": {item.action for item in proposal.dynamic_security},
    }


def _reference(
    ontology_id: str,
    ontology_version: str,
    kind: str,
    name: str,
) -> OntologyCapabilityReference:
    return OntologyCapabilityReference(
        ontology_id=ontology_id,
        ontology_version=ontology_version,
        declaration_kind=kind,
        declaration_name=name,
    )


def _parameter_policies(
    schema: Mapping[str, Any],
    *,
    source_by_field: Mapping[str, CapabilityParameterSource],
    sensitive_fields: tuple[str, ...] = (),
) -> tuple[CapabilityParameterPolicy, ...]:
    sensitive = {item.lower() for item in sensitive_fields}

    def contains_sensitive_name(value: Any, seen: set[str] | None = None) -> bool:
        visited = set() if seen is None else seen
        if isinstance(value, Mapping):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/"):
                if reference in visited:
                    return False
                target: Any = schema
                for segment in reference[2:].split("/"):
                    if not isinstance(target, Mapping):
                        target = None
                        break
                    target = target.get(segment)
                if target is not None and contains_sensitive_name(
                    target, visited | {reference}
                ):
                    return True
            for key, nested in value.items():
                lowered_key = str(key).lower()
                if lowered_key in sensitive or any(
                    token in lowered_key
                    for token in (
                        "id_number",
                        "phone",
                        "address",
                        "recording",
                        "authorization",
                        "consent",
                    )
                ):
                    return True
                if contains_sensitive_name(nested, visited):
                    return True
        elif isinstance(value, (list, tuple)):
            return any(contains_sensitive_name(item, visited) for item in value)
        return False

    properties = schema.get("properties") or {}
    if set(properties) != set(source_by_field):
        raise ValueError("显式参数来源白名单必须精确覆盖输入 Schema")
    policies = []
    for field in properties:
        lowered = field.lower()
        property_schema = properties.get(field, {})
        is_sensitive = (
            lowered in sensitive
            or any(
                token in lowered
                for token in (
                    "id_number",
                    "phone",
                    "address",
                    "recording",
                    "authorization",
                    "consent",
                )
            )
            or contains_sensitive_name(property_schema)
        )
        source = source_by_field[field]
        writable = source is CapabilityParameterSource.MODEL_PROPOSAL
        policies.append(CapabilityParameterPolicy(
            field=field,
            source=source,
            sensitive=is_sensitive,
            model_writable=writable,
        ))
    return tuple(policies)


def _runtime_context_policies(
    *fields: tuple[str, CapabilityParameterSource],
) -> tuple[CapabilityParameterPolicy, ...]:
    return tuple(CapabilityParameterPolicy(
        field=field,
        source=source,
        sensitive=field in {
            "actor_id",
            "actor_role",
            "confirmation_actor_id",
            "confirmation_actor_role",
        },
        model_writable=False,
    ) for field, source in fields)


def _function_bindings(
    registry: FunctionRegistry,
    names: tuple[str, ...] | list[str],
    ontology_functions: Mapping[str, Any],
    errors: list[str],
    *,
    owner: str,
) -> tuple[FunctionRuntimeBinding, ...]:
    bindings = []
    for name in names:
        try:
            described = registry.describe(name)
        except FunctionRegistryError:
            errors.append(f"ActionCapability Function 运行合同不存在：{owner}.{name}")
            continue
        if not described.get("bound") or not described.get("implementation_version"):
            errors.append(f"ActionCapability Function 尚未绑定：{owner}.{name}")
            continue
        declared = ontology_functions.get(name)
        if declared is None or described.get("contract") != declared.model_dump(
            mode="json"
        ):
            errors.append(f"ActionCapability Function 运行定义漂移：{owner}.{name}")
            continue
        bindings.append(FunctionRuntimeBinding(
            function_name=name,
            bound=True,
            implementation_version=str(described["implementation_version"]),
        ))
    return tuple(bindings)


def _execution_safety(design: ActionRuntimeDesign) -> CapabilityExecutionSafety:
    return CapabilityExecutionSafety(
        steps=tuple(CapabilityStepContract(
            name=item.name,
            kind=item.kind,
            depends_on=item.depends_on,
            function_name=item.function_name,
            effect=item.effect,
            external_system=item.external_system,
            timeout_seconds=item.timeout_seconds,
            max_attempts=item.max_attempts,
            failure_policy=item.failure_policy,
        ) for item in design.steps),
        completion_rule=design.completion_rule,
        consistency_boundary=design.consistency_boundary,
        compensation=design.compensation,
        audit_events=design.audit_events,
        error_codes=design.error_codes,
    )


def _write_evidence(
    ontology_id: str,
    version: str,
    action_name: str,
) -> tuple[EvidenceRequirement, ...]:
    return tuple(EvidenceRequirement(
        key=key,
        condition=condition,
        ontology_references=tuple(
            _reference(ontology_id, version, "ObjectType", object_type)
            for object_type in object_types
        ),
    ) for key, object_types, condition in _WRITE_EVIDENCE[action_name])


def _write_precondition_bindings(
    action_name: str,
    preconditions: tuple[str, ...],
    errors: list[str],
) -> tuple[CapabilityPreconditionBinding, ...]:
    configured = _WRITE_PRECONDITION_BINDINGS.get(action_name, {})
    if set(configured) != set(preconditions):
        errors.append(
            f"ActionCapability 前置条件 runtime binding 未精确覆盖：{action_name}"
        )
    return tuple(CapabilityPreconditionBinding(
        precondition=precondition,
        evidence_keys=evidence_keys,
        runtime_guard_ids=runtime_guard_ids,
    ) for precondition, (evidence_keys, runtime_guard_ids) in configured.items())


def _read_evidence(
    ontology_id: str,
    version: str,
    name: str,
    keys: tuple[str, ...],
) -> tuple[EvidenceRequirement, ...]:
    if name == "inspect_case_readiness":
        return ()
    return tuple(EvidenceRequirement(
        key=key,
        condition=f"{key} 证据完整",
        ontology_references=(
            _reference(
                ontology_id,
                version,
                "ObjectType",
                _EVIDENCE_OBJECT_TYPES[key],
            ),
        ),
    ) for key in keys)


def _validation_messages(owner: str, error: ValidationError) -> list[str]:
    return [
        f"ActionCapability 模型校验失败：{owner}."
        f"{'.'.join(str(value) for value in item['loc']) or 'root'}：{item['msg']}"
        for item in error.errors()
    ]


def build_action_capability_catalog(
    *,
    ontology_id: str,
    proposal: OntologyProposal,
    adapter_registry: ReusableActionAdapterRegistry,
    expected_write_bindings: Mapping[str, tuple[Any, Any]],
    read_tools: Mapping[str, Any],
    read_request_model: Any,
    read_executor: Any,
    read_function_registry: FunctionRegistry,
    designs: Mapping[str, ActionRuntimeDesign] | None = None,
) -> ActionCapabilityCatalog:
    """Compile the phase-2 internal contract without changing Agent routing."""

    normalized_ontology_id = str(ontology_id or "").strip().lower()
    version = str(proposal.version or "").strip()
    digest = proposal_digest(proposal)
    active_designs = dict(ACTION_RUNTIME_DESIGNS if designs is None else designs)
    active_read_tools = dict(read_tools)
    expected_bindings = dict(expected_write_bindings)
    errors = validate_action_design_catalog(proposal, active_designs)
    capabilities: list[ActionCapability] = []

    if not normalized_ontology_id:
        errors.append("ActionCapability ontology_id 不能为空")
    if not version:
        errors.append("ActionCapability ontology_version 不能为空")
    action_names = {item.name for item in proposal.action_types}
    if set(expected_bindings) != action_names:
        errors.append("ActionCapability 预期写绑定必须精确覆盖 ActionType")
    if set(active_designs) != action_names:
        errors.append("ActionCapability runtime design 必须精确覆盖 ActionType")
    if set(active_read_tools) != set(PERSONAL_CREDIT_READ_CAPABILITY_NAMES):
        errors.append("ActionCapability legacy READ 必须精确覆盖4项标准能力")
    if not hasattr(read_request_model, "model_json_schema"):
        errors.append("ActionCapability legacy READ 缺少请求 Schema")
    if not callable(read_executor):
        errors.append("ActionCapability legacy READ 执行入口不可调用")

    names_by_kind = _ontology_names(proposal)
    functions = {item.name: item for item in proposal.functions}
    security = {item.action: item for item in proposal.dynamic_security}
    if len(security) != len(proposal.dynamic_security) or set(security) != action_names:
        errors.append("ActionCapability DynamicSecurity 必须与 ActionType 一一对应")

    for action in proposal.action_types:
        design = active_designs.get(action.name)
        policy = security.get(action.name)
        expected = expected_bindings.get(action.name)
        if design is None or policy is None or expected is None:
            if policy is None:
                errors.append(f"ActionCapability 缺少 DynamicSecurity：{action.name}")
            continue
        if not str(action.label or "").strip() or not str(action.description or "").strip():
            errors.append(f"ActionType 标签或描述为空：{action.name}")
            continue
        try:
            binding = adapter_registry.resolve(
                normalized_ontology_id,
                action.name,
                ontology_version=version,
            )
        except ReusableActionNotFound:
            errors.append(f"ActionCapability 缺少 runtime binding：{action.name}")
            continue
        expected_model, expected_executor = expected
        if binding.request_model is not expected_model:
            errors.append(f"ActionCapability 请求模型绑定错误：{action.name}")
        if binding.executor is not expected_executor:
            errors.append(f"ActionCapability executor 绑定错误：{action.name}")
        expected_profile = PERSONAL_CREDIT_ACTION_REUSE_PROFILES.get(action.name)
        if binding.profile != expected_profile:
            errors.append(f"ActionCapability 复用原型绑定错误：{action.name}")
        if binding.ontology_id != normalized_ontology_id:
            errors.append(f"ActionCapability runtime ontology_id 错误：{action.name}")
        request_model = binding.request_model
        if not hasattr(request_model, "model_json_schema"):
            errors.append(f"ActionCapability 缺少输入 Schema：{action.name}")
            continue
        input_schema = deepcopy(request_model.model_json_schema())
        input_schema["$schema"] = JSON_SCHEMA_DRAFT
        if set(input_schema.get("properties") or {}) != set(design.request_fields):
            errors.append(f"ActionCapability 请求 Schema 与运行合同不一致：{action.name}")
        if input_schema.get("additionalProperties") is not False:
            errors.append(f"ActionCapability 写输入必须禁止额外字段：{action.name}")
        for field in design.idempotency_fields:
            if field not in (input_schema.get("properties") or {}):
                errors.append(f"ActionCapability 幂等字段不在输入 Schema：{action.name}.{field}")

        reference_values = [
            ("ActionType", action.name),
            ("DynamicSecurity", action.name),
            *(("ObjectType", item) for item in action.input_types),
            *(("ObjectType", item) for item in action.output_types),
            *(("Function", item) for item in action.invokes_functions),
        ]
        references = tuple(
            _reference(normalized_ontology_id, version, kind, name)
            for kind, name in dict.fromkeys(reference_values)
        )
        function_bindings = _function_bindings(
            read_function_registry,
            action.invokes_functions,
            functions,
            errors,
            owner=action.name,
        )
        try:
            parameter_policies = _parameter_policies(
                input_schema,
                source_by_field=_WRITE_PARAMETER_SOURCES.get(action.name, {}),
                sensitive_fields=tuple(policy.sensitive_fields),
            )
        except ValueError as error:
            errors.append(f"ActionCapability 参数来源合同错误：{action.name}.{error}")
            continue
        precondition_bindings = _write_precondition_bindings(
            action.name,
            tuple(action.preconditions),
            errors,
        )
        try:
            capabilities.append(ActionCapability(
                capability_id=(
                    f"{normalized_ontology_id}:{version}:{digest}:"
                    f"ActionType:{action.name}"
                ),
                name=action.name,
                label=str(action.label),
                description=str(action.description),
                tags=("ontology-action", "write", binding.profile.archetype),
                origin=CapabilityOrigin.ONTOLOGY_ACTION,
                access=CapabilityAccess.WRITE,
                declaration_status=OntologyDeclarationStatus.DECLARED,
                ontology_id=normalized_ontology_id,
                ontology_version=version,
                ontology_digest=digest,
                ontology_references=references,
                ontology_input_types=tuple(action.input_types),
                ontology_output_types=tuple(action.output_types),
                invokes_functions=tuple(action.invokes_functions),
                input_schema=input_schema,
                output_schema=_write_output_schema(action.name),
                runtime_design_response_fields=design.response_fields,
                parameter_policies=parameter_policies,
                runtime_context_policies=_runtime_context_policies(
                    ("ontology_version", CapabilityParameterSource.SERVER_CONTEXT),
                    ("actor_id", CapabilityParameterSource.SERVER_CONTEXT),
                    ("actor_role", CapabilityParameterSource.SERVER_CONTEXT),
                    *(
                        (
                            (
                                "confirmation_actor_id",
                                CapabilityParameterSource.TRUSTED_CONFIRMATION,
                            ),
                            (
                                "confirmation_actor_role",
                                CapabilityParameterSource.TRUSTED_CONFIRMATION,
                            ),
                        )
                        if action.name == "record_decision_recommendation"
                        else ()
                    ),
                ),
                required_evidence=_write_evidence(
                    normalized_ontology_id, version, action.name
                ),
                preconditions=tuple(action.preconditions),
                precondition_bindings=precondition_bindings,
                effects=tuple(action.effects),
                external_systems=(action.external_system,) if action.external_system else (),
                side_effects=True,
                risk=(
                    CapabilityRisk.CRITICAL
                    if policy.approval_required
                    else CapabilityRisk.HIGH
                ),
                confirmation_policy=CapabilityConfirmationPolicy(
                    mode=ConfirmationMode.PROPOSAL_ONLY,
                    required=True,
                    proposal_only=True,
                    agent_execution_enabled=False,
                    revalidate_on_execute=True,
                    ontology_approval_required=policy.approval_required,
                    approval_condition=design.approval_condition,
                ),
                idempotency_fields=design.idempotency_fields,
                runtime_binding=CapabilityRuntimeBinding(
                    kind=RuntimeBindingKind.REUSABLE_ACTION_ADAPTER,
                    binding_id=(
                        f"{binding.ontology_id}:{binding.ontology_version}:"
                        f"{binding.action_name}"
                    ),
                    bound=True,
                    implementation_version=f"adapter:{binding.ontology_version}",
                    request_model=_callable_name(binding.request_model),
                    executor=_callable_name(binding.executor),
                    archetype=binding.profile.archetype,
                    reuse_mode=binding.profile.reuse_mode,
                    function_bindings=function_bindings,
                ),
                security_policy=CapabilitySecurityPolicy(
                    source="ontology_dynamic_security",
                    allowed_roles=tuple(policy.allowed_roles),
                    denied_roles=tuple(policy.denied_roles),
                    execution_boundary=str(policy.execution_boundary or ""),
                    sensitive_fields=tuple(policy.sensitive_fields),
                    conditions=tuple(policy.conditions),
                    audit_required=bool(action.audit_required and policy.audit_required),
                ),
                execution_safety=_execution_safety(design),
                decision_support_only=action.name == "record_decision_recommendation",
                final_approval=False,
            ))
        except ValidationError as error:
            errors.extend(_validation_messages(action.name, error))

    if hasattr(read_request_model, "model_json_schema"):
        read_input_schema = deepcopy(read_request_model.model_json_schema())
        read_input_schema["$schema"] = JSON_SCHEMA_DRAFT
    else:
        read_input_schema = _root_object({}, required=())
    if set(read_input_schema.get("properties") or {}) != {
        "application_id",
        "ontology_version",
    }:
        errors.append("ActionCapability legacy READ 请求 Schema 字段不受控")
    if read_input_schema.get("additionalProperties") is not False:
        errors.append("ActionCapability legacy READ 输入必须禁止额外字段")
    read_properties = read_input_schema.get("properties") or {}
    application_schema = read_properties.get("application_id") or {}
    ontology_version_schema = read_properties.get("ontology_version") or {}
    if (
        application_schema.get("type") != "string"
        or application_schema.get("minLength") != 1
        or application_schema.get("maxLength") != 32
        or set(read_input_schema.get("required") or ()) != {"application_id"}
    ):
        errors.append("ActionCapability application_id 只读参数约束被放宽")
    ontology_choices = ontology_version_schema.get("anyOf") or []
    if (
        ontology_version_schema.get("default", object()) is not None
        or {item.get("type") for item in ontology_choices} != {"string", "null"}
        or not any(
            item.get("type") == "string" and item.get("maxLength") == 64
            for item in ontology_choices
        )
    ):
        errors.append("ActionCapability ontology_version 只读参数约束被放宽")
    try:
        read_parameter_policies = _parameter_policies(
            read_input_schema,
            source_by_field=_READ_PARAMETER_SOURCES,
        )
    except ValueError as error:
        errors.append(f"ActionCapability legacy READ 参数来源合同错误：{error}")
        read_parameter_policies = ()
    for name in PERSONAL_CREDIT_READ_CAPABILITY_NAMES:
        tool = active_read_tools.get(name)
        if tool is None:
            continue
        if str(getattr(tool, "name", "")) != name:
            errors.append(f"legacy READ key/name 不一致：{name}")
        if getattr(tool, "suggested_write_action", None) != (
            _READ_SUGGESTED_WRITE_ACTION[name]
        ):
            errors.append(f"legacy READ 建议写 Action 映射漂移：{name}")
        label = str(getattr(tool, "label", "") or "").strip()
        description = str(getattr(tool, "description", "") or "").strip()
        if not label or not description:
            errors.append(f"legacy READ 标签或描述为空：{name}")
            continue
        reference_values = list(_READ_ONTOLOGY_REFERENCES[name])
        evidence_keys = tuple(getattr(tool, "required_evidence", ()))
        if name == "inspect_case_readiness":
            ontology_input_types = tuple(_EVIDENCE_OBJECT_TYPES.values())
        else:
            unknown_evidence = set(evidence_keys) - set(_EVIDENCE_OBJECT_TYPES)
            if unknown_evidence:
                errors.append(
                    f"legacy READ 使用未知证据键：{name}."
                    + ",".join(sorted(unknown_evidence))
                )
            ontology_input_types = tuple(
                _EVIDENCE_OBJECT_TYPES[key]
                for key in evidence_keys
                if key in _EVIDENCE_OBJECT_TYPES
            )
            reference_values.extend(
                ("ObjectType", item) for item in ontology_input_types
            )
        references = tuple(
            _reference(normalized_ontology_id, version, kind, item)
            for kind, item in dict.fromkeys(reference_values)
        )
        invoked_functions = tuple(
            item
            for kind, item in _READ_ONTOLOGY_REFERENCES[name]
            if kind == "Function"
        )
        function_bindings = _function_bindings(
            read_function_registry,
            invoked_functions,
            functions,
            errors,
            owner=name,
        )
        for function_name in invoked_functions:
            function = functions.get(function_name)
            if function is None or function.side_effects:
                errors.append(
                    f"legacy READ 引用了未知或有副作用 Function：{name}.{function_name}"
                )
        try:
            capabilities.append(ActionCapability(
                capability_id=(
                    f"{normalized_ontology_id}:{version}:{digest}:"
                    f"LegacyReadTool:{name}"
                ),
                name=name,
                label=label,
                description=description,
                tags=("legacy-read-tool", "read", str(tool.result_type)),
                origin=CapabilityOrigin.LEGACY_READ_TOOL,
                access=CapabilityAccess.READ,
                declaration_status=OntologyDeclarationStatus.PROJECTED,
                ontology_id=normalized_ontology_id,
                ontology_version=version,
                ontology_digest=digest,
                ontology_references=references,
                ontology_input_types=ontology_input_types,
                ontology_output_types=(),
                invokes_functions=invoked_functions,
                input_schema=deepcopy(read_input_schema),
                output_schema=deepcopy(_READ_OUTPUT_SCHEMAS[name]),
                runtime_design_response_fields=(),
                parameter_policies=read_parameter_policies,
                runtime_context_policies=_runtime_context_policies(
                    ("ontology_version", CapabilityParameterSource.SERVER_CONTEXT),
                    ("actor", CapabilityParameterSource.SERVER_CONTEXT),
                    ("snapshot", CapabilityParameterSource.SERVER_CONTEXT),
                ),
                required_evidence=_read_evidence(
                    normalized_ontology_id, version, name, evidence_keys
                ),
                preconditions=(
                    ("案件快照不可用时返回 BLOCKED，不执行其他读取",)
                    if name == "inspect_case_readiness"
                    else tuple(f"required_evidence:{item}" for item in evidence_keys)
                ),
                effects=(),
                external_systems=(),
                side_effects=False,
                risk=_READ_RISK[name],
                confirmation_policy=CapabilityConfirmationPolicy(
                    mode=ConfirmationMode.NONE,
                    required=False,
                    proposal_only=False,
                    agent_execution_enabled=True,
                    revalidate_on_execute=False,
                    ontology_approval_required=False,
                    approval_condition=None,
                ),
                idempotency_fields=(),
                runtime_binding=CapabilityRuntimeBinding(
                    kind=RuntimeBindingKind.LEGACY_READ_SERVICE,
                    binding_id=f"legacy-read:{name}",
                    bound=callable(read_executor),
                    implementation_version="legacy-read-v1",
                    request_model=_callable_name(read_request_model),
                    executor=_callable_name(read_executor),
                    archetype="read_projection",
                    reuse_mode="legacy_adapter",
                    function_bindings=function_bindings,
                ),
                security_policy=CapabilitySecurityPolicy(
                    source="legacy_agent_session",
                    execution_boundary="仅允许读取当前申请的一致性快照",
                    sensitive_fields=(),
                    audit_required=False,
                ),
                execution_safety=None,
                suggested_write_action=getattr(tool, "suggested_write_action", None),
                decision_support_only=name == "generate_decision_support",
                final_approval=False,
            ))
        except ValidationError as error:
            errors.extend(_validation_messages(name, error))

    for capability in capabilities:
        for reference in capability.ontology_references:
            if reference.declaration_name not in names_by_kind.get(
                reference.declaration_kind, set()
            ):
                errors.append(
                    "ActionCapability 本体引用不存在："
                    f"{capability.name}.{reference.declaration_kind}."
                    f"{reference.declaration_name}"
                )
        for requirement in capability.required_evidence:
            for reference in requirement.ontology_references:
                if reference.declaration_name not in names_by_kind.get(
                    reference.declaration_kind, set()
                ):
                    errors.append(
                        "ActionCapability 证据引用不存在："
                        f"{capability.name}.{requirement.key}."
                        f"{reference.declaration_name}"
                    )

    if errors:
        raise ActionCapabilityContractError(errors)
    if len(capabilities) != 12:
        raise ActionCapabilityContractError([
            "ActionCapability 目录必须为8个 WRITE 加4个 legacy READ"
        ])
    try:
        return ActionCapabilityCatalog(
            ontology_id=normalized_ontology_id,
            ontology_version=version,
            ontology_digest=digest,
            ontology_action_count=8,
            legacy_read_capability_count=4,
            capability_count=12,
            capabilities=tuple(capabilities),
        )
    except ValidationError as error:
        raise ActionCapabilityContractError(
            _validation_messages("catalog", error)
        ) from error
