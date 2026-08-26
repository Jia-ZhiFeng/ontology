# 个人信审 Action 全量运行设计

版本：`action-design-1.0.0`  
设计日期：2026-08-24；运行状态更新：2026-08-25  
适用范围：个人汽车信审 MVP 的 8 个 Action

## 1. 设计结论与边界

8 个 Action 已完成可执行级设计，设计内容包括：

- 触发条件与请求/响应合同；
- 角色权限、人工确认和敏感数据边界；
- Function 依赖 DAG；
- 允许的数据副作用；
- 幂等键、超时、重试和一致性边界；
- 成功条件、失败关闭和补偿策略；
- 审计事件与受控错误码；
- 统一 Action 执行状态机。

机器可读权威实现位于 `backend/app/action/action_design_catalog.py`，并由 `backend/tests/test_action_design_catalog.py` 自动校验。

当前8个 Action 均已成为端到端运行链路，并接入 PostgreSQL 业务/outbox 原子写入、NebulaGraph 租约消费与自动重试、重启恢复和链式审计。人工复核会校验来源审计链和证据；决策建议要求认证人员本人确认并固定标记 `recommendation_only`。图同步死信时只精确恢复本次写入；生产征信、反欺诈和电核系统仍默认关闭。TBox 中的 Action 继续保持静态能力声明，由独立运行层显式启用。

## 2. 统一执行状态机

```text
RECEIVED
  → AUTHORIZING
  → VALIDATING
  → RUNNING
      ↔ WAITING_EXTERNAL
      ↔ WAITING_APPROVAL
  → VERIFYING
  → COMMITTING
  → SUCCEEDED

任一执行阶段失败
  → FAILED

提交阶段出现部分副作用
  → COMPENSATING
  → COMPENSATED / COMPENSATION_FAILED
```

终态为：`SUCCEEDED`、`FAILED`、`COMPENSATED`、`COMPENSATION_FAILED`。终态执行记录不得原地重开；重试必须复用原幂等键，或者创建新的执行 ID 并关联原执行记录。

## 3. 统一 Action 请求和响应

所有请求由业务字段和受信执行上下文组成。调用方不得自行提交角色、权限结果、Function 结果或数据库快照标识；这些内容由服务端生成或校验。

统一响应至少包含：

```json
{
  "execution_id": "ACT-...",
  "action_name": "generate_risk_assessment",
  "status": "SUCCEEDED",
  "ontology_version": "1.1.0",
  "result": {},
  "warnings": [],
  "audit_reference": "AUD-..."
}
```

失败响应只返回受控 `ACTION_*` 错误码、可公开说明和关联 ID，不回传外部系统原始正文、数据库异常、密钥、身份证、手机号或完整风控证据。

## 4. 全 Action 总览

| Action | 发起角色 | 幂等键 | Function | 外部系统 | 允许副作用 |
|---|---|---|---|---|---|
| `create_loan_application` | 系统适配器、信审员 | `application_id` | 无 | 进件系统（入站） | 创建申请和已声明角色关系 |
| `request_credit_report` | 系统适配器、信审员 | `application_id+subject_id_number` | 无 | 征信系统 | 创建征信报告和 `HOLDS` |
| `request_fraud_report` | 系统适配器、信审员、风控专员 | `application_id` | `detect_association_risk` | 反欺诈系统 | 创建反欺诈报告和 `HAS_FRAUD_REPORT` |
| `generate_risk_assessment` | 系统适配器、风控专员 | `application_id+assessment_version` | DTI、LTV、征信风险、欺诈风险、综合风险 | 无 | 创建风险评估报告和 `GENERATES` |
| `create_phone_verification_task` | 系统适配器、信审员 | `application_id+verification_round` | `generate_phone_questions` | 电核系统（出站） | 创建电核记录和 `HAS_PHONE_VERIFICATION` |
| `record_phone_verification` | 电核员、数字人适配器 | `verification_id` | 无 | 电核系统（入站） | 更新被分配的电核记录 |
| `trigger_manual_review` | 系统适配器、信审员、风控专员、风险经理 | `application_id+review_reason` | 决策支持、证据说明 | 无 | 设置待人工复核并记录原因 |
| `record_decision_recommendation` | 信审员、风控专员、风险经理、Agent | `application_id+recommendation_version` | 决策支持、证据说明 | 无 | 只记录建议、证据和血缘 |

没有设计 `execute_approval` 或 `approve_application`。最终审批继续由有权人员在业务系统完成。

## 5. Action 详细设计

### 5.1 创建贷款申请 `create_loan_application`

请求字段：`application_id`、申请数据、借款人、车辆、可选经销商/担保人/共借人、`request_id`。

执行链：

```text
接收进件 → 权限校验 → 请求校验 → 公理校验 → 幂等占位
→ 创建 LoanApplication → 创建适用角色关系 → 回读校验 → 审计
```

完成条件：自然人、车辆、可选经销商、申请和适用关系在同一 PostgreSQL 事务中提交并回读成功，同时写入 outbox；后台 Worker 完成 NebulaGraph 同步和 ABox–TBox 映射回读后，Action 才进入 `SUCCEEDED`。

