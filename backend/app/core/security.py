import hmac
import json
import re
from datetime import date,datetime,time
from decimal import Decimal
from urllib.parse import urlsplit,urlunsplit


ID_NUMBER_PATTERN=re.compile(r"(?<![0-9A-Za-z])(\d{3})\d{13}([0-9Xx]{2})(?![0-9A-Za-z])")
PHONE_PATTERN=re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)")

SENSITIVE_KEYS={
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


def _mask_text(value:str)->str:
    value=ID_NUMBER_PATTERN.sub(lambda match:f"{match.group(1)}*************{match.group(2)}",value)
    return PHONE_PATTERN.sub(lambda match:f"{match.group(1)}****{match.group(2)}",value)


def _mask_named_value(key:str,value):
    lowered=key.lower().split(".")[-1]
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
