# 个人信审 ActionCapability 合同（阶段2）

## 范围与计数

本阶段只建立后端内部、带本体版本身份的 `ActionCapability` 标准合同，不改变现有 Agent 选路，也不接入动态本体上下文、Replan 或写 Action 执行。

- 本体中仍然只有 **8 个业务 `ActionType`**，它们全部属于 `WRITE`。
- 现有 Agent 的 **4 个只读工具**被标准化为 `LEGACY_READ_TOOL / PROJECTED`，但它们不是 `ActionType`，也不计入 8 个业务 Action。
- 因此内部能力目录共有 **12 条记录：8 WRITE + 4 READ**。这只是统一合同的记录数，不表示业务 Action 从 8 个增加到了 12 个。

## 合同内容

机器可读定义与编译器位于 `backend/app/action/capabilities.py`。每条能力固定包含：

- 名称、标签、描述、标签集合、READ/WRITE、来源和发布声明状态；
- 本体 ID、版本、proposal SHA-256，以及同版本 ObjectType、Function、ActionType、DynamicSecurity 引用；
- Draft 2020-12 输入/输出 Schema；写能力的输出 Schema 描述真实 Action envelope 和嵌套 `result`，而不是把结果字段错误地视为顶层字段；
- 本体输入/输出类型、前置证据、前置条件、Function、效果、外部系统和副作用；
- 风险、确认策略、幂等字段、runtime binding、Function binding 和完整执行安全合同；
- 参数来源与模型写入边界，包括 `SERVER_CONTEXT`、`TRUSTED_USER`、`TRUSTED_CONFIRMATION` 和 `MODEL_PROPOSAL`。

参数来源采用逐 Action 完整白名单，未知字段会使目录编译失败，绝不默认开放给模型。`application_id`、`ontology_version`、`request_id`、模型/建议版本、快照策略、任务或来源执行 ID、轮次、运行操作者、回调 ID，以及批准人和批准断言等受信字段均禁止由模型覆盖。PII、进件载荷、人工/回调结果或授权材料也不会被标记为模型可写参数；当前只有电核触发原因和人工复核原因属于可由模型形成的 Proposal 参数。

## 强制安全边界

所有 8 个 WRITE capability 对 Agent 均固定为：

- `PROPOSAL_ONLY`；
- `agent_execution_enabled=false`；
- 执行前必须重新校验；
- 保留 Action 内部的权限、版本、幂等、审计、步骤 DAG、事务、一致性和补偿合同。

`record_decision_recommendation` 固定为 `CRITICAL`，保留本体人工确认门禁，并明确 `decision_support_only=true`、`final_approval=false`。真实 IAM/RBAC 接入前，不会通过此合同开放写 Action 的 Agent 执行。

4 个 READ capability 的风险分级为：就绪度 LOW、风险预览与电核问题 MEDIUM、决策支持 HIGH。它们必须没有 effects、外部副作用、幂等字段或写确认，并且引用的 Function 必须是无副作用且已绑定的实现。

## 编译门禁

`build_action_capability_catalog` 仅在以下集合和合同同时一致时成功：

```text
同版本 ActionType
∩ ActionRuntimeDesign
∩ ActionReuseProfile
∩ ReusableActionAdapterRegistry binding
∩ 请求模型 / executor 白名单
∩ DynamicSecurity
∩ 已绑定且无副作用的 Function
```

门禁要求精确覆盖 8 个 WRITE 和 4 个 READ；缺项、多项、错误请求模型、错误 executor、悬空本体引用、Function 声明与已绑定实现合同漂移、可写服务端参数或有副作用 READ 都会失败。每条 `ActionType.preconditions` 还必须精确绑定到已存在的 `validate/manual_gate` 运行步骤和对应证据键；发布本体新增前置条件但后端尚未实现时会 fail closed。能力 ID 同时包含本体版本和 proposal digest，防止同版本内容漂移后复用旧上下文。

现有本体版本兼容检查已调用该编译器。因此不兼容版本不能通过现有 Action 注册/执行入口，但 legacy `/api/agents/personal-credit/run` 的字段和行为不变。

## 暴露边界

完整 `ActionCapabilityCatalog` 是后端内部合同，其中包含请求字段、敏感字段标记和执行器绑定，**不得直接作为模型 manifest 或公开 API 响应**。当前仅提供不含 Schema、字段名、executor 和模块路径的 `safe_summary()`；阶段3应在此合同之上另行构造脱敏 capability manifest，并执行“已发布本体声明 ∩ runtime binding ∩ Agent 白名单 ∩ 动态安全策略”。
