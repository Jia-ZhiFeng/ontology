# 个人汽车信审本体 1.1.0 发布说明

发布日期：2026-08-24  
状态：`published` / 当前活动版本

## 本次变更

- PostgreSQL `external_debt` 非破坏性新增 `monthly_payment numeric(12,2)` 和非负检查约束；
- 两条仓库演示负债补入明确月还款额 3200、1200 元，不从负债余额推算；
- TBox 新增 `ExternalDebt.monthly_payment`（月还款额，元/月）；
- `calculate_dti` 的读取契约从错误的 `ExternalDebt.balance` 修正为 `ExternalDebt.monthly_payment`；
- 新增 `PostgresDTIDataAdapter`，在同一个 `REPEATABLE READ / READ ONLY` 快照中读取 DTI 输入；
- 修复版本审核记录无法序列化 PostgreSQL `datetime` 的 JSONB 缺陷。

## 发布门禁结果

- 候选审核：9/9 通过；
- 深度审核：16/16 通过；
- 公理执行：26/26 通过；
- 属性数据质量：74/74 通过，检查 212 个当前值；
- ABox–TBox 映射：有效，25/25 个实体、25/25 条业务关系、25 条 `INSTANCE_OF`；
- 变更集：新增 `ExternalDebt.monthly_payment`，修改 `calculate_dti`，无对象或关系删除。

## 当前图谱规模

| 范围 | 节点 | 关系 |
|---|---:|---:|
| TBox | 126 | 221 |
| ABox | 25 | 50 |
| 全部图谱 | 151 | 271 |

ABox 的 50 条边由 25 条业务关系和 25 条 `INSTANCE_OF` 映射边组成。字段映射条目由 210 增至 212，对应两条现有外部负债实例新增的月还款额映射。

## DTI 实际验证

- `APP20260801000001`：`3200 / 25000 = 0.128`；
- `APP20260803000002`：`1200 / 18000 = 0.066667`；
- 无外部负债时 DTI 为 0；月收入缺失或任一负债月供缺失时失败关闭。

本版本仍不执行自动审批。DTI 适配器是只读 Function 数据入口，尚未接入 Action API 或 `generate_risk_assessment` 的完整 DAG。