失败与补偿：任何 PostgreSQL 写入失败都回滚整笔事务，不得留下孤立申请或孤立关系。既有主数据只允许完全兼容复用，不允许本 Action 覆盖；图同步采用租约、指数退避和重启恢复，重试耗尽时只删除本 Action 创建、尚未被后续业务修改或引用的数据。主要错误包括角色拒绝、请求非法、公理失败、幂等冲突、申请已存在、主数据冲突、写入/回读失败、图同步失败和补偿失败。

### 5.2 发起征信查询 `request_credit_report`

请求字段：`application_id`、`subject_id_number`、授权引用、查询用途、`request_id`。

执行链：

```text
权限校验 → 加载申请主体 → 校验有效授权 → 幂等占位
→ 征信查询 → 响应结构校验 → 创建 CreditReport → 创建 HOLDS
→ 回读校验 → 审计
```

完成条件：征信响应有效，报告与唯一 `HOLDS` 在同一事务提交。

重试与补偿：外部查询只允许使用同一 `provider_request_id` 重试，避免重复计费或重复查询。外部成功但本地失败时，应保护性保存响应并仅重试本地写入。

### 5.3 发起反欺诈查询 `request_fraud_report`

请求字段：`application_id`、`request_id`。

执行链：

```text
权限校验 → 加载申请上下文 → 核心关系校验 → 幂等占位
├→ detect_association_risk
└→ 反欺诈系统查询
→ 合并结果校验 → 创建 AntiFraudReport → 创建 HAS_FRAUD_REPORT
→ 回读校验 → 审计
```

完成条件：关联风险和外部反欺诈结果均完整，报告与关系提交成功。

失败与补偿：Function 未绑定、结果不完整或外部响应非法时失败关闭；不得生成只有一部分证据的正式反欺诈报告。

### 5.4 生成风险评估 `generate_risk_assessment`

请求字段：`application_id`、`assessment_version`、数据快照策略、`request_id`。

执行链：

```text
权限校验 → 一致性快照 → 公理/数据门禁 → 幂等占位
├→ calculate_dti
├→ calculate_ltv
├→ evaluate_credit_risk
└→ evaluate_fraud_risk
→ assess_application_risk
→ 输出/证据校验 → 创建 RiskAssessmentReport → 创建 GENERATES
→ 回读校验 → 审计
```

完成条件：五个 Function 使用同一快照和固定实现版本完成，报告与 `GENERATES` 同事务提交。

关键边界：当前 DTI 纯计算和 PostgreSQL `ExternalDebt.monthly_payment` 一致性快照适配已经实现，明确禁止用负债余额替代月供。任一必需 Function 失败、不完整或输出合同不符时，不创建正式报告。

### 5.5 创建电核任务 `create_phone_verification_task`

请求字段：`application_id`、`verification_round`、触发原因、`request_id`。

执行链：

```text
权限校验 → 加载风险上下文 → 电核触发条件校验 → 幂等占位
→ generate_phone_questions → 问题集校验
→ 创建 PhoneVerificationRecord → 创建 HAS_PHONE_VERIFICATION
→ 写入可靠 outbox → 投递电核系统 → 审计
```

完成条件：电核任务、关系和投递事件已原子提交；实际呼叫允许异步完成。

重试与补偿：投递失败时任务保留为“待投递”，使用同一任务 ID 重试并告警，不删除已创建任务。

### 5.6 记录电核结果 `record_phone_verification`

请求字段：`verification_id`、`callback_id`、电核员身份、通话时间/时长、结论、存疑点、录音文件引用。

执行链：

```text
接收回调 → 权限校验 → 加载已分配任务 → 分配校验
→ 字段/公理校验 → 幂等校验 → 更新 PhoneVerificationRecord
→ 回读校验 → 审计
```

完成条件：同一任务终态只写一次；相同回调返回原结果，不同载荷返回终态冲突。

失败与补偿：写入失败保留原任务状态并允许同一回调重放。终态更正必须使用未来独立更正流程，不允许覆盖式重放。

### 5.7 触发人工复核 `trigger_manual_review`

请求字段：`application_id`、复核原因、来源执行 ID、`request_id`。

执行链：

```text
权限校验 → 加载决策上下文 → 转人工规则校验 → 幂等占位
→ aggregate_decision_support → build_evidence_explanation
→ 证据完整性校验 → 设置 application_status=待人工复核
→ 记录转人工路由、复核原因和证据引用 → 回读校验 → 审计
```

完成条件：至少一条转人工规则成立，状态、非最终的“转人工”路由、原因和证据引用原子提交。

失败与补偿：缺少证据、Function 未绑定或规则未命中时不得只改状态；提交失败时同时回滚状态和原因。

### 5.8 记录决策建议 `record_decision_recommendation`

请求字段：`application_id`、`recommendation_version`、确认人、确认声明、`request_id`。

执行链：

```text
权限校验 → 加载决策上下文 → 必要报告校验 → 幂等占位
→ aggregate_decision_support → build_evidence_explanation
→ 建议和高风险规则校验 → 有权人员确认
→ 记录 recommendation → 记录证据/血缘 → 回读校验 → 审计
```

