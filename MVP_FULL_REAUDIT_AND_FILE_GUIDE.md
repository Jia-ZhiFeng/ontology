# 信审本体 MVP 全量复核与逐文件说明

复核日期：2026-08-20；安全发布、隐私、版本工作流、数据质量、图查询、容器启动保障与前四个 Action 更新：2026-08-24；全八个 Action、Outbox 人工重放、持久化熔断与主动告警更新：2026-08-25  
项目目录：`D:\MVP\credit-ontology-mvp`  
复核范围：当前项目 111 个文件；不包含运行时缓存、Python 字节码和外部依赖。

## 1. 复核结论

当前 MVP 已经形成一个可以运行和演示的“个人汽车信审静态本体”闭环：PostgreSQL 数据浏览、确定性本体基线、AI 辅助候选、人工审核、26 条公理执行、NebulaGraph TBox/ABox 发布与映射、图谱检索和可视化均已具备。

但它目前仍应定位为**本体建模与技术验证 MVP**，不适合直接作为生产信审系统上线。主要原因不是本体目录或基础版本流程缺失，而是生产级 RBAC 与职责分离、图谱双版本原子切换、自动化覆盖和运行保障尚未完成。

### 1.1 当前运行验证结果

| 检查项 | 实际结果 | 结论 |
|---|---:|---|
| PostgreSQL / NebulaGraph 健康检查 | 均为 `true` | 当前服务可访问 |
| ObjectType | 9 | 与基线一致 |
| Property | 74 | 覆盖 75 个数据库字段中的 74 个；1 个有意排除 |
| LinkType | 11 | 与关系目录一致 |
| Interface / 公理 | 6 / 26 | 26 个执行器全部绑定 |
| Function / ActionType / DynamicSecurity | 9 / 8 / 8 | TBox 保持静态声明；可选运行注册中心已显式绑定全部 9 个只读 Function |
| 发布前深度审核 | 16/16 通过 | 代码实际口径 |
| 当前 PostgreSQL 公理校验 | 26/26 通过 | 无错误、无提示，数据集未截断 |
| 目录驱动通用数据质量 | 74/74 属性规则通过 | 212 个值已检查；只读 `REPEATABLE READ` 一致性快照、无截断 |
| ABox–TBox 映射 | `valid=true` | 25 个类型化 VID 实例、25 条业务关系、25 条 `INSTANCE_OF`，50 条 ABox 边均为稳定非零 rank |
| NebulaGraph 当前规模 | 151 个节点、271 条关系 | 与映射报告一致 |
| 服务端图查询 | 通过 | 空检索 151/271 完整返回；实体 25/25、本体 126/221、映射 34/25；支持 1–3 hops、四种模式和端点安全分页 |
| Python 静态语法 | 62/62 文件通过 | 使用 AST 只读解析，含44个应用模块和18个测试包文件 |
| 前端 JavaScript 语法 | 通过 | Node.js `--check` 通过，页面可正常渲染 |
| 前端关键流程 | 通过 | 浏览器使用 9 个本体、74 个属性、11 条关系验证动态画布和 0 个卡片重叠；拖动/重置逻辑测试通过 |
| API 安全基线 | 通过 | 未授权受保护接口返回 401；授权后 151/271 与映射有效；数据响应未出现原始身份证或手机号 |
| 本体版本登记 | `1.1.0` / `published` / 活动 | 经过 draft、reviewed、approved 后发布并同步 ABox |
| 自动化测试 | 249 个后端 + 1 个前端布局测试 | 覆盖全部8个 Action、申请级流程编排、八类 outbox、死信人工重放、熔断/告警/升级、9个 Function、人工确认门禁、发布、版本、安全、数据质量和图查询 |
| 版本控制 | 当前目录不是 Git 仓库 | 关键交付风险 |

### 1.2 能力成熟度判断

| 范围 | 判断 | 说明 |
|---|---|---|
| 静态个人信审本体 | 可用 | 目录、关系、公理、映射基线完整 |
| 演示数据闭环 | 可用 | 当前样例数据、公理与图谱映射均通过 |
| 本体可视化审核 | MVP 可用 | 节点拖拽、选中详情和无重叠重置布局可用；前端仍应从单文件拆分为模块 |
| 图谱查询与展示 | MVP 可用 | 已下沉服务端检索、1–3 hops、四种模式、明确分页/截断和端点安全关系；下一步是游标分页、专用索引与大图压测 |
| 发布与同步可靠性 | MVP 已缓解 | 已先写后清理、PostgreSQL advisory lock、版本登记和失败补偿恢复；仍无双版本活动别名原子切换 |
| 安全与隐私 | MVP 基线可用 | 已有单用户 API 令牌、默认响应脱敏、LLM 样本匿名化和 URL 白名单；仍无 RBAC、审计与服务端密钥托管 |
| 本体版本管理 | MVP 可用 | 已有 draft → reviewed → approved → published、差异记录、历史版本和兼容回滚；仍无角色职责分离与电子签名 |
| Action 运行层 | 八个 Action 已端到端 | 全部8个 Action 均具备权限、幂等、证据、outbox 和审计；决策建议不执行最终审批，生产外部系统默认关闭 |
| 测试与持续交付 | 初步建立 | 已有249个后端测试和1个前端布局测试；仍无完整公理/映射集成、生产外部系统故障注入和 Git/CI 基线 |
| 生产运维 | MVP 告警闭环可用 | 已有健康检查、持久化熔断、告警去重/升级、可选 Webhook、就绪门禁和重启策略；仍无正式指标平台、值班回执、迁移框架、备份和灾备 |

## 2. 主要问题与风险

### P0：建议在继续扩展功能前解决

#### P0-1 TBox 重新发布会删除现有 `INSTANCE_OF` 映射边（已修复）

原 `write_tbox()` 会先删除所有旧 `ontology` 节点，连带删除 `INSTANCE_OF`。2026-08-21 已改为先 upsert 完整目标 TBox、回读校验，再只清理不属于目标版本的旧节点和旧结构边；同 VID ObjectType 不再删除，既有 `INSTANCE_OF` 会保留。

后续生产化仍建议使用带版本前缀的 TBox 暂存区和活动版本切换，并在真实测试 Space 中补充故障注入。

#### P0-2 TBox/ABox 都是先删后写，失败会留下不完整图谱（已缓解）

2026-08-21 已改为“upsert 完整目标 → 回读校验 → 清理陈旧数据”，并增加进程内发布互斥。单条写入或校验失败时不会进入清理阶段，旧图谱不会被整体删除。

当前已用 PostgreSQL advisory lock 串行化跨后端实例的版本发布，发布后同步 ABox 并校验映射；失败时会尝试恢复原活动 TBox/ABox。它仍不是跨 PostgreSQL/NebulaGraph 的严格原子事务，生产化应采用“写入双版本暂存区 → 校验 → 原子切换活动别名 → 延迟清理”，并增加幂等发布 ID 和不可抵赖审计。

