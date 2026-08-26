# 个人信审记录电核结果 Action

版本：`record-phone-verification-action-1.0.0`  
日期：2026-08-25  
Action：`record_phone_verification`

## 1. 完成结论

该 Action 已成为 MVP 中第六个端到端业务 Action。它只接收已投递、已分配电核任务的结果回调，把一条不可覆盖的通话终态写入 PostgreSQL，并通过可靠 outbox 同步 NebulaGraph。它不更正历史终态、不触发人工复核，也不产生最终信贷决定。

## 2. 请求合同

受保护接口：`POST /api/actions/record-phone-verification/execute`。

请求字段与机器可读 Action 目录完全一致：

- `verification_id`：电核任务 ID，也是业务幂等键；
- `callback_id`：外部回调审计标识；
- `verifier_identity`：电核员或数字人显示身份；
- `call_time`：必须包含时区的 ISO 8601 时间；
- `duration_seconds`：0–86400；非“未接通”必须大于0；
- `conclusion`：仅允许“通过”“信息基本一致”“存疑”“未接通”；
- `doubts`：结论为“存疑”时必填；
- `recording_file_id`：可选录音文件引用，只保存引用，不接收音频正文。

调用者不能提交角色或分配信息。执行身份固定读取服务端 `MVP_PHONE_RESULT_ACTOR_ID` 和 `MVP_PHONE_RESULT_ACTOR_ROLE`。

## 3. 任务分配与权限

创建电核任务时，系统把允许回调的适配器 ID 和角色保存到 `mvp_control.phone_verification_tasks`。结果 Action 同时检查：

1. TBox DynamicSecurity 允许当前服务端角色；
2. 任务存在且已成功投递；
3. 当前服务端 actor ID/role 与任务保存的分配完全一致；
4. 业务电核记录尚未写入任何终态字段。

因此，仅持有 API Key 但未被任务分配的回调身份不能修改电核记录；历史种子记录或没有控制元数据的任务也不能通过该 Action 被覆盖。

## 4. 原子写入与幂等

以下内容在同一 PostgreSQL 事务中完成：

- 对任务和电核记录执行行锁；
- 更新 `public.phone_verification_record` 的终态字段；
- 写入 `mvp_control.phone_verification_callbacks` 回调收据和载荷摘要；
- 将任务标记为结果待同步；
- 写入 `PHONE_VERIFICATION_RESULT_GRAPH_SYNC` outbox 事件。

`verification_id` 是 Action 幂等键：相同任务、相同完整载荷返回首次执行结果；相同任务、不同载荷返回 `ACTION_IDEMPOTENCY_CONFLICT`。即使绕过执行记录发生并发提交，数据库行锁、终态空值条件和回调表唯一约束仍会拒绝第二次终态写入。终态更正必须由未来独立的受控更正流程完成。

## 5. 图同步失败策略

通话结果是已经发生的业务事实，不能因为 NebulaGraph 暂时不可用而从 PostgreSQL 删除。因此图同步会使用租约、指数退避和重启恢复；成功后任务进入 `SYNCHRONIZED`，重试耗尽进入 `DEAD_LETTER`，Action 失败但返回 `retained_terminal_result=true`。这与可撤销的新建报告补偿策略不同。

## 6. 受控错误

- `ACTION_ROLE_DENIED`：TBox 角色拒绝；
- `ACTION_PHONE_TASK_NOT_FOUND`：任务不存在或不是 Action 创建的受控任务；
- `ACTION_PHONE_TASK_NOT_ASSIGNED`：未投递或服务端回调身份与分配不符；
- `ACTION_PHONE_RESULT_INVALID`：字段、时间、时长、枚举或条件必填不合法；
- `ACTION_TERMINAL_STATE_CONFLICT`：终态或 callback 已存在；
- `ACTION_IDEMPOTENCY_CONFLICT`：同一任务提交不同载荷；
- `ACTION_WRITE_FAILED` / `ACTION_GRAPH_SYNC_FAILED`：持久化或图同步失败。

API 分别映射为403、404、409、422或500，且不会返回数据库异常正文、录音引用、完整存疑点或其他敏感载荷。

## 7. 验证结果

- 8个新增单元测试覆盖合同、公理、角色、任务分配、成功重放、不同载荷冲突、受控错误和图重试保留；
- 全量后端回归203项通过，前端布局测试1项通过；
- Docker 后端重建健康，OpenAPI 已包含新接口；
- 真实隔离验收依次得到首次200、相同载荷重放200和不同载荷409；
- PostgreSQL 回读确认终态、回调收据、outbox 和 `SYNCHRONIZED` 状态完整；
- 验收数据已精确清理并重新同步，最终保持3条演示电核记录、控制任务/回调/outbox均为0、NebulaGraph为151个节点和271条关系。
