import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time as time_module
from datetime import date,datetime,time
from decimal import Decimal
from urllib.parse import urlsplit,urlunsplit


ID_NUMBER_PATTERN=re.compile(r"(?<![0-9A-Za-z])(\d{3})\d{13}([0-9Xx]{2})(?![0-9A-Za-z])")
PHONE_PATTERN=re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)")
CREDENTIAL_PATTERN=re.compile(
    r"(?i)(?P<key>\b(?:api[_ -]?key|x-api-key|authorization|"
    r"access[_ -]?token|token|secret)\b)"
    r"(?P<separator>[\"']?\s*[:=]\s*)(?:bearer\s+)?"
    r"(?P<value>[^\s,;}\]]+)"
)

MVP_API_KEY_REQUIRED_DETAIL=(
    "身份凭据缺失或无效，请重新建立本机会话或提供有效的 X-API-Key"
)
LOCAL_SESSION_COOKIE_NAME="mvp_local_session"
LOCAL_SESSION_AUDIENCE="credit-ontology-mvp"
LOCAL_SESSION_SAFE_METHODS={"GET","HEAD","OPTIONS"}

SENSITIVE_KEYS={
    "api_key","x_api_key","api_key_ciphertext","authorization",
    "access_token","token","secret",
    "id_number","borrower_id","guarantor_id","co_borrower_id","subject_id_number",
    "phone","address","location","monthly_income","employer","guarantee_total",
    "loan_amount","invoice_price","guide_price","creditor","balance","risk_factors",
    "hit_details","doubts","recording_file_id","checker","verifier_name","dealer_name",
    "verification_target","credit_code","vin","vehicle_vin","name",
    "record_id",
    "authorization_reference","outstanding_loan_count","overdue_3y_count",
    "total_overdue_count","inquiry_count","response_payload",
    "fraud_score","fraud_result","fraud_hit_details","borrower_phone",
    "borrower_address","request_context_hash",
    "trigger_reason","questions","phone_questions",
}


def api_key_matches(expected:str,presented:str|None)->bool:
    """Constant-time comparison for the single-user MVP access token."""
    return bool(expected and presented) and hmac.compare_digest(expected,presented)


def local_session_secret_is_usable(secret:str)->bool:
    """Require enough entropy before a value may sign browser sessions."""
    return len((secret or "").encode("utf-8"))>=32


def _base64url_encode(value:bytes)->str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value:str)->bytes:
    padding="="*((4-len(value)%4)%4)
    return base64.b64decode(
        (value+padding).encode("ascii"),altchars=b"-_",validate=True
    )


def issue_local_session_token(
    secret:str,ttl_seconds:int,*,now:int|float|None=None
)->tuple[str,int]:
    """Create a short-lived, signed, opaque-to-the-browser session value."""
    if not local_session_secret_is_usable(secret):
        raise ValueError("本机会话签名密钥至少需要 32 字节")
    ttl=int(ttl_seconds)
    if ttl<60 or ttl>3600:
        raise ValueError("本机会话有效期必须在 60 到 3600 秒之间")
    issued_at=int(time_module.time() if now is None else now)
    expires_at=issued_at+ttl
    payload=json.dumps(
        {
            "aud":LOCAL_SESSION_AUDIENCE,
            "exp":expires_at,
            "iat":issued_at,
            "jti":secrets.token_urlsafe(12),
            "v":1,
        },
        separators=(",",":"),sort_keys=True,
    ).encode("utf-8")
    encoded_payload=_base64url_encode(payload)
    signature=hmac.new(
        secret.encode("utf-8"),encoded_payload.encode("ascii"),hashlib.sha256
    ).digest()
    return f"{encoded_payload}.{_base64url_encode(signature)}",expires_at


def verify_local_session_token(
    secret:str,token:str|None,max_ttl_seconds:int,*,now:int|float|None=None
)->int|None:
    """Return the expiry of a valid local session, otherwise fail closed."""
    if (
        not local_session_secret_is_usable(secret)
        or not isinstance(token,str)
        or not token
        or len(token)>2048
    ):
        return None
    try:
        encoded_payload,encoded_signature=token.split(".")
        if not encoded_payload or not encoded_signature:
            return None
        expected=hmac.new(
            secret.encode("utf-8"),encoded_payload.encode("ascii"),hashlib.sha256
        ).digest()
        signature=_base64url_decode(encoded_signature)
        if not hmac.compare_digest(expected,signature):
            return None
        payload=json.loads(_base64url_decode(encoded_payload))
        if not isinstance(payload,dict):
            return None
        issued_at=payload.get("iat")
        expires_at=payload.get("exp")
        if (
            payload.get("v")!=1
            or payload.get("aud")!=LOCAL_SESSION_AUDIENCE
            or isinstance(issued_at,bool)
            or isinstance(expires_at,bool)
            or not isinstance(issued_at,int)
            or not isinstance(expires_at,int)
        ):
            return None
        current=int(time_module.time() if now is None else now)
        ttl=int(max_ttl_seconds)
        if (
            ttl<60
            or ttl>3600
            or issued_at>current+30
            or expires_at<=current
            or expires_at<=issued_at
            or expires_at-issued_at>ttl
        ):
            return None
        return expires_at
    except (
        ValueError,TypeError,KeyError,AttributeError,UnicodeError,
        binascii.Error,json.JSONDecodeError,
    ):
        return None


