from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from ..ontology.audit import proposal_digest
from ..ontology.data_binding import OntologyDataBinding, OntologyDataBindingError
from ..ontology.models import OntologyProposal

if TYPE_CHECKING:
    from ..action.capabilities import OntologyCapabilityReference


PERSONAL_CREDIT_ONTOLOGY_ID = "personal-credit"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SQL_RE = re.compile(
    r"(?is)(?:\b(?:select|insert|update|delete|drop|alter|truncate)\b.{0,120}"
    r"\b(?:from|into|set|table|database|schema)\b|"
    r"\b(?:union\s+select|information_schema|pg_catalog)\b)"
)
_CYPHER_RE = re.compile(
    r"(?is)(?:\bcypher\b|\b(?:match|create|merge)\s*\([^\n]{0,200}\)"
    r"\s*(?:return|where|set|delete|merge|create|-|<)|"
    r"\bdetach\s+delete\b)"
)
_CREDENTIAL_RE = re.compile(
    r"(?is)(?:\b(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|secret|password|"
    r"passwd|bearer)\b\s*[:=]|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\bAKIA[0-9A-Z]{16}\b|\b(?:postgres|mysql|neo4j|bolt)://[^\s/@:]+:[^\s/@]+@)"
)
_PROMPT_INJECTION_RE = re.compile(
    r"(?is)(?:ignore\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions?|prompts?)|"
    r"reveal\s+(?:the\s+)?(?:system|developer)\s+prompt|"
    r"system\s*prompt\s*[:=]|developer\s*message\s*[:=]|"
    r"(?:you\s+are\s+now|act\s+as)\s+(?:the\s+)?(?:system|developer)|"
    r"<\s*system\s*>|\[INST\]|"
    r"忽略(?:以上|此前|之前|所有)(?:指令|提示)|"
    r"(?:泄露|输出|显示)(?:系统|开发者)(?:提示词|指令))"
)
_CHINESE_ID_RE = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?!\d)"
)
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_BANK_CARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[a-z0-9.-]+\.[a-z]{2,}(?![\w.-])")
_OPAQUE_MODEL_VALUE_PATTERNS = {
    "contextid": re.compile(r"^ctx_[A-Za-z0-9_-]{43}$"),
    "ontologydigest": re.compile(r"^[0-9a-f]{64}$"),
    "databindingdigest": re.compile(r"^[0-9a-f]{64}$"),
    "catalogdigest": re.compile(r"^[0-9a-f]{64}$"),
    "principalscopedigest": re.compile(r"^[0-9a-f]{64}$"),
    "manifestid": re.compile(r"^acm_[0-9a-f]{64}$"),
    "readprojectionattestationid": re.compile(r"^rpa_[0-9a-f]{64}$"),
    "capabilityid": re.compile(
        r"^personal-credit:[A-Za-z0-9][A-Za-z0-9._-]{0,63}:"
        r"[0-9a-f]{64}:(?:ActionType|LegacyReadTool):[a-z][a-z0-9_]{0,127}$"
    ),
}

_FORBIDDEN_MODEL_KEYS = frozenset({
    "properties",
    "property",
    "metadata",
    "expression",
    "dynamicsecurity",
    "securitypolicy",
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
    "applicationid",
    "snapshotid",
    "snapshotstartedat",
    "borrowerid",
    "subjectidnumber",
    "idnumber",
    "phonenumber",
    "recordingreference",
    "authorizationreference",
    "systemprompt",
    "developerprompt",
    "prompttemplate",
    "apikey",
    "accesstoken",
    "clientsecret",
    "password",
    "secret",
    "prompt",
    "phone",
    "address",
    "email",
    "vin",
    "recording",
    "authorization",
    "sql",
    "cypher",
})

_FORBIDDEN_MODEL_KEY_FRAGMENTS = (
    "properties",
    "metadata",
    "expression",
    "dynamicsecurity",
    "runtimebinding",
    "requestmodel",
    "executor",
    "sourcetable",
    "sourcecolumn",
    "datasource",
    "applicationid",
    "snapshotid",
    "borrowerid",
    "idnumber",
    "systemprompt",
    "developerprompt",
    "prompttemplate",
    "apikey",
    "accesstoken",
    "clientsecret",
    "sql",
    "cypher",
)