#### P0-3 API、PostgreSQL 页面和图谱页面没有身份认证与数据授权（已建立 MVP 基线）

2026-08-21 已为除健康检查外的 `/api/*` 增加 `X-API-Key` 单用户令牌校验，并在 PostgreSQL、图谱、关联、映射、公理和审核响应出口默认脱敏。前端令牌只保存在当前浏览器会话，未授权页面会显示明确的 401 提示。

剩余边界：这不是用户登录或 RBAC；所有持有令牌者权限相同，也没有查询/发布操作审计、密钥轮换、租户隔离和字段级授权。接入真实数据前仍需补齐这些生产能力。

#### P0-4 LLM 样本原文外发和任意 Base URL（已修复）

2026-08-21 已将 LLM 输入改为保留 Schema、字段、类型和空值状态的匿名占位符，不再发送 PostgreSQL 样本原值；Base URL 必须精确匹配服务端 `LLM_ALLOWED_BASE_URLS`，HTTP 默认禁止且仅能显式开放给白名单内本机地址。非成功响应也不再把模型服务正文回传页面。

剩余边界：LLM API Key 仍从页面临时传入，尚未使用服务器密钥管理；虽已接入持久化熔断，仍没有数据出境审批、调用审计、提示摘要留痕和总体预算。

#### P0-5 自动化测试覆盖有限，且缺少 Git 历史和 CI

当前已有249个后端单元测试和1个前端布局测试。全部8个 Action 已覆盖请求、权限、证据、幂等、图重试、成功收敛及各自补偿/事实保留边界；流程层覆盖固定依赖、并行就绪、条件跳过、摘要隐私和后台对账接入；死信和运维层另覆盖重放、熔断快速失败、自动恢复、通知成功/失败、Webhook 安全和持久化结构。项目仍不是 Git 仓库，26条公理逐规则、完整浏览器操作和真实外部系统/NebulaGraph 故障注入仍缺少自动化覆盖。

建议：先建立 Git 基线，再补充单元测试、PostgreSQL/NebulaGraph 集成测试和浏览器端到端测试，最后配置 CI 门禁。

### P1：建议作为下一批工程化任务

#### P1-1 PostgreSQL“快照”不是严格一致性快照（已修复）

2026-08-21 已把 `profile()`、`validation_profile()` 和 `table_data()` 改为只读 `REPEATABLE READ` 事务，在同一事务内读取 Schema、计数和记录，并返回 PostgreSQL 快照标识及事务时间。候选审核、ABox 同步和通用质量校验会在各自的一次调用中复用同一个 `validation_profile()`。

剩余边界：每次 API 调用仍是独立快照；当前校验最多把 10 万行/表读入内存，大数据量时仍应改为数据库侧规则 SQL、游标分批或增量校验。

#### P1-2 “全量图谱”实际最多读取 1,000 个节点和 1,000 条边（已修复）

2026-08-22 已新增 `POST /api/kg/query`，把检索字段、1–3 hops、全部/实体/本体/映射模式、节点上限和偏移量下沉后端。空检索会返回当前模式的真实节点/关系总量；超出单页容量时明确返回上一页、下一页和截断状态。每条返回关系都保证两个端点位于当前节点结果中，不再独立截断。

当前公共查询单次最多 500 个节点，搜索邻域最多 2,000 条关系；页面支持 100/300/500 单页选择。剩余边界：仍为 offset 分页，TBox 因当前没有本体属性索引而采用最多 10,000 节点的服务端受控筛选；生产大图应改为稳定 VID 游标、专用索引/全文检索和容量压测。

#### P1-3 可视化编辑和 AI 候选无法形成可发布的新版本（已建立 MVP 工作流）

2026-08-21 已新增持久化 `draft → reviewed → approved → published` 状态机、SemVer、候选差异集、版本历史、发布事件和兼容回滚。旧的固定 `1.0.0` 深度审核仍保留为基线一致性工具；新版本候选改用结构、公理、字段、映射和安全边界审核。

剩余边界：当前单一访问令牌不能实现审核人与批准人职责分离；删除或迁移 ABox 正在引用的 TBox 节点会被兼容门禁阻止，仍需双版本暂存和迁移编排。

#### P1-4 前端存在重复函数和失效的“重置布局”（已修复）

2026-08-22 已删除被覆盖的旧属性迁移和节点拖拽实现，只保留一套带拖动/点击区分的节点拖拽逻辑。初始预览与“重置布局”统一使用按卡片尺寸计算的网格：画布会根据行数增高，节点不会重叠；重置只修改可视化坐标，不修改本体 JSON、当前选中本体或属性表格。

剩余边界：`frontend/index.html` 仍同时承载 HTML、CSS、API、编辑器和两套图谱渲染，后续应拆分模块并增加浏览器端到端测试。

#### P1-5 业务主键直接作为全局 VID，单一 Edge Type 使用默认 rank（已修复）

2026-08-22 已把映射合同升级到 `personal-credit-abox-tbox-mapping-2.0`。ABox VID 统一为 `abox:<ObjectType>:<business_key>`，原业务主键保存在 `_ontology.business_id` 并继续支持精确检索；业务边和 `INSTANCE_OF` 使用按关系类型生成的稳定非负 int64 rank，陈旧关系按精确 rank 清理。实际迁移后 25/25 个实例为类型化 VID、50/50 条 ABox 边为稳定非零 rank、旧裸 VID 为 0，映射报告错误为 0，图谱仍为 150/269。

剩余边界：当前同端点、同关系类型被视为一条逻辑关系；未来需要保留多次同类业务事件时，应把关系业务唯一键也纳入 rank 或拆分专用 Edge Type。

#### P1-6 目录必填和枚举未形成通用数据校验（已修复）

2026-08-21 已新增受控的目录驱动执行器；1.1.0 扩展到 74 个属性的 `required`、`required_when`、类型、枚举、`0-1` 范围、空字符串和业务主键唯一性。未知条件、非一致性快照或数据集截断会直接阻断；外部负债月供在还款能力评估前必须存在。

该门禁已接入候选版本审核、发布重审和 ABox 同步，并在页面提供独立执行入口。剩余边界是外键/复杂跨表规则仍由 PostgreSQL、26 条公理和映射合同共同覆盖，大数据量校验仍需下沉数据库或增量化。

#### P1-7 Docker 初始化和运行保障较脆弱（已建立 MVP 基线）

2026-08-24 已为 PostgreSQL、NebulaGraph Meta/Storage/Graph、后端和前端增加真实健康检查和依赖条件；Storage 初始化改为 `SHOW HOSTS` 先查后写，并轮询到 `ONLINE`，超时或命令失败会明确退出，不再使用固定 `sleep 15` 或 `|| true`。常驻服务增加 `unless-stopped` 重启策略，另有 5 个静态回归测试保护配置。

在保留命名卷的情况下重建全部容器后，6 个常驻容器均为 `healthy`，三项 NebulaGraph `/status` 均为 `running`。1.1.0 发布后为 151/271、映射 25/25、字段规则 74/74、公理 26/26。已增加首个可重复、非破坏性 SQL 迁移，但仍需正式迁移执行框架、备份恢复、告警和灾备方案。