def _is_loopback_hostname(hostname:str|None)->bool:
    if not hostname:
        return False
    normalized=hostname.strip().lower()
    if normalized=="localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def is_loopback_request_host(host_header:str|None)->bool:
    """Validate the HTTP Host target, including bracketed IPv6 addresses."""
    if not host_header or any(char in host_header for char in "\r\n"):
        return False
    try:
        parsed=urlsplit(f"//{host_header}")
        # Accessing port also rejects malformed/non-numeric ports.
        _=parsed.port
    except ValueError:
        return False
    return not (parsed.username or parsed.password) and _is_loopback_hostname(parsed.hostname)


def _canonical_origin(origin:str|None)->str|None:
    if not origin or any(char in origin for char in "\r\n"):
        return None
    try:
        parsed=urlsplit(origin.strip())
        _=parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http","https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"","/"}
    ):
        return None
    return urlunsplit((parsed.scheme.lower(),parsed.netloc.lower(),"","",""))


def is_allowed_local_origin(origin:str|None,allowed_origins:list[str])->bool:
    """Require an exact configured origin whose hostname is itself loopback."""
    canonical=_canonical_origin(origin)
    if not canonical:
        return False
    if not _is_loopback_hostname(urlsplit(canonical).hostname):
        return False
    allowed={
        normalized
        for item in allowed_origins
        if (normalized:=_canonical_origin(item)) is not None
    }
    return canonical in allowed


def is_trusted_local_session_request(
    *,client_host:str|None,host_header:str|None,origin:str|None,
    allowed_origins:list[str],trust_local_proxy:bool,
    fetch_site:str|None=None,require_origin:bool=False,
)->bool:
    """Accept loopback traffic, or an explicitly trusted local container proxy.

    Proxy trust is safe only when the deployment binds its published HTTP ports
    to loopback. For proxy traffic an exact local Origin is mandatory when a
    session is issued; unsafe cookie-authenticated requests use the same rule.
    """
    if not is_loopback_request_host(host_header):
        return False
    normalized_fetch_site=(fetch_site or "").strip().lower()
    if normalized_fetch_site and normalized_fetch_site not in {
        "same-origin","same-site","none"
    }:
        return False
    if origin is not None and not is_allowed_local_origin(origin,allowed_origins):
        return False
    source_is_loopback=_is_loopback_hostname(client_host)
    if not source_is_loopback and not trust_local_proxy:
        return False
    if require_origin and not is_allowed_local_origin(origin,allowed_origins):
        return False
    return True


def cookie_write_origin_is_allowed(
    method:str,origin:str|None,allowed_origins:list[str],fetch_site:str|None=None
)->bool:
    if str(method or "GET").upper() in LOCAL_SESSION_SAFE_METHODS:
        return True
    normalized_fetch_site=(fetch_site or "").strip().lower()
    if normalized_fetch_site and normalized_fetch_site not in {"same-origin","same-site"}:
        return False
    return is_allowed_local_origin(origin,allowed_origins)


def external_llm_auth_error(
    expected:str,presented:str|None,has_llm_config:bool,*,authenticated:bool=False
)->tuple[int,str]|None:
    """Return the fail-closed auth response required before external LLM work."""
    if not has_llm_config:
        return None
    if authenticated:
        return None
    if not expected:
        return 503,"服务端尚未配置 MVP_API_KEY，外部模型调用已关闭"
    if not api_key_matches(expected,presented):
        return 401,MVP_API_KEY_REQUIRED_DETAIL
    return None


def is_unauthenticated_workbench_path(
    path:str,enabled:bool,method:str="GET"
)->bool:
    """Allow only the explicitly read-only UAT workbench surface without login."""
    if not enabled:
        return False
    normalized_method=str(method or "GET").upper()
    if normalized_method=="GET":
        if path in {
            "/api/workflows/personal-credit",
            "/api/workflows/personal-credit/summary",
            "/api/workflows/personal-credit/definition",
            "/api/agents/personal-credit/tools",
        }:
            return True
        if re.fullmatch(r"/api/workflows/personal-credit/[^/]+",path):
            return path.rsplit("/",1)[-1] not in {"data-search","summary","definition"}
        return bool(re.fullmatch(
            r"/api/agents/personal-credit/[^/]+/readiness",path
        ))
    if normalized_method=="POST":
        if path=="/api/agents/personal-credit/run":
            return True
        return bool(re.fullmatch(
            r"/api/agents/personal-credit/tools/[^/]+/execute",path
        ))
    return False


def _mask_text(value:str)->str:
    value=CREDENTIAL_PATTERN.sub(
        lambda match:(
            f"{match.group('key')}{match.group('separator')}[已脱敏]"
        ),
        value,
    )
    value=ID_NUMBER_PATTERN.sub(lambda match:f"{match.group(1)}*************{match.group(2)}",value)
    return PHONE_PATTERN.sub(lambda match:f"{match.group(1)}****{match.group(2)}",value)


