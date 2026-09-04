from __future__ import annotations

import hashlib
import hmac
import json
import re
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from ..action.capabilities import (
    ACTION_CAPABILITY_CONTRACT_VERSION,
    JSON_SCHEMA_DRAFT,
    PERSONAL_CREDIT_READ_CAPABILITY_NAMES,
    ActionCapability,
    ActionCapabilityCatalog,
    CapabilityAccess,
    CapabilityOrigin,
    CapabilityParameterSource,
    OntologyCapabilityReference,
    OntologyDeclarationStatus,
)
from ..ontology.audit import proposal_digest
from .ontology_context import (
    OntologyContextError,
    OntologyContextProvider,
    PublishedOntologyContext,
    assert_model_safe_payload,
    safe_ontology_text,
)


AGENT_CAPABILITY_MANIFEST_VERSION = "1.0"
PERSONAL_CREDIT_READ_PROJECTION_VERSION = "personal-credit-read-projection-v1"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MODEL_REASON_FIELDS = {
    "create_phone_verification_task": "trigger_reason",
    "trigger_manual_review": "review_reason",
}
_MODEL_SCHEMA_FORBIDDEN_KEYS = frozenset({
    "$defs",
    "$ref",
    "title",
    "default",
    "pattern",
})
_ARGUMENT_KEY_FORBIDDEN_PARTS = (
    "application",
    "version",
    "request",
    "actor",
    "approver",
    "approval",
    "principal",
    "snapshot",
    "subject",
    "borrower",
    "id_number",
    "idnumber",
    "phone",
    "address",
    "income",
    "credit_report",
    "password",
    "secret",
    "token",
    "sql",
    "cypher",
    "query",
)
_MANIFEST_FORBIDDEN_KEYS = frozenset({
    "applicationid",
    "snapshotid",
    "requestid",
    "actorid",
    "actorrole",
    "approverid",
    "principalid",
    "iamroles",
    "dynamicsecurity",
    "securitypolicy",
    "allowedroles",
    "deniedroles",
    "runtimebinding",
    "runtimecontextpolicies",
    "functionbindings",
    "inputschema",
    "outputschema",
    "requestmodel",
    "executor",
    "sourcetable",
    "sourcecolumn",
    "datasource",
    "idnumber",
    "phonenumber",
    "authorizationreference",
    "sql",
    "cypher",
})
_MANIFEST_MACHINE_VALUE_KEYS = frozenset({
    "capabilityid",
    "catalogdigest",
    "contextid",
    "databindingdigest",
    "manifestid",
    "ontologydigest",
    "principalscopedigest",
    "readprojectionattestationid",
})