#### P1-8 文档统计已经出现多套口径（已修复）

2026-08-24 已以运行接口和确定性基线复核模型、数据库覆盖、审核与图谱统计，新增 `personal_credit_release_metrics.json` 作为发布统计单一来源。1.1.0 当前口径为审核 16/16、ABox 25 个实体/25 条业务关系/25 条映射边、全图 151/271。

新增 4 个文档指标测试，校验指标内部加总、确定性基线和审核摘要，并阻止历史旧数字重新进入权威 Markdown 或 DOCX 生成脚本。剩余边界：JSON 中的运行态图谱统计仍需人工复核后更新；生产化可由受控发布流水线调用 API 自动刷新并留存签名快照。

### P2：进入真实业务数据或后续阶段时再实施

- 正式 FIBO 类、关系和 URI 对齐，以及本地业务本体到 FIBO 的映射表。
- CDC、事件驱动增量更新、图谱时间版本、冲突处理和回放。
- 全部9个只读 Function、数据适配、证据链、8个 Action、死信人工处理和主动告警入口已完成；下一阶段缺少生产外部系统适配、企业通知回执和与正式人工审批系统的职责隔离编排。
- 数据血缘、数据质量评分、字段授权、保留期限和隐私治理。
- 大规模 NebulaGraph 索引、查询计划、批量写入和容量压测。

这些能力目前被明确排除在静态本体一期范围之外，不应在当前版本中宣称已经实现。

## 3. 逐文件作用说明

状态含义：`正常` 表示作用清晰且当前可用；`需改进` 表示可运行但存在工程风险；`过时` 表示内容与当前实现不一致；`包标记` 表示仅用于 Python 包结构。

### 3.1 根目录、配置与交付物

| 文件 | 作用 | 状态与备注 |
|---|---|---|
| `.env` | 本地 Docker 后端运行配置，包含数据库、CORS、MVP 令牌和 LLM 白名单 | 需改进；仍使用演示口令和本地令牌，不应进入正式交付或版本库，接入真实数据前必须轮换 |
| `.env.example` | 环境变量模板，包含访问令牌占位、LLM 地址白名单和本机 HTTP 开关 | 正常；仅保留占位值，不包含真实密钥 |
| `docker-compose.yml` | 编排 PostgreSQL、NebulaGraph Meta/Storage/Graph、初始化容器、后端和前端 | MVP 可用；已有真实健康检查、依赖门禁、Storage 幂等注册/ONLINE 校验和常驻服务重启策略，仍非生产高可用配置 |
| `README.md` | 项目入口说明、能力边界、启动方式、端口、API 与访问/隐私配置 | 正常；已说明令牌、脱敏、LLM 白名单和就绪启动，仍需后续补迁移与生产运维体系 |
| `DOCKER_RUNTIME_RELIABILITY.md` | 说明 Compose 启动链、健康检查、NebulaGraph Storage 注册、失败语义、数据卷保护和验证基线 | 正常；当前本地运行保障手册，不替代生产监控、迁移、备份和灾备方案 |
| `personal_credit_release_metrics.json` | 统一保存 1.1.0 模型、字段覆盖、审核和当前图谱发布统计 | 正常；是文档生成与一致性测试的单一统计源，运行态数字更新前仍需复核 API |
| `个人信审本体八项任务总结.docx` | 八项本体建模任务的正式可阅读交付总结 | 正常；已按统一指标重新生成并完成逐页渲染检查 |
| `MVP_FULL_REAUDIT_AND_FILE_GUIDE.md` | 本次全量复核、逐文件说明、缺口和路线建议 | 正常；本报告 |
| `PERSONAL_CREDIT_OUTBOX_OPERATIONS.md` | 说明八类 Outbox 告警、人工重放、幂等、审计、页面操作和历史终态保留语义 | 正常；包含真实容器合成死信验收与清理结果 |
| `PERSONAL_CREDIT_OPERATIONS_ALERTING.md` | 说明四类外部边界熔断、告警生命周期、三级升级、Webhook、API、配置和真实探针 | 正常；Webhook 默认关闭，探针数据已清理 |
| `PERSONAL_CREDIT_WORKFLOW_ORCHESTRATION.md` | 说明申请级 8 Action 流程、并行依赖、条件步骤、状态、持久化、恢复、API 和前端操作 | 正常；终点只记录 `recommendation_only`，不执行最终审批 |

### 3.2 本体与发布文档

