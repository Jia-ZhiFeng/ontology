# 信审本体 MVP

数据链路：PostgreSQL → 数据剖析 → LLM 候选本体 → 人工审核 → NebulaGraph TBox/ABox → 图查询。

当前业务结构已合并《信审本体PRD》v1.1，覆盖自然人、经销商、车辆、贷款申请单、征信报告、外部存量负债、风险评估报告、反欺诈报告和电核记录。借款人、担保人、共借人统一建模为自然人与具体贷款申请之间的角色关系。

当前版本属于一期静态本体 MVP：支持人工生成、审核、发布和 ABox 同步，但不宣称已实现 CDC、事件驱动自动更新、实时推理、自动决策、Agent MCP 或正式 FIBO URI 对齐。当前范围和排除项见 `PERSONAL_CREDIT_ONTOLOGY_SCOPE.md`。

当前本体构建范围限定为个人汽车信贷，范围基线见 `PERSONAL_CREDIT_ONTOLOGY_SCOPE.md`。
本体编辑协作支持不可变提交、分支、标签、日志和结构 Diff；Git 层不直接发布线上本体，详见 `PERSONAL_CREDIT_ONTOLOGY_GIT.md`。
属性、主键、必填条件、枚举、单位和数据来源见 `PERSONAL_CREDIT_ATTRIBUTE_CATALOG.md`。
关系方向、基数、关系属性和数据来源见 `PERSONAL_CREDIT_RELATION_CATALOG.md`。
Interface、公理和业务约束见 `PERSONAL_CREDIT_INTERFACE_AXIOMS.md`。
26条公理的执行器、触发语义、门禁和验证结果见 `PERSONAL_CREDIT_AXIOM_EXECUTION.md`。
ActionType、Function和DynamicSecurity见 `PERSONAL_CREDIT_CAPABILITIES_SECURITY.md`。
Action 层分层、统一返回信封和 Function 注册中心边界见 `PERSONAL_CREDIT_ACTION_LAYER_GUIDE.md`。默认注册中心的 9 个 Function 均未绑定；可选只读注册中心已显式绑定全部 9 个实现，并提供 PostgreSQL/NebulaGraph 数据适配。全部8个 Action 已开放受保护的端到端执行 API。
9 个 Function 的实现版本、规则、数据适配、失败关闭和真实样例结果见 `PERSONAL_CREDIT_FUNCTION_IMPLEMENTATIONS.md`。
8 个 Action 的请求/响应、步骤 DAG、状态机、权限、幂等、审计、错误码、重试和补偿设计见 `PERSONAL_CREDIT_ACTION_DESIGN.md`；机器可读目录位于 `backend/app/action/action_design_catalog.py`。

