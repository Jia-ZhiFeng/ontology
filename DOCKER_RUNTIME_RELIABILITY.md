# Docker 与 NebulaGraph 启动可靠性说明

更新日期：2026-08-24  
适用范围：当前单机 Docker Compose MVP

## 1. 启动链

当前服务按真实就绪状态启动，不再依赖固定等待时间：

1. PostgreSQL 通过 `pg_isready` 后标记为健康；
2. NebulaGraph Meta、Storage、Graph 分别通过自身 `/status` 端点后标记为健康；
3. `nebula-init` 连接 Graph，执行 `SHOW HOSTS`；仅在 `storaged0:9779` 尚未注册时执行 `ADD HOSTS`；
4. `nebula-init` 持续检查 Storage Host，只有状态变为 `ONLINE` 才成功退出；超时或命令失败会返回非零状态，不再吞掉错误；
5. 后端等待 PostgreSQL、Graph 健康且 `nebula-init` 成功，启动时幂等创建 Action 控制表、审计触发器和 outbox 表；迁移失败则启动失败；随后启动带租约的 outbox worker，并通过 `/api/health` 同时校验 PostgreSQL 和 NebulaGraph；
6. 前端等待后端健康，再通过本机 HTTP 页面检查标记为健康。

PostgreSQL、NebulaGraph 三个常驻进程、后端和前端使用 `restart: unless-stopped`。健康检查本身负责暴露状态；若进程退出，重启策略负责拉起。单纯进入 `unhealthy` 不等同于自动重启，生产环境仍需外部监控告警或编排器处理。

## 2. 推荐启动与检查

后台启动并等待所有常驻服务就绪：

```bash
docker compose up -d --build --wait
```

检查配置和运行状态：

```bash
docker compose config --quiet
docker compose ps
docker compose logs --no-color nebula-init
```

预期结果：

- PostgreSQL、Meta、Storage、Graph、后端和前端均显示 `healthy`；
- `nebula-init` 以状态码 `0` 退出；
- 初始化日志显示 Storage Host 已注册或刚完成注册，随后显示 `ONLINE`；
- `GET http://localhost:8000/api/health` 返回 `status=ok`，且 PostgreSQL、NebulaGraph 均为 `true`。

## 3. 幂等与失败语义

- 重复执行 `docker compose up` 不会重复注册已存在的 Storage Host；初始化器先查后写。
- Graph 可连接但 Storage 未变为 `ONLINE` 时，后端不会启动，避免出现“端口可访问但图查询不可用”的假就绪。
- 初始化最多轮询 60 次，每次间隔 2 秒；超时会保留诊断输出并失败退出。
- Compose 的依赖条件只保证启动顺序和首次就绪门禁，不替代运行期监控、自动故障转移或数据恢复。

## 4. 数据卷与数据库变更

以下命令只重建容器并保留当前 PostgreSQL、NebulaGraph 数据卷：

```bash
docker compose up -d --build --wait
docker compose down
```

不要在需要保留数据时执行：

```bash
docker compose down -v
```

`postgres/credit_ontology_seed.sql` 只会在 PostgreSQL 空数据卷首次初始化时执行。后续表结构和数据变更不能依赖修改 seed 自动生效；进入真实数据阶段前，应引入可追踪、可回滚的数据库迁移工具，并建立 PostgreSQL 与 NebulaGraph 的备份恢复演练。

## 5. 本次验证基线

2026-08-24 在保留命名卷的情况下重建全部容器，结果如下：

- 6 个常驻容器全部为 `healthy`；
- Meta、Storage、Graph 的 `/status` 均返回 `running`；
- `nebula-init` 识别到 Storage Host 已注册并确认 `ONLINE`，状态码为 `0`；
- 当前图谱为 151 个顶点、271 条边；
- 25/25 个业务实体映射有效，25 条 `INSTANCE_OF` 和 50 条带稳定 rank 的 ABox 边保持不变；
- 73 条数据质量规则和 26 条公理继续全部通过；
- Action 执行/审计控制表已创建，幂等唯一约束和追加式审计触发器启用；记录数量取决于当前运行环境，真实受控拒绝验收已验证审计链有效且业务数据未被覆盖；
- 启动时幂等创建 `mvp_control.action_outbox`，风险评估和贷款进件两个后台 Worker 分别扫描自己的 `PENDING`、到期 `RETRY` 和租约过期 `PROCESSING` 事件；使用事件类型隔离和 `FOR UPDATE SKIP LOCKED`，既支持安全竞争，也不会跨类型误消费；
- 后端停止期间 outbox 事件保留在 PostgreSQL，重启后由新 worker 接管。真实恢复探针已验证 `PENDING → PROCESSING → CANCELLED` 收敛，探针随后精确删除，业务报告和图谱未变化；
- 对超过租约期限且没有 outbox 事件的非终态风险评估执行，worker 会按“事务提交前崩溃”失败关闭并追加审计，避免 Action 永久卡在中间态；
- 后端全量回归测试通过，其中 5 项专门验证 Compose 健康检查、依赖条件、幂等初始化、失败关闭和重启策略。
