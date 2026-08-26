def attribute(name, label, value_type, data_source, *, required=False, business_key=False,
              required_when=None, enum_values=None, unit=None):
    return {
        "name": name,
        "label": label,
        "range": value_type,
        "data_source": data_source,
        "required": required,
        "business_key": business_key,
        "required_when": required_when,
        "enum_values": enum_values or [],
        "unit": unit,
    }


PERSONAL_CREDIT_ATTRIBUTE_CATALOG = {
    "version": "personal-credit-attributes-1.1",
    "null_semantics": {
        "database_null": "字段尚未采集或对应业务阶段尚未完成，不代表业务上可选。",
        "confirmed_none": "已确认不存在或不适用，应由业务状态或明确枚举表达，不能与 NULL 混用。",
        "empty_string": "视为待清洗数据，不作为有效属性值。",
    },
    "objects": {
        "NaturalPerson": {
            "table": "natural_person",
            "properties": [
                attribute("id_number", "身份证号", "string", "客户填写 + 身份证OCR", required=True, business_key=True),
                attribute("name", "姓名", "string", "客户填写 + 身份证OCR", required=True),
                attribute("phone", "手机号", "string", "客户填写", required=True, required_when="申请提交前"),
                attribute("gender", "性别", "string", "客户填写 + 身份证OCR", enum_values=["男", "女"]),
                attribute("address", "居住地址", "string", "客户填写", required=True, required_when="申请提交前"),
                attribute("marital_status", "婚姻状况", "string", "客户填写", enum_values=["未婚", "已婚", "离异", "丧偶"]),
                attribute("monthly_income", "月收入", "number", "收入证明OCR + 客户填写", required=True, required_when="还款能力评估前", unit="元/月"),
                attribute("employer", "工作单位", "string", "客户填写 + 收入证明OCR"),
                attribute("guarantee_rating", "担保能力评级", "string", "规则引擎", required_when="自然人作为担保人时", enum_values=["优", "良", "中", "受限"]),
                attribute("guarantee_count", "历史担保次数", "integer", "系统累计", required_when="自然人作为担保人时", unit="次"),
                attribute("guarantee_total", "历史担保总额", "number", "系统累计", required_when="自然人作为担保人时", unit="元"),
            ],
        },
        "Dealer": {
            "table": "dealer",
            "properties": [
                attribute("credit_code", "经销商信用代码", "string", "经销商主数据 + OCR", required=True, business_key=True),
                attribute("dealer_name", "经销商名称", "string", "经销商主数据 + OCR", required=True),
                attribute("location", "经销商所在地", "string", "经销商主数据", required=True),
            ],
        },
        "Vehicle": {
            "table": "vehicle",
            "properties": [
                attribute("vin", "VIN码", "string", "发票/合格证OCR", required=True, business_key=True),
                attribute("brand", "品牌", "string", "发票OCR + 车辆主数据", required=True),
                attribute("model", "型号", "string", "发票OCR + 车辆主数据", required=True),
                attribute("invoice_price", "发票价格", "number", "发票OCR", required=True, unit="元"),
                attribute("guide_price", "指导价格", "number", "车辆主数据", unit="元"),
                attribute("pledge_status", "抵押状态", "string", "抵押登记系统", enum_values=["未抵押", "抵押中", "已解押"]),
            ],
        },
        "LoanApplication": {
            "table": "loan_application",
            "properties": [
                attribute("application_id", "申请编号", "string", "进件系统生成", required=True, business_key=True),
                attribute("submit_date", "申请日期时间", "datetime", "进件系统记录", required=True),
                attribute("channel", "申请渠道", "string", "进件系统记录", required=True, enum_values=["线上H5", "线下门店", "经销商APP"]),
                attribute("loan_amount", "贷款本金", "number", "客户填写 + 系统校验", required=True, unit="元"),
                attribute("loan_term", "贷款期限", "integer", "客户选择", required=True, unit="月"),
                attribute("interest_rate", "贷款利率", "number", "金融方案配置 + 客户确认", required=True, unit="小数比例"),
                attribute("borrower_id", "借款人身份证号", "string", "系统关联", required=True),
                attribute("vehicle_vin", "车辆VIN码", "string", "系统关联", required=True),
                attribute("dealer_credit_code", "经销商信用代码", "string", "系统关联", required_when="申请渠道为经销商APP或线下门店时"),
                attribute("guarantor_id", "担保人身份证号", "string", "系统关联", required_when="申请存在担保人时"),
                attribute("co_borrower_id", "共借人身份证号", "string", "系统关联", required_when="申请存在共借人时"),
                attribute("application_status", "申请状态", "string", "信审系统", required=True, enum_values=["资料待补充", "评估中", "待人工复核", "已完成"]),
                attribute("decision_result", "综合决策结果", "string", "信审系统", required_when="综合决策完成后", enum_values=["通过", "拒绝", "转人工", "补充材料"]),
            ],
        },
        "CreditReport": {
            "table": "credit_report",
            "properties": [
                attribute("report_id", "征信编号", "string", "征信API返回", required=True, business_key=True),
                attribute("subject_id_number", "申请人证件号", "string", "系统关联", required=True),
                attribute("query_time", "征信查询时间", "datetime", "征信API返回", required=True, required_when="征信查询完成后"),
                attribute("application_channel", "征信申请渠道", "string", "征信查询系统", required=True, required_when="征信查询完成后"),
                attribute("outstanding_loan_count", "未结清贷款数", "integer", "征信API返回 + LLM抽取", required=True, required_when="征信报告解析完成后", unit="笔"),
                attribute("overdue_3y_count", "近3年逾期次数", "integer", "征信API返回 + LLM抽取", required=True, required_when="征信报告解析完成后", unit="次"),
                attribute("total_overdue_count", "累计逾期次数", "integer", "征信API返回 + LLM抽取", required=True, required_when="征信报告解析完成后", unit="次"),
                attribute("inquiry_count", "征信查询次数", "integer", "征信API返回 + LLM抽取", required=True, required_when="征信报告解析完成后", unit="次"),
                attribute("reporting_agency", "征信报送机构", "string", "征信API返回", required=True, required_when="征信查询完成后", enum_values=["央行征信", "百行征信", "朴道征信"]),
            ],
        },
        "ExternalDebt": {
            "table": "external_debt",
            "properties": [
                attribute("debt_id", "负债编号", "string", "征信API返回", required=True, business_key=True),
                attribute("borrower_id", "借款人身份证号", "string", "系统关联", required=True),
                attribute("creditor", "负债机构", "string", "征信API返回 + LLM抽取", required=True),
                attribute("balance", "负债余额", "number", "征信API返回 + LLM抽取", required=True, unit="元"),
                attribute("monthly_payment", "月还款额", "number", "征信API返回 + 还款计划", required_when="还款能力评估前", unit="元/月"),
                attribute("start_date", "负债起始日", "date", "征信API返回 + LLM抽取", required=True),
                attribute("overdue_status", "逾期状态", "string", "征信API返回 + LLM抽取", required=True, enum_values=["正常", "逾期30天", "逾期60天", "逾期90天以上"]),
            ],
        },
        "RiskAssessmentReport": {
            "table": "risk_assessment_report",
            "properties": [
                attribute("assessment_id", "评估报告编号", "string", "信审系统生成", required=True, business_key=True),
                attribute("application_id", "申请单编号", "string", "系统关联", required=True),
                attribute("risk_level", "风险等级", "string", "规则引擎/风控模型", required=True, required_when="评估完成后", enum_values=["低", "中", "高", "拒绝"]),
                attribute("assessment_time", "评估时间", "datetime", "信审系统记录", required=True, required_when="评估完成后"),
                attribute("assessment_channel", "评估渠道", "string", "信审系统记录", required=True, required_when="评估完成后", enum_values=["自动决策", "人工评估"]),
                attribute("pd_value", "PD值", "number", "风控模型", required=True, required_when="模型评估完成后", unit="0-1"),
                attribute("threshold", "风险阈值", "number", "规则配置", required=True, required_when="模型评估完成后", unit="0-1"),
                attribute("risk_factors", "风险因子", "string", "规则引擎/风控模型", required=True, required_when="评估完成后"),
                attribute("check_item", "核验项名称", "string", "规则引擎", required=True, required_when="核验完成后"),
                attribute("check_result", "核验结果", "string", "规则引擎/人工复核", required=True, required_when="核验完成后", enum_values=["通过", "不通过", "待人工复核"]),
                attribute("checker", "核验人", "string", "人工填写", required_when="人工复核时"),
            ],
        },
        "AntiFraudReport": {
            "table": "anti_fraud_report",
            "properties": [
                attribute("fraud_report_id", "反欺诈报告编号", "string", "反欺诈系统返回", required=True, business_key=True),
                attribute("application_id", "申请单编号", "string", "系统关联", required=True),
                attribute("fraud_score", "欺诈评分", "number", "反欺诈系统返回", required=True, required_when="反欺诈查询完成后"),
                attribute("result", "反欺诈结果", "string", "反欺诈系统返回", required=True, required_when="反欺诈查询完成后", enum_values=["通过", "拒绝", "人工复核"]),
                attribute("hit_details", "命中明细", "string", "反欺诈系统返回", required_when="命中反欺诈规则时"),
            ],
        },
        "PhoneVerificationRecord": {
            "table": "phone_verification_record",
            "properties": [
                attribute("verification_id", "电核记录编号", "string", "电核系统生成", required=True, business_key=True),
                attribute("application_id", "申请单编号", "string", "系统关联", required=True),
                attribute("verifier_name", "电核员姓名", "string", "电核系统", required=True, required_when="电核完成后"),
                attribute("verification_target", "电核对象", "string", "电核任务", required=True, required_when="电核任务创建后"),
                attribute("call_time", "通话时间", "datetime", "电核系统", required=True, required_when="电核完成后"),
                attribute("call_duration_seconds", "通话时长", "integer", "电核系统", required=True, required_when="电核完成后", unit="秒"),
                attribute("conclusion", "电核结论", "string", "人工/数字人", required=True, required_when="电核完成后", enum_values=["通过", "信息基本一致", "存疑", "未接通"]),
                attribute("doubts", "存疑点", "string", "人工/数字人", required_when="电核结论为存疑时"),
                attribute("recording_file_id", "录音文件ID", "string", "电核系统"),
            ],
        },
    },
    "excluded_database_fields": [
        {"table": "credit_report", "column": "subject_type", "reason": "当前范围固定为个人信审，该字段恒为个人，不形成有区分度的本体属性。"}
    ],
}
