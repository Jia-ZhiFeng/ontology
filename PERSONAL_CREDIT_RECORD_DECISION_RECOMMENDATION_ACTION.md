# 个人信审记录决策建议 Action

## 1. 完成状态

`record_decision_recommendation` 已成为 MVP 中第八个端到端业务 Action。至此，确定性 TBox 声明的8个 Action 均已有独立运行实现。

本 Action 汇总风险评估、反欺诈和电核结果，生成证据化建议并由服务端认证的有权人员本人确认。它只记录建议，不把申请改成“已完成”，不调用最终审批系统，响应固定返回：

```json
{
  "decision_authority": "recommendation_only",
  "decision_boundary": "recommendation_only_not_final_approval"
}
```

## 2. API 与请求合同

接口：`POST /api/actions/record-decision-recommendation/execute`

请求字段：

```json
{
  "application_id": "APP20260803000002",
  "recommendation_version": "rec-v1",
  "approver_id": "mvp-risk-manager",
  "approval_assertion": "我确认该记录仅为决策建议，不代表最终审批",
  "request_id": "REQ-DECISION-REC-001"
}
```

执行身份和角色来自服务端的 `MVP_DECISION_RECOMMENDATION_ACTOR_ID`、`MVP_DECISION_RECOMMENDATION_ACTOR_ROLE`。`approver_id` 必须与服务端认证身份完全一致，调用方不能冒充确认人。

业务幂等键为 `application_id + recommendation_version`。同键同业务请求重放原结果；同键但确认人或其他业务内容变化时返回冲突。`request_id` 只做链路追踪。

## 3. 执行与安全门禁

执行顺序：

```text
权限校验
→ 加载申请、风险、反欺诈和电核上下文
→ aggregate_decision_support
→ build_evidence_explanation
→ 建议、证据来源和高风险规则校验
→ 当前认证人员本人确认
→ 建议、确认摘要、证据和血缘原子写入
→ Outbox 同步 NebulaGraph
→ 审计哈希链收敛
```

关键边界：

- 只有信审员、风控专员或风险经理可以作为确认人；
- Agent 可以运行只读候选计算，但不能通过人工确认门禁，不能产生业务写入；
- 风险等级非低、PD达到阈值、反欺诈非通过、电核存疑或结论不完整时，禁止记录“通过”建议；
- 已完成申请或已有非本 Action 写入的通过/拒绝结果不得覆盖；
- `loan_application.application_status` 不会被改成“已完成”；
- 确认声明只保存固定版本和 SHA-256 摘要，审计记录确认人、角色和时间，不保存密钥或原始敏感正文。

## 4. PostgreSQL 与 NebulaGraph 一致性

PostgreSQL 控制表 `mvp_control.decision_recommendations` 保存：

- 建议 ID、申请和建议版本；
- 四类建议之一：通过、拒绝、转人工、补充材料；
- `recommendation_only` 权限边界；
- 确认人、确认角色、声明版本和声明摘要；
- 建议原因、完整证据链、来源血缘和 Function 实现版本；
- 本次写入前的申请状态和建议值，用于精确补偿。

建议记录、`loan_application.decision_result` 和 `DECISION_RECOMMENDATION_GRAPH_SYNC` outbox 在同一 PostgreSQL 事务提交。worker 使用数据库租约、事件幂等和指数退避同步已发布 ABox。

若图同步重试耗尽，worker 只在申请状态与建议仍等于本次写入、且不存在更新版本时删除本条建议并恢复原值；检测到后续业务变化时拒绝自动覆盖并进入可告警的补偿失败状态。

## 5. 自动化验收

当前覆盖：

- 固定非最终审批声明和未知字段拒绝；
- 成功、同请求重放和同版本内容冲突；
- Agent 只能计算候选、不能确认；
- 确认人与认证身份一致；
- 高风险禁止通过、低风险通过仍保持建议权限；
- 不完整 Function/证据失败关闭；
- PostgreSQL 原子提交、图重试和死信精确补偿；
- 全量后端227项和前端布局1项回归。
