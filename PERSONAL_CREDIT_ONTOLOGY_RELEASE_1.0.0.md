# 个人汽车信审本体 1.0.0 发布说明

> 历史发布记录：当前活动版本已升级为 `1.1.0`，本文件保留 1.0.0 发布当时的统计。

发布日期：2026-08-19；当前图谱统计复核：2026-08-24  
发布目标：NebulaGraph `credit_ontology` Space  
发布状态：**已发布**  
审核摘要（SHA-256）：`280639b67f21328bf499a7feedb6d8332d8fe1cd991ffb01e559465442b67c0d`

## 发布内容

- 9 个 ObjectType；
- 73 个 Property；
- 11 个 LinkType；
- 6 个 Interface，内含 26 条公理；
- 9 个无副作用、未绑定的 Function 声明；
- 8 个仅声明态 ActionType；
- 8 条 DynamicSecurity 策略。

发布后的 NebulaGraph TBox 共 125 个本体节点、219 条结构边。根节点 `tbox:Ontology:credit_ontology` 已记录：

- `version = 1.0.0`；
- `status = published`；
- 本次审核摘要；
- 已批准目录版本和统计元数据。

## 旧版本替换

发布前图库中的 TBox 版本为 `0.1`。本次发布删除了旧版本的 89 个 `ontology` 节点及其结构边，再写入完整的 `1.0.0` TBox，因此图库中不再保留旧 `0.1` 根版本。

旧 TBox 替换没有修改 PostgreSQL 数据。当前 ABox 经映射升级和 `SHARES_CONTACT` 同步后包含 25 个业务实体、25 条业务关系和 25 条 `INSTANCE_OF` 映射边；全部 50 条 ABox 边均使用稳定 rank。

## 发布后图谱状态

| 范围 | 节点数 | 关系数 |
|---|---:|---:|
| TBox 本体结构 | 125 | 219 |
| ABox 业务实例与映射 | 25 | 50 |
| 全部图谱 | 150 | 269 |

ABox 的 50 条边由 25 条业务关系和 25 条 `INSTANCE_OF` 映射边组成；TBox 与 ABox 合计关系数为 `219 + 50 = 269`。

## 能力边界

`1.0.0` 是个人汽车信审的静态本体建模版本。当前未启用 CDC、事件驱动自动更新、运行态推理、自动审批、真实 Action 执行、Agent MCP 或正式 FIBO URI 对齐。Function、ActionType 和 DynamicSecurity 只形成可审核的契约声明。

## 页面查看

打开 `http://localhost:8080`，进入“图谱探索”，点击“加载/刷新”：

- 选择“本体结构”查看已发布 TBox；
- 选择“业务实例”查看保留的 ABox；
- 在 1.0.0 发布当时，选择“全部图谱”可查看 150 个节点和 269 条关系；
- 点击任意节点可查看完整属性和根节点中的版本、审核摘要。