class OntologyContextError(RuntimeError):
    """A published ontology context cannot be safely resolved or exposed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code: str, message: str) -> None:
    raise OntologyContextError(code, message)


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _assert_safe_text(value: str, *, field_name: str) -> None:
    if _CONTROL_RE.search(value):
        _fail("ONTOLOGY_CONTEXT_UNSAFE_TEXT", f"{field_name} 包含不安全控制字符")
    if _SQL_RE.search(value) or _CYPHER_RE.search(value):
        _fail("ONTOLOGY_CONTEXT_UNSAFE_TEXT", f"{field_name} 包含查询语言片段")
    if _CREDENTIAL_RE.search(value):
        _fail("ONTOLOGY_CONTEXT_UNSAFE_TEXT", f"{field_name} 包含凭据片段")
    if _PROMPT_INJECTION_RE.search(value):
        _fail("ONTOLOGY_CONTEXT_UNSAFE_TEXT", f"{field_name} 包含提示注入片段")
    if (
        _CHINESE_ID_RE.search(value)
        or _PHONE_RE.search(value)
        or _BANK_CARD_RE.search(value)
        or _EMAIL_RE.search(value)
    ):
        _fail("ONTOLOGY_CONTEXT_UNSAFE_TEXT", f"{field_name} 包含个人敏感信息")


def safe_ontology_text(
    value: Any,
    *,
    field: str = "ontology_text",
    max_length: int = 1000,
    allow_empty: bool = False,
) -> str:
    """Normalize one ontology-facing string and reject executable or sensitive text."""

    if not isinstance(value, str):
        _fail("ONTOLOGY_CONTEXT_INVALID_TEXT", f"{field} 必须是字符串")
    normalized = value.strip()
    if not normalized and not allow_empty:
        _fail("ONTOLOGY_CONTEXT_INVALID_TEXT", f"{field} 不能为空")
    if len(normalized) > max_length:
        _fail("ONTOLOGY_CONTEXT_LIMIT_EXCEEDED", f"{field} 超出长度限制")
    _assert_safe_text(normalized, field_name=field)
    return normalized


def assert_model_safe_payload(payload: Any) -> None:
    """Fail closed if a would-be model payload contains internal or sensitive data."""

    item_count = 0
    string_bytes = 0

    def visit(value: Any, path: str, depth: int) -> None:
        nonlocal item_count, string_bytes
        if depth > 12:
            _fail("ONTOLOGY_CONTEXT_LIMIT_EXCEEDED", "模型载荷嵌套层级超限")
        item_count += 1
        if item_count > 4096:
            _fail("ONTOLOGY_CONTEXT_LIMIT_EXCEEDED", "模型载荷条目数超限")
        if isinstance(value, Mapping):
            for raw_key, nested in value.items():
                if not isinstance(raw_key, str):
                    _fail("ONTOLOGY_CONTEXT_UNSAFE_KEY", "模型载荷包含非字符串字段名")
                key = _normalized_key(raw_key)
                if key in _FORBIDDEN_MODEL_KEYS or any(
                    fragment in key for fragment in _FORBIDDEN_MODEL_KEY_FRAGMENTS
                ):
                    _fail("ONTOLOGY_CONTEXT_UNSAFE_KEY", "模型载荷包含禁止字段")
                safe_ontology_text(
                    raw_key,
                    field=f"{path}.key",
                    max_length=128,
                )
                visit(nested, f"{path}.{raw_key}", depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for index, nested in enumerate(value):
                visit(nested, f"{path}[{index}]", depth + 1)
            return
        if isinstance(value, str):
            string_bytes += len(value.encode("utf-8"))
            if string_bytes > 128 * 1024:
                _fail("ONTOLOGY_CONTEXT_LIMIT_EXCEEDED", "模型载荷文本总量超限")
            leaf_key = _normalized_key(path.rsplit(".", 1)[-1])
            opaque_pattern = _OPAQUE_MODEL_VALUE_PATTERNS.get(leaf_key)
            if opaque_pattern is not None:
                if not opaque_pattern.fullmatch(value):
                    _fail("ONTOLOGY_CONTEXT_UNSAFE_TEXT", "模型载荷包含无效机器标识")
                # Opaque hashes and HMAC identifiers are shape-validated, not
                # scanned as prose. A valid digest can randomly contain a
                # 16-19 digit run and must not be mistaken for a bank card.
                return
            safe_ontology_text(
                value,
                field=path,
                max_length=8192,
                allow_empty=True,
            )
            return
        if value is None or isinstance(value, (bool, int, float)):
            return
        _fail("ONTOLOGY_CONTEXT_UNSAFE_PAYLOAD", "模型载荷包含不支持的数据类型")

    visit(payload, "payload", 0)


class SafeObjectType(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    label: str | None = None
    description: str | None = None


class SafeLinkType(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    label: str | None = None
    description: str | None = None
    source: str
    target: str
    direction: str
    cardinality: str | None = None
    source_cardinality: str | None = None
    target_cardinality: str | None = None


class SafeFunction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    label: str | None = None
    description: str | None = None
    input_types: tuple[str, ...] = ()
    deterministic: bool
    side_effects: bool


class SafeActionType(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    label: str | None = None
    description: str | None = None
    input_types: tuple[str, ...] = ()
    output_types: tuple[str, ...] = ()
    invokes_functions: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    audit_required: bool
    execution_mode: str


class SafeAxiom(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    description: str | None = None
    severity: str
    validation_phase: str


class OntologyModelSlice(BaseModel):
    """Allowlisted ontology declarations safe for use as model context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context: dict[str, str]
    object_types: tuple[SafeObjectType, ...] = ()
    link_types: tuple[SafeLinkType, ...] = ()
    functions: tuple[SafeFunction, ...] = ()
    action_types: tuple[SafeActionType, ...] = ()
    axioms: tuple[SafeAxiom, ...] = ()

    @classmethod
    def _validated_payload(cls, value: "OntologyModelSlice") -> dict[str, Any]:
        # `frozen=True` does not freeze nested dictionaries. Revalidate the
        # serialized value at the final model boundary so an in-process
        # mutation cannot add case identifiers after construction.
        checked = cls.model_validate(value.model_dump(mode="python"))
        expected_context_keys = {
            "context_id",
            "ontology_id",
            "ontology_version",
            "ontology_digest",
            "data_binding_digest",
        }
        if set(checked.context) != expected_context_keys:
            _fail("ONTOLOGY_CONTEXT_UNSAFE_KEY", "模型上下文字段集合无效")
        return checked.model_dump(mode="json")

    def model_payload(self) -> dict[str, Any]:
        payload = self._validated_payload(self)
        assert_model_safe_payload(payload)
        return payload


