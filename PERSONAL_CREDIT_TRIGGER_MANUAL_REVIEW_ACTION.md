# 个人信审触发人工复核 Action

版本：`trigger-manual-review-action-1.0.0`  
日期：2026-08-25  
Action：`trigger_manual_review`

## 1. 完成结论

该 Action 已成为 MVP 中第七个端到端业务 Action。它验证高/中风险复核信号、反欺诈人工复核或规则命中、电核结论/存疑点，运行 `aggregate_decision_support` 与 `build_evidence_explanation`，再将申请路由到人工复核。它不输出“通过/拒绝”的最终审批，不调用审批系统。

## 2. 请求与来源边界

受保护接口：`POST /api/actions/trigger-manual-review/execute`。

请求只包含 `application_id`、`review_reason`、`source_execution_id` 和 `request_id`。执行 actor 固定读取服务端 `MVP_MANUAL_REVIEW_ACTOR_ID`/`MVP_MANUAL_REVIEW_ACTOR_ROLE`，调用方不能自行提交角色。

`source_execution_id` 必须满足：

- 来源 Action 属于风险评估、反欺诈、电核任务或电核结果范围；
- 来源为成功终态，或为已保留真实电核终态的图同步失败执行；
- 来源结果明确属于同一 `application_id`；
- 追加式哈希审计链完整有效。

## 3. 转人工规则与证据

至少命中以下一类规则：风险等级中/高/拒绝或风险检查要求复核；反欺诈要求复核/拒绝或命中规则；电核结论存疑或仍有存疑点。

Action 只执行声明的两个只读 Function，并要求同一 PostgreSQL 快照、固定实现版本、完整结果、证据与血缘。决策支持必须形成“转人工”或更保守的“拒绝建议”，但 Action 最终只写“待人工复核/转人工”流程路由。

## 4. 原子事务与公理

下列副作用在一个 PostgreSQL 事务中提交：

- 锁定申请并复核风险、反欺诈、电核和来源执行上下文未变化；
- 设置 `application_status=待人工复核`；
- 设置 `decision_result=转人工` 作为非最终流程路由，以满足 `AX-PHN-001`；
- 写入 `mvp_control.manual_review_records` 的原因、命中规则、来源执行、证据摘要和血缘；
- 写入 `MANUAL_REVIEW_GRAPH_SYNC` outbox。

`application_id+review_reason` 是业务幂等键。相同请求返回原执行；相同键但来源或业务载荷不同返回409。已完成、已通过或已拒绝的申请不会被该 Action 重新打开。

## 5. 同步与补偿

Worker 使用数据库租约、指数退避、重启恢复和完整映射回读。图同步重试耗尽时，仅当状态、路由和复核记录尚未被后续业务使用，才删除本次原因并恢复原 `application_status/decision_result`，随后重同步图谱；若已有后续依赖则进入 `COMPENSATION_FAILED`，拒绝覆盖业务进展。

## 6. 验证结果

- 新增8个 Action 专项测试、2个人工复核 outbox 测试、1个严格 Function 适配测试和1个电核存疑规则测试；
- 当前全量后端227项、前端布局1项通过；
- 真实隔离验收：首次200、相同请求重放200、相同业务键不同来源409；
- PostgreSQL 回读确认申请状态、转人工路由、命中规则、证据摘要、审计链与 outbox 完整；
- 验收数据已清理，申请恢复原状态，控制记录/outbox为0，NebulaGraph恢复151个节点、271条关系。
