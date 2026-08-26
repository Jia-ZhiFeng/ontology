import re
from collections import defaultdict
from datetime import datetime, timedelta

from .mapping import derive_shared_contact_links
from .models import OntologyProposal


ID_PATTERN = re.compile(r"^[0-9]{17}[0-9Xx]$")
PHONE_PATTERN = re.compile(r"^1[0-9]{10}$")
VIN_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
ACTIVE_APPLICATION_STATUSES = {"资料待补充", "评估中", "待人工复核"}


def _present(value) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _datetime(value):
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def _record(table: str, identifier, message: str, fields=None, **details) -> dict:
    result = {
        "record": f"{table}/{identifier}",
        "message": message,
    }
    if fields:
        result["fields"] = fields
    if details:
        result["details"] = details
    return result


def _result(checked: int, applicable: int, violations=None, facts=None) -> dict:
    return {
        "checked_records": checked,
        "applicable_records": applicable,
        "violations": violations or [],
        "facts": facts or {},
    }


class AxiomContext:
    def __init__(self, profile: dict):
        self.profile = profile
        self.tables = profile.get("samples", {})
        self.people = self._index("natural_person", "id_number")
        self.vehicles = self._index("vehicle", "vin")
        self.dealers = self._index("dealer", "credit_code")
        self.applications = self._index("loan_application", "application_id")
        self.credit_reports = self._index("credit_report", "report_id")
        self.risk_reports = self._index("risk_assessment_report", "assessment_id")
        self.fraud_reports = self._index("anti_fraud_report", "fraud_report_id")
        self.phone_records = self._index("phone_verification_record", "verification_id")
        self.risk_by_application = self._group("risk_assessment_report", "application_id")
        self.fraud_by_application = self._group("anti_fraud_report", "application_id")
        self.phone_by_application = self._group("phone_verification_record", "application_id")
        self.credit_by_person = self._group("credit_report", "subject_id_number")
        self.shared_contacts = derive_shared_contact_links(self.rows("natural_person"))

    def rows(self, table: str) -> list[dict]:
        return list(self.tables.get(table, []))

    def _index(self, table: str, key: str) -> dict:
        return {
            str(row[key]): row
            for row in self.rows(table)
            if _present(row.get(key))
        }

    def _group(self, table: str, key: str) -> dict:
        grouped = defaultdict(list)
        for row in self.rows(table):
            if _present(row.get(key)):
                grouped[str(row[key])].append(row)
        return grouped


def _assessment_started(row: dict) -> bool:
    return any(_present(row.get(field)) for field in (
        "risk_level", "assessment_time", "assessment_channel", "pd_value",
        "risk_factors", "check_item", "check_result", "checker",
    ))


def ax_per_001(ctx):
    rows = ctx.rows("natural_person")
    violations = [
        _record("natural_person", row.get("id_number"), "身份证号不是18位合法格式", ["id_number"])
        for row in rows if not ID_PATTERN.fullmatch(str(row.get("id_number") or ""))
    ]
    return _result(len(rows), len(rows), violations)


def ax_per_002(ctx):
    rows = ctx.rows("natural_person")
    applicable = [row for row in rows if _present(row.get("phone"))]
    violations = [
        _record("natural_person", row.get("id_number"), "已采集手机号不是11位大陆手机格式", ["phone"])
        for row in applicable if not PHONE_PATTERN.fullmatch(str(row["phone"]).strip())
    ]
    return _result(len(rows), len(applicable), violations)


def ax_per_003(ctx):
    rows = ctx.rows("loan_application"); violations = []
    required = ["id_number", "name", "phone", "address"]
    for app in rows:
        person = ctx.people.get(str(app.get("borrower_id")))
        missing = required if not person else [field for field in required if not _present(person.get(field))]
        if missing:
            violations.append(_record(
                "loan_application", app.get("application_id"),
                "借款人提交申请前身份信息不完整", missing,
                borrower_id=app.get("borrower_id"),
            ))
    return _result(len(rows), len(rows), violations)


def ax_inc_001(ctx):
    violations = []; applicable = 0
    for app in ctx.rows("loan_application"):
        assessments = ctx.risk_by_application.get(str(app.get("application_id")), [])
        if not any(_assessment_started(row) for row in assessments):
            continue
        for role, field in (("借款人", "borrower_id"), ("共借人", "co_borrower_id")):
            person_id = app.get(field)
            if not _present(person_id):
                continue
            applicable += 1
            person = ctx.people.get(str(person_id))
            income = person.get("monthly_income") if person else None
            if income is None or income <= 0:
                violations.append(_record(
                    "natural_person", person_id,
                    f"{role}进入还款能力评估时月收入必须大于0", ["monthly_income"],
                    application_id=app.get("application_id"), role=role,
                ))
    return _result(len(ctx.rows("natural_person")), applicable, violations)


