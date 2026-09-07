# 个人信审本体 Agent 运行时（阶段4）

## 运行边界

阶段4在独立 `OntologyAgentRuntime` 中启用只读、有界的
Decide → Act → Observe → Replan 循环。`legacy` 运行时保持原样，回滚只需把
`PERSONAL_CREDIT_AGENT_MODE` 改回 `legacy` 并重启后端；本体运行时初始化、模型
决策或能力授权失败时绝不会静默调用 legacy 关键词路由。

模型每次只允许返回一个严格 JSON 决策：

- `LOOKUP_ONTOLOGY`：按当前 manifest 中的能力名获取受限本体切片；
- `CALL_READ_ACTIONS`：一次选择 1–4 个 READ，整批先授权、再顺序执行；
- `PROPOSE_WRITE_ACTION`：只创建内存中的待确认 Proposal；
- `ASK_USER`：请求必要的补充说明；
- `FINAL`：引用本轮 Observation 给出非最终审批答案。

协议不接受 reasoning、任意工具字段、案件/快照/身份参数、确认断言、SQL 或
Cypher。原始模型 JSON、提示词和思维过程不写入响应、trace 或日志。

## 发布与启动

启用前必须配置两个彼此独立、至少 32 字节的服务端秘密：

```dotenv
ONTOLOGY_AGENT_CONTEXT_HMAC_SECRET=<独立随机值>
ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY=<独立随机值>
PERSONAL_CREDIT_AGENT_MODE=ontology_agent
```

它们不会回退到 `MVP_API_KEY`、模型 API Key 或数据库口令。READ 投影证明只在
新本体的显式发布流程中签发，并按本体 ID、版本和 proposal digest
不可变存储在 PostgreSQL。普通 `/run` 请求只加载和验签，绝不现场签发；rollback
也只能复用历史证明。已有版本若是在配置证明密钥之前发布，必须通过新的受审核
版本完成一次发布，不能靠运行请求或回滚补签。

证明采用 proof-first 顺序：后续 Nebula 发布或版本登记失败时，可能留下同一发布
身份的不可变孤儿证明；它在目标版本成为 `published + active` 前不可用于运行，且
相同内容重试是幂等的。证明仓储、Nebula 与版本登记的统一发布状态机/对账属于后续
运维增强，不放宽当前 fail-closed 门禁。

本体模式还要求：请求已通过本机会话或 API Key 认证，并存在已测试、已启用的
托管模型。当前认证只证明来源，不冒充 IAM/RBAC 角色；本地/API 主体可按独立部署
策略生成 WRITE Proposal，但任何真实 WRITE 执行入口都未注入运行时。

## 单轮安全预算

- Action 选择最多 4 次，同时受兼容请求字段 `max_steps` 的更小值约束；
- 模型调用最多 3 次，失败尝试也计数；
- 补充本体查询最多 1 次；
- 软时限 30 秒后不启动新模型、查询或 Action；`/run` 从模型元数据、发布证明与
  运行时装配开始共用同一个 45 秒硬时限，阻塞式依赖移出事件循环；
- 同轮按 capability ID 与规范化参数签名去重；
- 所有 READ 共用同一案件快照和 data-binding digest。

案件快照查询和 READ 发布证明仓储同时设置服务端 PostgreSQL statement timeout，
避免 HTTP 协程结束后遗留无界查询。达到硬时限后不会再启动后续模型或 Action。

模型在已有成功 Observation 后失败、返回非法 FINAL 或用尽预算时，后端保留成功
结果并生成确定性答案，不重新执行 Action。`execute=false` 仍可让模型形成计划，
但 READ 调用为零；WRITE 无论该字段为何都只有 Proposal、零业务副作用。

阶段4的防重放边界仅覆盖**同一轮**的 Action 签名去重。为保持既有 `/run` 请求
Schema 不变，本阶段不向客户端增加 `request_id`，也不承诺跨请求或进程重启后的
去重。阶段6在可恢复 Agent Run 与事件持久化中引入服务端 `run_id/request_id`；
阶段7在 WRITE Proposal 持久化、确认重校验和幂等执行中绑定该请求身份与 Action
签名，届时完成跨请求重放防护。在此之前 WRITE 只存在于当前内存响应，不能执行，
因此不会产生业务写副作用。

## 上下文和响应兼容

运行时先固定真实 `published` 本体记录、proposal/audit digest 和数据绑定，再读取
案件快照并生成 context/manifest。会话作用域绑定 runtime mode、主体、本体 ID、
版本和 digest；每个新回合重新加载快照，不能跨案件、主体或本体内容复用授权。

`POST /api/agents/personal-credit/run` 的既有请求字段保持不变，响应继续包含
`plan`、`executions`、`result`、`proposed_actions`、`synthesis`、`trace`、
`answer/final_answer` 和会话字段。新增 Observation、manifest 和预算信息均为脱敏
投影。`GET /agents/personal-credit/tools` 在本体模式不返回静态工具目录，因为真实
能力必须绑定案件、版本、快照与主体；直接 readiness/静态工具执行入口暂时失败
关闭，避免绕过 Registry。

阶段4不包含 Proposal 持久化、跨请求 `request_id` 去重、人工确认后的重校验执行、
SSE 恢复事件或默认灰度切换；这些分别属于后续阶段。