完成条件：建议、证据、血缘和人工确认完整记录，响应固定返回 `decision_authority=recommendation_only`。

安全边界：Agent 可以生成候选建议和证据，但不能作为确认人；高风险或电核存疑不得建议直接通过；该 Action 不调用最终审批系统。

## 6. 统一幂等、重试和补偿原则

1. 幂等键必须在执行任何外部调用或本地写入前占位。
2. 相同幂等键和相同输入摘要返回原执行结果；相同键但不同摘要返回 `ACTION_IDEMPOTENCY_CONFLICT`。
3. Function 和数据库提交默认不自动重试；需要重试时创建新的受控执行尝试并保持原快照/版本。
4. 只有明确设计的外部调用或 outbox 投递允许自动重试。
5. 征信、反欺诈等可能计费或产生合规影响的调用必须复用供应方请求 ID。
6. 副作用步骤不得“盲重试”；提交前后必须通过幂等记录和回读校验判断真实状态。
7. 补偿失败进入 `COMPENSATION_FAILED`，必须告警并由人工处理，不得伪装为普通失败。

## 7. 统一审计合同

每个 Action 至少记录：

- 执行 ID、Action 名称、本体版本和实现版本；
- 调用者真实身份、角色、授权结果和确认人；
- 幂等键、请求摘要、数据快照时间；
- 每一步开始/结束时间、状态、耗时和受控错误码；
- Function 名称、实现版本、完整性和证据引用；
- 外部系统、供应方请求 ID、重试次数，不记录密钥和原始敏感正文；
- 副作用前后对象版本、补偿状态和最终审计引用。

所有 Action 都必须产生 `action_received`、`authorization_checked`、`idempotency_checked`、`action_succeeded` 或 `action_failed`。审计存储应采用追加写，业务角色不得修改历史记录。

## 8. 实现状态与后续顺序

| 能力 | 当前状态 |
|---|---|
| 8 个 Action 静态 TBox 声明 | 已完成 |
| 8 个 Action 运行设计目录 | 已完成 |
| Action 状态机设计 | 已完成 |
| Action 状态机流转服务 | 已完成 |
| 请求摘要与幂等哈希 | 已完成 |
| PostgreSQL 执行记录、乐观锁和幂等唯一约束 | 已完成 |
| 追加式审计表、前序哈希链和防篡改触发器 | 已完成 |
| Function 注册中心 | 已完成 |
| DTI / LTV 纯计算 | 已完成 |
| 其余 7 个 Function | 已完成只读 MVP 规则实现 |
| 全 9 Function 数据适配与统一只读执行入口 | 已完成 |
| `generate_risk_assessment` Action 执行 API 和步骤调度 | 已完成 |
| `create_loan_application` Action 执行 API、事务进件和主数据兼容复用 | 已完成 |
| `request_credit_report` Action 授权、供应商幂等收据、报告/HOLDS 和补偿 | 已完成 MVP 运行骨架；真实供应商默认关闭 |
| `request_fraud_report` Action 关联风险、供应商幂等收据、报告/HAS_FRAUD_REPORT 和补偿 | 已完成 MVP 运行骨架；真实供应商默认关闭 |
| 八类持久化 outbox、事件隔离、租约、重试和恢复 | 已完成 |
| 死信告警查询、服务端运维身份、逐事件人工重放和历史保留 | 已完成 MVP 运行骨架 |
| 外部调用持久化熔断、运行告警、三级升级与可选 Webhook | 已完成 MVP 运行骨架 |
| `create_phone_verification_task` 触发门禁、问题生成、任务/关系写入、幂等投递和死信保留 | 已完成 MVP 运行骨架；真实电核系统默认关闭 |
| `record_phone_verification` 分配校验、终态幂等、回调收据、结果/图同步和死信保留 | 已完成 MVP 运行骨架 |
| `trigger_manual_review` 来源审计、规则、双 Function 证据、状态/原因写入和精确补偿 | 已完成 MVP 运行骨架 |
| `record_decision_recommendation` 双 Function 证据、人工确认门禁、建议/血缘写入和精确补偿 | 已完成 MVP 运行骨架；不执行最终审批 |
| 生产征信/反欺诈等真实外部系统适配器 | 未实现；当前只有显式沙箱 |
| 风险报告受控写入、图同步与精确补偿 | 已完成 |
| `record_decision_recommendation` 业务执行器 | 已完成 |

控制表位于 PostgreSQL `mvp_control.action_executions` 和 `mvp_control.action_audit_events`。控制层只保存请求摘要和脱敏结果摘要，不保存原始请求；相同幂等键和相同业务摘要返回原执行，相同键但不同业务摘要返回 `ACTION_IDEMPOTENCY_CONFLICT`。`request_id` 和 `correlation_id` 只用于链路追踪，不参与业务摘要冲突判断。

下一阶段应把当前可选 Webhook 接入正式企业通知回执、生产指标平台与值班 SLA，并完成正式审批系统职责隔离；征信、反欺诈和电核仍必须等真实适配器、回调签名、供应方幂等与对账策略就绪后接入。
