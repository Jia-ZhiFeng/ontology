# 个人信审创建电核任务 Action

更新时间：2026-08-25  
Action：`create_phone_verification_task`  
接口：`POST /api/actions/create-phone-verification-task/execute`

## 1. 当前状态

该 Action 已成为 MVP 中第五个端到端业务 Action。它根据已落库的风险评估和反欺诈结果验证电核触发条件，只执行声明的 `generate_phone_questions` Function，创建待执行 `PhoneVerificationRecord`，同步 `HAS_PHONE_VERIFICATION`，并通过可靠 outbox 向电核系统投递任务。

它只负责创建与投递任务，不记录通话结果、不修改申请决策，也不执行最终审批。

## 2. 请求

```json
{
  "application_id": "APP20260801000001",
  "verification_round": 2,
  "trigger_reason": "风险评估待人工复核",
  "request_id": "REQ-PHONE-001"
}
```

- `application_id`：贷款申请编号；
- `verification_round`：1–99 的电核轮次，也是业务幂等键的一部分；
- `trigger_reason`：调用方提供的业务说明，仅用于任务留痕，不能替代系统触发校验；
- `request_id`：调用链关联编号，不参与业务幂等键。

执行身份由服务端 `MVP_PHONE_TASK_ACTOR_ID` 和 `MVP_PHONE_TASK_ACTOR_ROLE` 提供，调用方不能提交角色。

## 3. 受控执行链

```text
服务端身份与权限
→ application_id + verification_round 幂等占位
→ 加载风险评估、反欺诈报告和借款人
→ 使用真实报告校验电核触发信号
→ 仅执行 generate_phone_questions
→ 校验问题数量、唯一编号、分类、来源、模板和 Function 版本
→ PhoneVerificationRecord + 任务元数据 + outbox 同一 PostgreSQL 事务提交
→ 同步 NebulaGraph HAS_PHONE_VERIFICATION
→ 使用稳定 dispatch_id 投递电核系统
→ 完成审计或进入持久化重试
```

当前触发条件至少命中一项：中/高/拒绝风险等级、风险核验待人工/不通过、反欺诈人工复核/拒绝，或存在有效反欺诈命中。调用方填写的 `trigger_reason` 不能自行触发任务。

## 4. 数据边界

公开 ABox 继续使用既有 `phone_verification_record` 表，不增加未发布属性：

- 任务创建时只写 `verification_id`、`application_id` 和 `verification_target`；
- 电核员、通话时间、时长、结论、存疑点和录音引用保持为空，留给 `record_phone_verification`；
- `HAS_PHONE_VERIFICATION` 由 `application_id` 映射生成。

问题集、轮次、触发信号和投递状态保存在 `mvp_control.phone_verification_tasks`，属于执行控制元数据，不进入当前 TBox/ABox 属性目录。这避免为一个运行控制字段破坏已发布本体版本和字段映射门禁。

## 5. 投递、幂等与失败策略

- `dispatch_id` 由 `application_id + verification_round` 确定性生成；
- `mvp_control.phone_dispatch_receipts` 在外部接收成功后先保存响应，再确认 outbox；
- 重启或租约接管后复用成功收据，不重复创建外部任务；
- 真实适配器必须声明支持投递编号幂等，否则拒绝调用；
- `PHONE_TASK_DISPATCH_MODE` 默认是 `disabled`；本地验证可显式设为 `sandbox`；
- 投递失败时任务保持 `PENDING` 并指数退避重试；
- 重试耗尽后 outbox 进入 `DEAD_LETTER`，任务保留并标记 `DEAD_LETTER`，不会删除电核记录；
- 后续仍需增加死信告警和人工重放界面。

## 6. 返回边界

成功投递返回 `SUCCEEDED`，包含任务编号、轮次、问题数量、模板版本、外部任务编号、投递模式及图同步摘要。真实投递未配置或暂时失败时返回 `COMMITTING`/HTTP 202，明确表示任务已创建但仍待投递。

响应不会返回借款人姓名、完整问题集、风险因子正文或调用方触发说明。

## 7. 验证结果

- 新增9个 Action 专项测试；
- Function 数据适配测试增加为11个；
- outbox 测试增加为20个，包含成功投递、临时失败和死信保留；
- 全量后端回归为195个测试，另有1个前端布局测试；
- 真实 PostgreSQL 回滚探针验证：APP1 命中2个触发信号，生成1个问题，任务与 `HAS_PHONE_VERIFICATION` outbox 同事务可见；
- 同一沙箱投递第二次命中持久化缓存并返回相同外部任务编号；
- 回滚后临时任务、outbox、收据和 Action 执行记录均为0残留。

## 8. 尚未覆盖

- 真实电核供应商协议、鉴权、签名和回调地址；
- 电核任务死信操作台和人工重放；
- 通话结果回传，属于下一 Action `record_phone_verification`；
- 录音文件存储、访问授权和保留策略；
- 最终审批。