| 文件 | 作用 | 状态与备注 |
|---|---|---|
| `PERSONAL_CREDIT_ONTOLOGY_SCOPE.md` | 定义个人汽车信审范围、9 类对象、角色建模和排除项 | 正常；“运行态自动映射”排除项应明确指自动/持续映射，避免与现有手动同步混淆 |
| `PERSONAL_CREDIT_ATTRIBUTE_CATALOG.md` | 汇总 74 个属性、主键、必填口径、类型、单位和来源 | 正常；逐字段权威定义实际位于 Python 目录文件 |
| `PERSONAL_CREDIT_RELATION_CATALOG.md` | 定义 11 条关系、方向、基数、来源和 `SHARES_CONTACT` 语义 | 正常 |
| `PERSONAL_CREDIT_INTERFACE_AXIOMS.md` | 说明 6 个 Interface 和 26 条公理的业务含义 | 正常；与执行器覆盖一致 |
| `PERSONAL_CREDIT_AXIOM_EXECUTION.md` | 说明 26 条公理执行架构、接口、证据和当前边界 | 基本正常；历史验证描述应改为可重复测试输出，避免手工维护 |
| `PERSONAL_CREDIT_CAPABILITIES_SECURITY.md` | 记录 9 个 Function、8 个 ActionType 和 8 条安全策略声明 | 正常；这些只是 TBox 声明，不是 API 权限实现 |
| `PERSONAL_CREDIT_ACTION_LAYER_GUIDE.md` | 说明 Action、Function、API/MCP 的分层关系，并说明八个端到端执行引擎样板 | 正常；全部8个 Action、八类型 outbox 和死信人工处理入口已完成 |
| `PERSONAL_CREDIT_GENERATE_RISK_ASSESSMENT_ACTION.md` | 说明首个风险评估 Action 的 API、声明 Function、报告写入、outbox、跨库补偿和真实验收 | 正常；明确未校准 PD、不覆盖正式报告、不执行最终审批 |
| `PERSONAL_CREDIT_CREATE_LOAN_APPLICATION_ACTION.md` | 说明受控进件 Action 的请求、主数据兼容复用、事务 outbox、图同步、补偿和真实回滚探针 | 正常；不覆盖既有主数据，不写最终审批结论 |
| `PERSONAL_CREDIT_REQUEST_CREDIT_REPORT_ACTION.md` | 说明征信查询 Action 的授权、主体范围、供应商幂等收据、沙箱边界、报告/HOLDS、outbox 和补偿 | 正常；生产默认失败关闭，不冒充真实征信能力 |
| `PERSONAL_CREDIT_REQUEST_FRAUD_REPORT_ACTION.md` | 说明反欺诈查询 Action 的核心关系、关联风险、供应商幂等、沙箱边界、报告/HAS_FRAUD_REPORT 和补偿 | 正常；生产默认失败关闭，本地关联结论与供应商结果分开留痕 |
| `PERSONAL_CREDIT_CREATE_PHONE_VERIFICATION_TASK_ACTION.md` | 说明电核触发、问题生成、任务控制元数据、HAS_PHONE_VERIFICATION、幂等投递和死信保留 | 正常；真实电核系统默认关闭，任务投递失败不删除业务记录 |
| `PERSONAL_CREDIT_RECORD_PHONE_VERIFICATION_ACTION.md` | 说明电核结果合同、服务端任务分配、终态幂等、回调收据、事务写入和图同步事实保留 | 正常；不允许覆盖终态，不触发人工复核或最终决定 |
| `PERSONAL_CREDIT_TRIGGER_MANUAL_REVIEW_ACTION.md` | 说明来源审计链、转人工规则、双 Function 证据、状态/路由事务和图同步补偿 | 正常；只做人工复核路由，不执行最终审批 |
| `PERSONAL_CREDIT_RECORD_DECISION_RECOMMENDATION_ACTION.md` | 说明本人确认门禁、双 Function 证据、建议/血缘事务、非最终审批边界和精确补偿 | 正常；只记录 `recommendation_only` 建议 |
| `PERSONAL_CREDIT_ACTION_DESIGN.md` | 汇总8个 Action 的请求/响应、步骤 DAG、状态机、权限、幂等、审计、错误码、重试和补偿设计 | 正常；全部8个业务 Action 已运行 |
| `PERSONAL_CREDIT_FUNCTION_IMPLEMENTATIONS.md` | 汇总全部 9 个 Function 的实现版本、规则、输入输出、数据适配、失败关闭和真实验收结果 | 正常；明确 PD 为未校准代理值，决策仅供支持 |
| `PERSONAL_CREDIT_PREPUBLICATION_AUDIT.md` | 发布前深度审核报告 | 正常；16 项明细与运行时审核一致，包含公理可执行覆盖和 PostgreSQL 运行时校验 |
| `PERSONAL_CREDIT_ONTOLOGY_RELEASE_1.0.0.md` | 1.0.0 发布内容、替换过程、图谱规模和查看方式 | 正常；当前为 25 条业务关系、25 条映射边、全图 150/269 |
| `PERSONAL_CREDIT_ONTOLOGY_RELEASE_1.1.0.md` | 记录月供字段、DTI 契约/适配器、版本门禁和当前图谱规模 | 正常；当前活动版本发布说明 |
| `PERSONAL_CREDIT_ABOX_TBOX_MAPPING.md` | 说明表到 ObjectType、字段到 Property、实例到类型和关系映射 | 正常；当前 25/25、151/271 与运行结果一致 |
| `PERSONAL_CREDIT_ABOX_IDENTITY.md` | 说明类型化 ABox VID、稳定关系 rank、兼容查询和安全迁移结果 | 正常；当前映射合同 2.0 的身份规范 |
| `PERSONAL_CREDIT_ONTOLOGY_VERSION_WORKFLOW.md` | 说明版本状态机、候选审核、PostgreSQL 版本仓库、发布/回滚步骤、API 和安全边界 | 正常；是当前版本流程的实现口径 |
| `PERSONAL_CREDIT_DATA_QUALITY.md` | 说明 PostgreSQL 一致性快照、74 个属性规则、阶段性 NULL、版本/同步门禁和执行边界 | 正常；是当前通用字段数据质量的实现口径 |
| `PERSONAL_CREDIT_GRAPH_QUERY.md` | 说明已发布图谱服务端检索、1–3 hops、四种模式、分页、截断和端点安全语义 | 正常；包含接口、保护上限、实现边界和 151/271 实际验收结果 |

### 3.3 PostgreSQL、前端和脚本

| 文件 | 作用 | 状态与备注 |
|---|---|---|
| `postgres/credit_ontology_seed.sql` | 建立 9 张业务表、75 个字段、注释、约束和 25 条演示记录 | 正常用于新空卷；包含 `monthly_payment`，已有数据卷需执行迁移 |
| `postgres/migrations/002_add_external_debt_monthly_payment.sql` | 幂等新增外部负债月还款额、非负约束和两条演示值 | 已在当前数据卷成功执行；不删表、不清空数据、不以余额推算月供 |
| `frontend/index.html` | 单页 Web UI，包含会话级令牌、LLM、本体编辑、版本工作流、数据质量、图谱探索及运行监控 | MVP 可用；运行监控含告警红点、确认/解决、熔断器、Outbox 重放和历史；仍为单文件 |
| `frontend/layout.test.js` | 使用 Node.js 内置断言和模拟 SVG 事件验证拖拽实现唯一性、无重叠网格、动态画布高度、重置不修改本体及 pointer capture 清理 | 正常；无需 npm 依赖或浏览器权限 |
| `scripts/start-windows.cmd` | Windows 下复制缺失的 `.env` 并执行 `docker compose up --build` | 正常用于本地启动；没有依赖检测、后台模式或就绪提示 |
| `scripts/stop-windows.cmd` | Windows 下执行 `docker compose down` | 正常；默认保留数据卷 |
| `scripts/build_eight_tasks_doc.py` | 从统一发布指标生成八项任务总结 DOCX | 正常；模型、审核、字段和图谱关键数字均读取 `personal_credit_release_metrics.json` |

### 3.4 后端容器与入口

| 文件 | 作用 | 状态与备注 |
|---|---|---|
| `backend/Dockerfile` | 基于 Python 3.12 安装依赖并启动 Uvicorn | 需改进；默认 root 用户、无健康检查、多阶段构建和生产运行参数 |
| `backend/requirements.txt` | 固定 FastAPI、Pydantic、psycopg、NebulaGraph、httpx 等运行依赖版本 | 正常；缺少测试、lint、类型检查和安全扫描依赖 |
| `backend/app/__init__.py` | 标记 `app` 为 Python 包 | 包标记 |
| `backend/app/main.py` | 创建 FastAPI 应用，初始化 Action/Outbox/告警控制表，并运行 Outbox worker 与告警升级扫描器 | MVP 可用；后台任务共享停止信号并安全退出；仍缺登录/RBAC、统一异常、请求 ID 和安全响应头 |

### 3.5 API 层

| 文件 | 作用 | 状态与备注 |
|---|---|---|
| `backend/app/api/__init__.py` | 标记 API 模块为 Python 包 | 包标记 |
| `backend/app/api/routes.py` | 提供健康、数据、LLM、本体、版本、发布、公理、同步、图查询、Outbox 重放及告警/熔断运维接口 | MVP 可用；运维身份由服务端配置，LLM 与三类外部供应商已接入熔断；健康降级仍返回 HTTP 200 |

### 3.6 核心配置

