# 个人信审 ABox 身份与关系 rank 规范

更新日期：2026-08-22  
适用范围：PostgreSQL 业务记录同步到 NebulaGraph ABox

## 1. 解决的问题

旧实现直接把 PostgreSQL 业务主键作为 NebulaGraph 全局 VID，并让所有 `related` 边使用默认 rank `0`。这会产生两个风险：

- 不同业务对象碰巧使用相同主键字符串时，可能写入同一个图顶点；
- 同一对端点之间存在不同关系类型时，后写入的 `related@0` 可能覆盖先写入的关系。

当前映射合同已经升级为 `personal-credit-abox-tbox-mapping-2.0`，使用类型化 VID 和按关系类型生成的稳定 rank。

## 2. 类型化 ABox VID

格式：

```text
abox:<ObjectType>:<business_key>
```

例如，业务编号 `DEMO-001` 在不同对象中会生成不同 VID：

```text
abox:NaturalPerson:DEMO-001
abox:LoanApplication:DEMO-001
```

因此业务主键相同也不会造成跨类型顶点冲突。原业务编号不会丢失，仍保存在实例 `_ontology.business_id` 中；页面和 `POST /api/kg/query` 仍可使用原业务编号精确检索。若同一原业务编号在多个对象类型中重复，关联接口会要求调用方改用完整类型化 VID，避免误查。

相关元数据：

- `vid_scheme=typed-abox-vid-v1`
- `business_key`：PostgreSQL 主键字段名
- `business_id`：原始业务主键值
- `object_type`：实例所属 ObjectType

TBox VID 继续使用 `tbox:` 前缀，不受本次迁移影响。

## 3. 稳定关系 rank

所有 ABox 业务关系和 `INSTANCE_OF` 使用以下确定性方案：

```text
rank = low63bits(SHA-256("related:" + relation_kind))
```

方案标识为 `relation-kind-sha256-v1`。同一关系类型在重复同步时得到相同的非负 int64 rank，不同关系类型可以在相同源、目标之间并存。查询结果和关联接口都会返回实际 `rank`，删除陈旧关系时也按 `source + target + rank` 精确删除，不会误删同端点上的替代关系。

当前业务模型把“同端点、同关系类型”视为一条逻辑关系；例如多个联系方式命中会合并到一条 `SHARES_CONTACT` 的属性中。未来若要保留同类型关系的多条业务事件，rank 方案应升级为“关系类型 + 关系业务唯一键”的稳定哈希。

## 4. 安全迁移顺序

`POST /api/kg/sync` 按以下顺序执行：

1. 在同一 PostgreSQL 一致性快照上通过 74 项目录质量校验和 26 条公理；
2. 构建完整类型化 ABox 和稳定 ranked edges；
3. 写入全部新顶点与关系；
4. 回读并验证目标顶点、端点、关系类型和 rank 均存在；
5. 仅在验证成功后，按精确 rank 删除旧关系并删除旧裸 VID 顶点；
6. 重新执行 ABox–TBox 映射报告。

写入或回读失败时不会进入旧数据清理阶段。该过程解决单次全量同步的误覆盖和先删后写风险，但仍不是跨 PostgreSQL/NebulaGraph 的严格原子事务；生产化仍需要双版本暂存、活动版本切换和延迟清理。

## 5. 2026-08-22 实际迁移结果

| 检查项 | 结果 |
|---|---:|
| 迁移前 ABox 实例 | 25 |
| 新类型化 ABox 实例 | 25 |
| 已清理旧裸 VID 顶点 | 25 |
| 已清理旧默认 rank ABox 边 | 50 |
| 当前类型化 VID 实例 | 25 / 25 |
| 当前稳定 ranked ABox 边 | 50 / 50 |
| 当前旧裸 VID 实例 | 0 |
| 映射报告错误 | 0 |
| NebulaGraph 总量 | 151 个节点、271 条边 |

迁移前后图谱业务规模保持不变。原业务编号精确检索仍可命中对应类型化实例并展开邻域。旧 ABox 顶点和旧 rank `0` 关系已在新图回读验证成功后删除；ABox 可由 PostgreSQL 当前快照再次同步重建。
