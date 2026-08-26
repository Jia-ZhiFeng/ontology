# 个人信审本体版本工作流

更新日期：2026-08-24

## 1. 目标

本工作流将原来的“审核后直接发布”改为持久化、单向、可追踪的版本流程：

`draft → reviewed → approved → published`

版本号必须使用 SemVer，例如 `1.1.0`。进入 `reviewed` 后内容不可覆盖；需要继续修改时，必须另建新版本。

## 2. 持久化位置

版本快照和发布事件保存在 PostgreSQL 的独立 `mvp_control` Schema：

- `mvp_control.ontology_versions`：Proposal JSON、摘要、状态、审核结果、说明和各阶段时间；
- `mvp_control.ontology_publication_events`：首次登记、发布和回滚事件。

该 Schema 不会出现在现有 PostgreSQL 业务数据浏览、Schema 剖析或本体字段覆盖检查中。

系统首次读取版本历史时，会核对当前 NebulaGraph 映射；最初据此把确定性 `1.0.0` 登记为活动版本。当前 `1.1.0` 已完整经过草稿、审核、批准、发布和 ABox 同步，`1.0.0` 保留为历史已发布版本。

## 3. 状态操作

### 保存草稿

- 保存完整 Proposal 快照、变更说明和 SHA-256 摘要；
- 自动校正 `metadata.counts`；
- 同一 `draft` 可以覆盖保存；进入后续状态后不可修改。

### 审核草稿

审核不要求候选与代码基线逐项完全相同，但仍强制以下门禁：

- SemVer、结构引用和名称完整性；
- `metadata.counts` 与候选内容一致；
- 当前 PostgreSQL 只读一致性快照通过 74 个属性的目录必填、阶段条件、类型、枚举、范围和业务主键校验；
- 26 条公理完整绑定，当前 PostgreSQL 数据通过错误级校验；
- 业务字段均映射或明确排除，关系来源字段真实存在；
- 9 张核心业务表能够无歧义建立 ABox–TBox 映射；
- Function 保持无副作用且未绑定，Action 保持声明态和强制审计；
- 禁止自动审批，`runtime_capabilities_enabled` 必须为 `false`。

与当前活动版本的新增、删除和修改项会写入 `change_set`，作为审核依据。失败审核结果也会保存在草稿记录中。

### 批准

只有 `reviewed` 可以进入 `approved`。批准时再次核对已审核摘要与保存内容完全一致。

### 发布

只有 `approved` 可以发布。服务端在 PostgreSQL advisory lock 下串行执行：

1. 在同一个 PostgreSQL 只读 `REPEATABLE READ` 快照上重新执行目录数据质量、26 条公理和候选审核；
2. 检查目标 TBox 不会删除当前 ABox 仍引用的类型、属性或关系节点；
3. 安全写入并回读校验目标 TBox；
4. 按目标版本重新同步 ABox；
5. 校验全部实例和业务关系的 TBox 版本及映射完整性；
6. 最后更新活动版本并记录发布事件。

任一步失败都不会更新活动版本；如果图谱切换已经发生，服务端会尝试用原活动版本重新发布 TBox 和 ABox。

### 回滚

- 只能选择曾经成功进入 `published` 的历史版本；
- 目标就是当前活动版本时拒绝操作；
- 回滚使用与正常发布相同的重新审核、ABox 引用兼容、TBox/ABox 切换和映射校验；
- 不兼容时在写图前阻止，不执行强制回退。

## 4. API

- `GET /api/ontology/versions`：版本列表和当前活动版本；
- `GET /api/ontology/versions/{version}`：完整版本快照和审核结果；
- `GET /api/ontology/version-events`：发布与回滚事件；
- `POST /api/ontology/versions/drafts`：保存或更新草稿；
- `POST /api/ontology/versions/{version}/review`：审核草稿；
- `POST /api/ontology/versions/{version}/approve`：批准；
- `POST /api/ontology/versions/{version}/publish`：发布并同步 ABox；
- `POST /api/ontology/versions/{version}/rollback`：回滚到历史已发布版本。

旧的 `POST /api/ontology/approve` 已关闭直接发布，调用时返回 `409`。

`POST /api/kg/sync` 和 `GET /api/kg/mapping/report` 以登记的活动版本为准，不固定使用代码内版本号。

## 5. 当前边界

- 当前只有一个 MVP 访问令牌，无法证明审核人与批准人是不同角色；
- 发布是受控补偿流程，不是 PostgreSQL 与 NebulaGraph 的跨库原子事务；
- 删除或迁移当前 ABox 正在引用的 TBox 节点会被兼容性门禁阻止，需要后续实现双版本暂存、ABox 迁移和活动别名原子切换；
- 没有 Git/CI、电子签名、审批任务、发布窗口或外部工单集成。