@dataclass(frozen=True, slots=True)
class PublishedOntologyContext:
    context_id: str
    ontology_id: str
    ontology_version: str
    ontology_digest: str
    data_binding_digest: str
    application_id: str = field(repr=False)
    snapshot_id: str = field(repr=False)
    _proposal_json: str = field(repr=False, compare=False)

    def proposal_copy(self) -> OntologyProposal:
        return OntologyProposal.model_validate(json.loads(self._proposal_json))

    def safe_identity(self) -> dict[str, str]:
        return {
            "context_id": self.context_id,
            "ontology_id": self.ontology_id,
            "ontology_version": self.ontology_version,
            "ontology_digest": self.ontology_digest,
            "data_binding_digest": self.data_binding_digest,
        }

    def model_identity(self) -> dict[str, str]:
        return self.safe_identity()


def _record_value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(key, default)
    return getattr(record, key, default)


def _snapshot_value(snapshot: Any, key: str, default: Any = None) -> Any:
    if isinstance(snapshot, Mapping):
        return snapshot.get(key, default)
    return getattr(snapshot, key, default)


def _valid_publication_time(value: Any) -> bool:
    if isinstance(value, datetime):
        return value.tzinfo is not None and value.utcoffset() is not None
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    if not normalized:
        return False
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _optional_safe_text(value: Any, *, field_name: str, max_length: int = 1000) -> str | None:
    if value is None:
        return None
    return safe_ontology_text(value, field=field_name, max_length=max_length)