def _canonical_sha256(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _capability_contract_digest(capability: ActionCapability) -> str:
    return _canonical_sha256(capability.model_dump(mode="json"))


def _attestation_key(value: bytes | str) -> bytes:
    if isinstance(value, str):
        normalized = value.encode("utf-8")
    elif isinstance(value, bytes):
        normalized = bytes(value)
    else:
        raise TypeError("READ projection attestation key 类型无效")
    if len(normalized) < 32:
        raise ValueError("READ projection attestation key 长度不足")
    return normalized


def _attestation_signature(payload: Mapping[str, Any], key: bytes) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "rpas_" + hmac.new(key, encoded, hashlib.sha256).hexdigest()


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _assert_manifest_safe_payload(payload: Mapping[str, Any]) -> None:
    """Scan the strict manifest while allowing JSON Schema `properties` only."""

    item_count = 0

    def visit(value: Any, path: tuple[str, ...], depth: int) -> None:
        nonlocal item_count
        if depth > 12:
            _reject("AGENT_CAPABILITY_CATALOG_REJECTED", "manifest 嵌套过深")
        item_count += 1
        if item_count > 4096:
            _reject("AGENT_CAPABILITY_CATALOG_REJECTED", "manifest 条目过多")
        if isinstance(value, Mapping):
            for raw_key, nested in value.items():
                if not isinstance(raw_key, str):
                    _reject(
                        "AGENT_CAPABILITY_CATALOG_REJECTED",
                        "manifest 包含非字符串字段名",
                    )
                normalized = _normalized_key(raw_key)
                is_schema_properties = (
                    raw_key == "properties"
                    and bool(path)
                    and path[-1] == "model_input_schema"
                )
                if normalized in _MANIFEST_FORBIDDEN_KEYS or (
                    raw_key == "properties" and not is_schema_properties
                ):
                    _reject(
                        "AGENT_CAPABILITY_CATALOG_REJECTED",
                        "manifest 包含内部或敏感字段",
                    )
                if raw_key in _MODEL_SCHEMA_FORBIDDEN_KEYS:
                    _reject(
                        "AGENT_CAPABILITY_CATALOG_REJECTED",
                        "manifest Schema 包含禁止关键字",
                    )
                safe_ontology_text(
                    raw_key,
                    field="manifest.key",
                    max_length=128,
                )
                if is_schema_properties:
                    if not isinstance(nested, Mapping) or not set(nested).issubset(
                        set(_MODEL_REASON_FIELDS.values())
                    ):
                        _reject(
                            "AGENT_CAPABILITY_CATALOG_REJECTED",
                            "manifest Schema 暴露了非模型参数",
                        )
                visit(nested, path + (raw_key,), depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for nested in value:
                visit(nested, path, depth + 1)
            return
        if isinstance(value, str):
            leaf_key = _normalized_key(path[-1]) if path else ""
            if leaf_key in _MANIFEST_MACHINE_VALUE_KEYS:
                try:
                    assert_model_safe_payload({path[-1]: value})
                except OntologyContextError as exc:
                    raise AgentCapabilityRegistryError(
                        "AGENT_CAPABILITY_CATALOG_REJECTED",
                        f"manifest 机器标识校验失败: {exc.code}",
                    ) from exc
                return
            safe_ontology_text(
                value,
                field="manifest.value",
                max_length=8192,
                allow_empty=True,
            )
            return
        if value is None or isinstance(value, (bool, int, float)):
            return
        _reject(
            "AGENT_CAPABILITY_CATALOG_REJECTED",
            "manifest 包含不支持的数据类型",
        )

    visit(payload, (), 0)


class AgentCapabilityRegistryError(RuntimeError):
    """Fail-closed registry error whose public form never exposes internals."""

    _PUBLIC_MESSAGES = {
        "AGENT_CAPABILITY_CONTEXT_REJECTED": "当前本体上下文不可用于能力发现",
        "AGENT_CAPABILITY_CATALOG_REJECTED": "当前能力目录未通过后端校验",
        "AGENT_CAPABILITY_ATTESTATION_REJECTED": "只读能力发布证明无效",
        "AGENT_CAPABILITY_POLICY_REJECTED": "能力访问策略无效",
        "AGENT_CAPABILITY_PRINCIPAL_REJECTED": "当前认证主体不可使用 Agent 能力",
        "AGENT_CAPABILITY_MANIFEST_STALE": "能力清单已失效，请重新规划",
        "AGENT_CAPABILITY_NOT_AVAILABLE": "所选能力当前不可用",
        "AGENT_CAPABILITY_ARGUMENTS_REJECTED": "模型参数未通过后端校验",
        "AGENT_CAPABILITY_DECISION_REJECTED": "所选 Agent 决策不被允许",
        "AGENT_WRITE_EXECUTION_DISABLED": "Agent 写操作执行当前未开放",
    }

    def __init__(self, code: str, internal_detail: str = ""):
        self.code = code
        self.internal_detail = internal_detail
        self.message = self._PUBLIC_MESSAGES.get(
            code, "Agent 能力请求未通过安全校验"
        )
        super().__init__(self.message)

    def public(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _reject(code: str, detail: str = "") -> None:
    raise AgentCapabilityRegistryError(code, detail)


class AgentAuthenticationSource(str, Enum):
    LOCAL_SESSION = "LOCAL_SESSION"
    API_CLIENT = "API_CLIENT"
    IAM = "IAM"


class AgentCapabilityDecision(str, Enum):
    CALL_READ_ACTIONS = "CALL_READ_ACTIONS"
    PROPOSE_WRITE_ACTION = "PROPOSE_WRITE_ACTION"
    EXECUTE_WRITE_ACTION = "EXECUTE_WRITE_ACTION"


class AgentPrincipalContext(BaseModel):
    """Authenticated, case-scoped principal supplied only by the backend."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str
    authentication_source: AgentAuthenticationSource
    authenticated: bool
    application_id: str
    snapshot_id: str
    iam_roles: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_principal(self):
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                self.principal_id,
                self.application_id,
                self.snapshot_id,
            )
        ):
            raise ValueError("主体及案件作用域不能为空")
        if len(self.principal_id) > 256:
            raise ValueError("主体标识过长")
        if len(self.iam_roles) != len(set(self.iam_roles)) or any(
            not isinstance(role, str) or not role.strip()
            for role in self.iam_roles
        ):
            raise ValueError("IAM 角色必须唯一且非空")
        if (
            self.authentication_source is not AgentAuthenticationSource.IAM
            and self.iam_roles
        ):
            raise ValueError("本地/API 会话不得伪造 MVP 或 IAM 角色")
        return self

    def scope_digest(self) -> str:
        return _canonical_sha256(self.model_dump(mode="json"))


class AgentCapabilityAllowlist(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: str
    read_names: tuple[str, ...]
    write_proposal_names: tuple[str, ...]

    @model_validator(mode="after")
    def validate_allowlist(self):
        if not self.revision.strip():
            raise ValueError("allowlist revision 不能为空")
        for values in (self.read_names, self.write_proposal_names):
            if len(values) != len(set(values)) or any(
                not isinstance(name, str) or not name.strip() for name in values
            ):
                raise ValueError("allowlist 名称必须唯一且非空")
        if set(self.read_names) & set(self.write_proposal_names):
            raise ValueError("READ 与 WRITE Proposal 白名单不得重叠")
        return self


class AgentDynamicPolicy(BaseModel):
    """Deployment policy independent from ontology DynamicSecurity roles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: str
    allowed_names: tuple[str, ...]
    denied_names: tuple[str, ...] = ()
    write_proposal_authentication_sources: tuple[
        AgentAuthenticationSource, ...
    ] = ()

    @model_validator(mode="after")
    def validate_policy(self):
        if not self.revision.strip():
            raise ValueError("dynamic policy revision 不能为空")
        for values in (self.allowed_names, self.denied_names):
            if len(values) != len(set(values)) or any(
                not isinstance(name, str) or not name.strip() for name in values
            ):
                raise ValueError("dynamic policy 名称必须唯一且非空")
        if len(self.write_proposal_authentication_sources) != len(
            set(self.write_proposal_authentication_sources)
        ):
            raise ValueError("写 Proposal 认证来源不得重复")
        return self


class ReadProjectionContractAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    contract_digest: str

    @model_validator(mode="after")
    def validate_contract(self):
        if self.name not in PERSONAL_CREDIT_READ_CAPABILITY_NAMES:
            raise ValueError("证明包含未知 READ 投影")
        if not _DIGEST_RE.fullmatch(self.contract_digest):
            raise ValueError("READ 完整合同摘要格式无效")
        return self


class PublishedReadProjectionAttestation(BaseModel):
    """Independent publication attestation for the four projected READ tools."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attestation_id: str
    ontology_id: str
    ontology_version: str
    ontology_digest: str
    projection_version: str
    contracts: tuple[ReadProjectionContractAttestation, ...]
    signature: str

    @staticmethod
    def _identity_payload(
        *,
        ontology_id: str,
        ontology_version: str,
        ontology_digest: str,
        projection_version: str,
        contracts: Sequence[ReadProjectionContractAttestation],
    ) -> dict[str, Any]:
        return {
            "ontology_id": ontology_id,
            "ontology_version": ontology_version,
            "ontology_digest": ontology_digest,
            "projection_version": projection_version,
            "contracts": [
                item.model_dump(mode="json")
                for item in sorted(contracts, key=lambda value: value.name)
            ],
        }

    @model_validator(mode="after")
    def validate_attestation(self):
        if self.projection_version != PERSONAL_CREDIT_READ_PROJECTION_VERSION:
            raise ValueError("READ projection version 不受支持")
        if not _DIGEST_RE.fullmatch(self.ontology_digest):
            raise ValueError("本体摘要格式无效")
        names = tuple(item.name for item in self.contracts)
        if (
            len(names) != len(set(names))
            or set(names) != set(PERSONAL_CREDIT_READ_CAPABILITY_NAMES)
        ):
            raise ValueError("证明必须精确覆盖四个 READ 投影")
        payload = self._identity_payload(
            ontology_id=self.ontology_id,
            ontology_version=self.ontology_version,
            ontology_digest=self.ontology_digest,
            projection_version=self.projection_version,
            contracts=self.contracts,
        )
        expected = "rpa_" + _canonical_sha256(payload)
        if self.attestation_id != expected:
            raise ValueError("READ projection attestation 身份摘要不一致")
        if not re.fullmatch(r"rpas_[0-9a-f]{64}", self.signature):
            raise ValueError("READ projection attestation 签名格式无效")
        return self

    @classmethod
    def _catalog_attestation_payload(
        cls,
        context: PublishedOntologyContext,
        catalog: ActionCapabilityCatalog,
    ) -> tuple[
        tuple[ReadProjectionContractAttestation, ...],
        dict[str, Any],
    ]:
        try:
            checked_catalog = ActionCapabilityCatalog.model_validate(
                catalog.model_dump(mode="python")
            )
        except (AttributeError, ValidationError) as exc:
            raise AgentCapabilityRegistryError(
                "AGENT_CAPABILITY_ATTESTATION_REJECTED",
                "无法从未通过验证的目录签发 READ 投影证明",
            ) from exc
        if (
            checked_catalog.ontology_id != context.ontology_id
            or checked_catalog.ontology_version != context.ontology_version
            or checked_catalog.ontology_digest != context.ontology_digest
        ):
            _reject(
                "AGENT_CAPABILITY_ATTESTATION_REJECTED",
                "READ 投影证明与发布上下文不一致",
            )
        reads = tuple(
            item
            for item in checked_catalog.capabilities
            if item.access is CapabilityAccess.READ
        )
        if set(item.name for item in reads) != set(
            PERSONAL_CREDIT_READ_CAPABILITY_NAMES
        ):
            _reject(
                "AGENT_CAPABILITY_ATTESTATION_REJECTED",
                "READ 投影合同集合不完整",
            )
        if any(
            item.origin is not CapabilityOrigin.LEGACY_READ_TOOL
            or item.declaration_status is not OntologyDeclarationStatus.PROJECTED
            for item in reads
        ):
            _reject(
                "AGENT_CAPABILITY_ATTESTATION_REJECTED",
                "READ 投影不得伪装为已声明能力",
            )
        contracts = tuple(
            ReadProjectionContractAttestation(
                name=item.name,
                contract_digest=_capability_contract_digest(item),
            )
            for item in sorted(reads, key=lambda value: value.name)
        )
        payload = cls._identity_payload(
            ontology_id=context.ontology_id,
            ontology_version=context.ontology_version,
            ontology_digest=context.ontology_digest,
            projection_version=PERSONAL_CREDIT_READ_PROJECTION_VERSION,
            contracts=contracts,
        )
        return contracts, payload

    @classmethod
    def sign_for_publication(
        cls,
        context: PublishedOntologyContext,
        catalog: ActionCapabilityCatalog,
        *,
        signing_key: bytes | str,
    ) -> "PublishedReadProjectionAttestation":
        """Sign a controlled publication artifact; never call per request."""

        key = _attestation_key(signing_key)
        contracts, payload = cls._catalog_attestation_payload(context, catalog)
        return cls(
            attestation_id="rpa_" + _canonical_sha256(payload),
            ontology_id=context.ontology_id,
            ontology_version=context.ontology_version,
            ontology_digest=context.ontology_digest,
            projection_version=PERSONAL_CREDIT_READ_PROJECTION_VERSION,
            contracts=contracts,
            signature=_attestation_signature(payload, key),
        )

    def verify(
        self,
        context: PublishedOntologyContext,
        catalog: ActionCapabilityCatalog,
        *,
        verification_key: bytes | str,
    ) -> None:
        """Verify content and its independent trust-root signature."""

        key = _attestation_key(verification_key)
        contracts, expected_identity = self._catalog_attestation_payload(
            context, catalog
        )
        expected_payload = {
            "attestation_id": "rpa_" + _canonical_sha256(expected_identity),
            "ontology_id": context.ontology_id,
            "ontology_version": context.ontology_version,
            "ontology_digest": context.ontology_digest,
            "projection_version": PERSONAL_CREDIT_READ_PROJECTION_VERSION,
            "contracts": [item.model_dump(mode="json") for item in contracts],
        }
        supplied_payload = self.model_dump(mode="json")
        supplied_signature = supplied_payload.pop("signature")
        if supplied_payload != expected_payload or not hmac.compare_digest(
            supplied_signature,
            _attestation_signature(expected_identity, key),
        ):
            _reject(
                "AGENT_CAPABILITY_ATTESTATION_REJECTED",
                "READ projection publication artifact 或签名不匹配",
            )

    def by_name(self) -> dict[str, ReadProjectionContractAttestation]:
        return {item.name: item.model_copy(deep=True) for item in self.contracts}


class ModelCapabilityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    condition: str


class ModelOntologyReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    name: str


class ModelConfirmationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required: bool
    proposal_only: bool
    revalidate_on_execute: bool
    ontology_approval_required: bool


class ModelCapabilityManifestItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str
    name: str
    label: str
    description: str
    operation: CapabilityAccess
    risk: str
    required_evidence: tuple[ModelCapabilityEvidence, ...] = ()
    proposal_only: bool
    confirmation: ModelConfirmationPolicy
    model_input_schema: dict[str, Any]
    ontology_references: tuple[ModelOntologyReference, ...] = ()
    suggested_write_action: str | None = None
    decision_support_only: bool
    final_approval: bool = False

    @model_validator(mode="after")
    def validate_model_boundary(self):
        if self.final_approval:
            raise ValueError("模型清单不得声明最终审批")
        identity = self.capability_id.split(":")
        expected_kind = (
            "ActionType"
            if self.operation is CapabilityAccess.WRITE
            else "LegacyReadTool"
        )
        if (
            len(identity) != 5
            or identity[0] != "personal-credit"
            or not identity[1].strip()
            or not _DIGEST_RE.fullmatch(identity[2])
            or identity[3] != expected_kind
            or identity[4] != self.name
        ):
            raise ValueError("模型能力身份格式或操作类型不一致")
        schema = self.model_input_schema
        if schema.get("$schema") != JSON_SCHEMA_DRAFT:
            raise ValueError("模型输入 Schema draft 不正确")
        if schema.get("type") != "object" or schema.get(
            "additionalProperties"
        ) is not False:
            raise ValueError("模型输入 Schema 必须是封闭 object")
        serialized = json.dumps(schema, ensure_ascii=False).lower()
        if any(key.lower() in serialized for key in _MODEL_SCHEMA_FORBIDDEN_KEYS):
            raise ValueError("模型输入 Schema 包含不允许的元数据")
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ValueError("模型输入 Schema 结构无效")
        if set(required) != set(properties):
            raise ValueError("模型输入字段必须全部为显式必填字段")
        expected_field = _MODEL_REASON_FIELDS.get(self.name)
        expected_fields = {expected_field} if expected_field else set()
        if set(properties) != expected_fields:
            raise ValueError("模型输入 Schema 未遵循精确字段白名单")
        for specification in properties.values():
            if specification != {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
            }:
                raise ValueError("模型原因字段 Schema 被放宽")
        if self.operation is CapabilityAccess.READ:
            if self.proposal_only or self.confirmation.proposal_only:
                raise ValueError("READ 不得标记为 Proposal")
        elif self.operation is CapabilityAccess.WRITE:
            if not self.proposal_only or not self.confirmation.proposal_only:
                raise ValueError("WRITE 只能以 Proposal 形式暴露")
        return self


class AgentCapabilityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_version: str = AGENT_CAPABILITY_MANIFEST_VERSION
    manifest_id: str
    context: dict[str, str]
    catalog_digest: str
    read_projection_attestation_id: str
    allowlist_revision: str
    policy_revision: str
    principal_scope_digest: str
    capabilities: tuple[ModelCapabilityManifestItem, ...]
    direct_write_execution_enabled: bool = False
    final_approval: bool = False

    @model_validator(mode="after")
    def validate_manifest(self):
        if self.manifest_version != AGENT_CAPABILITY_MANIFEST_VERSION:
            raise ValueError("manifest version 不受支持")
        if not re.fullmatch(r"acm_[0-9a-f]{64}", self.manifest_id):
            raise ValueError("manifest_id 格式无效")
        if not _DIGEST_RE.fullmatch(self.catalog_digest):
            raise ValueError("catalog_digest 格式无效")
        if not _DIGEST_RE.fullmatch(self.principal_scope_digest):
            raise ValueError("principal_scope_digest 格式无效")
        if not re.fullmatch(r"rpa_[0-9a-f]{64}", self.read_projection_attestation_id):
            raise ValueError("READ projection attestation identity 格式无效")
        if not self.allowlist_revision.strip() or not self.policy_revision.strip():
            raise ValueError("manifest policy revision 不能为空")
        expected_context_keys = {
            "context_id",
            "ontology_id",
            "ontology_version",
            "ontology_digest",
            "data_binding_digest",
        }
        if set(self.context) != expected_context_keys:
            raise ValueError("manifest context 字段集合无效")
        if (
            not re.fullmatch(r"ctx_[A-Za-z0-9_-]{43}", self.context["context_id"])
            or self.context["ontology_id"] != "personal-credit"
            or not self.context["ontology_version"].strip()
            or not _DIGEST_RE.fullmatch(self.context["ontology_digest"])
            or not _DIGEST_RE.fullmatch(self.context["data_binding_digest"])
        ):
            raise ValueError("manifest context 身份格式无效")
        names = [item.name for item in self.capabilities]
        identifiers = [item.capability_id for item in self.capabilities]
        if len(names) != len(set(names)) or len(identifiers) != len(
            set(identifiers)
        ):
            raise ValueError("manifest capability 重复")
        if self.direct_write_execution_enabled or self.final_approval:
            raise ValueError("Agent 不得直接写入或最终审批")
        return self

    def by_name(self) -> dict[str, ModelCapabilityManifestItem]:
        return {item.name: item.model_copy(deep=True) for item in self.capabilities}

    def model_payload(self) -> dict[str, Any]:
        # The manifest has a deliberately rebuilt JSON Schema whose structural
        # key `properties` is not accepted by the generic ontology-slice key
        # scanner. All free text is scanned while schema structure is enforced
        # by ModelCapabilityManifestItem above.
        # Pydantic frozen models do not freeze nested schema/context dicts.
        # Revalidate and rescan immediately before anything reaches a model.
        try:
            checked = AgentCapabilityManifest.model_validate(
                self.model_dump(mode="python")
            )
        except ValidationError as exc:
            raise AgentCapabilityRegistryError(
                "AGENT_CAPABILITY_MANIFEST_STALE",
                "模型清单在输出前发生嵌套合同漂移",
            ) from exc
        payload = checked.model_dump(mode="json")
        # Kept only for backend stale-manifest comparison. This digest is
        # derived from principal/case/snapshot scope and must not become a
        # correlatable model-visible pseudonymous identifier.
        payload.pop("principal_scope_digest", None)
        _assert_manifest_safe_payload(payload)
        return payload


class AuthorizedCapabilityDecision(BaseModel):
    """Validated intent only. Deliberately contains no executor or binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_id: str
    context_id: str
    capability_id: str
    capability_name: str
    decision: AgentCapabilityDecision
    model_arguments: dict[str, str]
    proposal_only: bool
    direct_execution_allowed: bool = False
    final_approval: bool = False

    @model_validator(mode="after")
    def validate_authorization_boundary(self):
        if self.direct_execution_allowed or self.final_approval:
            raise ValueError("授权结果不得允许直接写入或最终审批")
        if (
            self.decision is AgentCapabilityDecision.PROPOSE_WRITE_ACTION
            and not self.proposal_only
        ):
            raise ValueError("写决策必须保持 proposal_only")
        return self


class AgentCapabilityRegistry:
    """Build a fresh, model-safe intersection of ontology and runtime policy."""

    def __init__(
        self,
        *,
        catalog_builder: Callable[[], ActionCapabilityCatalog],
        context_provider: OntologyContextProvider,
        read_projection_attestation: PublishedReadProjectionAttestation,
        read_projection_verification_key: bytes | str,
        allowlist: AgentCapabilityAllowlist,
        dynamic_policy: AgentDynamicPolicy,
    ):
        if not callable(catalog_builder):
            raise TypeError("catalog_builder 必须可调用")
        if not isinstance(context_provider, OntologyContextProvider):
            raise TypeError("context_provider 类型无效")
        self._catalog_builder = catalog_builder
        self._context_provider = context_provider
        self._attestation = PublishedReadProjectionAttestation.model_validate(
            read_projection_attestation.model_dump(mode="python")
        )
        self._read_projection_verification_key = _attestation_key(
            read_projection_verification_key
        )
        self._allowlist = AgentCapabilityAllowlist.model_validate(
            allowlist.model_dump(mode="python")
        )
        self._dynamic_policy = AgentDynamicPolicy.model_validate(
            dynamic_policy.model_dump(mode="python")
        )

    def _fresh_catalog(self) -> ActionCapabilityCatalog:
        try:
            built = self._catalog_builder()
            # Re-serializing and re-validating is intentional. Pydantic's
            # frozen models do not make nested dicts immutable.
            return ActionCapabilityCatalog.model_validate(
                built.model_dump(mode="python")
            )
        except AgentCapabilityRegistryError:
            raise
        except Exception as exc:
            raise AgentCapabilityRegistryError(
                "AGENT_CAPABILITY_CATALOG_REJECTED",
                "fresh catalog builder 或嵌套合同校验失败",
            ) from exc

    @staticmethod
    def _proposal(context: PublishedOntologyContext):
        try:
            proposal = context.proposal_copy()
        except Exception as exc:
            raise AgentCapabilityRegistryError(
                "AGENT_CAPABILITY_CONTEXT_REJECTED",
                "context proposal 无法解析",
            ) from exc
        if proposal_digest(proposal) != context.ontology_digest:
            _reject(
                "AGENT_CAPABILITY_CONTEXT_REJECTED",
                "context proposal digest 不一致",
            )
        return proposal

    def _validate_intersection_inputs(
        self,
        context: PublishedOntologyContext,
        principal: AgentPrincipalContext,
        catalog: ActionCapabilityCatalog,
    ) -> Any:
        if not isinstance(context, PublishedOntologyContext):
            _reject("AGENT_CAPABILITY_CONTEXT_REJECTED", "context 类型无效")
        if not isinstance(principal, AgentPrincipalContext):
            _reject("AGENT_CAPABILITY_PRINCIPAL_REJECTED", "principal 类型无效")
        if not principal.authenticated:
            _reject("AGENT_CAPABILITY_PRINCIPAL_REJECTED", "主体未认证")
        if (
            principal.application_id != context.application_id
            or principal.snapshot_id != context.snapshot_id
        ):
            _reject(
                "AGENT_CAPABILITY_PRINCIPAL_REJECTED",
                "主体案件范围与本体上下文不一致",
            )
        if (
            catalog.contract_version != ACTION_CAPABILITY_CONTRACT_VERSION
            or catalog.ontology_id != context.ontology_id
            or catalog.ontology_version != context.ontology_version
            or catalog.ontology_digest != context.ontology_digest
        ):
            _reject(
                "AGENT_CAPABILITY_CATALOG_REJECTED",
                "live catalog 与发布上下文不一致",
            )
        attestation = self._attestation
        if (
            attestation.ontology_id != context.ontology_id
            or attestation.ontology_version != context.ontology_version
            or attestation.ontology_digest != context.ontology_digest
        ):
            _reject(
                "AGENT_CAPABILITY_ATTESTATION_REJECTED",
                "READ projection attestation 与 context 不一致",
            )
        attestation.verify(
            context,
            catalog,
            verification_key=self._read_projection_verification_key,
        )
        known = {item.name: item for item in catalog.capabilities}
        read_names = {
            name
            for name, item in known.items()
            if item.access is CapabilityAccess.READ
        }
        write_names = {
            name
            for name, item in known.items()
            if item.access is CapabilityAccess.WRITE
        }
        if (
            not set(self._allowlist.read_names).issubset(read_names)
            or not set(self._allowlist.write_proposal_names).issubset(write_names)
        ):
            _reject(
                "AGENT_CAPABILITY_POLICY_REJECTED",
                "allowlist 名称或操作类型与目录不一致",
            )
        all_names = set(known)
        if not set(self._dynamic_policy.allowed_names).issubset(
            all_names
        ) or not set(self._dynamic_policy.denied_names).issubset(all_names):
            _reject(
                "AGENT_CAPABILITY_POLICY_REJECTED",
                "dynamic policy 包含未知能力名称",
            )

        proposal = self._proposal(context)
        action_types = {item.name: item for item in proposal.action_types}
        functions = {item.name: item for item in proposal.functions}
        security = {item.action: item for item in proposal.dynamic_security}
        if len(action_types) != len(proposal.action_types) or len(security) != len(
            proposal.dynamic_security
        ):
            _reject(
                "AGENT_CAPABILITY_CONTEXT_REJECTED",
                "发布本体存在重复 ActionType 或 DynamicSecurity",
            )

        attested = attestation.by_name()
        for name in PERSONAL_CREDIT_READ_CAPABILITY_NAMES:
            capability = known.get(name)
            proof = attested.get(name)
            if capability is None or proof is None:
                _reject(
                    "AGENT_CAPABILITY_ATTESTATION_REJECTED",
                    "READ 投影或证明缺失",
                )
            if (
                capability.access is not CapabilityAccess.READ
                or capability.origin is not CapabilityOrigin.LEGACY_READ_TOOL
                or capability.declaration_status
                is not OntologyDeclarationStatus.PROJECTED
                or capability.side_effects
                or proof.contract_digest != _capability_contract_digest(capability)
            ):
                _reject(
                    "AGENT_CAPABILITY_ATTESTATION_REJECTED",
                    "READ 投影完整合同与证明不一致",
                )
            for reference in capability.ontology_references:
                if (
                    reference.ontology_id != context.ontology_id
                    or reference.ontology_version != context.ontology_version
                ):
                    _reject(
                        "AGENT_CAPABILITY_ATTESTATION_REJECTED",
                        "READ 投影引用越过 context 版本",
                    )
            for function_name in capability.invokes_functions:
                function = functions.get(function_name)
                if function is None or function.side_effects:
                    _reject(
                        "AGENT_CAPABILITY_ATTESTATION_REJECTED",
                        "READ 投影 Function 未声明或包含副作用",
                    )

        for name in write_names:
            capability = known[name]
            action = action_types.get(name)
            policy = security.get(name)
            expected_security = None
            if policy is not None:
                expected_security = {
                    "source": "ontology_dynamic_security",
                    "allowed_roles": list(policy.allowed_roles),
                    "denied_roles": list(policy.denied_roles),
                    "execution_boundary": str(policy.execution_boundary or ""),
                    "sensitive_fields": list(policy.sensitive_fields),
                    "conditions": list(policy.conditions),
                    "audit_required": bool(policy.audit_required),
                }
            if (
                action is None
                or policy is None
                or capability.origin is not CapabilityOrigin.ONTOLOGY_ACTION
                or capability.declaration_status
                is not OntologyDeclarationStatus.DECLARED
                or capability.confirmation_policy.agent_execution_enabled
                or not capability.confirmation_policy.proposal_only
                or capability.confirmation_policy.ontology_approval_required
                is not bool(policy.approval_required)
                or capability.security_policy.model_dump(mode="json")
                != expected_security
                or capability.final_approval
            ):
                _reject(
                    "AGENT_CAPABILITY_CATALOG_REJECTED",
                    "WRITE 未满足发布声明或 Proposal 安全边界",
                )
        return proposal

    def _principal_allows_write(
        self,
        capability: ActionCapability,
        principal: AgentPrincipalContext,
        published_policy: Any,
    ) -> bool:
        if (
            principal.authentication_source
            not in self._dynamic_policy.write_proposal_authentication_sources
        ):
            return False
        if principal.authentication_source is not AgentAuthenticationSource.IAM:
            # Local session and API authentication are authorized by the
            # independent deployment policy, never by MVP adapter role names.
            return True
        roles = set(principal.iam_roles)
        if not roles:
            return False
        denied_roles = set(published_policy.denied_roles)
        allowed_roles = set(published_policy.allowed_roles)
        if roles & denied_roles:
            return False
        # Match the real Action execution service: an empty published allowlist
        # authorizes nobody, never everybody.
        return bool(allowed_roles and roles & allowed_roles)

    def _selected_capabilities(
        self,
        catalog: ActionCapabilityCatalog,
        principal: AgentPrincipalContext,
        proposal: Any,
    ) -> tuple[ActionCapability, ...]:
        allow_read = set(self._allowlist.read_names)
        allow_write = set(self._allowlist.write_proposal_names)
        policy_allow = set(self._dynamic_policy.allowed_names)
        policy_deny = set(self._dynamic_policy.denied_names)
        published_security = {
            item.action: item for item in proposal.dynamic_security
        }
        selected: list[ActionCapability] = []
        for capability in catalog.capabilities:
            if capability.name not in policy_allow or capability.name in policy_deny:
                continue
            if capability.access is CapabilityAccess.READ:
                if capability.name in allow_read:
                    selected.append(capability)
                continue
            if (
                capability.name in allow_write
                and self._principal_allows_write(
                    capability,
                    principal,
                    published_security.get(capability.name),
                )
            ):
                selected.append(capability)
        return tuple(selected)

    @staticmethod
    def _model_input_schema(capability: ActionCapability) -> dict[str, Any]:
        model_fields = tuple(
            item.field
            for item in capability.parameter_policies
            if item.source is CapabilityParameterSource.MODEL_PROPOSAL
            and item.model_writable
        )
        expected_field = _MODEL_REASON_FIELDS.get(capability.name)
        if expected_field is None:
            if model_fields:
                _reject(
                    "AGENT_CAPABILITY_CATALOG_REJECTED",
                    "非预期能力包含模型可写字段",
                )
            properties: dict[str, Any] = {}
        else:
            if model_fields != (expected_field,):
                _reject(
                    "AGENT_CAPABILITY_CATALOG_REJECTED",
                    "模型原因字段合同发生漂移",
                )
            properties = {
                expected_field: {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                }
            }
        return {
            "$schema": JSON_SCHEMA_DRAFT,
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    @staticmethod
    def _model_references(
        capability: ActionCapability,
    ) -> tuple[ModelOntologyReference, ...]:
        allowed_kinds = {"ObjectType", "LinkType", "Function", "ActionType", "Axiom"}
        values = {
            (item.declaration_kind, item.declaration_name)
            for item in capability.ontology_references
            if item.declaration_kind in allowed_kinds
        }
        return tuple(
            ModelOntologyReference(
                kind=safe_ontology_text(kind, field="reference.kind", max_length=64),
                name=safe_ontology_text(name, field="reference.name", max_length=256),
            )
            for kind, name in sorted(values)
        )

    def _manifest_item(
        self,
        capability: ActionCapability,
        selected_names: set[str],
    ) -> ModelCapabilityManifestItem:
        evidence = tuple(
            ModelCapabilityEvidence(
                key=safe_ontology_text(
                    item.key, field="evidence.key", max_length=128
                ),
                condition=safe_ontology_text(
                    item.condition, field="evidence.condition", max_length=1000
                ),
            )
            for item in capability.required_evidence
        )
        suggested = capability.suggested_write_action
        if suggested not in selected_names:
            suggested = None
        confirmation = capability.confirmation_policy
        item = ModelCapabilityManifestItem(
            # The stage-2 model already validates this structured identity.
            # Do not run opaque SHA-256 material through prose PII regexes.
            capability_id=capability.capability_id,
            name=safe_ontology_text(capability.name, field="name", max_length=128),
            label=safe_ontology_text(capability.label, field="label", max_length=256),
            description=safe_ontology_text(
                capability.description, field="description", max_length=1200
            ),
            operation=capability.access,
            risk=capability.risk.value,
            required_evidence=evidence,
            proposal_only=bool(confirmation.proposal_only),
            confirmation=ModelConfirmationPolicy(
                required=bool(confirmation.required),
                proposal_only=bool(confirmation.proposal_only),
                revalidate_on_execute=bool(confirmation.revalidate_on_execute),
                ontology_approval_required=bool(
                    confirmation.ontology_approval_required
                ),
            ),
            model_input_schema=self._model_input_schema(capability),
            ontology_references=self._model_references(capability),
            suggested_write_action=suggested,
            decision_support_only=bool(capability.decision_support_only),
            final_approval=False,
        )
        # Scan all free-form fields without submitting JSON Schema structural
        # keys to the ontology slice scanner.
        safe_values = item.model_dump(mode="json")
        safe_values.pop("model_input_schema", None)
        assert_model_safe_payload(safe_values)
        return item

    def build_manifest(
        self,
        *,
        context: PublishedOntologyContext,
        principal: AgentPrincipalContext,
    ) -> AgentCapabilityManifest:
        catalog = self._fresh_catalog()
        proposal = self._validate_intersection_inputs(context, principal, catalog)
        selected = self._selected_capabilities(catalog, principal, proposal)
        selected_names = {item.name for item in selected}

        references = tuple(
            reference
            for capability in selected
            for reference in capability.ontology_references
            if reference.declaration_kind
            in {"ObjectType", "LinkType", "Function", "ActionType", "Axiom"}
        )
        try:
            # This validates declaration existence, context version isolation,
            # side-effect flags and the safe slice before a manifest is issued.
            self._context_provider.build_model_slice(context, references)
        except OntologyContextError as exc:
            raise AgentCapabilityRegistryError(
                "AGENT_CAPABILITY_CONTEXT_REJECTED",
                f"ontology slice rejected: {exc.code}",
            ) from exc

        items = tuple(
            self._manifest_item(capability, selected_names)
            for capability in selected
        )
        catalog_digest = _canonical_sha256(catalog)
        principal_digest = principal.scope_digest()
        context_identity = context.model_identity()
        identity_payload = {
            "namespace": "personal-credit-agent-capability-manifest-v1",
            "context": context_identity,
            "catalog_digest": catalog_digest,
            "read_projection_attestation": self._attestation.model_dump(
                mode="json"
            ),
            "allowlist": self._allowlist.model_dump(mode="json"),
            "dynamic_policy": self._dynamic_policy.model_dump(mode="json"),
            "principal_scope_digest": principal_digest,
            "capabilities": [item.model_dump(mode="json") for item in items],
        }
        manifest = AgentCapabilityManifest(
            manifest_id="acm_" + _canonical_sha256(identity_payload),
            context=context_identity,
            catalog_digest=catalog_digest,
            read_projection_attestation_id=self._attestation.attestation_id,
            allowlist_revision=self._allowlist.revision,
            policy_revision=self._dynamic_policy.revision,
            principal_scope_digest=principal_digest,
            capabilities=items,
            direct_write_execution_enabled=False,
            final_approval=False,
        )
        # The strict DTO omits executors, runtime bindings, security roles,
        # database mappings and all case data by construction.
        manifest.model_payload()
        return manifest

    @staticmethod
    def validate_model_arguments(
        manifest: AgentCapabilityManifest,
        *,
        capability_name: str,
        arguments: Mapping[str, Any] | None,
    ) -> dict[str, str]:
        try:
            checked_manifest = AgentCapabilityManifest.model_validate(
                manifest.model_dump(mode="python")
            )
            checked_manifest.model_payload()
        except (AttributeError, ValidationError, AgentCapabilityRegistryError) as exc:
            raise AgentCapabilityRegistryError(
                "AGENT_CAPABILITY_MANIFEST_STALE",
                "参数校验收到的 manifest 无效或已被修改",
            ) from exc
        capability = checked_manifest.by_name().get(str(capability_name or ""))
        if capability is None:
            _reject("AGENT_CAPABILITY_NOT_AVAILABLE", "manifest 中没有所选能力")
        if arguments is None:
            raw: dict[str, Any] = {}
        elif isinstance(arguments, Mapping):
            raw = dict(arguments)
        else:
            _reject("AGENT_CAPABILITY_ARGUMENTS_REJECTED", "参数不是 object")
        if any(not isinstance(key, str) for key in raw):
            _reject("AGENT_CAPABILITY_ARGUMENTS_REJECTED", "参数名不是字符串")
        lowered = {key.lower() for key in raw}
        if any(
            forbidden in key
            for key in lowered
            for forbidden in _ARGUMENT_KEY_FORBIDDEN_PARTS
        ):
            _reject("AGENT_CAPABILITY_ARGUMENTS_REJECTED", "参数包含系统或敏感字段")
        if any(isinstance(value, (Mapping, list, tuple, set)) for value in raw.values()):
            _reject("AGENT_CAPABILITY_ARGUMENTS_REJECTED", "参数包含嵌套结构")

        schema = capability.model_input_schema
        properties = schema["properties"]
        required = set(schema["required"])
        if set(raw) != set(properties) or not required.issubset(raw):
            _reject("AGENT_CAPABILITY_ARGUMENTS_REJECTED", "参数字段集合不匹配")
        normalized: dict[str, str] = {}
        for key, value in raw.items():
            specification = properties[key]
            if not isinstance(value, str):
                _reject("AGENT_CAPABILITY_ARGUMENTS_REJECTED", "模型字段必须为字符串")
            text = value.strip()
            if not (
                specification["minLength"]
                <= len(text)
                <= specification["maxLength"]
            ):
                _reject("AGENT_CAPABILITY_ARGUMENTS_REJECTED", "模型字段长度不合法")
            normalized[key] = text
        try:
            assert_model_safe_payload(normalized)
        except OntologyContextError as exc:
            raise AgentCapabilityRegistryError(
                "AGENT_CAPABILITY_ARGUMENTS_REJECTED",
                f"模型参数安全扫描失败: {exc.code}",
            ) from exc
        return normalized

    def authorize_decision(
        self,
        *,
        context: PublishedOntologyContext,
        principal: AgentPrincipalContext,
        manifest: AgentCapabilityManifest,
        decision: AgentCapabilityDecision | str,
        capability_name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> AuthorizedCapabilityDecision:
        try:
            selected_decision = AgentCapabilityDecision(decision)
        except (TypeError, ValueError) as exc:
            raise AgentCapabilityRegistryError(
                "AGENT_CAPABILITY_DECISION_REJECTED",
                "未知决策枚举",
            ) from exc
        if selected_decision is AgentCapabilityDecision.EXECUTE_WRITE_ACTION:
            _reject(
                "AGENT_WRITE_EXECUTION_DISABLED",
                "所有 Agent Action 均禁止直接写执行",
            )
        try:
            supplied = AgentCapabilityManifest.model_validate(
                manifest.model_dump(mode="python")
            )
        except (AttributeError, ValidationError) as exc:
            raise AgentCapabilityRegistryError(
                "AGENT_CAPABILITY_MANIFEST_STALE",
                "调用方 manifest 无效",
            ) from exc

        current = self.build_manifest(context=context, principal=principal)
        if supplied.model_dump(mode="json") != current.model_dump(mode="json"):
            _reject(
                "AGENT_CAPABILITY_MANIFEST_STALE",
                "manifest 身份或内容与 fresh registry 不一致",
            )
        capability = current.by_name().get(str(capability_name or ""))
        if capability is None:
            _reject("AGENT_CAPABILITY_NOT_AVAILABLE", "能力不在当前交集")
        if (
            capability.operation is CapabilityAccess.READ
            and selected_decision is not AgentCapabilityDecision.CALL_READ_ACTIONS
        ) or (
            capability.operation is CapabilityAccess.WRITE
            and selected_decision
            is not AgentCapabilityDecision.PROPOSE_WRITE_ACTION
        ):
            _reject(
                "AGENT_CAPABILITY_DECISION_REJECTED",
                "决策类型与能力操作不匹配",
            )
        validated = self.validate_model_arguments(
            current,
            capability_name=capability.name,
            arguments=arguments,
        )
        return AuthorizedCapabilityDecision(
            manifest_id=current.manifest_id,
            context_id=context.context_id,
            capability_id=capability.capability_id,
            capability_name=capability.name,
            decision=selected_decision,
            model_arguments=validated,
            proposal_only=capability.operation is CapabilityAccess.WRITE,
            direct_execution_allowed=False,
            final_approval=False,
        )


__all__ = (
    "AGENT_CAPABILITY_MANIFEST_VERSION",
    "PERSONAL_CREDIT_READ_PROJECTION_VERSION",
    "AgentAuthenticationSource",
    "AgentCapabilityAllowlist",
    "AgentCapabilityDecision",
    "AgentCapabilityManifest",
    "AgentCapabilityRegistry",
    "AgentCapabilityRegistryError",
    "AgentDynamicPolicy",
    "AgentPrincipalContext",
    "AuthorizedCapabilityDecision",
    "ModelCapabilityEvidence",
    "ModelCapabilityManifestItem",
    "ModelConfirmationPolicy",
    "ModelOntologyReference",
    "PublishedReadProjectionAttestation",
    "ReadProjectionContractAttestation",
)
