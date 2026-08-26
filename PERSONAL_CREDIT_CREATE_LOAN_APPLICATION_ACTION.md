# 创建贷款申请 Action 实现说明

更新日期：2026-08-24  
适用 Action：`create_loan_application`

## 1. 当前结论

该 Action 已成为 MVP 中第二个端到端业务 Action，也是个人信审业务链的受控进件入口：

```text
服务端身份与角色校验
→ application_id 幂等预留
→ 请求结构、类型、枚举与本体公理前置校验
→ 主数据兼容性、七日频次、活动 VIN 唯一性事务校验
→ 自然人/车辆/可选经销商与 LoanApplication 写入
→ PostgreSQL 同事务写入图同步 outbox
→ 数据质量和26条公理门禁
→ NebulaGraph ABox 同步与映射回读
→ Action 成功及防篡改审计
```

它只创建进件和 `SUBMITS`、`TARGETS`、可选 `SOLD_BY`、`GUARANTEES`、`CO_BORROWS` 关系，不写入最终审批结论，也不允许从请求注入 `decision_result`。

## 2. API

```http
POST /api/actions/create-loan-application/execute
X-API-Key: <MVP_API_KEY>
Content-Type: application/json
```

最小完整样例：

```json
{
  "application_id": "APP20260824000999",
  "application": {
    "submit_date": "2026-08-24T10:30:00+08:00",
    "channel": "线上H5",
    "loan_amount": 100000,
    "loan_term": 36,
    "interest_rate": 0.045,
    "application_status": "评估中"
  },
  "borrower": {
    "id_number": "110101199901010019",
    "name": "测试借款人",
    "phone": "13900000009",
    "address": "测试地址",
    "monthly_income": 20000
  },
  "vehicle": {
    "vin": "LVGBM51K0HG999999",
    "brand": "测试品牌",
    "model": "测试车型",
    "invoice_price": 160000,
    "guide_price": 180000,
    "pledge_status": "未抵押"
  },
  "request_id": "REQ-CREATE-999"
}
```

`线下门店` 或 `经销商APP` 渠道必须提供 `dealer`。`guarantor` 和 `co_borrower` 可选；担保人出现时必须提供担保评级、次数和总额。调用方不能提交执行身份，服务端通过 `MVP_CREATE_APPLICATION_ACTOR_ID` 和 `MVP_CREATE_APPLICATION_ACTOR_ROLE` 注入，默认角色为 `系统适配器`。

## 3. 受控写入规则

- `application_id` 是 Action 幂等键；相同业务内容重放返回原执行结果，不重复写入。
- 同一 `application_id` 携带不同业务内容会返回 `ACTION_IDEMPOTENCY_CONFLICT`。
- 数据库已有同编号但没有本 Action 幂等记录时返回 `ACTION_APPLICATION_ALREADY_EXISTS`，不接管或覆盖历史申请。
- 借款人、担保人、共借人的身份证号必须两两不同。
- 新申请状态只允许 `资料待补充` 或 `评估中`，不得直接创建为已完成。
- 同一借款人滚动7日内最多3笔申请；同一 VIN 同时最多关联一笔未完成申请。
- 既有自然人、车辆或经销商只允许兼容复用：请求提供的非空字段必须与现有主数据相同；本 Action 不更新既有主数据。
- 人员、车辆、经销商、申请和 outbox 使用同一个 PostgreSQL 事务；任一写入或回读失败时整体回滚。

数据库中没有独立的关系表。`borrower_id`、`vehicle_vin`、`dealer_credit_code`、`guarantor_id`、`co_borrower_id` 外键列分别是上述五类本体关系的 ABox 映射来源。

## 4. Outbox、重试与补偿

请求线程会尝试立即消费 `LOAN_APPLICATION_GRAPH_SYNC`；未完成时返回 `202 / COMMITTING`。后台 Worker 只领取本事件类型，避免与风险评估 Worker 竞争或误取消，并支持：

- `FOR UPDATE SKIP LOCKED` 多 Worker 安全领取；
- 超时租约接管；
- 指数退避重试；
- 服务重启后续跑；
- 事务提交前无 outbox 的孤立 Action 自动失败关闭；
- 重试耗尽后进入死信并执行受限补偿。

补偿只删除本 Action 新建且内容指纹未变化的贷款申请；如果申请已产生风险、反欺诈或电核记录，或者申请本身已被修改，则拒绝自动删除并进入 `COMPENSATION_FAILED`。只清理本 Action 新建、且没有被其他申请/报告/负债引用的人员、车辆或经销商；复用的既有主数据永不删除。

## 5. 安全与验收

- Action 控制表只保存请求摘要与幂等哈希，不保存原始进件正文。
- API 参数校验错误不回显被拒绝的原始输入。
- Outbox 补偿标识使用 `id_number`、`vin`、`credit_code` 显式键，查询接口可递归脱敏。
- 审计事件形成追加式 SHA-256 哈希链。
- 13 个 `create_loan_application` 专项测试覆盖输入、权限、幂等、冲突、成功、补偿、服务端身份、事务结构和隐私。
- 2 个新增 outbox 测试覆盖贷款申请图同步成功与重试耗尽补偿。
- 真实 PostgreSQL 强制回滚探针在事务内部同时看到人员、车辆、申请和 `PENDING` outbox；退出事务后四类记录均为0。
- 真实 API 非法请求返回 `422`，不创建 Action 执行或 outbox 记录；Pydantic 错误正文不包含原始输入。

本轮没有新增或修改当前3笔业务样例、3份风险报告及已发布图谱数据。
