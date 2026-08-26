def function(name, label, description, input_types, reads_properties, reads_relations, returns,
             *, deterministic=True):
    return {
        "name": name,
        "label": label,
        "description": description,
        "input_types": input_types,
        "reads_properties": reads_properties,
        "reads_relations": reads_relations,
        "returns": returns,
        "deterministic": deterministic,
        "side_effects": False,
        "implementation_status": "not_bound",
    }


def action(name, label, description, input_types, output_types, invokes_functions, effects,
           preconditions, *, external_system=None, idempotency_key=None):
    return {
        "name": name,
        "label": label,
        "description": description,
        "input_types": input_types,
        "output_types": output_types,
        "invokes_functions": invokes_functions,
        "effects": effects,
        "preconditions": preconditions,
        "external_system": external_system,
        "idempotency_key": idempotency_key,
        "audit_required": True,
        "execution_mode": "declaration_only",
    }


PERSONAL_CREDIT_CAPABILITY_CATALOG = {
    "version": "personal-credit-capabilities-1.1",
    "boundary": "仅定义本体动力层契约；不绑定真实API、不执行动作、不自动写图、不自动审批。",
    "functions": [
        function("calculate_dti", "计算负债收入比", "根据月收入和外部负债月还款额计算DTI。",
                 ["NaturalPerson", "ExternalDebt"], ["NaturalPerson.monthly_income", "ExternalDebt.monthly_payment"], ["OWES"], ["dti"]),
        function("calculate_ltv", "计算贷款价值比", "根据贷款本金和车辆价格计算LTV。",
                 ["LoanApplication", "Vehicle"], ["LoanApplication.loan_amount", "Vehicle.invoice_price", "Vehicle.guide_price"], ["TARGETS"], ["ltv"]),
        function("evaluate_credit_risk", "评估征信风险", "汇总贷款、逾期和查询记录形成征信风险信号。",
                 ["NaturalPerson", "CreditReport", "ExternalDebt"], ["CreditReport.outstanding_loan_count", "CreditReport.overdue_3y_count", "CreditReport.total_overdue_count", "CreditReport.inquiry_count", "ExternalDebt.overdue_status"], ["HOLDS", "OWES"], ["credit_risk_signals"]),
        function("evaluate_fraud_risk", "评估欺诈风险", "结合反欺诈报告和同联系方式路径形成欺诈风险信号。",
                 ["LoanApplication", "AntiFraudReport", "NaturalPerson"], ["AntiFraudReport.fraud_score", "AntiFraudReport.hit_details"], ["HAS_FRAUD_REPORT", "SHARES_CONTACT"], ["fraud_risk_signals"]),
        function("assess_application_risk", "综合风险评估", "聚合DTI、LTV、征信与欺诈信号，输出PD、风险等级和风险因子。",
                 ["LoanApplication", "NaturalPerson", "Vehicle", "CreditReport", "ExternalDebt", "AntiFraudReport"], [], ["SUBMITS", "TARGETS", "HOLDS", "OWES", "HAS_FRAUD_REPORT"], ["pd_value", "risk_level", "risk_factors", "check_result"], deterministic=False),
        function("generate_phone_questions", "生成电核问题", "根据风险因子和反欺诈命中信息生成电核问题清单。",
                 ["RiskAssessmentReport", "AntiFraudReport"], ["RiskAssessmentReport.risk_factors", "AntiFraudReport.hit_details"], ["GENERATES", "HAS_FRAUD_REPORT"], ["phone_questions"], deterministic=False),
        function("detect_association_risk", "识别关联风险", "执行一度/二度关联查询并返回风险路径。",
                 ["NaturalPerson", "LoanApplication"], [], ["SHARES_CONTACT", "SUBMITS", "GUARANTEES", "CO_BORROWS"], ["association_paths", "association_risk_signals"]),
        function("aggregate_decision_support", "聚合决策建议", "汇总风险、反欺诈和电核结果形成通过、拒绝、转人工或补充材料建议。",
                 ["LoanApplication", "RiskAssessmentReport", "AntiFraudReport", "PhoneVerificationRecord"], ["RiskAssessmentReport.risk_level", "RiskAssessmentReport.pd_value", "AntiFraudReport.result", "PhoneVerificationRecord.conclusion", "PhoneVerificationRecord.doubts"], ["GENERATES", "HAS_FRAUD_REPORT", "HAS_PHONE_VERIFICATION"], ["recommended_decision", "decision_reasons"]),
        function("build_evidence_explanation", "生成证据说明", "整理规则、公理、报告来源和关联路径，形成可审核证据说明。",
                 ["LoanApplication", "RiskAssessmentReport", "AntiFraudReport", "PhoneVerificationRecord"], [], ["GENERATES", "HAS_FRAUD_REPORT", "HAS_PHONE_VERIFICATION"], ["evidence_chain", "lineage"]),
    ],
    "actions": [
        action("create_loan_application", "创建贷款申请", "创建贷款申请及借款人、车辆、经销商和可选角色关联。",
               ["NaturalPerson", "LoanApplication", "Vehicle", "Dealer"], ["LoanApplication"], [], ["create LoanApplication", "create SUBMITS/TARGETS/SOLD_BY/GUARANTEES/CO_BORROWS"], ["PersonalIdentity、AssessableVehicle和ApplicationIntegrity校验通过"], external_system="进件系统", idempotency_key="application_id"),
        action("request_credit_report", "发起征信查询", "向征信系统发起个人征信查询并登记返回报告。",
               ["NaturalPerson", "LoanApplication"], ["CreditReport"], [], ["create CreditReport", "create HOLDS"], ["存在有效借款人授权", "身份证号格式有效"], external_system="征信系统", idempotency_key="application_id+subject_id_number"),
        action("request_fraud_report", "发起反欺诈查询", "向反欺诈系统发起查询并登记返回报告。",
               ["LoanApplication", "NaturalPerson"], ["AntiFraudReport"], ["detect_association_risk"], ["create AntiFraudReport", "create HAS_FRAUD_REPORT"], ["申请核心关系完整"], external_system="反欺诈系统", idempotency_key="application_id"),
        action("generate_risk_assessment", "生成风险评估", "运行只读风险函数并登记一份风险评估报告。",
               ["LoanApplication", "NaturalPerson", "Vehicle", "CreditReport", "ExternalDebt", "AntiFraudReport"], ["RiskAssessmentReport"], ["calculate_dti", "calculate_ltv", "evaluate_credit_risk", "evaluate_fraud_risk", "assess_application_risk"], ["create RiskAssessmentReport", "create GENERATES"], ["申请、征信、反欺诈及车辆数据满足公理"], idempotency_key="application_id+assessment_version"),
        action("create_phone_verification_task", "创建电核任务", "根据风险信号生成电核问题并创建待执行电核记录。",
               ["LoanApplication", "RiskAssessmentReport", "AntiFraudReport"], ["PhoneVerificationRecord"], ["generate_phone_questions"], ["create PhoneVerificationRecord", "create HAS_PHONE_VERIFICATION"], ["风险信号触发电核条件"], external_system="电核系统", idempotency_key="application_id+verification_round"),
        action("record_phone_verification", "记录电核结果", "登记人工或数字人电核结果、存疑点和录音引用。",
               ["PhoneVerificationRecord"], ["PhoneVerificationRecord"], [], ["update PhoneVerificationRecord"], ["电核任务存在", "结果字段满足电核约束"], external_system="电核系统", idempotency_key="verification_id"),
        action("trigger_manual_review", "触发人工复核", "在高风险、欺诈命中或电核存疑时将申请标记为待人工复核。",
               ["LoanApplication", "RiskAssessmentReport", "AntiFraudReport", "PhoneVerificationRecord"], ["LoanApplication"], ["aggregate_decision_support", "build_evidence_explanation"], ["update application_status=待人工复核", "record review reasons"], ["至少命中一条转人工公理"], idempotency_key="application_id+review_reason"),
        action("record_decision_recommendation", "记录决策建议", "记录受控的决策建议和证据，不执行最终审批或外部业务操作。",
               ["LoanApplication", "RiskAssessmentReport", "AntiFraudReport", "PhoneVerificationRecord"], ["LoanApplication"], ["aggregate_decision_support", "build_evidence_explanation"], ["update decision_result as recommendation", "record evidence and lineage"], ["必要报告和校验已完成", "高风险规则不得直接通过"], idempotency_key="application_id+recommendation_version"),
    ],
}