def ax_veh_001(ctx):
    rows = ctx.rows("vehicle")
    violations = [
        _record("vehicle", row.get("vin"), "VIN不是17位合法格式或包含I/O/Q", ["vin"])
        for row in rows if not VIN_PATTERN.fullmatch(str(row.get("vin") or ""))
    ]
    return _result(len(rows), len(rows), violations)


def ax_veh_002(ctx):
    rows = ctx.rows("loan_application"); violations = []
    for app in rows:
        vehicle = ctx.vehicles.get(str(app.get("vehicle_vin")))
        price = vehicle.get("invoice_price") if vehicle else None
        if price is None or price <= 0:
            violations.append(_record(
                "loan_application", app.get("application_id"),
                "申请关联车辆的发票价格必须大于0", ["vehicle_vin", "invoice_price"],
                vehicle_vin=app.get("vehicle_vin"),
            ))
    return _result(len(ctx.rows("vehicle")), len(rows), violations)


def ax_veh_003(ctx):
    rows = ctx.rows("vehicle"); applicable = [row for row in rows if row.get("guide_price") is not None]
    violations = [
        _record("vehicle", row.get("vin"), "指导价格必须大于0", ["guide_price"])
        for row in applicable if row["guide_price"] <= 0
    ]
    return _result(len(rows), len(applicable), violations)


def ax_veh_004(ctx):
    rows = ctx.rows("vehicle"); allowed = {"未抵押", "抵押中", "已解押"}
    applicable = [row for row in rows if _present(row.get("pledge_status"))]
    violations = [
        _record("vehicle", row.get("vin"), "抵押状态不在允许枚举中", ["pledge_status"])
        for row in applicable if row["pledge_status"] not in allowed
    ]
    return _result(len(rows), len(applicable), violations)


def ax_cr_001(ctx):
    rows = ctx.rows("credit_report"); violations = []
    for row in rows:
        subject = row.get("subject_id_number")
        count = 1 if _present(subject) and str(subject) in ctx.people else 0
        if count != 1:
            violations.append(_record(
                "credit_report", row.get("report_id"),
                "征信报告必须且只能归属于一个自然人", ["subject_id_number"],
                resolved_owner_count=count,
            ))
    return _result(len(rows), len(rows), violations)


def ax_cr_002(ctx):
    rows = ctx.rows("credit_report"); violations = []
    fields = ["outstanding_loan_count", "overdue_3y_count", "total_overdue_count", "inquiry_count"]
    for row in rows:
        invalid = [field for field in fields if row.get(field) is not None and row[field] < 0]
        if invalid:
            violations.append(_record("credit_report", row.get("report_id"), "征信计数不得为负数", invalid))
    return _result(len(rows), len(rows), violations)


def ax_cr_003(ctx):
    rows = [row for row in ctx.rows("risk_assessment_report") if _datetime(row.get("assessment_time"))]
    violations = []; selected = []
    for assessment in rows:
        app = ctx.applications.get(str(assessment.get("application_id")))
        borrower_id = app.get("borrower_id") if app else None
        assessment_time = _datetime(assessment.get("assessment_time"))
        eligible = []
        for report in ctx.credit_by_person.get(str(borrower_id), []):
            query_time = _datetime(report.get("query_time"))
            if query_time and query_time <= assessment_time:
                eligible.append((query_time, report))
        if not eligible:
            violations.append(_record(
                "risk_assessment_report", assessment.get("assessment_id"),
                "评估时间之前没有可用征信报告，无法选择最新征信", ["assessment_time"],
                application_id=assessment.get("application_id"), borrower_id=borrower_id,
            ))
        else:
            latest = max(eligible, key=lambda item: item[0])[1]
            selected.append({
                "assessment_id": assessment.get("assessment_id"),
                "credit_report_id": latest.get("report_id"),
            })
    return _result(len(ctx.rows("risk_assessment_report")), len(rows), violations, {"selected_latest_reports": selected})


def ax_gua_001(ctx):
    apps = [row for row in ctx.rows("loan_application") if _present(row.get("guarantor_id"))]
    violations = []; required = ["guarantee_rating", "guarantee_count", "guarantee_total"]
    for app in apps:
        person = ctx.people.get(str(app.get("guarantor_id")))
        missing = required if not person else [field for field in required if not _present(person.get(field))]
        if missing:
            violations.append(_record(
                "natural_person", app.get("guarantor_id"),
                "担保人的担保能力信息不完整", missing,
                application_id=app.get("application_id"),
            ))
    return _result(len(ctx.rows("natural_person")), len(apps), violations)


