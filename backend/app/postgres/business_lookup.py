from __future__ import annotations

from typing import Any, Mapping

from ..core.security import mask_payload


class BusinessLookupError(ValueError):
    pass


PERSON_FIELDS = (
    "id_number",
    "name",
    "phone",
    "gender",
    "address",
    "marital_status",
    "monthly_income",
    "employer",
    "guarantee_rating",
    "guarantee_count",
    "guarantee_total",
)
VEHICLE_FIELDS = (
    "vin",
    "brand",
    "model",
    "invoice_price",
    "guide_price",
    "pledge_status",
)
DEALER_FIELDS = ("credit_code", "dealer_name", "location")
# A historical application is a reusable data source, not the new business event.
# Keep the current workflow's application_id, submit_date and initial status.
APPLICATION_TEMPLATE_FIELDS = ("channel", "loan_amount", "loan_term", "interest_rate")


SEARCH_SQL = {
    "natural_person": """
        SELECT id_number,name,phone,gender,address,marital_status,monthly_income,
               employer,guarantee_rating,guarantee_count,guarantee_total
        FROM natural_person
        WHERE concat_ws(' ',id_number,name,phone,employer) ILIKE %s ESCAPE '\\'
        ORDER BY CASE
            WHEN id_number=%s OR phone=%s THEN 0
            WHEN name=%s THEN 1 ELSE 2 END,name,id_number
        LIMIT %s
    """,
    "vehicle": """
        SELECT vin,brand,model,invoice_price,guide_price,pledge_status
        FROM vehicle
        WHERE concat_ws(' ',vin,brand,model) ILIKE %s ESCAPE '\\'
        ORDER BY CASE WHEN vin=%s THEN 0 ELSE 1 END,brand,model,vin
        LIMIT %s
    """,
    "dealer": """
        SELECT credit_code,dealer_name,location
        FROM dealer
        WHERE concat_ws(' ',credit_code,dealer_name,location) ILIKE %s ESCAPE '\\'
        ORDER BY CASE WHEN credit_code=%s THEN 0 ELSE 1 END,dealer_name,credit_code
        LIMIT %s
    """,
    "loan_application": """
        SELECT
            application.application_id,
            application.channel,
            application.loan_amount,
            application.loan_term,
            application.interest_rate,
            borrower.id_number AS borrower__id_number,
            borrower.name AS borrower__name,
            borrower.phone AS borrower__phone,
            borrower.gender AS borrower__gender,
            borrower.address AS borrower__address,
            borrower.marital_status AS borrower__marital_status,
            borrower.monthly_income AS borrower__monthly_income,
            borrower.employer AS borrower__employer,
            vehicle.vin AS vehicle__vin,
            vehicle.brand AS vehicle__brand,
            vehicle.model AS vehicle__model,
            vehicle.invoice_price AS vehicle__invoice_price,
            vehicle.guide_price AS vehicle__guide_price,
            vehicle.pledge_status AS vehicle__pledge_status,
            dealer.credit_code AS dealer__credit_code,
            dealer.dealer_name AS dealer__dealer_name,
            dealer.location AS dealer__location,
            guarantor.id_number AS guarantor__id_number,
            guarantor.name AS guarantor__name,
            guarantor.phone AS guarantor__phone,
            guarantor.gender AS guarantor__gender,
            guarantor.address AS guarantor__address,
            guarantor.marital_status AS guarantor__marital_status,
            guarantor.monthly_income AS guarantor__monthly_income,
            guarantor.employer AS guarantor__employer,
            guarantor.guarantee_rating AS guarantor__guarantee_rating,
            guarantor.guarantee_count AS guarantor__guarantee_count,
            guarantor.guarantee_total AS guarantor__guarantee_total,
            co_borrower.id_number AS co_borrower__id_number,
            co_borrower.name AS co_borrower__name,
            co_borrower.phone AS co_borrower__phone,
            co_borrower.gender AS co_borrower__gender,
            co_borrower.address AS co_borrower__address,
            co_borrower.marital_status AS co_borrower__marital_status,
            co_borrower.monthly_income AS co_borrower__monthly_income,
            co_borrower.employer AS co_borrower__employer
        FROM loan_application AS application
        JOIN natural_person AS borrower ON borrower.id_number=application.borrower_id
        LEFT JOIN vehicle ON vehicle.vin=application.vehicle_vin
        LEFT JOIN dealer ON dealer.credit_code=application.dealer_credit_code
        LEFT JOIN natural_person AS guarantor ON guarantor.id_number=application.guarantor_id
        LEFT JOIN natural_person AS co_borrower ON co_borrower.id_number=application.co_borrower_id
        WHERE concat_ws(
            ' ',application.application_id,borrower.id_number,borrower.name,
            borrower.phone,vehicle.vin,vehicle.brand,vehicle.model,
            dealer.credit_code,dealer.dealer_name
        ) ILIKE %s ESCAPE '\\'
        ORDER BY CASE WHEN application.application_id=%s THEN 0 ELSE 1 END,
                 application.submit_date DESC NULLS LAST,application.application_id
        LIMIT %s
    """,
}


