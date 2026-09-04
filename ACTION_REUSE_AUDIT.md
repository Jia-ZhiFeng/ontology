# 八个 Action 复用性审核与改造说明

## 审核结论

八个个人信审 Action 均保留原业务入口。改造不把不同业务强行合并为一个万能 Action，而是将公共运行能力和业务绑定分离：

- 公共运行能力：状态机、鉴权、幂等、审计、超时/重试边界、事务/补偿合同。
- 业务绑定：本体对象和字段、业务公理、Function、Repository、外部供应商、结果枚举及状态映射。

| 当前 Action | 复用原型 | 复用模式 | 结论 |
| --- | --- | --- | --- |
| `create_loan_application` | `create_business_case` | 编排模板 | 复用案件创建流程，贷款对象与关系单独绑定 |
| `request_credit_report` | `request_external_report` | 编排模板 | 与反欺诈共用外部报告获取模板，保留征信授权语义 |
| `request_fraud_report` | `request_external_report` | 编排模板 | 与征信共用外部报告获取模板，保留关联风险逻辑 |
| `generate_risk_assessment` | `generate_assessment` | 编排模板 | 复用快照和 Function 编排，模型与门禁按本体绑定 |
| `create_phone_verification_task` | `create_verification_task` | 编排模板 | 电话作为当前渠道绑定，原型可用于其他核验渠道 |
| `record_phone_verification` | `record_verification_result` | 运行核心 | 复用任务归属、回调幂等和结果登记 |
| `trigger_manual_review` | `route_human_review` | 运行核心 | 复用证据门禁和人工路由，规则及状态由业务定义 |
| `record_decision_recommendation` | `record_decision_support` | 运行核心 | 复用证据和人工确认，保持仅建议、非最终审批 |

八个 Action 对应七个复用原型，其中两个外部报告 Action 共用 `request_external_report`。

## 已完成改造

1. `ActionExecutionService` 支持注入任意本体的 `proposal` 和运行设计目录，不再只能从个人信审全局目录解析 Action 和幂等字段。
2. 新增 Action 复用审核档案，明确每个 Action 的复用原型、可复用能力和必须实现的业务绑定点。
3. 新增 `ReusableActionAdapterRegistry`，以 `ontology_id + action_name` 注册请求模型与执行器。同一个 Action 名称可以在不同业务本体下绑定不同实现。
4. 现有个人信审八个 Action 已全部注册到统一适配器注册表；原有八个执行接口和工作流保持兼容。
5. 新增通用接口：

   - `GET /api/actions/reuse-catalog`：查看八个 Action 的审核结果。
   - `GET /api/ontologies/{ontology_id}/actions`：查看某个本体已绑定的 Action。
   - `POST /api/ontologies/{ontology_id}/actions/{action_name}/execute`：通过统一入口执行本体 Action，请求体直接使用目标 Action 的业务入参。

## 第二阶段：版本锁定与本体数据绑定

1. Action 适配器现在按 `ontology_id + ontology_version + action_name` 解析；版本专用适配器优先于 `*` 通配兼容适配器。
2. 新增版本化接口：

   - `GET /api/ontologies/{ontology_id}/versions/{ontology_version}/actions`
   - `POST /api/ontologies/{ontology_id}/versions/{ontology_version}/actions/{action_name}/execute`

3. 未显式传版本的兼容接口仍可使用，但服务端会先解析当前活动版本，再进入同一个版本化执行入口。
4. 工作流创建时保存的 `ontology_version` 已成为强制执行版本。版本切换后，存量流程继续使用创建时版本；版本不可用或与当前运行合同不兼容时失败关闭。
5. 本体发布增加 Action 运行合同门禁。ActionType、幂等字段、Function、副作用、请求模型或运行设计不一致时禁止发布。
6. 五个依赖只读 Function 快照的 Action 已使用锁定版本的本体 `source_table/source_column` 构造查询，并将物理字段重新别名为稳定的本体属性名。数据库标识符必须通过白名单格式校验，查询值继续使用参数化绑定。
7. ABox 映射主键不再依赖固定表名目录，而是从本体属性的 `business_key` 和对应物理字段派生；物理表改名后仍可生成版本一致的映射合同。

写入型 Repository 仍然是显式业务适配器。本阶段没有把本体字段或 Agent 输入直接拼接为写入 SQL。

## 接入第二个业务本体

第二个业务本体需要提供自己的：

1. `OntologyProposal`、ActionType、DynamicSecurity 和 `ActionRuntimeDesign`。
2. Pydantic 请求模型。
3. 使用本体数据绑定的 Repository/DataSourceAdapter。
4. 业务 Function、外部系统 Provider、规则和结果映射。
5. 实现统一 `execute(request, ontology_version, actor_id, actor_role, correlation_id)` 边界的业务服务，或提供 API 层适配器。
6. 将该实现注册到 `ReusableActionAdapterRegistry`。

数据库访问仍必须通过本体映射和字段白名单完成。注册器只解决跨业务分发及运行框架复用，不允许 Agent 传入表名、字段名或原始 SQL。
