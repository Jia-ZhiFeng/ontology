# 个人信审 ABox–TBox 映射说明

## 1. 目标与版本

- TBox 版本：`1.1.0`
- 映射合同版本：`personal-credit-abox-tbox-mapping-2.0`
- 数据源：PostgreSQL
- 图存储：NebulaGraph
- 映射方式：确定性映射，不依赖 LLM 临时判断

本映射把 PostgreSQL 中的业务记录建成 ABox 实例，并把实例类型、字段语义和业务关系显式对齐到已发布 TBox。当前仍是手动触发的全量同步，不代表已经实现 CDC、事件驱动更新或实时推理。

## 2. 表到 ObjectType

| PostgreSQL 表 | ABox 主键 | TBox ObjectType | Property 数 |
|---|---|---|---:|
| `natural_person` | `id_number` | `NaturalPerson` | 11 |
| `dealer` | `credit_code` | `Dealer` | 3 |
| `vehicle` | `vin` | `Vehicle` | 6 |
| `loan_application` | `application_id` | `LoanApplication` | 13 |
| `credit_report` | `report_id` | `CreditReport` | 9 |
| `external_debt` | `debt_id` | `ExternalDebt` | 7 |
| `risk_assessment_report` | `assessment_id` | `RiskAssessmentReport` | 11 |
| `anti_fraud_report` | `fraud_report_id` | `AntiFraudReport` | 5 |
| `phone_verification_record` | `verification_id` | `PhoneVerificationRecord` | 9 |

共映射 9 张表和 74 个 TBox Property。`credit_report.subject_type` 按个人信审一期范围有意排除，并记录排除原因，不计为未映射字段。

## 3. 三层映射

### 3.1 实例到类型

每一行 PostgreSQL 记录生成一个 `entity` 顶点，并通过以下显式边连接到 ObjectType：

```text
ABox 实例 -[INSTANCE_OF]-> tbox:ObjectType:<ObjectType>
```

ABox VID 使用 `abox:<ObjectType>:<business_key>`，避免不同表使用相同业务主键字符串时发生跨类型顶点冲突。原业务主键保留在 `_ontology.business_id`，查询仍兼容原业务编号。完整规范与迁移记录见 `PERSONAL_CREDIT_ABOX_IDENTITY.md`。

实例节点的 `_ontology` 元数据同时记录：

- `mapping_version`
- `tbox_version`
- `source_table`
- `object_type`
- `object_type_vid`
- `business_key` / `business_id`
- `vid_scheme=typed-abox-vid-v1`
- `edge_rank_scheme=relation-kind-sha256-v1`

### 3.2 字段到 Property

实例节点的 `_ontology.property_mappings` 按源字段记录对应的 Property、类型、必填规则和映射状态：

- `mapped_value`：字段当前有值；
- `mapped_null`：字段当前为空，但已完成语义映射，后续回填仍使用同一 Property。

映射元数据只保存字段到 Property 的引用，不重复保存身份证号、手机号等业务值。

### 3.3 业务边到 LinkType

业务实例之间的边继续使用业务关系名，例如 `SUBMITS`、`HOLDS`、`GENERATES`。每条边的 `_ontology` 元数据记录对应的 TBox LinkType、源/目标 ObjectType、版本和 rank 方案。ABox 业务边及 `INSTANCE_OF` 按关系类型生成确定性非负 int64 rank，不再共享默认 rank `0`；不同关系类型可以安全存在于同一源、目标之间。

当前 TBox 定义 11 种 LinkType。`SHARES_CONTACT` 仅在两个自然人的标准化手机号或标准化地址重复时派生。手机号支持去除空格、短横线和中国区号 `+86`，地址去除空白并做 Unicode 归一化。空值不参与匹配，自然人不能与自身连接，无向关系按身份证号排序去重；匹配值只保存 SHA-256 摘要。

当前样例中，王强与张伟使用同一演示手机号，因此生成 1 条 `SHARES_CONTACT`。该边表示关联线索，不等同于同一人、欺诈事实或自动拒贷结论。

## 4. 同步安全规则

执行 `POST /api/kg/sync` 时，后端在删除旧 ABox 前完成以下检查：

1. 9 张表都有确定的主键和 ObjectType；
2. 数据行没有未决字段，允许有明确记录原因的排除字段；
3. 9 个 ObjectType、74 个 Property 和 11 个 LinkType 均已存在于 NebulaGraph；
4. 所有业务关系端点都包含在本次同步实例中；
5. 每种业务关系都有确定的 LinkType。

任一检查失败时不会清理旧 ABox。检查通过后，系统先写入全部目标 `entity` 顶点、业务关系和 `INSTANCE_OF`，并回读校验预期顶点、关系类型和 rank 均已存在；只有写入与校验全部成功后，才按 `source + target + rank` 精确清理陈旧关系，并清理不再属于目标数据集的旧顶点。写入或回读校验失败时不会进入清理阶段。

该流程避免了“先删后写”导致的旧图谱整体丢失，也保证同 VID 的 ObjectType 在 TBox 重发时不会删除现有 `INSTANCE_OF`。当前仍是单 Space 内的兼容性安全写入，并非跨版本原子切换；后续生产化应使用版本暂存、活动版本切换和可回滚发布。

## 5. 映射报告与页面使用

- `POST /api/kg/sync`：同步 ABox，并返回本次映射报告；
- `GET /api/kg/mapping/report`：检查当前 NebulaGraph 中 ABox–TBox 映射完整性；
- 图谱探索 → `ABox ↔ TBox 映射`：只显示业务实例、ObjectType 和 `INSTANCE_OF`；
- 映射模式仍支持检索字段/节点/属性值和 hops 筛选；
- 点击实例节点可在节点详情中查看完整 `_ontology.property_mappings`；
- 页面下方的“ABox–TBox 映射报告”展示未映射实例、字段、关系和缺失 TBox 引用。

## 6. 当前验证结果

2026-08-24 发布 1.1.0 并同步现有 PostgreSQL 样例数据后：

| 检查项 | 结果 |
|---|---:|
| ABox 实例 | 25 / 25 已映射 |
| `INSTANCE_OF` | 25 |
| 字段映射条目 | 212 |
| 其中当前空值 | 40 |
| 有意排除字段条目 | 3 |
| 业务关系 | 25 / 25 已映射 |
| 有实例的 ObjectType | 9 / 9 |
| 有实例边的 LinkType | 11 / 11 |
| `SHARES_CONTACT` | 1 |
| 类型化 VID | 25 / 25 |
| 稳定 ranked ABox 边 | 50 / 50 |
| 旧裸 VID | 0 |
| 映射错误 | 0 |
| NebulaGraph 总量 | 151 个节点、271 条边 |

`GET /api/kg/mapping/report` 当前返回 `valid: true`。映射视图显示 25 个 ABox 实例、9 个 ObjectType 和 25 条 `INSTANCE_OF`；业务实例视图可检索 `SHARES_CONTACT` 及其两端自然人。

ABox 全量重建前会先执行26条公理。存在错误级违规、执行异常、执行器缺失或数据集截断时返回 HTTP 422，不删除当前 ABox。