def _normalize_query(value: str) -> str:
    query = " ".join(str(value or "").split())
    if len(query) < 2:
        raise BusinessLookupError("数据库检索词至少需要 2 个字符")
    if len(query) > 80:
        raise BusinessLookupError("数据库检索词不能超过 80 个字符")
    return query


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _compact(row: Mapping[str, Any], fields: tuple[str, ...], prefix: str = "") -> dict[str, Any]:
    return {
        field: row.get(f"{prefix}{field}")
        for field in fields
        if row.get(f"{prefix}{field}") is not None
    }


def _masked(value: Any, key: str) -> str:
    masked = mask_payload(value, key)
    return str(masked) if masked not in {None, ""} else "—"


def _natural_person_result(row: Mapping[str, Any]) -> dict[str, Any]:
    values = _compact(row, PERSON_FIELDS)
    phone = _masked(row.get("phone"), "phone") if row.get("phone") else "未登记手机号"
    return {
        "title": _masked(row.get("name"), "name"),
        "subtitle": f"证件 {_masked(row.get('id_number'), 'id_number')} · {phone}",
        "meta": f"自然人主数据 · 可回填 {len(values)} 个字段",
        "values": values,
    }


def _vehicle_result(row: Mapping[str, Any]) -> dict[str, Any]:
    values = _compact(row, VEHICLE_FIELDS)
    title = " ".join(str(row.get(key) or "").strip() for key in ("brand", "model")).strip()
    return {
        "title": title or "未命名车辆",
        "subtitle": f"VIN {_masked(row.get('vin'), 'vin')}",
        "meta": f"车辆主数据 · 可回填 {len(values)} 个字段",
        "values": values,
    }


def _dealer_result(row: Mapping[str, Any]) -> dict[str, Any]:
    values = _compact(row, DEALER_FIELDS)
    return {
        "title": _masked(row.get("dealer_name"), "dealer_name"),
        "subtitle": f"信用代码 {_masked(row.get('credit_code'), 'credit_code')}",
        "meta": f"经销商主数据 · 可回填 {len(values)} 个字段",
        "values": values,
    }


def _application_result(row: Mapping[str, Any]) -> dict[str, Any]:
    borrower = _compact(row, PERSON_FIELDS, "borrower__")
    vehicle = _compact(row, VEHICLE_FIELDS, "vehicle__")
    values: dict[str, Any] = {
        "application": _compact(row, APPLICATION_TEMPLATE_FIELDS),
        "borrower": borrower,
        "vehicle": vehicle,
    }
    for group, fields in (
        ("dealer", DEALER_FIELDS),
        ("guarantor", PERSON_FIELDS),
        ("co_borrower", PERSON_FIELDS),
    ):
        selected = _compact(row, fields, f"{group}__")
        if selected:
            values[group] = selected
    vehicle_name = " ".join(
        str(row.get(f"vehicle__{key}") or "").strip() for key in ("brand", "model")
    ).strip()
    borrower_name = _masked(row.get("borrower__name"), "name")
    return {
        "title": str(row.get("application_id") or "未编号申请"),
        "subtitle": f"借款人 {borrower_name} · {vehicle_name or '未登记车辆'}",
        "meta": f"历史申请关联资料 · 可回填 {sum(len(item) for item in values.values())} 个字段",
        "values": values,
    }


FORMATTERS = {
    "natural_person": _natural_person_result,
    "vehicle": _vehicle_result,
    "dealer": _dealer_result,
    "loan_application": _application_result,
}


class PersonalCreditBusinessLookup:
    """Allowlisted, read-only lookup used only to prefill authenticated Action forms."""

    def __init__(self, postgres_service):
        self.postgres = postgres_service

    def search(self, entity_type: str, query: str, limit: int = 8) -> dict[str, Any]:
        entity = str(entity_type or "").strip()
        if entity not in SEARCH_SQL:
            raise BusinessLookupError("不支持的数据检索类型")
        normalized_query = _normalize_query(query)
        normalized_limit = max(1, min(int(limit), 20))
        pattern = _like_pattern(normalized_query)
        if entity == "natural_person":
            parameters = (
                pattern,
                normalized_query,
                normalized_query,
                normalized_query,
                normalized_limit,
            )
        else:
            parameters = (pattern, normalized_query, normalized_limit)
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cursor.execute(SEARCH_SQL[entity], parameters)
            rows = cursor.fetchall()
        return {
            "entity_type": entity,
            "result_count": len(rows),
            "results": [FORMATTERS[entity](row) for row in rows],
            "selection_scope": "authenticated_action_prefill",
        }
