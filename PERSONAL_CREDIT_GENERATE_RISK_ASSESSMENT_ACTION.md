# 风险评估 Action 实现说明

更新日期：2026-08-24  
适用 Action：`generate_risk_assessment`

## 1. 当前结论

该 Action 已成为 MVP 中第一个端到端业务 Action，执行链为：

```text
服务端身份与角色校验
→ application_id + assessment_version 幂等预留
→ PostgreSQL REPEATABLE READ 只读快照
→ 5 个声明 Function
→ 输出完整性、证据、版本、枚举和决策边界门禁
→ PostgreSQL 风险报告事务写入
→ 数据质量和26条公理门禁
→ NebulaGraph ABox 同步与映射校验
→ Action 成功及防篡改审计
```

它只生成风险评估报告和 `GENERATES` 关系，不修改贷款申请的最终审批结论。

## 2. API

```http
POST /api/actions/generate-risk-assessment/execute
X-API-Key: <MVP_API_KEY>
Content-Type: application/json
```

```json
{
  "application_id": "APP20260824000001",
  "assessment_version": "risk-v1",
  "data_snapshot_policy": "repeatable_read_read_only",
  "request_id": "REQ-001"
}
```

调用方不能提交 `actor_id` 或 `actor_role`。当前单用户 MVP 从服务端环境变量 `MVP_ACTION_ACTOR_ID` 和 `MVP_ACTION_ACTOR_ROLE` 注入身份，默认角色为 `风控专员`；这仍不是生产级登录/RBAC。

执行状态和审计查询：

```http
GET /api/actions/executions/{execution_id}
```

## 3. Function 与输出边界

该 Action 严格执行 TBox 声明的 5 个 Function：

1. `calculate_dti`
2. `calculate_ltv`
3. `evaluate_credit_risk`
4. `evaluate_fraud_risk`
5. `assess_application_risk`

`detect_association_risk`、电核问题、决策建议和证据说明属于其他 Action 的能力，不在本 Action 中越权调用。综合输出的 `pd_value` 是未校准的 MVP 规则代理值，必须携带 `risk_assessment_only_not_final_approval` 边界。

## 4. 报告写入规则

- 当前公理 `AX-APP-007` 要求每笔申请最多一份有效风险报告。
- 申请没有报告时，使用 `application_id + assessment_version` 的稳定 SHA-256 派生 ID 创建报告。
- 申请只有一份尚未开始的预创建空报告时，允许原位回填；预置 `threshold` 不视为评估已开始。
- 申请已有正式报告时返回 `ACTION_ASSESSMENT_ALREADY_EXISTS`，不覆盖历史结论。
- 不同版本并发写同一申请时先锁定贷款申请行，避免产生两份正式报告。

正式的多版本报告历史需要后续增加“版本号 + 生效/失效状态”并同步修改 `AX-APP-007`，当前实现不静默绕过该公理。

## 5. 跨库一致性与补偿

PostgreSQL 与 NebulaGraph 没有分布式事务，因此当前实现不声明跨库原子性：

1. PostgreSQL 在同一事务创建或回填报告，并写入 `mvp_control.action_outbox` 图同步事件；
2. 请求线程尝试立即消费，未完成时返回 `202 / COMMITTING`；
3. 后台 worker 使用 `FOR UPDATE SKIP LOCKED` 和超时租约接管事件；
4. 使用写入后的完整一致性快照执行目录质量与26条公理，再同步 NebulaGraph 并回读映射；
5. 临时失败进入 `RETRY`，按指数退避再次执行；服务重启后继续处理 `PENDING`、`RETRY` 和过期 `PROCESSING`；
6. 重试耗尽进入 `DEAD_LETTER`，删除本次新建报告或恢复空占位报告，再重同步图；
7. 成功补偿进入 `COMPENSATED`，补偿本身失败进入 `COMPENSATION_FAILED`。

如果进程在 Action 已进入执行态、但报告与 outbox 的原子事务尚未提交前崩溃，则不会留下业务报告或半条 outbox 记录。后台 worker 会识别超过租约期限且没有对应 outbox 事件的孤立 `generate_risk_assessment` 执行，将其失败关闭为 `FAILED / ACTION_EXECUTION_RECOVERED_AFTER_CRASH` 并追加审计；调用方可使用新的 `request_id` 安全重试。

当前仍需在前端增加死信告警、人工重放/确认入口，并为未来外部系统 Action 注册各自的 outbox handler。

## 6. 审计与真实验收

Action 控制表不保存原始业务请求，只保存请求摘要、幂等哈希和脱敏结果。业务审计事件可插入状态流转之间，事件以 SHA-256 前序摘要形成追加式链，数据库触发器禁止更新或删除。

真实容器受控验收使用已有正式报告的申请执行：

- HTTP：`409`
- 错误码：`ACTION_ASSESSMENT_ALREADY_EXISTS`
- 终态：`FAILED`
- 风险报告：`3 → 3`
- 图谱：`151` 节点、`271` 关系，未变化
- 申请决策：仍为“转人工”
- 审计：6 个事件，哈希链校验通过

成功创建、幂等重放、不完整 Function 失败关闭、图同步重试、过期租约接管、事务提交前孤立执行恢复、重启恢复和死信补偿由自动化测试覆盖。真实容器还验证了停机期间事件保持 `PENDING`、重启后被新 worker 接管，以及报告和 outbox 同事务写入后整体回滚；合成事件已清理，没有修改当前业务样例。