def ax_gua_002(ctx):
    rows = ctx.rows("natural_person"); violations = []
    for row in rows:
        invalid = [
            field for field in ("guarantee_count", "guarantee_total")
            if row.get(field) is not None and row[field] < 0
        ]
        if invalid:
            violations.append(_record("natural_person", row.get("id_number"), "担保统计不得为负数", invalid))
    return _result(len(rows), len(rows), violations)


def ax_app_001(ctx):
    rows = ctx.rows("loan_application"); violations = []
    for app in rows:
        borrower_count = 1 if str(app.get("borrower_id")) in ctx.people else 0
        vehicle_count = 1 if str(app.get("vehicle_vin")) in ctx.vehicles else 0
        if borrower_count != 1 or vehicle_count != 1:
            violations.append(_record(
                "loan_application", app.get("application_id"),
                "申请必须且只能关联一个借款人和一辆车辆", ["borrower_id", "vehicle_vin"],
                borrower_count=borrower_count, vehicle_count=vehicle_count,
            ))
    return _result(len(rows), len(rows), violations)


def ax_app_002(ctx):
    rows = ctx.rows("loan_application"); violations = []
    for app in rows:
        roles = [app.get(field) for field in ("borrower_id", "guarantor_id", "co_borrower_id") if _present(app.get(field))]
        if len(roles) != len(set(map(str, roles))):
            violations.append(_record(
                "loan_application", app.get("application_id"),
                "借款人、担保人和共借人必须互不相同", ["borrower_id", "guarantor_id", "co_borrower_id"],
            ))
    return _result(len(rows), len(rows), violations)


def ax_app_003(ctx):
    rows = ctx.rows("loan_application"); violations = []
    for app in rows:
        invalid = []
        if app.get("loan_amount") is None or app["loan_amount"] <= 0: invalid.append("loan_amount")
        if app.get("loan_term") is None or app["loan_term"] <= 0: invalid.append("loan_term")
        rate = app.get("interest_rate")
        if rate is None or not 0 < rate < 1: invalid.append("interest_rate")
        if invalid:
            violations.append(_record("loan_application", app.get("application_id"), "融资参数不满足有效范围", invalid))
    return _result(len(rows), len(rows), violations)


def ax_app_004(ctx):
    rows = [row for row in ctx.rows("loan_application") if row.get("channel") in {"经销商APP", "线下门店"}]
    violations = []
    for app in rows:
        dealer_count = 1 if str(app.get("dealer_credit_code")) in ctx.dealers else 0
        if dealer_count != 1:
            violations.append(_record(
                "loan_application", app.get("application_id"),
                "经销商APP或线下门店申请必须关联一个经销商", ["channel", "dealer_credit_code"],
                resolved_dealer_count=dealer_count,
            ))
    return _result(len(ctx.rows("loan_application")), len(rows), violations)


def ax_app_005(ctx):
    grouped = defaultdict(list); violations = []
    for app in ctx.rows("loan_application"):
        submit_time = _datetime(app.get("submit_date"))
        if not submit_time:
            violations.append(_record(
                "loan_application", app.get("application_id"),
                "申请日期为空，无法执行7日频次校验", ["submit_date"],
            ))
            continue
        grouped[str(app.get("borrower_id"))].append((submit_time, app))
    for borrower_id, entries in grouped.items():
        entries.sort(key=lambda item: item[0])
        start = 0
        for end, (current_time, current_app) in enumerate(entries):
            while current_time - entries[start][0] > timedelta(days=7):
                start += 1
            count = end - start + 1
            if count > 3:
                violations.append(_record(
                    "loan_application", current_app.get("application_id"),
                    "同一借款人滚动7日内申请超过3笔", ["borrower_id", "submit_date"],
                    borrower_id=borrower_id, rolling_count=count,
                ))
    return _result(len(ctx.rows("loan_application")), len(ctx.rows("loan_application")), violations)


def ax_app_006(ctx):
    active = defaultdict(list)
    for app in ctx.rows("loan_application"):
        if app.get("application_status") in ACTIVE_APPLICATION_STATUSES or not _present(app.get("application_status")):
            active[str(app.get("vehicle_vin"))].append(app.get("application_id"))
    violations = [
        _record("vehicle", vin, "同一VIN同时关联多笔未完成申请", ["vehicle_vin", "application_status"], application_ids=ids)
        for vin, ids in active.items() if len(ids) > 1
    ]
    return _result(len(ctx.rows("loan_application")), sum(map(len, active.values())), violations)


