# 个人信审 Interface 与公理目录 v1.0

## Interface

| Interface | 中文名称 | 实现对象 | 核心约束 |
|---|---|---|---|
| `PersonalIdentity` | 个人身份 | NaturalPerson | 身份证、姓名、手机号、地址与格式完整性 |
| `IncomeSource` | 有收入来源 | NaturalPerson | 借款人、共借人在评估前具备有效月收入 |
| `AssessableVehicle` | 可评估车辆 | Vehicle | VIN、品牌、型号、发票价格和抵押状态有效 |
| `CreditRecordHolder` | 有信用记录 | NaturalPerson | 征信唯一归属、计数非负、评估使用最新征信 |
| `GuaranteeCapablePerson` | 可担保主体 | NaturalPerson | 担保角色下评级、次数、总额完整且非负 |
| `ApplicationIntegrity` | 申请完整性 | LoanApplication | 角色、车辆、经销商、报告、评分和关联风险一致性 |

## 公理统计

- 身份与收入：4条。
- 车辆：4条。
- 征信：3条。
- 担保能力：2条。
- 申请、报告与风险：13条。
- 合计：26条公理，其中“使用最新征信”为提示级，其余为错误级约束。

## 关键业务规则

- 身份证18位；手机号11位；VIN为17位且排除I、O、Q。
- 每笔申请必须且只能关联一个借款人和一辆车辆。
- 同一申请中的借款人、担保人和共借人必须互不相同。
- 本金和期限大于零，利率、PD和阈值使用0到1的小数比例。
- 同一身份证号滚动7日内最多提交3笔申请。
- 同一VIN不能同时关联多笔未完成申请。
- PD达到阈值、欺诈评分大于70或电核存疑时，申请不得直接通过。
- 同联系方式关系不得自连，NULL和空字符串不能形成关系。

26条公理现已绑定受控执行器，可对当前 PostgreSQL 数据执行同步前批量校验，并接入发布审核和 ABox 同步门禁。详细执行语义、接口、违规证据和边界见 `PERSONAL_CREDIT_AXIOM_EXECUTION.md`。当前仍未实现流式规则引擎、CDC、持续推理或自动审批。
