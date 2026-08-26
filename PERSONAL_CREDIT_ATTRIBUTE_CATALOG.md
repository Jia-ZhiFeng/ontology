# 个人信审本体属性目录 v1.1

## 口径

- 共 9 个 ObjectType、74 个纳入本体的 Property。
- `required=true` 表示对应业务阶段完成时必须具备，不等同于 PostgreSQL 必须立即 `NOT NULL`。
- PostgreSQL `NULL` 表示尚未采集或业务阶段尚未完成；明确不存在应使用业务状态或枚举表达；空字符串属于待清洗数据。
- `credit_report.subject_type` 在个人信审范围内恒为“个人”，保留为数据源技术字段，但不纳入本体属性。

## 属性审核结果

| ObjectType | 业务主键 | 业务必填属性 | 条件必填/可选属性 | 主要来源 |
|---|---|---|---|---|
| NaturalPerson | `id_number` | `id_number`、`name`、`phone`、`address`、`monthly_income` | `gender`、`marital_status`、`employer`；作为担保人时补齐 `guarantee_rating`、`guarantee_count`、`guarantee_total` | 客户填写、身份证/收入证明 OCR、规则引擎、系统累计 |
| Dealer | `credit_code` | `credit_code`、`dealer_name`、`location` | 无 | 经销商主数据、OCR |
| Vehicle | `vin` | `vin`、`brand`、`model`、`invoice_price` | `guide_price`、`pledge_status` | 发票/合格证 OCR、车辆主数据、抵押登记系统 |
| LoanApplication | `application_id` | `application_id`、`submit_date`、`channel`、`loan_amount`、`loan_term`、`interest_rate`、`borrower_id`、`vehicle_vin`、`application_status` | `dealer_credit_code`、`guarantor_id`、`co_borrower_id`、`decision_result` | 进件系统、客户输入、金融方案、信审系统 |
| CreditReport | `report_id` | 除固定技术字段 `subject_type` 外，目录中的报告字段均在查询/解析完成后必填 | 采集完成前允许 NULL | 征信 API、LLM 抽取、征信查询系统 |
| ExternalDebt | `debt_id` | `debt_id`、`borrower_id`、`creditor`、`balance`、`start_date`、`overdue_status` | `monthly_payment` 在还款能力评估前必填 | 征信 API、还款计划、LLM 抽取 |
| RiskAssessmentReport | `assessment_id` | `assessment_id`、`application_id`；评分、阈值、风险等级、风险因子和核验结果在对应评估完成后必填 | `checker` 仅人工复核时必填 | 风控模型、规则引擎、信审系统、人工复核 |
| AntiFraudReport | `fraud_report_id` | `fraud_report_id`、`application_id`；`fraud_score`、`result` 在查询完成后必填 | `hit_details` 仅命中规则时必填 | 反欺诈系统 |
| PhoneVerificationRecord | `verification_id` | `verification_id`、`application_id`；电核执行字段在任务/通话完成后必填 | `doubts` 仅存疑时必填，`recording_file_id` 可选 | 电核系统、人工或数字人 |

## 关键类型和单位

- 身份证号、手机号、VIN、申请编号等标识全部使用字符串，禁止数值化。
- 金额使用 `number`，单位为元；月收入和月还款额单位为元/月。
- 贷款期限使用整数，单位为月；通话时长使用整数，单位为秒。
- 贷款利率、PD 和阈值使用小数比例，例如 `0.045` 表示 4.5%。
- 时间点使用 `datetime`，负债起始日使用 `date`。

完整逐字段定义由 `backend/app/ontology/attribute_catalog.py` 提供，并直接作为本体生成器的输入约束。当前目录已由 `backend/app/ontology/data_quality.py` 转化为可执行校验；一致性快照、阶段判定和发布/同步门禁见 `PERSONAL_CREDIT_DATA_QUALITY.md`。
