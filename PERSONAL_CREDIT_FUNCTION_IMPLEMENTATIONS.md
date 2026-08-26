# 个人信审 Function 实现说明

更新日期：2026-08-24  
本体版本：`1.1.0`  
运行规则版本：`personal-credit-readonly-rules-v1.0.0`

## 1. 当前结论

个人信审本体声明的 9 个 Function 均已有只读实现。默认注册中心仍保持 9 个 Function 全部 `not_bound`；只有显式调用 `build_personal_credit_readonly_registry()` 才会绑定全部实现，因此不会因导入模块而自动启用运行能力。

Function 只读取数据、计算信号、生成问题或形成决策支持，不创建报告、不修改申请、不写 PostgreSQL/NebulaGraph，也不执行最终审批。

## 2. 实现清单

| Function | 实现版本 | 主要输入 | 主要输出 |
|---|---|---|---|
| `calculate_dti` | `dti-v1.0.0` | 月收入、各笔外部负债月还款额 | DTI、月供合计 |
| `calculate_ltv` | `ltv-v1.0.0` | 贷款本金、发票价、指导价 | LTV、采用的车辆价格 |
| `evaluate_credit_risk` | `credit-risk-rules-v1.0.0` | 征信贷款/逾期/查询次数、负债逾期状态 | 征信风险信号 |
| `evaluate_fraud_risk` | `fraud-risk-rules-v1.0.0` | 欺诈评分/结果/命中明细、关联路径 | 欺诈风险信号及查询覆盖 |
| `assess_application_risk` | `application-risk-proxy-v1.0.0` | DTI、LTV、征信和欺诈 Function 结果 | 规则代理 PD、风险等级、风险因子、核验结果 |
| `generate_phone_questions` | `phone-questions-v1.0.0` | 风险因子、欺诈命中、关联信号 | 去重且可追溯的电核问题 |
| `detect_association_risk` | `association-risk-v1.0.0` | NebulaGraph 1–2 跳关联邻域 | 关联路径、关联风险信号、覆盖范围 |
| `aggregate_decision_support` | `decision-support-v1.0.0` | 风险、反欺诈、电核结果 | 通过/拒绝/转人工/补充材料建议及原因 |
| `build_evidence_explanation` | `evidence-explanation-v1.0.0` | 申请、报告、决策支持结果 | 顺序证据链和数据血缘 |

## 3. 数据适配

`PersonalCreditFunctionDataAdapter` 一次性加载一笔申请需要的最小字段：

1. 在单个 PostgreSQL `REPEATABLE READ / READ ONLY` 事务中读取申请、月收入、车辆价格、最新征信报告、外部负债、反欺诈报告、风险报告和电核记录；
2. 单独读取 NebulaGraph 的借款人关联邻域；
3. 依赖顺序执行全部 9 个 Function；
4. 返回每个 Function 的统一信封、双数据源读取时间和执行顺序。

PostgreSQL 和 NebulaGraph 不具备跨库分布式快照，因此返回结果明确记录 `cross_store_atomic=false`。关联查询发生分页或容量截断时，`detect_association_risk` 和 `evaluate_fraud_risk` 返回 `complete=false`，综合风险 Function 拒绝继续聚合。

## 4. 失败关闭规则

- 不使用负债余额替代月还款额；
- 月收入、月供、车辆价格、征信必需计数或反欺诈结果缺失时受控失败；
- 未知枚举、负数、NaN、无穷值或超范围评分受控失败；
- 任一上游 Function 不成功或 `complete=false` 时，综合评估不输出完整结论；
- 没有有效电核结论时，决策支持只能建议补充材料或转人工；
- 电核结论虽为“信息基本一致”，但 `doubts` 仍有内容时，决策支持必须升级为转人工；
- 关联查询截断时，不能输出“未发现其他关联风险”；
- 自然人图节点在关联输出中使用 SHA-256 短引用，不返回身份证号形式的 VID。

## 5. MVP 规则边界

`assess_application_risk` 返回的 `pd_value` 是 `mvp_rule_proxy_not_calibrated_pd`：它用于验证 Function 编排、证据和门禁，不是经过样本训练、校准、回溯验证或监管审查的真实违约概率。

所有决策输出均带 `decision_support_only_not_final_approval` 边界。即使规则产生“拒绝”建议，也必须由有权人员在业务系统完成最终审批。

## 6. 真实样例验收

| 申请 | DTI | LTV | 征信信号 | 关联路径 | 欺诈信号 | 规则风险 | PD 代理值 | 电核问题 | 建议 |
|---|---:|---:|---:|---:|---:|---|---:|---:|---|
| `APP20260801000001` | 0.128 | 0.746269 | 4 | 2 | 1 | 中 | 0.17 | 5 | 转人工 |
| `APP20260803000002` | 0.066667 | 0.757576 | 1 | 0 | 3 | 中 | 0.16 | 3 | 转人工 |

两条申请均为 9/9 Function 成功、证据链完整。本次执行为只读验证，没有创建或更新风险评估报告，也没有改变图谱节点、关系或活动本体版本。
