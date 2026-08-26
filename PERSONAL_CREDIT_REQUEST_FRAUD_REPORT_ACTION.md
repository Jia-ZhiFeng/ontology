# 发起反欺诈查询 Action 实现说明

更新日期：2026-08-24  
适用 Action：`request_fraud_report`

## 1. 当前结论

该 Action 已成为 MVP 中第四个端到端业务 Action：

```text
服务端身份与角色校验
→ 当前申请及 SUBMITS/TARGETS 核心关系校验
→ application_id 幂等预留
→ PostgreSQL 一致性快照 + NebulaGraph 两跳关联查询
→ 执行 detect_association_risk
→ 使用稳定 provider_request_id 调用反欺诈供应商
→ 严格校验供应商响应及关联风险完整性
→ 先持久化供应商幂等收据
→ 创建或回填 AntiFraudReport
→ PostgreSQL 同事务写入 HAS_FRAUD_REPORT 图同步 outbox
→ 数据质量与26条公理门禁
→ NebulaGraph ABox 同步、映射回读与链式审计
```

它只登记反欺诈报告和关联风险摘要，不运行最终贷款审批，不修改申请状态或 `decision_result`。

## 2. API

```http
POST /api/actions/request-fraud-report/execute
X-API-Key: <MVP_API_KEY>
Content-Type: application/json
```

请求：

```json
{
  "application_id": "APP20260805000003",
  "request_id": "REQ-FRAUD-001"
}
```

客户端只能提交申请编号和请求编号。执行身份由服务端 `MVP_FRAUD_REPORT_ACTOR_ID`、`MVP_FRAUD_REPORT_ACTOR_ROLE` 注入；允许角色为系统适配器、信审员、风控专员，`Agent` 被拒绝。

## 3. 核心关系与关联风险门禁

执行前必须满足：

- 贷款申请存在且具备状态；
- 借款人及其 `NaturalPerson` 主数据存在，对应 `SUBMITS`；
- 目标车辆及其 `Vehicle` 主数据存在，对应 `TARGETS`；
- 当前申请不存在已完成的正式反欺诈报告；
- 最多只有一份反欺诈空占位报告。

`detect_association_risk` 是该 Action 唯一声明的 Function。它读取 PostgreSQL 的 `REPEATABLE READ, READ ONLY` 快照，并以借款人节点查询 NebulaGraph 的两跳 `SHARES_CONTACT`、`SUBMITS`、`GUARANTEES`、`CO_BORROWS` 路径。

Function 必须返回完整证据、实现版本和一致的快照时间。图查询被截断、覆盖不完整、Function 未绑定或输出合同无效时，Action 失败关闭，不调用供应商，也不创建报告。

返回和审计只保存关联路径数量、风险信号数量、最高等级、硬停止数量、覆盖范围和快照信息；自然人节点在 Function 内已转换为哈希引用，不把身份证号写入关联摘要。

## 4. 供应商与沙箱边界

生产默认配置：

```dotenv
FRAUD_PROVIDER_MODE=disabled
```

没有真实供应商合同、认证和签名规则时，Action 返回 `ACTION_FRAUD_PROVIDER_NOT_CONFIGURED`，不会伪造生产结果。

本地演示可以显式设置 `FRAUD_PROVIDER_MODE=sandbox`。沙箱响应具有以下约束：

- 报告编号以 `SBXFR` 开头；
- `synthetic=true`；
- 评分和结果由稳定请求号确定性生成；
- 命中明细明确注明“合成数据”；
- 不得用于真实信审、模型训练、模型评估或自动决策。

正式适配器必须保证同一 `provider_request_id` 幂等，并完成认证、签名验签、网络超时、错误码映射和字段映射。

## 5. 幂等收据与隐私

`mvp_control.fraud_provider_receipts` 在外部调用前预留稳定请求号。供应商最多尝试2次且必须复用同一个请求号。

收据只保存申请编号、请求上下文 SHA-256、供应商信息、响应和状态；不保存借款人身份证、手机号、地址或 VIN 明文。API 查询时整个 `response_payload` 继续脱敏。

若申请核心上下文在既有收据后发生变化，系统拒绝复用旧响应。供应商成功但本地写入失败时，本地最多重试2次，复用已缓存响应，不重新触发可计费查询。

## 6. 报告、关系与补偿

- 有一份全空占位报告时，保留其 `fraud_report_id` 并回填供应商结果；
- 没有占位报告时，使用供应商报告编号创建；
- `application_id` 同时是 `HAS_FRAUD_REPORT` 的确定性映射来源；
- 报告与图同步 outbox 在同一 PostgreSQL 事务提交；
- 本地关联风险摘要与供应商 `hit_details` 分开保存来源，不把本地推断伪装成供应商命中。

`ANTI_FRAUD_REPORT_GRAPH_SYNC` 由独立 Worker 消费，支持租约、`SKIP LOCKED`、指数退避、重启恢复和死信补偿。重试耗尽时，仅删除内容指纹未变化的本次新报告，或恢复未被后续修改的原空占位；否则进入 `COMPENSATION_FAILED`。

## 7. 主要错误

- `ACTION_ROLE_DENIED`
- `ACTION_APPLICATION_INCOMPLETE`
- `ACTION_IDEMPOTENCY_CONFLICT`
- `ACTION_FRAUD_REPORT_ALREADY_EXISTS`
- `ACTION_FUNCTION_NOT_BOUND`
- `ACTION_FUNCTION_FAILED`
- `ACTION_FUNCTION_OUTPUT_INVALID`
- `ACTION_FRAUD_PROVIDER_NOT_CONFIGURED`
- `ACTION_EXTERNAL_TIMEOUT`
- `ACTION_EXTERNAL_RESPONSE_INVALID`
- `ACTION_WRITE_FAILED`
- `ACTION_GRAPH_SYNC_FAILED`

## 8. 验证结果

- 9个 Action 专项测试覆盖请求、权限、核心关系、Function 完整性、幂等、供应商超时/响应、沙箱标识及不重复外部查询。
- 2个 Outbox 专项测试覆盖图同步成功和重试耗尽后的精确补偿。
- 1个新增数据适配测试验证该 Action 只执行 `detect_association_risk`。
- 当前全量后端回归为195个测试。
- 真实运行探针识别出3条关联路径，供应商重放命中缓存，并完成空占位回填/outbox 写入；事务退出后报告保持全空、outbox与供应商收据均为0残留。