def ax_app_007(ctx):
    rows = ctx.rows("loan_application"); violations = []
    for app in rows:
        application_id = str(app.get("application_id"))
        risk_count = len(ctx.risk_by_application.get(application_id, []))
        fraud_count = len(ctx.fraud_by_application.get(application_id, []))
        phone_count = len(ctx.phone_by_application.get(application_id, []))
        if risk_count > 1 or fraud_count > 1:
            violations.append(_record(
                "loan_application", application_id,
                "申请的有效风险报告或反欺诈报告超过1份", ["application_id"],
                risk_report_count=risk_count, fraud_report_count=fraud_count, phone_record_count=phone_count,
            ))
    return _result(len(rows), len(rows), violations)


def ax_rsk_001(ctx):
    rows = ctx.rows("risk_assessment_report"); applicable = [row for row in rows if _assessment_started(row)]
    violations = []
    for row in applicable:
        invalid = []
        for field in ("pd_value", "threshold"):
            value = row.get(field)
            if value is None or not 0 <= value <= 1:
                invalid.append(field)
        if invalid:
            violations.append(_record("risk_assessment_report", row.get("assessment_id"), "PD值或阈值不在0到1之间", invalid))
    return _result(len(rows), len(applicable), violations)


def ax_rsk_002(ctx):
    rows = ctx.rows("risk_assessment_report"); applicable = []; violations = []
    for row in rows:
        pd_value = row.get("pd_value"); threshold = row.get("threshold")
        if pd_value is None or threshold is None or pd_value < threshold:
            continue
        applicable.append(row)
        app = ctx.applications.get(str(row.get("application_id"))) or {}
        if row.get("check_result") == "通过" or app.get("decision_result") == "通过":
            violations.append(_record(
                "risk_assessment_report", row.get("assessment_id"),
                "PD达到阈值时核验或申请不得直接通过", ["pd_value", "threshold", "check_result", "decision_result"],
                application_id=row.get("application_id"),
            ))
    return _result(len(rows), len(applicable), violations)


def ax_frd_001(ctx):
    rows = ctx.rows("anti_fraud_report"); applicable = [row for row in rows if row.get("fraud_score") is not None]
    violations = [
        _record("anti_fraud_report", row.get("fraud_report_id"), "欺诈评分不在0到100之间", ["fraud_score"])
        for row in applicable if not 0 <= row["fraud_score"] <= 100
    ]
    return _result(len(rows), len(applicable), violations)


def ax_frd_002(ctx):
    rows = [row for row in ctx.rows("anti_fraud_report") if row.get("fraud_score") is not None and row["fraud_score"] > 70]
    allowed = {"人工复核", "拒绝"}
    violations = [
        _record("anti_fraud_report", row.get("fraud_report_id"), "欺诈评分大于70时必须人工复核或拒绝", ["fraud_score", "result"])
        for row in rows if row.get("result") not in allowed
    ]
    return _result(len(ctx.rows("anti_fraud_report")), len(rows), violations)


def ax_phn_001(ctx):
    rows = [
        row for row in ctx.rows("phone_verification_record")
        if row.get("conclusion") == "存疑" or _present(row.get("doubts"))
    ]
    violations = []
    for row in rows:
        app = ctx.applications.get(str(row.get("application_id"))) or {}
        if app.get("decision_result") not in {"转人工", "补充材料"}:
            violations.append(_record(
                "phone_verification_record", row.get("verification_id"),
                "电核存疑时申请必须转人工或补充材料", ["conclusion", "doubts", "decision_result"],
                application_id=row.get("application_id"),
            ))
    return _result(len(ctx.rows("phone_verification_record")), len(rows), violations)


def ax_shc_001(ctx):
    violations = []
    for source, target, _, data in ctx.shared_contacts:
        invalid = source == target or not _present(data.get("matched_value"))
        if invalid:
            violations.append(_record(
                "SHARES_CONTACT", f"{source}--{target}",
                "同联系方式关系不能自连且匹配摘要不能为空", ["source", "target", "matched_value"],
            ))
    return _result(
        len(ctx.shared_contacts), len(ctx.shared_contacts), violations,
        {"derived_relation_count": len(ctx.shared_contacts)},
    )