| 文件 | 作用 | 状态与备注 |
|---|---|---|
| `backend/app/core/__init__.py` | 标记核心配置模块为 Python 包 | 包标记 |
| `backend/app/core/config.py` | 读取数据库、CORS、令牌、运维身份、LLM 白名单，以及告警扫描/升级/Webhook/熔断参数 | 需改进；仍含演示口令，单一运维身份和静态阈值不等同于生产 RBAC/SLA |
| `backend/app/core/security.py` | 提供常量时间令牌比较、递归字段/模式脱敏、图端点一致脱敏、LLM 样本匿名化和 Base URL 精确白名单校验 | 正常；是当前 MVP 运行时安全底座，不能替代 RBAC、密钥托管和审计 |

### 3.7 PostgreSQL 适配器

| 文件 | 作用 | 状态与备注 |
|---|---|---|
| `backend/app/postgres/__init__.py` | 标记 PostgreSQL 模块为 Python 包 | 包标记 |
| `backend/app/postgres/client.py` | 连接 PostgreSQL，在只读 `REPEATABLE READ` 事务中读取 Schema、样本、校验数据集和分页表数据 | 正常用于 MVP；返回快照标识与时间，每次调用仍新建连接且无连接池；默认最多 10,000 行/表；原始值由 API 出口统一脱敏 |

### 3.8 LLM 适配器

| 文件 | 作用 | 状态与备注 |
|---|---|---|
| `backend/app/llm/__init__.py` | 标记 LLM 模块为 Python 包 | 包标记 |
| `backend/app/llm/client.py` | 调用兼容 OpenAI Chat Completions 的 JSON 接口，校验地址白名单，并处理 240 秒超时和可重试错误 | MVP 可用；路由层已增加持久化熔断；API Key 仍经前端临时传输，缺少服务端密钥托管和总体预算 |

### 3.9 NebulaGraph 适配器

| 文件 | 作用 | 状态与备注 |
|---|---|---|
| `backend/app/kg/__init__.py` | 标记知识图谱模块为 Python 包 | 包标记 |
| `backend/app/kg/nebula.py` | 管理 Space/Tag/Edge、安全写入 TBox、同步类型化 ABox、稳定 edge rank、ABox 引用兼容检查、版本映射报告及服务端检索/hops/分页 | MVP 可用；查询返回真实总量、明确截断并保持边端点完整；仍缺双版本原子别名、游标分页和 TBox 专用搜索索引 |

### 3.10 本体模型与目录

| 文件 | 作用 | 状态与备注 |
|---|---|---|
| `backend/app/ontology/__init__.py` | 标记本体模块为 Python 包 | 包标记 |
| `backend/app/ontology/models.py` | 定义 ObjectType、Property、LinkType、Axiom、Interface、Function、ActionType、DynamicSecurity 和 OntologyProposal 数据模型 | 正常；可进一步增加名称格式、版本格式、枚举和跨字段约束 |
| `backend/app/ontology/scope.py` | 以程序数据定义个人信审范围、9 类对象、角色和排除能力 | 正常；是确定性基线的范围来源 |
| `backend/app/ontology/attribute_catalog.py` | 以程序数据定义 9 类对象的 74 个属性、来源、必填、枚举和单位 | 正常；当前属性目录的权威实现 |
| `backend/app/ontology/relation_catalog.py` | 以程序数据定义 11 条关系、关系属性、来源字段、基数和约束 | 正常；当前关系目录的权威实现 |
| `backend/app/ontology/interface_catalog.py` | 定义 6 个 Interface 和 26 条公理声明 | 正常；表达式是声明文本，实际执行由公理 ID 绑定处理器 |
| `backend/app/ontology/capability_catalog.py` | 定义 9 个只读 Function、8 个声明态 ActionType 和 8 条 DynamicSecurity | 正常；不代表运行时安全已经实现 |
| `backend/app/ontology/baseline.py` | 将范围与各目录确定性组装成 `1.1.0` OntologyProposal | 正常；当前正式基线的单一代码入口 |
| `backend/app/ontology/generator.py` | 构建严格提示词，以匿名化字段结构调用 LLM 生成候选本体，并在瞬时失败后用精简匿名样本重试一次 | MVP 可用；AI 候选可由人工采用后保存为新版本草稿，但调用治理仍需加强 |
| `backend/app/ontology/validator.py` | 校验名称唯一性、domain/source/target、Interface 引用、函数、动作和安全策略等结构一致性 | 正常但覆盖有限；不检查全部目录字段、属性重复、名称格式、必填/枚举数据质量和业务版本策略 |
| `backend/app/ontology/audit.py` | 将候选与确定性基线逐目录对比，检查 26 条公理执行、数据库字段覆盖、边界和元数据并生成摘要 | 正常；当前实际产生 16 项检查；固定等值策略导致任何新版本变更都不能发布 |
| `backend/app/ontology/axiom_executor.py` | 为 26 条公理提供受控 Python 执行器，输出违规记录、证据、错误/提示和数据集完整性 | 正常；当前样例 26/26 通过并使用一致性快照；仍需逐规则单元测试和大数据执行方案 |
| `backend/app/ontology/data_quality.py` | 在同一 PostgreSQL 快照上执行属性目录必填/阶段条件、类型、枚举、范围、空字符串和业务主键校验 | 正常；74/74 属性规则通过；条件使用受控注册表，未知条件和截断数据会阻断，真实大数据量需数据库侧或增量执行 |
| `backend/app/ontology/mapping.py` | 构建 ABox–TBox 映射合同、类型化实例 VID、稳定关系 rank、实例元数据、TBox 引用和 `SHARES_CONTACT` 派生关系 | 正常；联系方式只写摘要；已覆盖跨类型主键碰撞和 rank 稳定性测试 |
| `backend/app/ontology/versioning.py` | 定义 SemVer、单向状态转换、计数归一化、版本差异和候选发布审核 | 正常；把固定基线等值审核与可变版本候选审核分开，当前有 9 项候选门禁，包含目录驱动数据质量 |
| `backend/app/ontology/version_repository.py` | 在 PostgreSQL `mvp_control` Schema 持久化版本、审核快照、时间戳和发布事件，并提供 advisory lock | MVP 可用；当前单用户无操作者身份，表结构仍由运行时代码创建而非迁移工具 |

### 3.11 Action 运行基础设施

