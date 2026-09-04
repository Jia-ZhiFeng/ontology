# 个人信审本体 Agent 上下文与能力注册表（阶段3）

## 阶段边界

本阶段建立 `OntologyContextProvider` 和 `AgentCapabilityRegistry`，为后续只读多轮运行时提供可信输入，但不启用模型的 Decide → Act → Observe → Replan 循环。`PERSONAL_CREDIT_AGENT_MODE=ontology_agent` 仍保持失败关闭，现有 `legacy` `/api/agents/personal-credit/run`、传统 Workflow 和独立 Action API 不变。

本体中的业务 `ActionType` 仍为 8 个。阶段2标准化的 4 个 legacy READ 工具仍标记为 `PROJECTED`，不会伪装成 `ActionType`；只有同时提供与已发布本体版本、proposal 摘要和完整 READ 合同精确绑定、由独立 32 字节以上信任密钥签名的受控投影证明时，注册表才允许它们进入模型清单。证明应在发布流程中签发并由运行时加载、验证，禁止按请求从 live catalog 现场签发。后续如将 READ 升级为一等本体声明，必须发布新本体版本，不能在原版本号下修改内容。

## 发布上下文

`backend/app/agent/ontology_context.py` 只从版本仓储加载真实发布记录，不使用代码基线回退。上下文解析会同时校验：

- `status=published`；未指定版本时还要求记录为当前 active；
- 发布记录版本、proposal 版本和请求版本一致；
- 重新计算的 proposal SHA-256 同时等于保存摘要和审核摘要；
- 存在带时区的发布时间；
- 案件、快照、本体版本和数据绑定摘要完全一致。

上下文身份使用服务端 HMAC 绑定本体 ID、版本、摘要、案件、快照和数据绑定摘要。模型只看到不透明 `context_id` 及非敏感版本摘要，不看到申请编号或快照编号。共享 PostgreSQL 快照也会在执行适配器处重新比较数据绑定摘要，避免同版本绑定漂移后复用旧快照。

本体切片采用重新构造的白名单 DTO，只包含选中能力引用的对象类型、关系、Function、ActionType 和公理语义摘要。Property、物理表列、metadata、公理表达式、DynamicSecurity、runtime binding、输入输出 Schema、executor、案件数据、PII、密钥、SQL/Cypher 均不进入切片；未知引用、跨版本引用、超限内容或不安全文本直接失败。

## 能力交集

`backend/app/agent/capability_registry.py` 每次发现和授权都重新构建阶段2目录，不缓存 runtime binding。模型可见集合为：

```text
已验证的发布本体上下文
∩ 当前 Action/Function runtime binding
∩ 精确 Agent allowlist
∩ deny-first 动态部署策略
∩ 认证主体的案件/快照作用域
```

4 个 READ 还必须通过独立投影证明的完整合同摘要校验；8 个 WRITE 必须具有同版本 `ActionType` 和 `DynamicSecurity`，并继续满足 `proposal_only=true`、`agent_execution_enabled=false`、`final_approval=false`。

当前本机会话和 API Key 只证明认证来源，不提供真实业务角色。它们不得借用 `MVP_*_ACTOR_ROLE` 充当调用者 RBAC；是否允许生成 WRITE Proposal 由独立部署策略控制。只有未来接入真实 IAM 的主体才会使用本体 DynamicSecurity 的 allow/deny roles，且 deny 优先。

## 模型暴露与后端授权

模型 manifest 从内部合同重新投影，不裁剪或透传内部 Schema。READ 不接受模型参数；WRITE 只允许 `trigger_reason` 或 `review_reason` 两个已声明的原因字段，其他案件、版本、快照、身份、确认、幂等和 PII 参数都由后端注入或受信通道提供。用于后端防止过期清单的 `principal_scope_digest` 不进入模型 payload。

授权时注册表会重新生成最新 manifest 并做完整相等比较。过期 manifest、错误决策枚举、额外或嵌套参数、敏感字段、SQL/Cypher、PII 和提示注入都会被拒绝。`EXECUTE_WRITE_ACTION` 在所有主体和策略下恒定拒绝；阶段3授权结果只表达 READ 调用意图或 WRITE Proposal 意图，不携带 executor，也不会产生业务副作用。

本阶段不新增公开 context/manifest API。阶段4运行时接入时应复用这些内部对象，并在每次 Action 前再次执行授权与系统参数注入校验。
