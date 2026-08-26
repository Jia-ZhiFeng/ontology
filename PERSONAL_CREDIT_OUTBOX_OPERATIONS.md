# 个人信审 Action Outbox 运行监控与人工重放

运行状态更新：2026-08-25

## 1. 目标

该能力用于发现八类 Action 外部同步事件中的死信或过期处理租约，并由服务端运维身份执行受控重放。它修复的是未完成的 NebulaGraph 同步或电核任务投递，不重新执行 Function，也不重新提交 PostgreSQL 业务写入。

前端入口为“运行监控”，后端接口为：

- `GET /api/actions/outbox/status`：状态计数、过期租约和健康摘要；
- `GET /api/actions/outbox/alerts`：需要人工关注的事件；
- `GET /api/actions/outbox/replays`：人工重放历史；
- `POST /api/actions/outbox/{event_id}/replay`：提交一次受控重放。

## 2. 支持范围

人工重放显式绑定下列八种事件，不接受未知类型：

| 事件类型 | 重放效果 |
|---|---|
| `LOAN_APPLICATION_GRAPH_SYNC` | 重新同步并验证 ABox–TBox 映射 |
| `CREDIT_REPORT_GRAPH_SYNC` | 重新同步征信报告与 `HOLDS` |
| `ANTI_FRAUD_REPORT_GRAPH_SYNC` | 重新同步反欺诈报告与 `HAS_FRAUD_REPORT` |
| `RISK_ASSESSMENT_GRAPH_SYNC` | 重新同步风险报告与 `GENERATES` |
| `PHONE_VERIFICATION_TASK_DISPATCH` | 重新同步图谱并按稳定投递号投递电核任务 |
| `PHONE_VERIFICATION_RESULT_GRAPH_SYNC` | 重新同步电核终态事实并更新同步标记 |
| `MANUAL_REVIEW_GRAPH_SYNC` | 重新同步人工复核路由和证据事实 |
| `DECISION_RECOMMENDATION_GRAPH_SYNC` | 重新同步非最终决策建议和证据血缘 |

只有 `DEAD_LETTER` 事件可以创建新的重放请求。对应 Action 必须处于 `COMMITTING`、`FAILED` 或 `COMPENSATION_FAILED`；`COMPENSATED` 事件不会被重新产生副作用。

## 3. 历史保留语义

人工重放不会把历史 `FAILED` 或 `COMPENSATION_FAILED` Action 改成成功：

```text
DEAD_LETTER
  → 获取单事件数据库租约并增加尝试次数
  → 重新执行该事件的外部同步/投递 handler
  → 成功：outbox = SUCCEEDED，原 Action 终态保持不变
  → 失败：outbox = DEAD_LETTER，保留最新受控错误
```

如果 Action 仍为 `COMMITTING`，重放成功后沿用标准 worker 完成 Action；如果 Action 已经是失败终态，仅追加 `manual_outbox_replay_succeeded` 审计事件。这样不会把历史失败伪装成一次成功的原始业务执行。

每次请求都会保存操作人、角色、原因、原因摘要、请求 ID、事件/执行 ID、结果或受控错误。操作人和角色来自服务端 `MVP_OUTBOX_OPERATOR_ID`、`MVP_OUTBOX_OPERATOR_ROLE`，调用方不能在请求体中指定身份。

## 4. 幂等与并发

- `request_id` 在同一事件内唯一；相同请求 ID、相同原因和相同服务端操作人返回原结果，不再次调用 handler；
- 相同请求 ID 但原因或操作人不同返回冲突；
- 同一事件已有 `REQUESTED` 或 `PROCESSING` 重放时，新的请求返回冲突；
- 原事件使用精确行锁和处理租约领取，重放失败会恢复为 `DEAD_LETTER`；
- 重放原因要求 10–500 个字符，数据库保存原原因和 SHA-256 摘要；
- 所有接口继续受 `X-API-Key` 保护，响应经过统一脱敏。

## 5. 页面操作

1. 打开 `http://localhost:8080`，在“访问与 LLM 配置”保存当前 `MVP_API_KEY`；
2. 进入“运行监控”，查看待处理、死信、处理中、待消费、已成功和过期租约计数；
3. 点击告警行可查看完整脱敏 JSON；只有 `DEAD_LETTER` 行启用“人工重放”；
4. 填写问题原因、已完成处置和重放目的，确认不会改写原 Action 终态后提交；
5. 在下方历史表查看 `SUCCEEDED` 或 `FAILED` 结果，必要时刷新告警。

如果重放再次失败，应先根据 `error_code`、`error_message`、数据质量、公理、映射或外部系统状态排障，不要循环点击重放。

## 6. 数据表

`mvp_control.outbox_replay_requests` 保存重放请求及结果，通过外键关联原 `action_outbox` 和 `action_executions`。应用启动时幂等创建该表和索引；表结构不修改九张业务表。

原 Action 审计仍写入追加式 `mvp_control.action_audit_events`，并继续受前序哈希链和禁止更新/删除触发器保护。

## 7. 当前边界

当前已接入持久化告警、三级升级、外部调用熔断、前端红点和可选 Webhook，详见 `PERSONAL_CREDIT_OPERATIONS_ALERTING.md`。它仍是单用户 MVP 运维入口，生产化仍需：

- 登录、RBAC、运维/审核职责分离和电子签名；
- 正式指标平台、企业通知回执、动态 SLA 和值班表集成；
- 正式外部系统 handler、回调签名、供应方对账和容量故障注入；
- PostgreSQL/NebulaGraph 故障注入、浏览器端到端测试和 CI；
- 批量重放审批策略。当前实现只允许逐事件重放。

## 8. 验证结果

2026-08-25 已在真实 Docker 服务中完成合成死信验收：告警发现事件，首次重放成功，相同请求再次提交返回幂等历史；Outbox 变为 `SUCCEEDED`，原 Action 保持 `FAILED`，四条审计事件的哈希链有效。验收事件、重放记录、审计和执行记录随后按唯一执行 ID 清理，残留计数为 0；全量图同步保持 25 个业务实体和 25 条业务关系。