def security(action_name, allowed_roles, denied_roles, execution_boundary, sensitive_fields,
             *, approval_required=False, conditions=None):
    return {
        "action": action_name,
        "allowed_roles": allowed_roles,
        "denied_roles": denied_roles,
        "approval_required": approval_required,
        "execution_boundary": execution_boundary,
        "sensitive_fields": sensitive_fields,
        "conditions": conditions or [],
        "audit_required": True,
    }


PERSONAL_CREDIT_SECURITY_CATALOG = {
    "version": "personal-credit-security-1.0",
    "roles": ["系统适配器", "信审员", "风控专员", "风险经理", "电核员", "数字人适配器", "Agent"],
    "policies": [
        security("create_loan_application", ["系统适配器", "信审员"], ["Agent"], "只允许创建申请及已定义关系", ["id_number", "phone", "address"]),
        security("request_credit_report", ["系统适配器", "信审员"], ["Agent"], "必须具备授权，不允许超出本申请查询", ["id_number", "credit_report"], conditions=["校验征信授权"]),
        security("request_fraud_report", ["系统适配器", "信审员", "风控专员"], ["Agent"], "只允许查询当前申请", ["id_number", "phone", "address"]),
        security("generate_risk_assessment", ["系统适配器", "风控专员"], ["Agent"], "只写评估报告，不得写最终审批结果", ["monthly_income", "credit_report", "external_debt"]),
        security("create_phone_verification_task", ["系统适配器", "信审员"], ["Agent"], "只创建电核任务和问题清单", ["phone", "risk_factors"]),
        security("record_phone_verification", ["电核员", "数字人适配器"], ["Agent"], "只能更新被分配的电核记录", ["phone", "recording_file_id", "doubts"]),
        security("trigger_manual_review", ["系统适配器", "信审员", "风控专员", "风险经理"], ["Agent"], "只改变人工复核状态，不得给出最终审批", ["risk_factors", "hit_details", "doubts"]),
        security("record_decision_recommendation", ["信审员", "风控专员", "风险经理", "Agent"], [], "decision_support_only：仅记录建议，最终审批必须由有权人员在业务系统完成", ["credit_report", "external_debt", "risk_factors", "hit_details", "doubts"], approval_required=True, conditions=["Agent输出必须附证据链", "风险经理或有权信审人员确认"]),
    ],
}
