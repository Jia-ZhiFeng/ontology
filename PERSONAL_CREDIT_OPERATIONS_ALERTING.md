# 个人信审 MVP 外部调用熔断与主动告警

运行状态更新：2026-08-25

## 1. 实现目标

该能力在既有 Action 重试、Outbox 死信和人工重放之上增加三层运行保护：

1. 外部调用连续失败时自动熔断，避免请求堆积和级联故障；
2. 运行问题持久化为可确认、可解决、可审计的告警；
3. 告警长时间未解决时自动升级，并可选择通过 HTTPS Webhook 主动通知。

它不改变本体、九张业务表、信审规则或最终决策权限。新增数据全部位于 PostgreSQL `mvp_control` 控制域。

## 2. 熔断器

当前保护四个明确的外部边界：

- `credit-provider`：征信供应商；
- `fraud-provider`：反欺诈供应商；
- `phone-task-dispatch`：电核任务系统；
- `llm-service`：本体 AI 候选与 LLM 连接测试。

状态机：

```text
CLOSED
  ├─ 连续失败未达阈值 → CLOSED（累加 failure_count）
  └─ 达到阈值 → OPEN（产生 CRITICAL 告警）

OPEN
  ├─ 冷却期内 → 快速失败 ACTION_EXTERNAL_CIRCUIT_OPEN
  └─ 到达探测时间 → HALF_OPEN（只允许一个探测）

HALF_OPEN
  ├─ 探测成功 → CLOSED（清零失败计数并自动解决告警）
  └─ 探测失败 → OPEN（重新计算下一次探测时间）
```

熔断状态持久化在 `mvp_control.external_circuit_breakers`，因此后端重启后不会遗忘故障。供应商已成功返回的幂等缓存仍可直接复用，不会因熔断而重复调用或重复计费。

默认阈值为连续失败 3 次、打开 60 秒。页面支持人工复位，但必须填写处理说明；人工复位只允许下一次调用继续，不代表外部服务已经真实恢复。

## 3. 告警生命周期

告警表为 `mvp_control.operational_alerts`，事件历史为 `mvp_control.operational_alert_events`。

告警状态：

- `OPEN`：尚未确认；
- `ACKNOWLEDGED`：运维人员已确认并正在处理；
- `RESOLVED`：故障已经解决；同一问题再次发生时会重新打开。

告警等级为 `INFO`、`WARNING`、`CRITICAL`。相同 `alert_key` 不会不断新增记录，而是增加发生次数、更新时间和最新受控信息。

后台扫描器会主动发现：

- Outbox `DEAD_LETTER`；
- Outbox 过期 `PROCESSING` 租约；
- 外部依赖熔断打开；
- 已恢复的 Outbox 或熔断器，并自动解决对应系统告警。

所有确认、解决、升级和通知结果都记录服务端操作人或系统执行身份。API 请求体不能指定操作人。

## 4. 告警升级

活动告警到达 `next_escalation_at` 后由后台扫描器自动升级，最高三级：

- 0级：首次告警；
- 1级：首次超时未解决；
- 2级：升级为 `CRITICAL`；
- 3级：最高升级级别，继续保留到解决。

确认告警表示“有人处理”，不会删除告警；只有解决才停止继续升级。默认升级间隔为 900 秒，可通过环境变量调整。

## 5. 主动通知

页面红点和 PostgreSQL 告警始终启用。外部 Webhook 默认关闭：

```dotenv
ALERT_WEBHOOK_ENABLED=false
ALERT_WEBHOOK_URL=
```

启用后只发送以下运维字段：告警 ID、类别、等级、标题、发生次数和升级级别；不发送原始错误正文、身份证号、手机号、业务样本或数据库记录。

Webhook 默认必须使用 HTTPS。只有显式设置 `ALERT_WEBHOOK_ALLOW_INSECURE_LOCAL=true` 时，才允许本机回环 HTTP 地址。通知失败不会影响 Action 或告警入库，而是追加 `NOTIFICATION_FAILED` 事件，等待下一次升级或冷却期后的通知机会。

## 6. API 与页面

所有接口继续要求 `X-API-Key`：

- `GET /api/operations/summary`：活动告警、等级、熔断器和通知状态摘要；
- `GET /api/operations/alerts`：按状态查询告警；
- `GET /api/operations/circuits`：查询外部依赖熔断状态；
- `POST /api/operations/alerts/{alert_id}/acknowledge`：确认处理；
- `POST /api/operations/alerts/{alert_id}/resolve`：标记解决；
- `POST /api/operations/circuits/{dependency_name}/reset`：带说明人工复位。

前端“运行监控”新增导航红点、告警摘要、告警表和熔断器表，并保留原 Outbox 告警、人工重放及历史功能。

## 7. 配置

```dotenv
OPERATIONS_SCAN_SECONDS=15
ALERT_ESCALATION_SECONDS=900
ALERT_NOTIFICATION_COOLDOWN_SECONDS=300
EXTERNAL_CIRCUIT_FAILURE_THRESHOLD=3
EXTERNAL_CIRCUIT_OPEN_SECONDS=60
ALERT_WEBHOOK_ENABLED=false
ALERT_WEBHOOK_URL=
ALERT_WEBHOOK_TIMEOUT_SECONDS=5
ALERT_WEBHOOK_ALLOW_INSECURE_LOCAL=false
```

接入正式企业微信、钉钉或事件平台前，应使用企业提供的受控中转 Webhook，并配置出站白名单、签名认证、密钥托管和通知接收方职责。

## 8. 验证结果与边界

2026-08-25 已在真实 Docker 环境完成合成故障探针：两次失败达到探针阈值后熔断器进入 `OPEN`，后续调用返回 `ACTION_EXTERNAL_CIRCUIT_OPEN`；告警成功产生并从 0 级升级到 1 级，经 API 依次进入 `ACKNOWLEDGED`、`RESOLVED`，人工复位后熔断器回到 `CLOSED`。事件执行身份分别为 `circuit-breaker`、`operations-monitor` 和服务端 `mvp-outbox-operator`。Webhook 保持关闭，未产生外部发送。全部探针记录按唯一标识清理为 0。

当前仍属于单实例 MVP 运维闭环。生产化还需要正式用户 RBAC、确认人与解决人职责分离、不可抵赖签名、Prometheus/日志平台指标、企业通知渠道回执、值班表、SLA 策略和多实例压力/故障注入测试。