def _safe_text_tuple(
    values: Any,
    *,
    field_name: str,
    max_items: int = 64,
    max_length: int = 512,
) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        _fail("ONTOLOGY_CONTEXT_INVALID_DECLARATION", f"{field_name} 必须是列表")
    if len(values) > max_items:
        _fail("ONTOLOGY_CONTEXT_LIMIT_EXCEEDED", f"{field_name} 条目数超限")
    return tuple(
        safe_ontology_text(
            item,
            field=f"{field_name}[{index}]",
            max_length=max_length,
        )
        for index, item in enumerate(values)
    )


class OntologyContextProvider:
    """Resolve a version/snapshot-bound context and expose only safe ontology slices."""

    def __init__(
        self,
        version_loader: Callable[[str], Any],
        active_loader: Callable[[], Any],
        *,
        secret: bytes | str | None = None,
        hmac_secret: bytes | str | None = None,
        ontology_id: str = PERSONAL_CREDIT_ONTOLOGY_ID,
        max_references: int = 128,
        max_slice_bytes: int = 64 * 1024,
    ):
        if not callable(version_loader) or not callable(active_loader):
            raise TypeError("version_loader 和 active_loader 必须可调用")
        normalized_ontology_id = str(ontology_id or "").strip()
        if normalized_ontology_id != PERSONAL_CREDIT_ONTOLOGY_ID:
            _fail("ONTOLOGY_CONTEXT_SCOPE_UNSUPPORTED", "仅支持个人信审本体")
        if secret is not None and hmac_secret is not None:
            raise TypeError("secret 和 hmac_secret 不能同时提供")
        secret_value = hmac_secret if hmac_secret is not None else secret
        if isinstance(secret_value, str):
            secret_bytes = secret_value.encode("utf-8")
        elif isinstance(secret_value, bytes):
            secret_bytes = secret_value
        else:
            raise TypeError("必须提供 HMAC secret")
        if len(secret_bytes) < 32:
            _fail("ONTOLOGY_CONTEXT_SECRET_INVALID", "HMAC secret 长度不足")
        if not isinstance(max_references, int) or not 1 <= max_references <= 512:
            raise ValueError("max_references 必须在 1 到 512 之间")
        if not isinstance(max_slice_bytes, int) or not 1024 <= max_slice_bytes <= 512 * 1024:
            raise ValueError("max_slice_bytes 必须在 1024 到 524288 之间")
        self._version_loader = version_loader
        self._active_loader = active_loader
        self._secret = bytes(secret_bytes)
        self._ontology_id = normalized_ontology_id
        self._max_references = max_references
        self._max_slice_bytes = max_slice_bytes

    def resolve_context(
        self,
        application_id: str,
        snapshot: Any,
        requested_version: str | None = None,
    ) -> PublishedOntologyContext:
        application = safe_ontology_text(
            application_id,
            field="application_id",
            max_length=32,
        )
        requested = None
        if requested_version is not None:
            requested = safe_ontology_text(
                requested_version,
                field="requested_version",
                max_length=64,
            )
        try:
            record = (
                self._version_loader(requested)
                if requested is not None
                else self._active_loader()
            )
        except OntologyContextError:
            raise
        except Exception as exc:
            code = (
                "ONTOLOGY_CONTEXT_VERSION_NOT_FOUND"
                if requested is not None
                else "ONTOLOGY_CONTEXT_ACTIVE_VERSION_UNAVAILABLE"
            )
            raise OntologyContextError(code, "无法读取已发布本体版本") from exc
        if record is None:
            code = (
                "ONTOLOGY_CONTEXT_VERSION_NOT_FOUND"
                if requested is not None
                else "ONTOLOGY_CONTEXT_ACTIVE_VERSION_MISSING"
            )
            _fail(code, "不存在可用的已发布本体版本")

        raw_record_version = _record_value(record, "version")
        record_version = safe_ontology_text(
            raw_record_version,
            field="ontology_version",
            max_length=64,
        )
        if raw_record_version != record_version:
            _fail("ONTOLOGY_CONTEXT_RECORD_INVALID", "本体发布记录版本格式无效")
        if requested is not None and record_version != requested:
            _fail("ONTOLOGY_CONTEXT_VERSION_MISMATCH", "请求版本与发布记录不一致")
        if _record_value(record, "status") != "published":
            _fail("ONTOLOGY_CONTEXT_VERSION_NOT_PUBLISHED", "本体版本尚未发布")
        if requested is None and _record_value(record, "is_active") is not True:
            _fail("ONTOLOGY_CONTEXT_VERSION_NOT_ACTIVE", "当前本体记录未标记为 active")
        if not _valid_publication_time(_record_value(record, "published_at")):
            _fail("ONTOLOGY_CONTEXT_PUBLICATION_MISSING", "本体发布记录缺少发布时间")
        declared_ontology_id = _record_value(record, "ontology_id")
        if declared_ontology_id is not None and str(declared_ontology_id).strip() != self._ontology_id:
            _fail("ONTOLOGY_CONTEXT_SCOPE_MISMATCH", "本体发布记录不属于个人信审")

        raw_proposal = _record_value(record, "proposal")
        try:
            proposal = (
                raw_proposal.model_copy(deep=True)
                if isinstance(raw_proposal, OntologyProposal)
                else OntologyProposal.model_validate(raw_proposal)
            )
        except Exception as exc:
            raise OntologyContextError(
                "ONTOLOGY_CONTEXT_PROPOSAL_INVALID",
                "本体发布记录中的 proposal 无效",
            ) from exc
        if proposal.version != record_version:
            _fail("ONTOLOGY_CONTEXT_VERSION_MISMATCH", "proposal 与发布记录版本不一致")

        calculated_digest = proposal_digest(proposal)
        stored_digest = _record_value(record, "proposal_digest")
        audit_digest = _record_value(record, "audit_digest")
        if not isinstance(stored_digest, str) or not isinstance(audit_digest, str):
            _fail("ONTOLOGY_CONTEXT_DIGEST_INVALID", "本体发布摘要格式无效")
        if not _DIGEST_RE.fullmatch(stored_digest) or not _DIGEST_RE.fullmatch(audit_digest):
            _fail("ONTOLOGY_CONTEXT_DIGEST_INVALID", "本体发布摘要格式无效")
        if not (
            hmac.compare_digest(calculated_digest, stored_digest)
            and hmac.compare_digest(calculated_digest, audit_digest)
        ):
            _fail("ONTOLOGY_CONTEXT_DIGEST_MISMATCH", "本体内容与发布审核摘要不一致")

        if snapshot is None:
            _fail("ONTOLOGY_CONTEXT_SNAPSHOT_INVALID", "必须提供一致性快照")
        snapshot_application = _snapshot_value(snapshot, "application")
        if not isinstance(snapshot_application, Mapping):
            _fail("ONTOLOGY_CONTEXT_SNAPSHOT_INVALID", "快照缺少申请标识")
        snapshot_application_id = str(snapshot_application.get("application_id") or "").strip()
        if not hmac.compare_digest(
            snapshot_application_id.encode("utf-8"),
            application.encode("utf-8"),
        ):
            _fail(
                "ONTOLOGY_CONTEXT_SNAPSHOT_APPLICATION_MISMATCH",
                "快照与当前申请不匹配",
            )
        snapshot_version = _snapshot_value(snapshot, "data_binding_version")
        if snapshot_version != proposal.version:
            _fail(
                "ONTOLOGY_CONTEXT_DATA_BINDING_VERSION_MISMATCH",
                "快照数据绑定版本与本体不一致",
            )
        snapshot_digest = _snapshot_value(snapshot, "data_binding_digest")
        if not isinstance(snapshot_digest, str):
            _fail(
                "ONTOLOGY_CONTEXT_DATA_BINDING_DIGEST_INVALID",
                "快照数据绑定摘要格式无效",
            )
        if not _DIGEST_RE.fullmatch(snapshot_digest):
            _fail(
                "ONTOLOGY_CONTEXT_DATA_BINDING_DIGEST_INVALID",
                "快照数据绑定摘要格式无效",
            )
        try:
            expected_binding_digest = OntologyDataBinding(proposal).binding_digest
        except OntologyDataBindingError as exc:
            raise OntologyContextError(
                "ONTOLOGY_CONTEXT_DATA_BINDING_INVALID",
                "已发布本体无法构造安全数据绑定",
            ) from exc
        if not hmac.compare_digest(snapshot_digest, expected_binding_digest):
            _fail(
                "ONTOLOGY_CONTEXT_DATA_BINDING_DIGEST_MISMATCH",
                "快照数据绑定摘要与已发布本体不一致",
            )
        snapshot_id = safe_ontology_text(
            _snapshot_value(snapshot, "snapshot_id"),
            field="snapshot_id",
            max_length=256,
        )

        context_id = self._context_id(
            ontology_version=proposal.version,
            ontology_digest=calculated_digest,
            application_id=application,
            snapshot_id=snapshot_id,
            data_binding_digest=expected_binding_digest,
        )
        proposal_json = json.dumps(
            proposal.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return PublishedOntologyContext(
            context_id=context_id,
            ontology_id=self._ontology_id,
            ontology_version=proposal.version,
            ontology_digest=calculated_digest,
            data_binding_digest=expected_binding_digest,
            application_id=application,
            snapshot_id=snapshot_id,
            _proposal_json=proposal_json,
        )

    def build_model_slice(
        self,
        context: PublishedOntologyContext,
        capability_references: Sequence["OntologyCapabilityReference"],
    ) -> OntologyModelSlice:
        if not isinstance(context, PublishedOntologyContext):
            _fail("ONTOLOGY_CONTEXT_INVALID", "context 类型无效")
        context_fields = (
            context.context_id,
            context.ontology_id,
            context.ontology_version,
            context.ontology_digest,
            context.data_binding_digest,
            context.application_id,
            context.snapshot_id,
        )
        if any(not isinstance(value, str) or not value for value in context_fields):
            _fail("ONTOLOGY_CONTEXT_INVALID", "context 字段无效")
        if (
            not _DIGEST_RE.fullmatch(context.ontology_digest)
            or not _DIGEST_RE.fullmatch(context.data_binding_digest)
        ):
            _fail("ONTOLOGY_CONTEXT_INVALID", "context 摘要格式无效")
        if context.ontology_id != self._ontology_id:
            _fail("ONTOLOGY_CONTEXT_SCOPE_MISMATCH", "context 不属于个人信审")
        expected_context_id = self._context_id(
            ontology_version=context.ontology_version,
            ontology_digest=context.ontology_digest,
            application_id=context.application_id,
            snapshot_id=context.snapshot_id,
            data_binding_digest=context.data_binding_digest,
        )
        if not hmac.compare_digest(context.context_id, expected_context_id):
            _fail("ONTOLOGY_CONTEXT_IDENTITY_INVALID", "context 身份校验失败")
        try:
            references = tuple(capability_references)
        except TypeError as exc:
            raise OntologyContextError(
                "ONTOLOGY_CONTEXT_REFERENCE_INVALID",
                "能力引用必须是可枚举序列",
            ) from exc
        if len(references) > self._max_references:
            _fail("ONTOLOGY_CONTEXT_REFERENCE_LIMIT_EXCEEDED", "能力引用数量超限")

        try:
            proposal = context.proposal_copy()
        except Exception as exc:
            raise OntologyContextError(
                "ONTOLOGY_CONTEXT_PROPOSAL_INVALID",
                "context 中的本体副本无效",
            ) from exc
        if proposal_digest(proposal) != context.ontology_digest:
            _fail("ONTOLOGY_CONTEXT_DIGEST_MISMATCH", "context 中的本体副本摘要不一致")
        if proposal.version != context.ontology_version:
            _fail("ONTOLOGY_CONTEXT_VERSION_MISMATCH", "context 本体版本不一致")
        try:
            context_binding_digest = OntologyDataBinding(proposal).binding_digest
        except OntologyDataBindingError as exc:
            raise OntologyContextError(
                "ONTOLOGY_CONTEXT_DATA_BINDING_INVALID",
                "context 中的本体无法构造安全数据绑定",
            ) from exc
        if not hmac.compare_digest(
            context.data_binding_digest,
            context_binding_digest,
        ):
            _fail(
                "ONTOLOGY_CONTEXT_DATA_BINDING_DIGEST_MISMATCH",
                "context 数据绑定摘要不一致",
            )
        indexes = self._declaration_indexes(proposal)
        requested: dict[str, set[str]] = {
            "ObjectType": set(),
            "LinkType": set(),
            "Function": set(),
            "ActionType": set(),
            "Axiom": set(),
        }
        for reference in references:
            ontology_id = str(getattr(reference, "ontology_id", "") or "").strip()
            ontology_version = str(
                getattr(reference, "ontology_version", "") or ""
            ).strip()
            kind = str(getattr(reference, "declaration_kind", "") or "").strip()
            name = safe_ontology_text(
                getattr(reference, "declaration_name", None),
                field="capability_reference.declaration_name",
                max_length=256,
            )
            if ontology_id != context.ontology_id or ontology_version != context.ontology_version:
                _fail(
                    "ONTOLOGY_CONTEXT_REFERENCE_SCOPE_MISMATCH",
                    "能力引用与当前本体 context 不一致",
                )
            if kind not in requested:
                _fail(
                    "ONTOLOGY_CONTEXT_REFERENCE_KIND_UNSUPPORTED",
                    "能力引用类型不在安全白名单中",
                )
            if name not in indexes[kind]:
                _fail("ONTOLOGY_CONTEXT_REFERENCE_UNKNOWN", "能力引用不存在")
            requested[kind].add(name)

        model_slice = OntologyModelSlice(
            context=context.model_identity(),
            object_types=tuple(
                self._safe_object(indexes["ObjectType"][name])
                for name in sorted(requested["ObjectType"])
            ),
            link_types=tuple(
                self._safe_link(indexes["LinkType"][name])
                for name in sorted(requested["LinkType"])
            ),
            functions=tuple(
                self._safe_function(indexes["Function"][name])
                for name in sorted(requested["Function"])
            ),
            action_types=tuple(
                self._safe_action(indexes["ActionType"][name])
                for name in sorted(requested["ActionType"])
            ),
            axioms=tuple(
                self._safe_axiom(indexes["Axiom"][name])
                for name in sorted(requested["Axiom"])
            ),
        )
        payload = model_slice.model_payload()
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > self._max_slice_bytes:
            _fail("ONTOLOGY_CONTEXT_SLICE_LIMIT_EXCEEDED", "本体切片大小超限")
        return model_slice

    def load_context(
        self,
        application_id: str,
        snapshot: Any,
        capability_references: Sequence["OntologyCapabilityReference"],
        requested_version: str | None = None,
    ) -> tuple[PublishedOntologyContext, OntologyModelSlice]:
        context = self.resolve_context(application_id, snapshot, requested_version)
        return context, self.build_model_slice(context, capability_references)

    def _context_id(
        self,
        *,
        ontology_version: str,
        ontology_digest: str,
        application_id: str,
        snapshot_id: str,
        data_binding_digest: str,
    ) -> str:
        identity_payload = {
            "namespace": "ontology-agent-context-v1",
            "ontology_id": self._ontology_id,
            "ontology_version": ontology_version,
            "ontology_digest": ontology_digest,
            "application_id": application_id,
            "snapshot_id": snapshot_id,
            "data_binding_digest": data_binding_digest,
        }
        canonical_identity = json.dumps(
            identity_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        context_mac = hmac.new(
            self._secret,
            canonical_identity,
            hashlib.sha256,
        ).digest()
        return "ctx_" + base64.urlsafe_b64encode(context_mac).decode("ascii").rstrip("=")

    @staticmethod
    def _declaration_indexes(proposal: OntologyProposal) -> dict[str, dict[str, Any]]:
        indexes: dict[str, dict[str, Any]] = {
            "ObjectType": {},
            "LinkType": {},
            "Function": {},
            "ActionType": {},
            "Axiom": {},
        }

        def add(kind: str, name: str, declaration: Any) -> None:
            if name in indexes[kind]:
                _fail(
                    "ONTOLOGY_CONTEXT_DECLARATION_DUPLICATE",
                    "已发布本体包含重复声明",
                )
            indexes[kind][name] = declaration

        for item in proposal.object_types:
            add("ObjectType", item.name, item)
        for item in proposal.link_types:
            add("LinkType", item.name, item)
        for item in proposal.functions:
            add("Function", item.name, item)
        for item in proposal.action_types:
            add("ActionType", item.name, item)
        for interface in proposal.interfaces:
            for axiom in interface.constraints:
                add("Axiom", axiom.id, axiom)
        return indexes

    @staticmethod
    def _safe_object(item: Any) -> SafeObjectType:
        return SafeObjectType(
            name=safe_ontology_text(item.name, field="ObjectType.name", max_length=256),
            label=_optional_safe_text(item.label, field_name="ObjectType.label", max_length=256),
            description=_optional_safe_text(item.description, field_name="ObjectType.description"),
        )

    @staticmethod
    def _safe_link(item: Any) -> SafeLinkType:
        return SafeLinkType(
            name=safe_ontology_text(item.name, field="LinkType.name", max_length=256),
            label=_optional_safe_text(item.label, field_name="LinkType.label", max_length=256),
            description=_optional_safe_text(item.description, field_name="LinkType.description"),
            source=safe_ontology_text(item.source, field="LinkType.source", max_length=256),
            target=safe_ontology_text(item.target, field="LinkType.target", max_length=256),
            direction=safe_ontology_text(item.direction, field="LinkType.direction", max_length=64),
            cardinality=_optional_safe_text(item.cardinality, field_name="LinkType.cardinality", max_length=64),
            source_cardinality=_optional_safe_text(
                item.source_cardinality,
                field_name="LinkType.source_cardinality",
                max_length=64,
            ),
            target_cardinality=_optional_safe_text(
                item.target_cardinality,
                field_name="LinkType.target_cardinality",
                max_length=64,
            ),
        )

    @staticmethod
    def _safe_function(item: Any) -> SafeFunction:
        return SafeFunction(
            name=safe_ontology_text(item.name, field="Function.name", max_length=256),
            label=_optional_safe_text(item.label, field_name="Function.label", max_length=256),
            description=_optional_safe_text(item.description, field_name="Function.description"),
            input_types=_safe_text_tuple(item.input_types, field_name="Function.input_types"),
            deterministic=bool(item.deterministic),
            side_effects=bool(item.side_effects),
        )

    @staticmethod
    def _safe_action(item: Any) -> SafeActionType:
        return SafeActionType(
            name=safe_ontology_text(item.name, field="ActionType.name", max_length=256),
            label=_optional_safe_text(item.label, field_name="ActionType.label", max_length=256),
            description=_optional_safe_text(item.description, field_name="ActionType.description"),
            input_types=_safe_text_tuple(item.input_types, field_name="ActionType.input_types"),
            output_types=_safe_text_tuple(item.output_types, field_name="ActionType.output_types"),
            invokes_functions=_safe_text_tuple(
                item.invokes_functions,
                field_name="ActionType.invokes_functions",
            ),
            preconditions=_safe_text_tuple(
                item.preconditions,
                field_name="ActionType.preconditions",
            ),
            audit_required=bool(item.audit_required),
            execution_mode=safe_ontology_text(
                item.execution_mode,
                field="ActionType.execution_mode",
                max_length=128,
            ),
        )

    @staticmethod
    def _safe_axiom(item: Any) -> SafeAxiom:
        return SafeAxiom(
            id=safe_ontology_text(item.id, field="Axiom.id", max_length=256),
            name=safe_ontology_text(item.name, field="Axiom.name", max_length=256),
            description=_optional_safe_text(item.description, field_name="Axiom.description"),
            severity=safe_ontology_text(item.severity, field="Axiom.severity", max_length=64),
            validation_phase=safe_ontology_text(
                item.validation_phase,
                field="Axiom.validation_phase",
                max_length=128,
            ),
        )


__all__ = (
    "OntologyContextError",
    "OntologyContextProvider",
    "OntologyModelSlice",
    "PublishedOntologyContext",
    "SafeActionType",
    "SafeAxiom",
    "SafeFunction",
    "SafeLinkType",
    "SafeObjectType",
    "assert_model_safe_payload",
    "safe_ontology_text",
)
