from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

from .function_registry import (
    FunctionAlreadyBoundError,
    FunctionContractError,
    FunctionExecutionContext,
    FunctionOutput,
    FunctionRegistry,
)
from .risk_rules import (
    CREDIT_RISK_THRESHOLDS,
    DECISION_SUPPORT_VERSION,
    FRAUD_RISK_THRESHOLDS,
    PD_PROXY_RULES,
    PERSONAL_CREDIT_RULESET_VERSION,
    PHONE_QUESTION_TEMPLATES,
    PHONE_QUESTION_VERSION,
)


CREDIT_RISK_IMPLEMENTATION_VERSION = "credit-risk-rules-v1.0.0"
FRAUD_RISK_IMPLEMENTATION_VERSION = "fraud-risk-rules-v1.0.0"
APPLICATION_RISK_IMPLEMENTATION_VERSION = "application-risk-proxy-v1.0.0"
PHONE_QUESTION_IMPLEMENTATION_VERSION = "phone-questions-v1.0.0"
ASSOCIATION_RISK_IMPLEMENTATION_VERSION = "association-risk-v1.0.0"
DECISION_SUPPORT_IMPLEMENTATION_VERSION = "decision-support-v1.0.0"
EVIDENCE_EXPLANATION_IMPLEMENTATION_VERSION = "evidence-explanation-v1.0.0"

BUSINESS_FUNCTION_VERSIONS = {
    "evaluate_credit_risk": CREDIT_RISK_IMPLEMENTATION_VERSION,
    "evaluate_fraud_risk": FRAUD_RISK_IMPLEMENTATION_VERSION,
    "assess_application_risk": APPLICATION_RISK_IMPLEMENTATION_VERSION,
    "generate_phone_questions": PHONE_QUESTION_IMPLEMENTATION_VERSION,
    "detect_association_risk": ASSOCIATION_RISK_IMPLEMENTATION_VERSION,
    "aggregate_decision_support": DECISION_SUPPORT_IMPLEMENTATION_VERSION,
    "build_evidence_explanation": EVIDENCE_EXPLANATION_IMPLEMENTATION_VERSION,
}

_SEVERITY = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FunctionContractError(f"{field_name} 必须是对象")
    return value


def _list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise FunctionContractError(f"{field_name} 必须是列表")
    return list(value)


def _text(value: Any, field_name: str, *, optional: bool = False) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise FunctionContractError(f"{field_name} 不能为空")
    return value.strip()


def _decimal(value: Any, field_name: str, *, minimum: Decimal | None = None) -> Decimal:
    if value is None or isinstance(value, bool):
        raise FunctionContractError(f"{field_name} 必须是有限数字")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise FunctionContractError(f"{field_name} 必须是有限数字") from exc
    if not parsed.is_finite() or minimum is not None and parsed < minimum:
        raise FunctionContractError(f"{field_name} 超出允许范围")
    return parsed