申请级流程编排见 `PERSONAL_CREDIT_WORKFLOW_ORCHESTRATION.md`。它把 8 个 Action 组织为持久化流程，支持征信/反欺诈并行、人工复核条件跳过、Outbox 状态自动对账、崩溃租约恢复和追加式流程审计；流程终点仍只是决策建议。
八 Action 合成业务验收数据、独立 `mvp_uat` 存储、沙箱启用和可重复执行方式见 `ACTION_CHAIN_UAT_GUIDE.md`；业务结果只允许由对应 Action 生成，不通过 SQL 预填。
首个风险评估 Action 的 API、写入规则、跨库补偿和真实受控验收见 `PERSONAL_CREDIT_GENERATE_RISK_ASSESSMENT_ACTION.md`。
受控进件 Action 的请求、主数据复用、事务写入、图同步与补偿边界见 `PERSONAL_CREDIT_CREATE_LOAN_APPLICATION_ACTION.md`。
受控征信查询 Action 的授权、供应商幂等收据、沙箱边界、报告/HOLDS 写入和补偿见 `PERSONAL_CREDIT_REQUEST_CREDIT_REPORT_ACTION.md`。
受控反欺诈查询 Action 的核心关系门禁、关联风险 Function、供应商幂等、报告/HAS_FRAUD_REPORT 和补偿见 `PERSONAL_CREDIT_REQUEST_FRAUD_REPORT_ACTION.md`。
创建电核任务 Action 的触发门禁、问题集、任务控制元数据、可靠投递和死信保留边界见 `PERSONAL_CREDIT_CREATE_PHONE_VERIFICATION_TASK_ACTION.md`。
记录电核结果 Action 的任务分配、终态幂等、回调收据、事务写入和图同步保留策略见 `PERSONAL_CREDIT_RECORD_PHONE_VERIFICATION_ACTION.md`。
触发人工复核 Action 的来源审计链、转人工规则、双 Function 证据、状态/路由写入和补偿边界见 `PERSONAL_CREDIT_TRIGGER_MANUAL_REVIEW_ACTION.md`。
记录决策建议 Action 的人工确认门禁、证据血缘、非最终审批边界和精确补偿见 `PERSONAL_CREDIT_RECORD_DECISION_RECOMMENDATION_ACTION.md`。
Action 执行控制层已建立请求摘要、幂等哈希、乐观锁状态流转，以及 PostgreSQL `action_executions`/`action_audit_events` 控制表。全部8个 Action 均具备持久化 outbox、崩溃恢复和防篡改审计；人工复核与决策建议图同步死信会精确恢复本次状态或建议写入。
八类 Outbox 的死信告警、服务端身份、逐事件人工重放、幂等与历史终态保留语义见 `PERSONAL_CREDIT_OUTBOX_OPERATIONS.md`；前端“运行监控”已提供告警、确认重放和历史查询。
外部征信、反欺诈、电核与 LLM 的持久化熔断、告警去重、后台升级、可选 HTTPS Webhook 和前端红点见 `PERSONAL_CREDIT_OPERATIONS_ALERTING.md`。Webhook 默认关闭，不会向外部发送数据。
历史 `1.0.0` 深度审核见 `PERSONAL_CREDIT_PREPUBLICATION_AUDIT.md`；当前活动版本及月供/DTI 变更见 `PERSONAL_CREDIT_ONTOLOGY_RELEASE_1.1.0.md`。
ABox 实例、字段和业务关系到 TBox 的确定性映射规则见 `PERSONAL_CREDIT_ABOX_TBOX_MAPPING.md`。
ABox 类型化 VID、稳定关系 rank 和安全迁移规则见 `PERSONAL_CREDIT_ABOX_IDENTITY.md`。
草稿、审核、批准、发布和受控回滚流程见 `PERSONAL_CREDIT_ONTOLOGY_VERSION_WORKFLOW.md`。
PostgreSQL 一致性快照、目录驱动字段校验、阶段性 NULL 和同步门禁见 `PERSONAL_CREDIT_DATA_QUALITY.md`。
已发布图谱的服务端检索、1–3 hops、模式筛选、分页与截断语义见 `PERSONAL_CREDIT_GRAPH_QUERY.md`。
Docker Compose 就绪门禁、NebulaGraph Storage 注册、健康检查和数据卷注意事项见 `DOCKER_RUNTIME_RELIABILITY.md`。
发布模型、审核、数据库覆盖和图谱规模的统一统计源见 `personal_credit_release_metrics.json`；文档统计一致性由自动化测试保护。

## 启动

需要 Docker Desktop / Docker Compose：

```bash
docker compose up -d --build --wait
```

- 前端：http://localhost:8080
- 后端 API：http://localhost:8000/docs
- PostgreSQL：localhost:5432
- NebulaGraph：localhost:9669

Compose 会依次等待 PostgreSQL、NebulaGraph Meta/Storage/Graph、Storage Host `ONLINE`、后端和前端真实就绪；不再依赖固定等待时间。配置写在 `.env`，生产环境请替换默认口令。不要在需要保留数据时执行 `docker compose down -v`。

## 访问与隐私

- 除 `GET /api/health` 外，所有 `/api/*` 接口都要求请求头 `X-API-Key`，其值由服务端 `.env` 的 `MVP_API_KEY` 配置。
- 首次打开前端后，在“模型管理”的“MVP 服务访问”中填写同一令牌。前端仅保存到当前浏览器的 `sessionStorage`，关闭会话后清除。
- 当前前端以候选 OntoStudio HTML 为唯一界面，通过 `frontend/live-backend.js` 对接真实服务。账号管理、权限管理和角色切换已从界面与源码删除；工作流 Action 使用后端 `request_fields` 生成字段化表单，不要求手写 JSON，并可在每个步骤查看脱敏后的有效输入与完整业务输出。
- 提示词模板当前仅保留为候选设计预览，文档抽取也明确显示“后端尚未提供接口”；两者不会返回前端模拟结果。
- `.env.example` 只提供占位值；接入真实业务数据前，必须为 `MVP_API_KEY`、PostgreSQL 和 NebulaGraph 分别更换强随机密钥，并确保 `.env` 不进入版本库或交付包。
- PostgreSQL 数据、图谱快照、关联查询、映射报告和公理违规结果在 API 出口默认脱敏；数据库与图数据库内部值不被修改，因此不会影响公理校验、映射和同步。
- LLM 只接收 Schema、字段结构、空值状态和类型占位符，不再发送 PostgreSQL 样本原值。允许的服务地址由 `LLM_ALLOWED_BASE_URLS` 精确白名单控制，多个地址以英文逗号分隔。
- HTTP LLM 地址默认禁止。仅在本机调试时可将 `LLM_ALLOW_INSECURE_LOCAL=true`，并把 `http://localhost...` 或 `http://127.0.0.1...` 的完整地址加入白名单。

