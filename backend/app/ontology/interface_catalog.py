def axiom(identifier, name, description, expression, applies_to, *, severity="error",
          validation_phase="ontology_and_data_validation"):
    return {
        "id": identifier,
        "name": name,
        "description": description,
        "expression": expression,
        "applies_to": applies_to,
        "severity": severity,
        "validation_phase": validation_phase,
    }


def interface(name, label, description, implementing_types, required_properties,
              required_relations, constraints):
    return {
        "name": name,
        "label": label,
        "description": description,
        "implementing_types": implementing_types,
        "required_properties": required_properties,
        "required_relations": required_relations,
        "constraints": constraints,
    }


PERSONAL_CREDIT_INTERFACE_CATALOG = {
    "version": "personal-credit-interfaces-1.0",
    "execution_scope": "当前仅声明和校验本体约束，不执行自动推理、自动决策或外部动作。",
    "interfaces": [
        interface(
            "PersonalIdentity", "个人身份", "统一自然人身份标识和基础联系信息。",
            ["NaturalPerson"], {"NaturalPerson": ["id_number", "name", "phone", "address"]}, [],
            [
                axiom("AX-PER-001", "身份证格式", "身份证号必须为18位，末位可为X。", "id_number matches ^[0-9]{17}[0-9Xx]$", ["NaturalPerson.id_number"]),
                axiom("AX-PER-002", "手机号格式", "已采集手机号必须为11位中国大陆手机号码。", "phone is NULL or phone matches ^1[0-9]{10}$", ["NaturalPerson.phone"]),
                axiom("AX-PER-003", "申请前身份完整", "自然人作为借款人提交申请前必须具备身份证号、姓名、手机号和地址。", "SUBMITS exists implies all(id_number,name,phone,address) are present", ["NaturalPerson", "SUBMITS"]),
            ],
        ),
        interface(
            "IncomeSource", "有收入来源", "保证还款能力评估所需收入数据可用。",
            ["NaturalPerson"], {"NaturalPerson": ["monthly_income"]}, ["SUBMITS", "CO_BORROWS"],
            [
                axiom("AX-INC-001", "收入大于零", "借款人或共借人在还款能力评估前月收入必须大于零。", "role in [借款人,共借人] and assessment starts implies monthly_income > 0", ["NaturalPerson.monthly_income", "SUBMITS", "CO_BORROWS"]),
            ],
        ),
        interface(
            "AssessableVehicle", "可评估车辆", "保证车辆金融标的具有可识别和可估值信息。",
            ["Vehicle"], {"Vehicle": ["vin", "brand", "model", "invoice_price"]}, ["TARGETS"],
            [
                axiom("AX-VEH-001", "VIN格式", "VIN必须为17位且不能包含I、O、Q。", "vin matches ^[A-HJ-NPR-Z0-9]{17}$", ["Vehicle.vin"]),
                axiom("AX-VEH-002", "发票价格有效", "进入评估的车辆发票价格必须大于零。", "TARGETS exists implies invoice_price > 0", ["Vehicle.invoice_price", "TARGETS"]),
                axiom("AX-VEH-003", "指导价格有效", "指导价格存在时必须大于零。", "guide_price is NULL or guide_price > 0", ["Vehicle.guide_price"]),
                axiom("AX-VEH-004", "抵押状态枚举", "抵押状态只能为未抵押、抵押中或已解押。", "pledge_status in [未抵押,抵押中,已解押] or pledge_status is NULL", ["Vehicle.pledge_status"]),
            ],
        ),
        interface(
            "CreditRecordHolder", "有信用记录", "保证借款主体的风险评估具有征信证据。",
            ["NaturalPerson"], {"CreditReport": ["report_id", "subject_id_number", "query_time", "overdue_3y_count", "total_overdue_count", "inquiry_count"]}, ["HOLDS"],
            [
                axiom("AX-CR-001", "征信唯一归属", "每份个人征信报告必须且只能归属于一个自然人。", "count(incoming HOLDS for CreditReport) = 1", ["CreditReport", "HOLDS"]),
                axiom("AX-CR-002", "征信计数非负", "贷款数、逾期次数和查询次数不得为负数。", "all(outstanding_loan_count,overdue_3y_count,total_overdue_count,inquiry_count) >= 0", ["CreditReport"]),
                axiom("AX-CR-003", "使用最新征信", "风险评估应使用评估时间之前查询时间最新的有效征信报告。", "selected CreditReport has max(query_time) where query_time <= assessment_time", ["NaturalPerson", "CreditReport", "RiskAssessmentReport"], severity="warning"),
            ],
        ),
        interface(
            "GuaranteeCapablePerson", "可担保主体", "量化担保人历史担保能力。",
            ["NaturalPerson"], {"NaturalPerson": ["guarantee_rating", "guarantee_count", "guarantee_total"]}, ["GUARANTEES"],
            [
                axiom("AX-GUA-001", "担保能力完整", "自然人作为担保人时必须具备担保评级、次数和总额。", "GUARANTEES exists implies all(guarantee_rating,guarantee_count,guarantee_total) are present", ["NaturalPerson", "GUARANTEES"]),
                axiom("AX-GUA-002", "担保统计非负", "担保次数和担保总额不得为负数。", "guarantee_count >= 0 and guarantee_total >= 0", ["NaturalPerson.guarantee_count", "NaturalPerson.guarantee_total"]),
            ],
        ),
        interface(
            "ApplicationIntegrity", "申请完整性", "约束个人信审申请、角色、报告和关联风险的一致性。",
            ["LoanApplication"], {"LoanApplication": ["application_id", "submit_date", "channel", "loan_amount", "loan_term", "interest_rate", "borrower_id", "vehicle_vin", "application_status"]},
            ["SUBMITS", "GUARANTEES", "CO_BORROWS", "TARGETS", "SOLD_BY", "GENERATES", "HAS_FRAUD_REPORT", "HAS_PHONE_VERIFICATION", "SHARES_CONTACT"],
            [
                axiom("AX-APP-001", "核心关系完整", "每笔申请必须且只能有一个借款人和一辆车辆。", "count(incoming SUBMITS)=1 and count(outgoing TARGETS)=1", ["LoanApplication", "SUBMITS", "TARGETS"]),
                axiom("AX-APP-002", "申请角色互斥", "同一申请中的借款人、担保人和共借人身份证号必须互不相同。", "borrower_id, guarantor_id, co_borrower_id are pairwise distinct when present", ["LoanApplication"]),
                axiom("AX-APP-003", "融资参数有效", "贷款本金、期限必须大于零，利率必须在0到1之间。", "loan_amount > 0 and loan_term > 0 and 0 < interest_rate < 1", ["LoanApplication"]),
                axiom("AX-APP-004", "经销商条件关联", "经销商APP或线下门店申请必须关联经销商。", "channel in [经销商APP,线下门店] implies count(outgoing SOLD_BY)=1", ["LoanApplication.channel", "SOLD_BY"]),
                axiom("AX-APP-005", "七日申请频次", "同一身份证号7日内最多提交3笔申请。", "count(SUBMITS by id_number within rolling 7 days) <= 3", ["NaturalPerson.id_number", "LoanApplication.submit_date", "SUBMITS"]),
                axiom("AX-APP-006", "车辆活动申请唯一", "同一VIN不能同时关联多笔未完成申请。", "count(active LoanApplication TARGETS same vin) <= 1", ["Vehicle.vin", "LoanApplication.application_status", "TARGETS"]),
                axiom("AX-APP-007", "报告基数", "每笔申请最多一份有效风险报告和反欺诈报告，电核记录允许多条。", "GENERATES <= 1 and HAS_FRAUD_REPORT <= 1 and HAS_PHONE_VERIFICATION >= 0", ["LoanApplication", "GENERATES", "HAS_FRAUD_REPORT", "HAS_PHONE_VERIFICATION"]),
                axiom("AX-RSK-001", "概率值范围", "PD值和阈值必须在0到1之间。", "0 <= pd_value <= 1 and 0 <= threshold <= 1", ["RiskAssessmentReport.pd_value", "RiskAssessmentReport.threshold"]),
                axiom("AX-RSK-002", "高风险禁止直接通过", "PD达到或超过阈值时不得直接给出通过结论。", "pd_value >= threshold implies check_result != 通过 and decision_result != 通过", ["RiskAssessmentReport", "LoanApplication.decision_result"]),
                axiom("AX-FRD-001", "欺诈评分范围", "欺诈评分必须在0到100之间。", "0 <= fraud_score <= 100", ["AntiFraudReport.fraud_score"]),
                axiom("AX-FRD-002", "高欺诈分转人工", "欺诈评分大于70时结果必须为人工复核或拒绝。", "fraud_score > 70 implies result in [人工复核,拒绝]", ["AntiFraudReport"]),
                axiom("AX-PHN-001", "电核存疑转人工", "电核结论为存疑或存在存疑点时申请不得直接通过。", "conclusion=存疑 or doubts present implies decision_result != 通过", ["PhoneVerificationRecord", "LoanApplication.decision_result"]),
                axiom("AX-SHC-001", "同联系方式有效", "同联系方式关系不能自连，且匹配值不能为空。", "source != target and matched_value not in [NULL,empty]", ["SHARES_CONTACT"]),
            ],
        ),
    ],
}
