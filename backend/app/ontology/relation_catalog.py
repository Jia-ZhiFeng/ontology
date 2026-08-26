def relation_property(name, label, value_type, source, *, required=False, derived=False, enum_values=None):
    return {
        "name": name,
        "label": label,
        "range": value_type,
        "source": source,
        "required": required,
        "derived": derived,
        "enum_values": enum_values or [],
    }


def relation(name, label, source, target, source_cardinality, target_cardinality,
             description, source_columns, *, direction="directed", properties=None, constraints=None):
    return {
        "name": name,
        "label": label,
        "source": source,
        "target": target,
        "direction": direction,
        "source_cardinality": source_cardinality,
        "target_cardinality": target_cardinality,
        "cardinality": f"{source_cardinality}:{target_cardinality}",
        "description": description,
        "source_columns": source_columns,
        "relationship_properties": properties or [],
        "constraints": constraints or [],
    }


PERSONAL_CREDIT_RELATION_CATALOG = {
    "version": "personal-credit-relations-1.0",
    "cardinality_semantics": {
        "source_cardinality": "一个源对象最多/最少可以连接多少个目标对象",
        "target_cardinality": "一个目标对象最多/最少可以被多少个源对象连接",
    },
    "relations": [
        relation(
            "SUBMITS", "提交申请", "NaturalPerson", "LoanApplication", "0..N", "1",
            "自然人以借款人角色提交贷款申请。", ["loan_application.borrower_id"],
            properties=[relation_property("role", "申请角色", "string", "关系类型常量", required=True, derived=True, enum_values=["借款人"])],
            constraints=["每笔贷款申请必须且只能有一个借款人"],
        ),
        relation(
            "GUARANTEES", "提供担保", "NaturalPerson", "LoanApplication", "0..N", "0..1",
            "自然人在具体贷款申请中承担担保人角色。", ["loan_application.guarantor_id"],
            properties=[relation_property("role", "申请角色", "string", "关系类型常量", required=True, derived=True, enum_values=["担保人"])],
            constraints=["当前数据结构每笔申请最多关联一个担保人", "没有担保人时不创建关系"],
        ),
        relation(
            "CO_BORROWS", "共同借款", "NaturalPerson", "LoanApplication", "0..N", "0..1",
            "自然人在具体贷款申请中承担共借人角色。", ["loan_application.co_borrower_id"],
            properties=[relation_property("role", "申请角色", "string", "关系类型常量", required=True, derived=True, enum_values=["共借人"])],
            constraints=["当前数据结构每笔申请最多关联一个共借人", "没有共借人时不创建关系"],
        ),
        relation(
            "TARGETS", "针对车辆", "LoanApplication", "Vehicle", "1", "0..N",
            "贷款申请以一辆车辆作为金融标的。", ["loan_application.vehicle_vin"],
            constraints=["每笔申请必须关联一辆车辆"],
        ),
        relation(
            "SOLD_BY", "来源经销商", "LoanApplication", "Dealer", "0..1", "0..N",
            "贷款申请关联车辆销售或进件经销商。", ["loan_application.dealer_credit_code"],
            constraints=["经销商渠道或线下门店申请应关联经销商", "直接线上申请可以不关联经销商"],
        ),
        relation(
            "HOLDS", "持有征信报告", "NaturalPerson", "CreditReport", "0..N", "1",
            "自然人关联其历次征信查询报告。", ["credit_report.subject_id_number"],
            constraints=["每份个人征信报告必须且只能关联一个自然人"],
        ),
        relation(
            "OWES", "负有外部负债", "NaturalPerson", "ExternalDebt", "0..N", "1",
            "自然人关联其外部存量负债。", ["external_debt.borrower_id"],
            constraints=["每笔外部负债必须且只能关联一个自然人"],
        ),
        relation(
            "GENERATES", "生成风险评估", "LoanApplication", "RiskAssessmentReport", "0..1", "1",
            "贷款申请完成风险评估后生成一份风险评估报告。", ["risk_assessment_report.application_id"],
            constraints=["当前本体版本每笔申请最多保留一份有效风险评估报告"],
        ),
        relation(
            "HAS_FRAUD_REPORT", "具有关联反欺诈报告", "LoanApplication", "AntiFraudReport", "0..1", "1",
            "贷款申请完成反欺诈查询后关联一份反欺诈报告。", ["anti_fraud_report.application_id"],
            constraints=["当前本体版本每笔申请最多保留一份有效反欺诈报告"],
        ),
        relation(
            "HAS_PHONE_VERIFICATION", "具有电核记录", "LoanApplication", "PhoneVerificationRecord", "0..N", "1",
            "贷款申请可以关联多次电核尝试和结果。", ["phone_verification_record.application_id"],
            constraints=["每条电核记录必须且只能属于一笔贷款申请"],
        ),
        relation(
            "SHARES_CONTACT", "同联系方式", "NaturalPerson", "NaturalPerson", "0..N", "0..N",
            "两个不同自然人因相同手机号或地址形成无向关联，用于关联欺诈分析。",
            ["natural_person.phone", "natural_person.address"], direction="undirected",
            properties=[
                relation_property("match_type", "匹配类型", "string", "phone/address 字段匹配结果", required=True, derived=True, enum_values=["phone", "address"]),
                relation_property("matched_value", "匹配值", "string", "natural_person.phone 或 natural_person.address", required=True, derived=True),
            ],
            constraints=["不允许自然人与自身建立关系", "无向关系按自然人主键排序去重", "NULL和空字符串不得形成匹配关系"],
        ),
    ],
}