| 文件 | 作用 | 状态与备注 |
|---|---|---|
| `backend/app/action/__init__.py` | 标记 Action 运行模块并导出注册中心合同与受控错误类型 | 包入口；不创建全局注册实例，不自动绑定 Function |
| `backend/app/action/function_registry.py` | 从确定性基线加载 9 个只读 Function 契约，提供显式绑定、解绑、执行、输入隔离、统一信封校验和受控异常包装 | 运行底座已可用；默认全部未绑定，不访问 PostgreSQL、NebulaGraph 或外部 API |
| `backend/app/action/ratio_functions.py` | 实现 DTI/LTV 的 Decimal 纯计算、严格输入边界、保守车辆取价策略及显式绑定入口 | 正常；不使用负债余额冒充月供，不访问数据库、图谱或外部 API |
| `backend/app/action/dti_data_adapter.py` | 在单个 PostgreSQL 只读一致性快照中加载 DTI 输入并执行标准 Function 信封 | 正常；月供或收入缺失时失败关闭，不读取余额作为替代 |
| `backend/app/action/risk_rules.py` | 保存征信、欺诈、PD 代理、电核问题和决策支持的版本化 MVP 规则参数 | 正常；参数不是生产评分卡或自动审批政策 |
| `backend/app/action/business_functions.py` | 实现征信、欺诈、综合风险、电核问题、关联风险、决策支持和证据说明 7 个只读 Function | 正常；统一信封、规则版本、证据、完整性和决策边界齐全 |
| `backend/app/action/function_data_adapter.py` | 在 PostgreSQL 单快照和 NebulaGraph 关联查询上统一执行全部 9 个 Function，并为风险、反欺诈和电核任务 Action 提供严格声明 Function 入口 | 正常；电核任务只执行问题生成，反欺诈只执行关联风险，风险评估只执行其5个核心 Function |
| `backend/app/action/action_design_catalog.py` | 以机器可读形式定义 8 个 Action 的状态机、请求/响应、步骤依赖、幂等、审计、错误、重试和补偿，并校验与 TBox/安全目录一致 | 设计目录已完成；不执行 Action 或副作用 |
| `backend/app/action/execution.py` | 定义 Action 执行记录、受控错误、请求摘要、幂等哈希、状态机和角色边界服务 | 控制层已可用；不调度 Function、不执行外部调用或业务副作用 |
| `backend/app/action/execution_repository.py` | 在 PostgreSQL 持久化 Action 状态、乐观锁和幂等记录，并维护脱敏的追加式哈希链审计 | 已在真实 PostgreSQL 验证；支持状态流转间追加业务事件，审计触发器禁止 UPDATE/DELETE |
| `backend/app/action/risk_assessment_repository.py` | 锁定申请后创建风险报告或回填未开始的空占位报告，并提供精确补偿 | 不覆盖正式报告；补偿删除仅限本次新建 ID，空占位则恢复原字段 |
| `backend/app/action/generate_risk_assessment.py` | 编排首个端到端风险评估 Action：请求、权限、幂等、5 Function、输出门禁、写入、图同步、补偿和审计 | 不修改申请最终决策；PD 明确为未校准 MVP 代理值；跨库不宣称原子事务 |
| `backend/app/action/loan_application_repository.py` | 在单事务中兼容复用/创建人员、车辆、经销商，校验频次和VIN并创建申请/outbox，提供受限补偿 | 不覆盖既有主数据；申请被修改或引用后拒绝自动删除 |
| `backend/app/action/create_loan_application.py` | 编排受控进件 Action：请求、公理、权限、幂等、事务写入、图同步、补偿和审计 | 只允许初始状态，不接受或写入最终审批结论 |
| `backend/app/action/credit_provider.py` | 定义征信授权/供应商中立接口、显式沙箱、稳定供应商请求号、响应验证和受保护幂等收据 | 真实供应商未接入且默认关闭；沙箱响应显式标记为合成数据 |
| `backend/app/action/credit_report_repository.py` | 校验申请主体范围，创建或回填征信报告并原子写入 HOLDS 图同步 outbox，提供内容指纹补偿 | 不存独立关系行，`subject_id_number` 是 HOLDS 的确定性映射来源 |
| `backend/app/action/request_credit_report.py` | 编排授权、主体校验、供应商查询、响应缓存、报告/HOLDS、图同步、补偿和审计 | 只登记征信报告，不执行评估或最终审批 |
| `backend/app/action/fraud_provider.py` | 定义反欺诈供应商接口、显式沙箱、稳定请求号、响应验证和不含原始业务上下文的幂等收据 | 真实供应商未接入且默认关闭；沙箱明确标记合成数据 |
| `backend/app/action/anti_fraud_report_repository.py` | 校验申请核心关系，创建或回填反欺诈报告并原子写入图同步 outbox，提供内容指纹补偿 | `application_id` 是 HAS_FRAUD_REPORT 的确定性映射来源 |
| `backend/app/action/request_fraud_report.py` | 编排核心关系、关联风险 Function、供应商查询、报告/HAS_FRAUD_REPORT、图同步、补偿和审计 | 图查询不完整时失败关闭，不执行最终审批 |
| `backend/app/action/phone_dispatch.py` | 定义电核任务系统中立接口、显式沙箱、稳定投递号、响应校验和不含原始任务上下文的幂等收据 | 真实电核系统未接入且默认关闭；接收端必须支持投递号幂等 |
| `backend/app/action/phone_verification_repository.py` | 校验风险/反欺诈触发条件，原子创建电核记录、任务控制元数据和投递 outbox，并维护投递状态 | 问题集和轮次不扩展当前 ABox；死信保留任务，不执行删除补偿 |
| `backend/app/action/create_phone_verification_task.py` | 编排触发门禁、单 Function 问题生成、任务/HAS_PHONE_VERIFICATION、可靠投递和审计 | 只创建任务，不登记通话结果或修改最终决策 |
| `backend/app/action/phone_verification_result_repository.py` | 锁定已分配任务，原子写入不可覆盖的电核终态、回调摘要收据和结果图同步 outbox | 仅允许已投递且分配身份匹配的任务；终态和 callback 均有唯一边界 |
| `backend/app/action/record_phone_verification.py` | 编排电核回调字段公理、角色/分配、终态幂等、事务提交、图同步和链式审计 | 不允许覆盖终态、不触发人工复核或最终决定 |
| `backend/app/action/manual_review_repository.py` | 校验来源执行与规则，原子写入申请状态、转人工路由、原因/证据和图同步 outbox，并提供并发保护补偿 | 已完成申请/来源范围和后续业务保护；控制证据不保存敏感原文 |
| `backend/app/action/trigger_manual_review.py` | 编排来源审计链、转人工规则、两项决策 Function、证据门禁、状态提交、图同步和审计 | 不接受调用方角色，不写最终通过/拒绝结论 |
| `backend/app/action/decision_recommendation_repository.py` | 锁定申请和三类报告，原子写入建议、本人确认摘要、证据血缘及 outbox，并提供后续业务保护补偿 | 建议固定 `recommendation_only`，不修改申请完成状态 |
| `backend/app/action/record_decision_recommendation.py` | 编排双 Function、来源与高风险校验、本人确认门禁、建议提交、图同步和链式审计 | Agent 不能确认，不调用最终审批系统 |
| `backend/app/action/outbox.py` | 持久化八类图同步/外部投递事件，提供事件隔离、幂等入队、数据库租约、`SKIP LOCKED`、指数退避、重启恢复、死信精确领取/恢复和终态收敛 | 支持人工重放时保留历史失败 Action 终态；电核任务/终态仍按事实边界处理 |
| `backend/app/action/outbox_replay.py` | 保存重放请求和原因摘要，查询死信/过期租约，按八类 handler 执行受控重放并记录成功或失败审计 | 仅允许逐个 `DEAD_LETTER` 重放；服务端身份、请求幂等和并发互斥已实现 |
| `backend/app/action/operations.py` | 实现告警/事件/熔断表、告警去重、确认/解决、三级升级、半开探测、自动恢复、Webhook 和 Outbox 扫描 | MVP 运维闭环可用；通知载荷不含错误正文和业务样本，Webhook 默认关闭 |
| `backend/app/action/workflow.py` | 持久化申请级流程、8 步依赖、征信/反欺诈并行、条件人工复核、派发租约、Action 状态对账和追加式哈希审计 | MVP 编排可用；不保存原始 Action 请求，不静默重发已有副作用的失败 Action |

