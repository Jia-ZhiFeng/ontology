"""个人信审 MVP 只读 Function 的版本化规则参数。

这些参数用于技术验证，不是生产评分卡、授信政策或自动审批规则。
"""

from decimal import Decimal


PERSONAL_CREDIT_RULESET_VERSION = "personal-credit-readonly-rules-v1.0.0"

CREDIT_RISK_THRESHOLDS = {
    "overdue_3y_medium": 1,
    "overdue_3y_high": 3,
    "total_overdue_medium": 2,
    "total_overdue_high": 5,
    "inquiry_medium": 4,
    "inquiry_high": 6,
    "outstanding_low": 2,
    "outstanding_high": 5,
}

FRAUD_RISK_THRESHOLDS = {
    "score_medium": Decimal("60"),
    "score_high": Decimal("80"),
}

PD_PROXY_RULES = {
    "base": Decimal("0.010000"),
    "dti": (
        (Decimal("0.50"), Decimal("0.120000"), "DTI_VERY_HIGH"),
        (Decimal("0.35"), Decimal("0.060000"), "DTI_HIGH"),
        (Decimal("0.20"), Decimal("0.020000"), "DTI_ELEVATED"),
    ),
    "ltv": (
        (Decimal("1.00"), Decimal("0.150000"), "LTV_EXCEEDS_VALUE"),
        (Decimal("0.85"), Decimal("0.080000"), "LTV_HIGH"),
        (Decimal("0.70"), Decimal("0.030000"), "LTV_ELEVATED"),
    ),
    "signal_contributions": {
        "LOW": Decimal("0.010000"),
        "MEDIUM": Decimal("0.030000"),
        "HIGH": Decimal("0.060000"),
    },
    "credit_signal_cap": Decimal("0.150000"),
    "fraud_signal_cap": Decimal("0.200000"),
    "medium_threshold": Decimal("0.080000"),
    "high_threshold": Decimal("0.200000"),
    "maximum": Decimal("0.990000"),
}

PHONE_QUESTION_TEMPLATES = {
    "DTI": ("还款能力", "请确认当前月收入、现有贷款月供及其他固定月度负债。"),
    "LTV": ("车辆与贷款", "请确认车辆成交价格、首付金额和本次贷款本金。"),
    "OVERDUE": ("征信逾期", "请说明历史逾期发生时间、原因及目前是否已经结清。"),
    "INQUIRY": ("征信查询", "请说明近期多次征信查询对应的融资申请及当前结果。"),
    "OUTSTANDING": ("存量负债", "请逐项确认当前未结清贷款及对应月还款额。"),
    "FRAUD": ("反欺诈命中", "请核实反欺诈报告命中事项，并提供可验证的解释或材料。"),
    "SHARED": ("关联关系", "请说明与共享联系方式关联主体的真实关系及联系方式使用原因。"),
    "ASSOCIATION": ("关联关系", "请说明与关联申请人、担保人或共借人的业务关系。"),
}

DECISION_SUPPORT_VERSION = "decision-support-rules-v1.0.0"
PHONE_QUESTION_VERSION = "phone-question-templates-v1.0.0"