AXIOM_HANDLERS = {
    "AX-PER-001": ax_per_001,
    "AX-PER-002": ax_per_002,
    "AX-PER-003": ax_per_003,
    "AX-INC-001": ax_inc_001,
    "AX-VEH-001": ax_veh_001,
    "AX-VEH-002": ax_veh_002,
    "AX-VEH-003": ax_veh_003,
    "AX-VEH-004": ax_veh_004,
    "AX-CR-001": ax_cr_001,
    "AX-CR-002": ax_cr_002,
    "AX-CR-003": ax_cr_003,
    "AX-GUA-001": ax_gua_001,
    "AX-GUA-002": ax_gua_002,
    "AX-APP-001": ax_app_001,
    "AX-APP-002": ax_app_002,
    "AX-APP-003": ax_app_003,
    "AX-APP-004": ax_app_004,
    "AX-APP-005": ax_app_005,
    "AX-APP-006": ax_app_006,
    "AX-APP-007": ax_app_007,
    "AX-RSK-001": ax_rsk_001,
    "AX-RSK-002": ax_rsk_002,
    "AX-FRD-001": ax_frd_001,
    "AX-FRD-002": ax_frd_002,
    "AX-PHN-001": ax_phn_001,
    "AX-SHC-001": ax_shc_001,
}


def axiom_executability(proposal: OntologyProposal) -> dict:
    axioms = [axiom for interface in proposal.interfaces for axiom in interface.constraints]
    ids = [axiom.id for axiom in axioms]
    duplicates = sorted({identifier for identifier in ids if ids.count(identifier) > 1})
    missing_handlers = sorted(set(ids) - set(AXIOM_HANDLERS))
    handlers_without_axiom = sorted(set(AXIOM_HANDLERS) - set(ids))
    return {
        "valid": not duplicates and not missing_handlers and not handlers_without_axiom and len(ids) == 26,
        "proposal_axiom_count": len(ids),
        "registered_handler_count": len(AXIOM_HANDLERS),
        "duplicates": duplicates,
        "missing_handlers": missing_handlers,
        "handlers_without_axiom": handlers_without_axiom,
    }


def execute_axioms(proposal: OntologyProposal, profile: dict) -> dict:
    coverage = axiom_executability(proposal)
    ctx = AxiomContext(profile)
    axioms = [axiom for interface in proposal.interfaces for axiom in interface.constraints]
    results = []
    for axiom in axioms:
        handler = AXIOM_HANDLERS.get(axiom.id)
        if not handler:
            results.append({
                "id": axiom.id,
                "name": axiom.name,
                "severity": axiom.severity,
                "status": "not_executable",
                "expression": axiom.expression,
                "checked_records": 0,
                "applicable_records": 0,
                "violation_count": 1,
                "violations": [{"message": "没有注册可执行处理器"}],
            })
            continue
        try:
            evidence = handler(ctx)
            violations = evidence.pop("violations")
            status = "passed"
            if violations:
                status = "warning" if axiom.severity == "warning" else "failed"
            results.append({
                "id": axiom.id,
                "name": axiom.name,
                "description": axiom.description,
                "severity": axiom.severity,
                "status": status,
                "expression": axiom.expression,
                **evidence,
                "violation_count": len(violations),
                "violations": violations,
            })
        except Exception as exc:
            results.append({
                "id": axiom.id,
                "name": axiom.name,
                "severity": axiom.severity,
                "status": "execution_error",
                "expression": axiom.expression,
                "checked_records": 0,
                "applicable_records": 0,
                "violation_count": 1,
                "violations": [{"message": f"执行异常: {type(exc).__name__}: {exc}"}],
            })

    truncated_tables = profile.get("truncated_tables", [])
    error_failures = [item for item in results if item["status"] in {"failed", "execution_error", "not_executable"}]
    warning_failures = [item for item in results if item["status"] == "warning"]
    passed = [item for item in results if item["status"] == "passed"]
    valid = coverage["valid"] and not truncated_tables and not error_failures
    status = "passed"
    if not valid:
        status = "failed"
    elif warning_failures:
        status = "passed_with_warnings"
    return {
        "valid": valid,
        "status": status,
        "engine": "personal-credit-axiom-executor-1.0",
        "proposal_version": proposal.version,
        "data_source": "PostgreSQL pre-sync snapshot + deterministically derived relations",
        "dataset_complete": not truncated_tables,
        "row_counts": profile.get("row_counts", {table: len(rows) for table, rows in ctx.tables.items()}),
        "truncated_tables": truncated_tables,
        "executability": coverage,
        "summary": {
            "total": len(results),
            "executed": sum(item["status"] != "not_executable" for item in results),
            "passed": len(passed),
            "failed": len(error_failures),
            "warnings": len(warning_failures),
            "violation_count": sum(item["violation_count"] for item in results),
        },
        "results": results,
    }