### 3.12 自动化测试

| 文件 | 作用 | 状态与备注 |
|---|---|---|
| `backend/tests/__init__.py` | 标记后端测试目录为 Python 包 | 包标记 |
| `backend/tests/test_nebula_safe_publication.py` | 验证 TBox/ABox 先写后清理、替换边保护、`INSTANCE_OF` 保留、类型化 VID、稳定 rank，以及插入/校验失败时不清理旧图谱 | 正常；当前 9 个标准库 `unittest` 用例通过，不依赖真实 NebulaGraph |
| `backend/tests/test_security.py` | 验证令牌比较、递归脱敏、图端点一致性、LLM 样本匿名化、HTTPS 精确白名单与本机 HTTP 限制 | 正常；6 个标准库 `unittest` 用例通过，不访问网络或真实业务数据 |
| `backend/tests/test_ontology_versioning.py` | 验证 SemVer、单向状态机、草稿归一化、差异集、JSONB 时间归一化、完整候选审核和关键失败门禁 | 正常；8 个标准库 `unittest` 用例通过，不修改版本仓库或图谱 |
| `backend/tests/test_data_quality.py` | 验证条件阶段、外部负债月供、待回填 NULL、预置阈值、待人工复核、类型、枚举、主键、空字符串、未知条件和快照完整性 | 正常；11 个标准库 `unittest` 用例通过，不访问真实数据库或图谱 |
| `backend/tests/test_nebula_query.py` | 用内存图验证空检索分页、关系端点完整、类型化 VID/原业务编号兼容、精确/属性匹配、hops、模式隔离和显式截断 | 正常；6 个标准库 `unittest` 用例通过，不依赖真实 NebulaGraph 驱动 |
| `backend/tests/test_compose_runtime_config.py` | 静态验证 NebulaGraph 状态端点、Storage 幂等初始化/失败关闭、Compose 依赖条件、应用健康检查和重启策略 | 正常；5 个标准库 `unittest` 用例通过，不启动或修改容器 |
| `backend/tests/test_document_metrics.py` | 校验统一发布指标内部加总、确定性基线/审核匹配、权威文档当前口径并阻止旧统计回归 | 正常；4 个标准库 `unittest` 用例通过，不访问数据库或图谱 |
| `backend/tests/test_function_registry.py` | 验证 9 个 Function 默认未绑定、显式绑定/替换/解绑、输入隔离、统一输出合同和受控异常 | 正常；9 个标准库 `unittest` 用例通过，不访问数据库、图谱或外部 API |
| `backend/tests/test_ratio_functions.py` | 验证 DTI/LTV 显式绑定、Decimal 精度、零负债、车辆价格选择、缺失值和非法数字失败边界 | 正常；9 个标准库 `unittest` 用例通过，不访问数据库、图谱或外部 API |
| `backend/tests/test_dti_data_adapter.py` | 验证单快照读取、实际 DTI 信封、零负债、缺失收入/月供失败及迁移非破坏性 | 正常；8 个标准库 `unittest` 用例通过，不访问真实数据库或图谱 |
| `backend/tests/test_business_functions.py` | 验证剩余 7 个 Function 的规则、失败关闭、关联脱敏、PD 代理和决策边界 | 正常；21 个标准库 `unittest` 用例通过，不访问真实数据库或图谱 |
| `backend/tests/test_function_data_adapter.py` | 验证全 9 Function 的最小字段单快照读取、跨存储编排、只读性、截断传播及风险/反欺诈/电核任务 Action 的声明函数集 | 正常；11个标准库 `unittest` 用例通过，使用内存适配器 |
| `backend/tests/test_action_design_catalog.py` | 验证 8 个 Action 全覆盖、状态机终态、DAG、幂等、Function/副作用、外部系统、审计和人工门禁 | 正常；9 个标准库 `unittest` 用例通过，不执行 Action 或数据写入 |
| `backend/tests/test_action_execution.py` | 验证请求摘要、幂等语义、角色拒绝、状态转换、终态保护、乐观锁和 PostgreSQL 防篡改 Schema | 正常；10 个标准库 `unittest` 用例通过，不写真实业务数据 |
| `backend/tests/test_generate_risk_assessment_action.py` | 验证首个端到端 Action 的成功、成功/失败重放、覆盖拒绝、Function 门禁、图失败补偿、稳定 ID、写集和服务端身份 | 正常；9 个标准库 `unittest` 用例通过，业务和图依赖使用内存替身 |
| `backend/tests/test_create_loan_application_action.py` | 验证进件请求、公理、权限、幂等、冲突、成功、图失败补偿、事务结构、服务端身份和隐私 | 正常；13个标准库 `unittest` 用例通过，业务和图依赖使用内存替身 |
| `backend/tests/test_request_credit_report_action.py` | 验证征信请求、权限、主体范围、授权、幂等、供应商超时/响应、沙箱标识和不重复供应商查询 | 正常；9个标准库 `unittest` 用例通过，业务和外部依赖使用内存替身 |
| `backend/tests/test_request_fraud_report_action.py` | 验证反欺诈请求、权限、核心关系、Function完整性、幂等、供应商超时/响应、沙箱标识和不重复供应商查询 | 正常；9个标准库 `unittest` 用例通过，业务和外部依赖使用内存替身 |
| `backend/tests/test_create_phone_verification_task_action.py` | 验证电核任务请求、权限、触发条件、问题完整性、幂等、待投递、沙箱和控制表边界 | 正常；9个标准库 `unittest` 用例通过，业务和外部依赖使用内存替身 |
| `backend/tests/test_record_phone_verification_action.py` | 验证电核结果合同、公理、角色与任务分配、终态幂等、冲突、受控错误和图重试保留 | 正常；8个标准库 `unittest` 用例通过，业务和图依赖使用内存替身 |
| `backend/tests/test_trigger_manual_review_action.py` | 验证人工复核请求、权限、来源审计、规则、双 Function 证据、幂等、图重试和仓储边界 | 正常；8个标准库 `unittest` 用例通过，业务和图依赖使用内存替身 |
| `backend/tests/test_record_decision_recommendation_action.py` | 验证固定声明、本人确认、Agent门禁、高风险禁止通过、证据、幂等、图重试和仓储边界 | 正常；10个标准库 `unittest` 用例通过，业务和图依赖使用内存替身 |
| `backend/tests/test_personal_credit_workflow.py` | 验证 8 Action 覆盖、并行依赖、条件跳过、请求摘要、失败关闭、持久化隐私和运行接入 | 正常；8个标准库 `unittest` 用例通过，不写真实业务数据 |
| `backend/tests/test_action_outbox.py` | 验证八类 outbox 成功、重试、租约接管、终态幂等、事务前崩溃恢复、死信补偿/任务保留、人工重放、补偿失败和持久化结构 | 正常；26个标准库 `unittest` 用例通过，不访问真实业务数据 |
| `backend/tests/test_outbox_replay.py` | 验证重放请求校验、服务端身份、handler 白名单、成功/失败、幂等历史、行锁、原因摘要和告警查询结构 | 正常；6个标准库 `unittest` 用例通过，不访问真实业务数据 |
| `backend/tests/test_operations.py` | 验证熔断打开/快速失败/恢复、通知成功与失败审计、关闭通知、Webhook 安全和运行时接入结构 | 正常；6个标准库 `unittest` 用例通过，不访问网络或真实业务数据 |