命令行调用示例：

```bash
curl -H "X-API-Key: <MVP_API_KEY>" http://localhost:8000/api/kg/stats
```

当前是单用户 MVP 令牌，不等同于生产级登录、RBAC、租户隔离、密钥托管或操作审计。

## 主要接口

下列接口除健康检查外均需携带 `X-API-Key`。

- `GET /api/health`：检查 PostgreSQL 与 NebulaGraph
- `GET /api/postgres/schema`：读取 PostgreSQL Schema
- `GET /api/postgres/data/{table}`：通过服务端白名单校验读取指定业务表的脱敏分页预览
- `POST /api/postgres/profile`：读取样本
- `POST /api/ontology/generate`：调用 LLM 生成候选本体
- `GET /api/ontology/baseline`：生成已批准的个人信审 `1.1.0` 确定性基线
- `POST /api/ontology/audit`：执行目录、字段、公理和能力边界的发布前深度审核
- `GET /api/ontology/versions`：读取版本历史和当前活动版本
- `POST /api/ontology/versions/drafts`：保存可修改草稿；版本号必须为 SemVer
- `POST /api/ontology/versions/{version}/review`：执行结构、公理、字段、映射和安全边界审核
- `POST /api/ontology/versions/{version}/approve`：批准审核通过且摘要一致的版本
- `POST /api/ontology/versions/{version}/publish`：发布 TBox、同步 ABox、校验映射后切换活动版本
- `POST /api/ontology/versions/{version}/rollback`：受控回滚到兼容的历史已发布版本
- `GET /api/ontology/git/status|log|diff|show/{ref}`：读取本体 Git 分支、标签、提交历史、结构差异和不可变快照
- `POST /api/ontology/git/commits|branches|tags`：提交本体快照、创建协作分支和不可变标签；这些操作不会直接发布线上本体
- `GET/POST /api/ontology/axioms/validate`：对当前 PostgreSQL 执行26条个人信审公理
- `GET/POST /api/ontology/data-quality/validate`：在只读 `REPEATABLE READ` 一致性快照上执行目录必填、阶段条件、类型、枚举、单位范围和业务主键校验
- 旧的 `POST /api/ontology/approve` 直接发布入口已关闭并返回 `409`，必须使用版本工作流
- `POST /api/kg/sync`：在同一 PostgreSQL 一致性快照上依次执行通用数据质量、26条公理和映射合同，再全量同步为 ABox；任一门禁未通过时保留旧图谱
- TBox 发布与 ABox 同步均采用“先写入完整目标并校验、再清理旧数据”；写入或校验失败时不会进入清理阶段，同 VID ObjectType 的 `INSTANCE_OF` 映射边会在 TBox 重发时保留
- ABox 同步会根据标准化手机号或地址自动派生、去重并映射 `SHARES_CONTACT`，图中不保存匹配明文
- ABox 实例使用 `abox:<ObjectType>:<business_key>` 类型化 VID，业务边和 `INSTANCE_OF` 使用按关系类型生成的稳定 rank；原业务编号仍可用于精确检索
- `GET /api/kg/mapping/report`：检查当前 ABox–TBox 映射完整性
- `GET /api/kg/related/{node_id}`：查询关联边
- `POST /api/kg/query`：按检索词、1–3 hops、显示模式、单页上限和偏移量查询已发布图谱；返回总量、分页和截断状态
- `GET /api/kg/snapshot`：兼容性完整图库分页接口，支持 `limit` 与 `offset`
- `GET /api/kg/stats`：读取图谱节点和关系总量
- `POST /api/actions/create-loan-application/execute`：以服务端系统适配器身份受控创建人员、车辆、可选经销商和贷款申请，自动同步已发布图谱；不得写最终审批结论
- `POST /api/actions/request-credit-report/execute`：校验当前申请主体与授权后，通过幂等供应商接口登记征信报告并同步 `HOLDS`；真实供应商默认关闭，沙箱必须显式启用
- `POST /api/actions/request-fraud-report/execute`：校验申请核心关系，运行关联风险 Function，通过幂等供应商接口登记反欺诈报告并同步 `HAS_FRAUD_REPORT`；真实供应商默认关闭
- `POST /api/actions/generate-risk-assessment/execute`：以活动本体版本执行首个端到端风险评估 Action；只接受业务字段，执行身份由服务端配置
- `POST /api/actions/create-phone-verification-task/execute`：验证风险/反欺诈触发条件，生成问题集，创建电核任务并同步 `HAS_PHONE_VERIFICATION`；真实电核系统默认关闭，未投递任务会保留重试
- `POST /api/actions/record-phone-verification/execute`：仅允许服务端配置且已分配的电核回调身份写入一次终态结果；同载荷幂等重放，不同载荷冲突，图同步失败不会覆盖或删除通话事实
- `POST /api/actions/trigger-manual-review/execute`：校验来源 Action 审计链和转人工规则，运行决策支持/证据说明，只将申请路由为待人工复核，不执行最终审批
- `POST /api/actions/record-decision-recommendation/execute`：由服务端认证的有权人员确认并记录带证据的建议；Agent 不能确认，响应固定标记 `decision_authority=recommendation_only`，不执行最终审批
- `GET /api/actions/executions/{execution_id}`：读取脱敏执行状态、审计事件并校验审计哈希链
- `GET /api/actions/outbox`：按状态查看脱敏 outbox 事件、尝试次数、租约和失败信息
- `GET /api/actions/outbox/status`：汇总各状态数量、过期租约和需要人工关注的死信数
- `GET /api/actions/outbox/alerts`：读取死信和过期处理租约告警，以及对应 Action 状态和最近重放结果
- `GET /api/actions/outbox/replays`：读取人工重放请求、服务端操作人、原因和执行结果
- `POST /api/actions/outbox/{event_id}/replay`：以服务端运维身份逐事件重放死信；保持历史失败 Action 终态不变，同一请求幂等返回
- `POST /api/workflows/personal-credit`：按新申请或已有申请入口创建幂等的个人信审流程实例
- `GET /api/workflows/personal-credit/summary|definition`：读取流程数量、状态和 8 步依赖定义
- `GET /api/workflows/personal-credit`：按申请编号或状态查询流程；详情包含步骤和追加式审计事件
- `POST /api/workflows/personal-credit/{workflow_id}/steps/{action_name}/execute`：仅派发依赖已满足的 Action；不保存原始请求，只留存请求摘要和服务端校验、补齐并脱敏后的有效输入摘要
- `POST /api/workflows/personal-credit/{workflow_id}/refresh`：根据 Action/Outbox 状态立即推进流程；后台也会定期自动对账
- `POST /api/workflows/personal-credit/{workflow_id}/steps/{action_name}/skip`：填写原因后跳过条件人工复核；其他步骤不可跳过
- `POST /api/workflows/personal-credit/{workflow_id}/cancel`：在没有运行中 Action 时取消编排，不删除既有业务事实和审计
- `GET /api/operations/summary`：读取活动告警、等级、熔断器和通知配置摘要
- `GET /api/operations/alerts`：按 `OPEN`、`ACKNOWLEDGED` 或 `RESOLVED` 查询运行告警
- `GET /api/operations/circuits`：查询征信、反欺诈、电核与 LLM 外部依赖熔断状态
- `POST /api/operations/alerts/{alert_id}/acknowledge|resolve`：以服务端运维身份确认或解决告警
- `POST /api/operations/circuits/{dependency_name}/reset`：填写说明后人工复位熔断器

## 目录

`postgres/` 是 PostgreSQL 初始化数据和非破坏性迁移，`backend/app/postgres/` 是通用一致性快照数据源适配器，`backend/app/action/dti_data_adapter.py` 是 DTI 专用只读适配器，`backend/app/ontology/data_quality.py` 是目录驱动数据质量执行器，`backend/app/kg/nebula.py` 是 NebulaGraph 适配器。

## 本地回归测试

```bash
PYTHONPATH=backend python -m unittest discover -s backend/tests
node frontend/layout.test.js
```

前者执行258个后端单元测试，覆盖全部8个 Action、申请级流程编排、Action 输入摘要脱敏、Action 表单受控数据库检索、9个 Function、本体 Git、八类 outbox、死信人工重放、熔断/告警/升级、Action设计/控制、人工确认门禁、发布、版本、安全、数据质量、图查询和文档一致性。后者无需 npm 依赖，验证候选 HTML 页面结构、账号/权限/角色代码清除、真实 API 路径、字段化工作流表单、Action 输入输出详情、数据库字段回填、数据源脱敏分页预览、未接功能边界和运行表格文本转义。