def _count(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise FunctionContractError(f"{field_name} 必须是非负整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise FunctionContractError(f"{field_name} 必须是非负整数") from exc
    if parsed < 0 or str(parsed) != str(value).strip() and not isinstance(value, int):
        raise FunctionContractError(f"{field_name} 必须是非负整数")
    return parsed


def _signal_max(signals: list[dict[str, Any]]) -> str:
    return max((item["risk_level"] for item in signals), key=_SEVERITY.get, default="LOW")


def _unwrap_result(value: Any, field_name: str) -> Mapping[str, Any]:
    item = _mapping(value, field_name)
    if "status" in item and item.get("status") != "success":
        raise FunctionContractError(f"{field_name} 不是成功结果")
    if item.get("complete") is False:
        raise FunctionContractError(f"{field_name} 数据不完整，不能继续聚合")
    result = item.get("result", item)
    return _mapping(result, f"{field_name}.result")


def evaluate_credit_risk(
    inputs: dict[str, Any], context: FunctionExecutionContext
) -> FunctionOutput:
    report = _mapping(inputs.get("credit_report"), "credit_report")
    report_id = _text(report.get("report_id"), "credit_report.report_id")
    outstanding = _count(report.get("outstanding_loan_count"), "outstanding_loan_count")
    overdue_3y = _count(report.get("overdue_3y_count"), "overdue_3y_count")
    total_overdue = _count(report.get("total_overdue_count"), "total_overdue_count")
    inquiries = _count(report.get("inquiry_count"), "inquiry_count")
    debts = _list(inputs.get("external_debts", []), "external_debts")
    signals: list[dict[str, Any]] = []

    def add(
        code: str,
        level: str,
        description: str,
        source_property: str,
        value: Any,
        *,
        entity_id: str | None = None,
        hard_stop=False,
    ):
        signals.append({
            "signal_code": code,
            "risk_level": level,
            "description": description,
            "hard_stop": hard_stop,
            "evidence": [{"entity_id": entity_id or report_id,
                          "property": source_property, "value": value}],
        })

    t = CREDIT_RISK_THRESHOLDS
    if overdue_3y >= t["overdue_3y_high"]:
        add("MULTIPLE_OVERDUE", "HIGH", "近3年存在多次逾期", "CreditReport.overdue_3y_count", overdue_3y)
    elif overdue_3y >= t["overdue_3y_medium"]:
        add("OVERDUE_HISTORY", "MEDIUM", "近3年存在逾期记录", "CreditReport.overdue_3y_count", overdue_3y)
    if total_overdue >= t["total_overdue_high"]:
        add("TOTAL_OVERDUE_HIGH", "HIGH", "累计逾期次数较高", "CreditReport.total_overdue_count", total_overdue)
    elif total_overdue >= t["total_overdue_medium"]:
        add("TOTAL_OVERDUE", "MEDIUM", "存在多次累计逾期", "CreditReport.total_overdue_count", total_overdue)
    if inquiries >= t["inquiry_high"]:
        add("CREDIT_INQUIRY_HIGH", "HIGH", "近期征信查询次数较高", "CreditReport.inquiry_count", inquiries)
    elif inquiries >= t["inquiry_medium"]:
        add("CREDIT_INQUIRY_ELEVATED", "MEDIUM", "近期征信查询较频繁", "CreditReport.inquiry_count", inquiries)
    if outstanding >= t["outstanding_high"]:
        add("OUTSTANDING_LOANS_HIGH", "HIGH", "未结清贷款数量较高", "CreditReport.outstanding_loan_count", outstanding)
    elif outstanding >= t["outstanding_low"]:
        add("OUTSTANDING_LOANS", "LOW", "存在多笔未结清贷款", "CreditReport.outstanding_loan_count", outstanding)

    status_levels = {"逾期30天": "MEDIUM", "逾期60天": "HIGH", "逾期90天以上": "HIGH"}
    for index, raw in enumerate(debts):
        debt = _mapping(raw, f"external_debts[{index}]")
        debt_id = _text(debt.get("debt_id"), f"external_debts[{index}].debt_id")
        status = _text(debt.get("overdue_status"), f"external_debts[{index}].overdue_status")
        if status not in {"正常", *status_levels}:
            raise FunctionContractError(f"未知逾期状态：{status}")
        if status in status_levels:
            add(
                "ACTIVE_DEBT_OVERDUE_" + status.replace("逾期", "").replace("天以上", "_PLUS").replace("天", ""),
                status_levels[status],
                f"外部负债当前状态为{status}",
                "ExternalDebt.overdue_status",
                status,
                entity_id=debt_id,
                hard_stop=status == "逾期90天以上",
            )

    signals.sort(key=lambda item: (-_SEVERITY[item["risk_level"]], item["signal_code"]))
    return FunctionOutput(
        result={
            "credit_risk_signals": signals,
            "max_risk_level": _signal_max(signals),
            "signal_count": len(signals),
            "rule_version": PERSONAL_CREDIT_RULESET_VERSION,
        },
        evidence=(
            {"source": "CreditReport", "reference_id": report_id},
            {"source": "ExternalDebt", "item_count": len(debts)},
        ),
        confidence=1.0,
        complete=True,
        data_snapshot_time=context.data_snapshot_time,
    )


def evaluate_fraud_risk(
    inputs: dict[str, Any], context: FunctionExecutionContext
) -> FunctionOutput:
    report = _mapping(inputs.get("anti_fraud_report"), "anti_fraud_report")
    report_id = _text(report.get("fraud_report_id"), "anti_fraud_report.fraud_report_id")
    score = _decimal(report.get("fraud_score"), "anti_fraud_report.fraud_score", minimum=Decimal("0"))
    if score > 100:
        raise FunctionContractError("anti_fraud_report.fraud_score 必须位于 0 到 100")
    result = _text(report.get("result"), "anti_fraud_report.result")
    if result not in {"通过", "拒绝", "人工复核"}:
        raise FunctionContractError(f"未知反欺诈结果：{result}")
    details = _text(report.get("hit_details"), "anti_fraud_report.hit_details", optional=True)
    paths = _list(inputs.get("association_paths", []), "association_paths")
    coverage = dict(_mapping(inputs.get("query_coverage", {}), "query_coverage"))
    truncated = bool(coverage.get("truncated", False))
    signals: list[dict[str, Any]] = []

    def add(code: str, level: str, description: str, evidence: Any, *, hard_stop=False):
        signals.append({
            "signal_code": code,
            "risk_level": level,
            "description": description,
            "hard_stop": hard_stop,
            "evidence": evidence,
        })

    if result == "拒绝":
        add("FRAUD_REPORT_REJECT", "HIGH", "反欺诈系统返回拒绝", {"report_id": report_id}, hard_stop=True)
    elif result == "人工复核":
        add("FRAUD_REPORT_MANUAL_REVIEW", "MEDIUM", "反欺诈系统要求人工复核", {"report_id": report_id})
    if score >= FRAUD_RISK_THRESHOLDS["score_high"]:
        add("FRAUD_SCORE_HIGH", "HIGH", "欺诈评分达到高风险阈值", {"report_id": report_id, "score": float(score)})
    elif score >= FRAUD_RISK_THRESHOLDS["score_medium"]:
        add("FRAUD_SCORE_ELEVATED", "MEDIUM", "欺诈评分达到人工关注阈值", {"report_id": report_id, "score": float(score)})
    normalized_details = details.replace(" ", "")
    if details and not any(token in normalized_details for token in ("未命中", "无命中", "未发现")):
        add("FRAUD_RULE_HIT", "MEDIUM", "反欺诈报告包含规则命中明细", {"report_id": report_id})
    if paths:
        rejected = any(str(_mapping(path, "association_path").get("target_status")) in {"拒绝", "已拒绝"} for path in paths)
        add(
            "ASSOCIATED_REJECTED_CASE" if rejected else "SHARED_CONTACT_ASSOCIATION",
            "HIGH" if rejected else "MEDIUM",
            "共享联系方式关联到被拒申请" if rejected else "存在共享联系方式关联路径",
            {"path_count": len(paths)},
            hard_stop=rejected,
        )

    signals.sort(key=lambda item: (-_SEVERITY[item["risk_level"]], item["signal_code"]))
    coverage = {
        "candidate_count": int(coverage.get("candidate_count", len(paths))),
        "evaluated_count": int(coverage.get("evaluated_count", len(paths))),
        "truncated": truncated,
    }
    warnings = ("关联查询发生截断，不能表述为未发现其他风险",) if truncated else ()
    return FunctionOutput(
        result={
            "fraud_risk_signals": signals,
            "max_risk_level": _signal_max(signals),
            "signal_count": len(signals),
            "query_coverage": coverage,
            "rule_version": PERSONAL_CREDIT_RULESET_VERSION,
        },
        evidence=({"source": "AntiFraudReport", "reference_id": report_id},
                  {"source": "NebulaGraph association query", **coverage}),
        confidence=1.0 if not truncated else 0.7,
        complete=not truncated,
        warnings=warnings,
        data_snapshot_time=context.data_snapshot_time,
    )


def _threshold_factor(value: Decimal, rules, source: str) -> dict[str, Any] | None:
    for threshold, contribution, code in rules:
        if value >= threshold:
            return {"factor": code, "value": float(value), "contribution": float(contribution),
                    "evidence_source": source}
    return None


def assess_application_risk(
    inputs: dict[str, Any], context: FunctionExecutionContext
) -> FunctionOutput:
    application_id = _text(inputs.get("application_id"), "application_id")
    dti = _decimal(_unwrap_result(inputs.get("dti_result"), "dti_result").get("dti"), "dti", minimum=Decimal("0"))
    ltv = _decimal(_unwrap_result(inputs.get("ltv_result"), "ltv_result").get("ltv"), "ltv", minimum=Decimal("0"))
    credit = _unwrap_result(inputs.get("credit_risk_result"), "credit_risk_result")
    fraud = _unwrap_result(inputs.get("fraud_risk_result"), "fraud_risk_result")
    credit_signals = [_mapping(item, "credit_risk_signal") for item in _list(credit.get("credit_risk_signals"), "credit_risk_signals")]
    fraud_signals = [_mapping(item, "fraud_risk_signal") for item in _list(fraud.get("fraud_risk_signals"), "fraud_risk_signals")]

    score = PD_PROXY_RULES["base"]
    factors: list[dict[str, Any]] = []
    for value, rules, source in ((dti, PD_PROXY_RULES["dti"], "calculate_dti"),
                                 (ltv, PD_PROXY_RULES["ltv"], "calculate_ltv")):
        factor = _threshold_factor(value, rules, source)
        if factor:
            factors.append(factor)
            score += Decimal(str(factor["contribution"]))

    hard_stop = False
    for group_name, signals, cap in (
        ("credit", credit_signals, PD_PROXY_RULES["credit_signal_cap"]),
        ("fraud", fraud_signals, PD_PROXY_RULES["fraud_signal_cap"]),
    ):
        subtotal = Decimal("0")
        for signal in signals:
            level = _text(signal.get("risk_level"), f"{group_name}.risk_level")
            if level not in PD_PROXY_RULES["signal_contributions"]:
                raise FunctionContractError(f"未知风险等级：{level}")
            contribution = PD_PROXY_RULES["signal_contributions"][level]
            if subtotal + contribution > cap:
                contribution = max(Decimal("0"), cap - subtotal)
            if contribution:
                factors.append({
                    "factor": _text(signal.get("signal_code"), f"{group_name}.signal_code"),
                    "value": level,
                    "contribution": float(contribution),
                    "evidence_source": f"evaluate_{group_name}_risk",
                })
                subtotal += contribution
            hard_stop = hard_stop or bool(signal.get("hard_stop", False))
        score += subtotal

    score = min(score, PD_PROXY_RULES["maximum"]).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    if hard_stop:
        risk_level, check_result = "拒绝", "不通过"
    elif score >= PD_PROXY_RULES["high_threshold"]:
        risk_level, check_result = "高", "待人工复核"
    elif score >= PD_PROXY_RULES["medium_threshold"]:
        risk_level, check_result = "中", "待人工复核"
    else:
        risk_level, check_result = "低", "通过"

    return FunctionOutput(
        result={
            "pd_value": float(score),
            "risk_level": risk_level,
            "risk_factors": factors,
            "check_result": check_result,
            "model": {
                "name": "personal-credit-mvp-rule-proxy",
                "version": APPLICATION_RISK_IMPLEMENTATION_VERSION,
                "calibrated_pd": False,
            },
            "calculation_policy": "mvp_rule_proxy_not_calibrated_pd",
            "decision_boundary": "risk_assessment_only_not_final_approval",
        },
        evidence=(
            {"source": "calculate_dti"}, {"source": "calculate_ltv"},
            {"source": "evaluate_credit_risk"}, {"source": "evaluate_fraud_risk"},
            {"application_id": application_id, "rule_version": PERSONAL_CREDIT_RULESET_VERSION},
        ),
        confidence=0.75,
        complete=True,
        warnings=("pd_value 是未校准的 MVP 规则代理值，不得作为生产违约概率或自动审批依据",),
        data_snapshot_time=context.data_snapshot_time,
    )


def generate_phone_questions(
    inputs: dict[str, Any], context: FunctionExecutionContext
) -> FunctionOutput:
    factors = [_mapping(item, "risk_factor") for item in _list(inputs.get("risk_factors", []), "risk_factors")]
    hit_details = _text(inputs.get("fraud_hit_details"), "fraud_hit_details", optional=True)
    association_signals = [_mapping(item, "association_signal") for item in _list(inputs.get("association_risk_signals", []), "association_risk_signals")]
    categories: dict[str, set[str]] = {}
    for factor in factors:
        code = _text(factor.get("factor"), "risk_factor.factor")
        prefix = next((name for name in PHONE_QUESTION_TEMPLATES if name in code), None)
        if prefix:
            categories.setdefault(prefix, set()).add(code)
    if hit_details and not any(token in hit_details.replace(" ", "") for token in ("未命中", "无命中", "未发现")):
        categories.setdefault("FRAUD", set()).add("FRAUD_RULE_HIT")
    for signal in association_signals:
        code = _text(signal.get("signal_code"), "association_signal.signal_code")
        categories.setdefault("SHARED" if "SHARED" in code else "ASSOCIATION", set()).add(code)
    if not categories:
        categories["DTI"] = {"BASE_INFORMATION_CONFIRMATION"}

    questions = []
    for index, category in enumerate(PHONE_QUESTION_TEMPLATES, start=1):
        if category not in categories:
            continue
        label, question = PHONE_QUESTION_TEMPLATES[category]
        questions.append({
            "question_id": f"Q{len(questions)+1:02d}",
            "category": label,
            "question": question,
            "source_factors": sorted(categories[category]),
            "required": True,
        })
    return FunctionOutput(
        result={
            "phone_questions": questions,
            "question_count": len(questions),
            "template_version": PHONE_QUESTION_VERSION,
        },
        evidence=tuple({"source_factor": code} for values in categories.values() for code in sorted(values)),
        confidence=1.0,
        complete=True,
        data_snapshot_time=context.data_snapshot_time,
    )


def _node_data(node: Mapping[str, Any]) -> dict[str, Any]:
    props = node.get("properties") if isinstance(node.get("properties"), Mapping) else {}
    raw = props.get("props") or props.get("entity.props") or "{}"
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
    else:
        data = raw if isinstance(raw, Mapping) else {}
    return dict(data)


def _safe_node_ref(node_id: str, data: Mapping[str, Any]) -> str:
    ontology = data.get("_ontology") if isinstance(data.get("_ontology"), Mapping) else {}
    object_type = ontology.get("object_type")
    if object_type == "NaturalPerson" or node_id.startswith("abox:NaturalPerson:"):
        return "person_ref:" + hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:16]
    return node_id


def detect_association_risk(
    inputs: dict[str, Any], context: FunctionExecutionContext
) -> FunctionOutput:
    start = _text(inputs.get("borrower_node_id"), "borrower_node_id")
    nodes = [_mapping(item, "graph_node") for item in _list(inputs.get("graph_nodes"), "graph_nodes")]
    edges = [_mapping(item, "graph_edge") for item in _list(inputs.get("graph_edges"), "graph_edges")]
    coverage = dict(_mapping(inputs.get("query_coverage", {}), "query_coverage"))
    hops = _count(inputs.get("hops", 2), "hops")
    if not 1 <= hops <= 3:
        raise FunctionContractError("hops 必须位于 1 到 3")
    node_map = {str(node.get("id")): _node_data(node) for node in nodes}
    if start not in node_map:
        raise FunctionContractError("关联查询结果不包含借款人起点")
    allowed = {"SHARES_CONTACT", "SUBMITS", "GUARANTEES", "CO_BORROWS"}
    adjacency: dict[str, list[tuple[str, str]]] = {}
    for edge in edges:
        source, target = str(edge.get("source")), str(edge.get("target"))
        props = edge.get("properties") if isinstance(edge.get("properties"), Mapping) else {}
        kind = str(props.get("kind") or props.get("related.kind") or edge.get("kind") or "")
        if kind not in allowed:
            continue
        adjacency.setdefault(source, []).append((target, kind))
        adjacency.setdefault(target, []).append((source, kind))

    paths: list[dict[str, Any]] = []
    frontier = [(start, (start,), tuple())]
    for _depth in range(hops):
        next_frontier = []
        for current, path_nodes, relations in frontier:
            for neighbor, kind in sorted(adjacency.get(current, [])):
                if neighbor in path_nodes:
                    continue
                new_nodes, new_relations = path_nodes + (neighbor,), relations + (kind,)
                next_frontier.append((neighbor, new_nodes, new_relations))
                if "SHARES_CONTACT" not in new_relations:
                    continue
                target_data = node_map.get(neighbor, {})
                paths.append({
                    "nodes": [_safe_node_ref(node_id, node_map.get(node_id, {})) for node_id in new_nodes],
                    "relations": list(new_relations),
                    "hop_count": len(new_relations),
                    "target_status": target_data.get("decision_result") or target_data.get("application_status"),
                })
        frontier = next_frontier
    unique = []
    signatures = set()
    for path in paths:
        signature = (tuple(path["nodes"]), tuple(path["relations"]))
        if signature not in signatures:
            signatures.add(signature); unique.append(path)
    limit_truncated = len(unique) > 100
    unique = unique[:100]
    truncated = bool(coverage.get("truncated", False)) or limit_truncated
    signals = []
    for path in unique:
        rejected = str(path.get("target_status")) in {"拒绝", "已拒绝"}
        signals.append({
            "signal_code": "ASSOCIATED_REJECTED_CASE" if rejected else "SHARED_CONTACT_PATH",
            "risk_level": "HIGH" if rejected else "MEDIUM",
            "description": "共享联系方式关联到被拒申请" if rejected else "存在共享联系方式关联路径",
            "hard_stop": rejected,
            "path_reference": len(signals),
        })
    query_coverage = {
        "candidate_count": int(coverage.get("candidate_count", len(paths))),
        "evaluated_count": len(unique),
        "truncated": truncated,
    }
    return FunctionOutput(
        result={
            "association_paths": unique,
            "association_risk_signals": signals,
            "query_coverage": query_coverage,
            "rule_version": PERSONAL_CREDIT_RULESET_VERSION,
        },
        evidence=({"source": "NebulaGraph", "hops": hops, **query_coverage},),
        confidence=1.0 if not truncated else 0.7,
        complete=not truncated,
        warnings=("关联图查询被截断，风险路径结果不完整",) if truncated else (),
        data_snapshot_time=context.data_snapshot_time,
    )


def aggregate_decision_support(
    inputs: dict[str, Any], context: FunctionExecutionContext
) -> FunctionOutput:
    application = _mapping(inputs.get("application"), "application")
    application_id = _text(application.get("application_id"), "application.application_id")
    risk = _mapping(inputs.get("risk_assessment"), "risk_assessment")
    fraud = _mapping(inputs.get("anti_fraud_report"), "anti_fraud_report")
    phone = inputs.get("phone_verification")
    phone = _mapping(phone, "phone_verification") if phone is not None else {}
    risk_level = _text(risk.get("risk_level"), "risk_assessment.risk_level")
    if risk_level not in {"低", "中", "高", "拒绝"}:
        raise FunctionContractError(f"未知风险等级：{risk_level}")
    _decimal(risk.get("pd_value"), "risk_assessment.pd_value", minimum=Decimal("0"))
    fraud_result = _text(fraud.get("result"), "anti_fraud_report.result")
    if fraud_result not in {"通过", "拒绝", "人工复核"}:
        raise FunctionContractError(f"未知反欺诈结果：{fraud_result}")
    phone_conclusion = str(phone.get("conclusion") or "").strip()
    phone_has_doubts = bool(str(phone.get("doubts") or "").strip())
    reasons: list[dict[str, str]] = []

    if risk_level == "拒绝" or fraud_result == "拒绝":
        recommendation = "拒绝"
        reasons.append({"code": "HARD_RISK_REJECT", "description": "风险评估或反欺诈结果触发拒绝建议"})
    elif (
        risk_level in {"中", "高"}
        or fraud_result == "人工复核"
        or phone_conclusion == "存疑"
        or phone_has_doubts
    ):
        recommendation = "转人工"
        if risk_level in {"中", "高"}:
            reasons.append({"code": "RISK_REVIEW", "description": f"综合风险等级为{risk_level}"})
        if fraud_result == "人工复核":
            reasons.append({"code": "FRAUD_REVIEW", "description": "反欺诈系统要求人工复核"})
        if phone_conclusion == "存疑" or phone_has_doubts:
            reasons.append({"code": "PHONE_DOUBT", "description": "电核结论存在存疑"})
    elif not phone_conclusion or phone_conclusion == "未接通":
        recommendation = "补充材料"
        reasons.append({"code": "PHONE_INCOMPLETE", "description": "电核尚未形成有效结论"})
    elif risk_level == "低" and fraud_result == "通过" and phone_conclusion in {"通过", "信息基本一致"}:
        recommendation = "通过"
        reasons.append({"code": "CHECKS_PASSED", "description": "风险、反欺诈和电核检查均未触发升级条件"})
    else:
        recommendation = "转人工"
        reasons.append({"code": "UNRESOLVED_COMBINATION", "description": "存在未覆盖的结果组合，需要人工判断"})

    return FunctionOutput(
        result={
            "recommended_decision": recommendation,
            "decision_reasons": reasons,
            "decision_boundary": "decision_support_only_not_final_approval",
            "rule_version": DECISION_SUPPORT_VERSION,
        },
        evidence=(
            {"source": "LoanApplication", "reference_id": application_id},
            {"source": "RiskAssessmentReport", "reference_id": risk.get("assessment_id")},
            {"source": "AntiFraudReport", "reference_id": fraud.get("fraud_report_id")},
            {"source": "PhoneVerificationRecord", "reference_id": phone.get("verification_id")},
        ),
        confidence=1.0,
        complete=True,
        warnings=("recommended_decision 仅为决策支持，最终审批必须由有权人员在业务系统完成",),
        data_snapshot_time=context.data_snapshot_time,
    )


def build_evidence_explanation(
    inputs: dict[str, Any], context: FunctionExecutionContext
) -> FunctionOutput:
    application = _mapping(inputs.get("application"), "application")
    application_id = _text(application.get("application_id"), "application.application_id")
    sources = (
        ("RiskAssessmentReport", inputs.get("risk_assessment"), "assessment_id"),
        ("AntiFraudReport", inputs.get("anti_fraud_report"), "fraud_report_id"),
        ("PhoneVerificationRecord", inputs.get("phone_verification"), "verification_id"),
    )
    chain = [{
        "sequence": 1,
        "source_type": "LoanApplication",
        "reference_id": application_id,
        "facts": {"application_status": application.get("application_status")},
    }]
    missing = []
    for source_type, raw, id_field in sources:
        if raw is None:
            missing.append(source_type); continue
        item = _mapping(raw, source_type)
        reference = item.get(id_field)
        if not reference:
            missing.append(source_type); continue
        facts = {}
        for field in ("risk_level", "pd_value", "check_result", "result", "conclusion"):
            if item.get(field) is not None:
                facts[field] = item.get(field)
        chain.append({"sequence": len(chain)+1, "source_type": source_type,
                      "reference_id": str(reference), "facts": facts})
    decision = inputs.get("decision_support")
    if decision is not None:
        decision_result = _unwrap_result(decision, "decision_support")
        chain.append({
            "sequence": len(chain)+1,
            "source_type": "DecisionSupport",
            "reference_id": application_id,
            "facts": {
                "recommended_decision": decision_result.get("recommended_decision"),
                "decision_reasons": decision_result.get("decision_reasons"),
            },
        })
    else:
        missing.append("DecisionSupport")

    complete = not missing
    lineage = {
        "application_id": application_id,
        "ontology_version": context.ontology_version,
        "rule_versions": [PERSONAL_CREDIT_RULESET_VERSION, DECISION_SUPPORT_VERSION],
        "data_snapshot_time": context.data_snapshot_time,
        "action_execution_id": context.action_execution_id,
        "source_references": [
            {"source_type": item["source_type"], "reference_id": item["reference_id"]}
            for item in chain
        ],
        "missing_sources": missing,
    }
    return FunctionOutput(
        result={"evidence_chain": chain, "lineage": lineage},
        evidence=tuple(lineage["source_references"]),
        confidence=1.0 if complete else 0.6,
        complete=complete,
        warnings=("证据链缺少来源：" + ",".join(missing),) if missing else (),
        data_snapshot_time=context.data_snapshot_time,
    )


def bind_personal_credit_business_functions(
    registry: FunctionRegistry, *, replace: bool = False
) -> FunctionRegistry:
    implementations = {
        "evaluate_credit_risk": evaluate_credit_risk,
        "evaluate_fraud_risk": evaluate_fraud_risk,
        "assess_application_risk": assess_application_risk,
        "generate_phone_questions": generate_phone_questions,
        "detect_association_risk": detect_association_risk,
        "aggregate_decision_support": aggregate_decision_support,
        "build_evidence_explanation": build_evidence_explanation,
    }
    for name in implementations:
        registry.describe(name)
    collisions = sorted(set(implementations) & set(registry.bound_names()))
    if collisions and not replace:
        raise FunctionAlreadyBoundError(f"Function 已绑定：{','.join(collisions)}")
    for name, implementation in implementations.items():
        registry.bind(name, implementation, BUSINESS_FUNCTION_VERSIONS[name], replace=replace)
    return registry


def build_personal_credit_readonly_registry() -> FunctionRegistry:
    """显式构造绑定全部 9 个只读实现的注册中心；默认注册中心仍不绑定。"""
    from .ratio_functions import build_personal_credit_ratio_registry

    return bind_personal_credit_business_functions(build_personal_credit_ratio_registry())