## 4. 当前 MVP 还缺少什么

如果目标仍然是“输出一版个人信审静态本体建模”，核心本体内容已经基本齐全，不缺新的业务对象数量；真正缺少的是让这版本体**可安全变更、可靠发布、可重复验证**的工程闭环：

1. 在现有版本登记、兼容回滚和补偿恢复基础上补充双版本 TBox/ABox 暂存、活动别名原子切换和不兼容迁移编排；
2. 在现有单用户令牌与默认脱敏之上增加正式登录、RBAC、字段授权、密钥轮换和完整查询/发布审计；
3. 在现有 LLM 匿名化、地址白名单和熔断之上增加服务器密钥托管、数据出境审批、调用留痕和预算；
4. 扩展自动化测试覆盖，并建立 Git 版本库、CI 和可重复验证脚本；
5. 为现有版本工作流增加真实用户身份、审核/批准职责分离、电子签名和幂等发布 ID；
6. 将已完成的一致性快照与目录质量校验扩展为数据库侧/增量执行，并补齐外键和大数据性能验证；
7. 在已完成的服务端图查询和显式分页基础上增加稳定 VID 游标、TBox/属性专用索引、查询超时及大图容量压测；
8. 数据库迁移、备份恢复、生产指标平台和灾备部署配置；
9. 将已完成的告警升级/可选 Webhook 接入企业通知回执、动态 SLA 和值班表；生产外部适配器和正式审批系统职责隔离最后接入。

正式 FIBO 对齐、CDC、事件驱动和自动推理属于后续阶段，不是当前静态本体版本的完成前置条件。

## 5. 建议的下一步顺序

### 第一步：先保护已有成果

- 建立 Git 仓库并提交当前可运行基线；
- 修复 TBox 重发丢失 `INSTANCE_OF` 和先删后写问题；
- 已增加进程内写锁、PostgreSQL advisory lock、版本登记、兼容回滚和失败补偿；下一步补幂等发布 ID 与双版本原子切换；
- 将现有 16 项深度审核、9 项候选门禁、74 项通用属性规则、26 条公理和 25/25 映射做成更完整的自动化回归测试。

### 第二步：补安全和隐私底座

- 已完成单用户 API 令牌、响应默认脱敏和浏览器会话级令牌配置；
- 已完成 LLM 样本匿名化、服务地址精确白名单和错误正文收敛；
- 下一步增加正式登录、RBAC、字段授权、操作审计和服务器密钥托管；
- 区分开发演示数据与真实业务数据环境。

### 第三步：让本体变更真正可管理

- 已增加 SemVer 和 `draft → reviewed → approved → published` 状态机；
- 已生成页面编辑/AI 候选相对活动版本的结构差异，并执行字段、公理、映射和安全影响门禁；
- 已保留历史版本和发布事件，支持兼容回滚；
- 已在发布后自动同步 ABox 并验证实例、关系及 TBox 版本映射；
- 下一步增加真实用户、职责分离、电子签名，以及不兼容结构变更的双版本迁移。

### 第四步：提升图谱查询和可运维性

- 已把关键词、1–3 hops 和模式筛选下沉服务端，并增加总量、分页、截断标记和边端点完整性；
- 下一步将 offset 分页替换为稳定 VID 游标，增加查询超时、TBox/属性专用索引和大图采样；
- 加入数据库迁移、健康检查、日志、指标、备份和恢复演练；
- 拆分前端单文件并补端到端测试。

### 第五步：真实业务数据接入时

- 复用现有目录校验先做字段分级、主键稳定性、阶段条件和授权复核，再把大数据校验下沉到数据库或增量管道；
- 再完成本地业务本体与 FIBO 的类/关系/属性对齐；
- 最后才考虑 CDC、事件驱动更新、持续推理和真实动作执行。

## 6. 本次未执行的破坏性验证

本次版本工作流改造在 PostgreSQL 新建了独立 `mvp_control` Schema，并将已核对的现有 `1.0.0` 登记为活动 `published` 版本；没有调用 TBox 发布或 ABox 同步，也没有创建测试版本。非破坏性验收确认非法 SemVer 和对当前活动版本的回滚均返回 `409`，业务 Schema 不包含控制表，NebulaGraph 仍为 150/269 且版本映射错误为 0。完整发布、补偿恢复和历史回滚的真实故障注入仍应只在独立测试 Space 中执行。

2026-08-22 图查询改造只重建并重启了后端/前端容器，所有验收均为读取操作；没有执行发布、ABox 同步、Schema 变更或数据卷清理，NebulaGraph 保持 150 个节点、269 条关系。

2026-08-22 类型化 VID/rank 迁移实际调用了一次受门禁保护的 ABox 同步：先写入并回读验证 25 个类型化实例和 50 条稳定 ranked edges，然后删除 25 个旧裸 VID 顶点及 50 条旧默认 rank ABox 边。PostgreSQL 与 TBox 未改动，NebulaGraph 总量保持 150/269，73 项数据质量、26 条公理和映射报告均有效。被清理的旧 ABox 表示可由 PostgreSQL 当前快照再次同步重建。

2026-08-22 本体预览布局修复只更新前端代码和无外部依赖的布局测试，没有调用发布、同步、版本写入或数据库修改接口。浏览器使用内存中的 9/73/11 测试本体检查画布布局，测试内容未保存为草稿且不会进入 PostgreSQL 或 NebulaGraph。

2026-08-24 执行 `002_add_external_debt_monthly_payment.sql`，只新增月还款额字段和非负约束，并为两条仓库演示负债补入明确月供；未删除或清空业务数据。随后 1.1.0 经 9/9 候选审核、批准和发布完成 TBox/ABox 同步，当前为 151/271、字段映射 212、数据质量 74/74、公理 26/26、映射有效。实际只读 DTI 结果分别为 0.128 和 0.066667。
