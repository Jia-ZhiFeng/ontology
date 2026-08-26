# 个人信审本体关系目录 v1.0

## 基数口径

- 源端基数：一个源对象可连接的目标对象数量。
- 目标端基数：一个目标对象可被多少个源对象连接。
- 除 `SHARES_CONTACT` 外均为有向关系。

## 关系清单

| LinkType | 中文名称 | 源 → 目标 | 源端:目标端基数 | 关系属性 | 数据来源 |
|---|---|---|---|---|---|
| `SUBMITS` | 提交申请 | NaturalPerson → LoanApplication | 0..N:1 | `role=借款人` | `loan_application.borrower_id` |
| `GUARANTEES` | 提供担保 | NaturalPerson → LoanApplication | 0..N:0..1 | `role=担保人` | `loan_application.guarantor_id` |
| `CO_BORROWS` | 共同借款 | NaturalPerson → LoanApplication | 0..N:0..1 | `role=共借人` | `loan_application.co_borrower_id` |
| `TARGETS` | 针对车辆 | LoanApplication → Vehicle | 1:0..N | 无 | `loan_application.vehicle_vin` |
| `SOLD_BY` | 来源经销商 | LoanApplication → Dealer | 0..1:0..N | 无 | `loan_application.dealer_credit_code` |
| `HOLDS` | 持有征信报告 | NaturalPerson → CreditReport | 0..N:1 | 无 | `credit_report.subject_id_number` |
| `OWES` | 负有外部负债 | NaturalPerson → ExternalDebt | 0..N:1 | 无 | `external_debt.borrower_id` |
| `GENERATES` | 生成风险评估 | LoanApplication → RiskAssessmentReport | 0..1:1 | 无 | `risk_assessment_report.application_id` |
| `HAS_FRAUD_REPORT` | 具有关联反欺诈报告 | LoanApplication → AntiFraudReport | 0..1:1 | 无 | `anti_fraud_report.application_id` |
| `HAS_PHONE_VERIFICATION` | 具有电核记录 | LoanApplication → PhoneVerificationRecord | 0..N:1 | 无 | `phone_verification_record.application_id` |
| `SHARES_CONTACT` | 同联系方式 | NaturalPerson — NaturalPerson | 0..N:0..N | `match_type`、`matched_value` | 根据手机号或地址相等派生 |

## 建模决策

- 申请日期、申请渠道属于贷款申请单，不重复复制到 `SUBMITS`。
- 征信查询时间和报送机构属于征信报告，不重复复制到 `HOLDS`。
- 外部负债起始日和逾期状态属于负债对象，不重复复制到 `OWES`。
- 当前数据源每笔申请最多一个担保人和一个共借人，因此目标端基数为 `0..1`。
- `SHARES_CONTACT` 是派生无向关系；NULL、空字符串及自然人与自身不能形成该关系。
- `SHARES_CONTACT` 当前采用 `exact_normalized_contact_match_v1`：手机号去除格式字符和 `+86`，地址去除空白并做 Unicode 归一化；匹配明文不写入 NebulaGraph，只保存 SHA-256 摘要和证据规则。
- `SHARES_CONTACT` 只表示关联排查线索，不直接证明两人身份相同、存在欺诈或应被拒贷。