def _mask_named_value(key:str,value):
    lowered=key.lower().split(".")[-1].replace("-","_")
    if value is None:
        return None
    if lowered=="name" and isinstance(value,str) and not re.search(r"[\u3400-\u9fff]",value):
        return _mask_text(value)
    if lowered not in SENSITIVE_KEYS:
        return None
    if lowered in {"id_number","borrower_id","guarantor_id","co_borrower_id","subject_id_number","phone"}:
        return _mask_text(str(value))
    if isinstance(value,str) and len(value)>=2:
        if lowered in {"name","dealer_name","checker","verifier_name","creditor"}:
            return value[0]+"**"
        return "[已脱敏]"
    return "[已脱敏]"


def mask_payload(value,key:str=""):
    """Recursively redact personal and financial values while preserving response shape."""
    named=_mask_named_value(key,value)
    if named is not None:
        return named
    if isinstance(value,dict):
        return {item_key:mask_payload(item_value,str(item_key)) for item_key,item_value in value.items()}
    if isinstance(value,(list,tuple)):
        return [mask_payload(item,key) for item in value]
    if isinstance(value,str):
        stripped=value.strip()
        if stripped and stripped[0] in "[{":
            try:
                parsed=json.loads(value)
            except (ValueError,TypeError):
                pass
            else:
                return json.dumps(mask_payload(parsed),ensure_ascii=False,default=str)
        return _mask_text(value)
    return value


def mask_graph_snapshot(snapshot:dict)->dict:
    """Redact graph identifiers consistently so edges still resolve to returned nodes."""
    identifiers={str(node.get("id")) for node in snapshot.get("nodes",[]) if node.get("id") is not None}
    for edge in snapshot.get("edges",[]):
        identifiers.update(str(edge.get(field)) for field in ("source","target") if edge.get(field) is not None)
    identifier_map={identifier:_mask_text(identifier) for identifier in identifiers}
    output=mask_payload(snapshot)
    for index,node in enumerate(output.get("nodes",[])):
        original=str(snapshot.get("nodes",[])[index].get("id"))
        node["id"]=identifier_map.get(original,_mask_text(original))
    for index,edge in enumerate(output.get("edges",[])):
        for field in ("source","target"):
            original=str(snapshot.get("edges",[])[index].get(field))
            edge[field]=identifier_map.get(original,_mask_text(original))
    return output


def _anonymous_value(value,table:str,column:str):
    if value is None:
        return None
    if isinstance(value,bool):
        return False
    if isinstance(value,(int,float,Decimal)):
        return 0
    if isinstance(value,datetime):
        return "2000-01-01T00:00:00+00:00"
    if isinstance(value,date):
        return "2000-01-01"
    if isinstance(value,time):
        return "00:00:00"
    if isinstance(value,(bytes,bytearray,memoryview)):
        return "<binary>"
    if isinstance(value,dict):
        return {key:_anonymous_value(item,table,str(key)) for key,item in value.items()}
    if isinstance(value,(list,tuple)):
        return [_anonymous_value(item,table,column) for item in value]
    return f"<{table}.{column}:string>"


def anonymize_profile_for_llm(profile:dict)->dict:
    """Keep schema, nullness, and scalar kinds without sending any database value."""
    output={key:value for key,value in profile.items() if key!="samples"}
    output["samples"]={
        table:[
            {column:_anonymous_value(value,table,column) for column,value in row.items()}
            for row in rows
        ]
        for table,rows in profile.get("samples",{}).items()
    }
    output["sample_policy"]="schema-preserving anonymized placeholders; no PostgreSQL values included"
    return output


def validate_llm_base_url(base_url:str,allowed_urls:list[str],allow_insecure_local:bool=False)->str:
    value=(base_url or "").strip().rstrip("/")
    parsed=urlsplit(value)
    if parsed.scheme not in {"http","https"} or not parsed.hostname:
        raise ValueError("LLM Base URL 必须是完整的 HTTP(S) 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("LLM Base URL 不得包含账号、密码、查询参数或片段")
    host=parsed.hostname.lower()
    if parsed.scheme=="http" and not (allow_insecure_local and host in {"localhost","127.0.0.1","::1"}):
        raise ValueError("LLM Base URL 必须使用 HTTPS；仅显式启用的本机服务可使用 HTTP")
    canonical=urlunsplit((parsed.scheme.lower(),parsed.netloc.lower(),parsed.path.rstrip("/"),"",""))
    canonical_allowed=set()
    for item in allowed_urls:
        allowed=urlsplit(item.strip().rstrip("/"))
        if allowed.scheme and allowed.netloc and not (allowed.username or allowed.password or allowed.query or allowed.fragment):
            canonical_allowed.add(urlunsplit((allowed.scheme.lower(),allowed.netloc.lower(),allowed.path.rstrip("/"),"","")))
    if canonical not in canonical_allowed:
        raise ValueError("LLM Base URL 不在服务端白名单中，请由管理员配置 LLM_ALLOWED_BASE_URLS")
    return canonical
