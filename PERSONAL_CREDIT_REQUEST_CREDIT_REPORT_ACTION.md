# 发起征信查询 Action 实现说明

更新日期：2026-08-24  
适用 Action：`request_credit_report`

## 1. 当前结论

该 Action 已成为 MVP 中第三个端到端业务 Action，完成以下受控链路：

```text
服务端身份与角色校验
→ 当前申请主体范围校验
→ 征信授权校验
→ application_id + subject_id_number 幂等预留
→ 使用稳定 provider_request_id 调用供应商
→ 严格校验响应主体、来源、时间、渠道、计数和机构枚举
→ 先持久化供应商幂等收据
→ 创建征信报告或回填空占位报告
→ PostgreSQL 同事务写入图同步 outbox
→ 数据质量与26条公理门禁
→ NebulaGraph 同步 CreditReport 与 HOLDS
→ Action 成功及防篡改审计
```

它只登记征信查询结果和 `HOLDS`，不运行风险评估、不形成贷款决策，也不修改申请状态或最终审批结论。

## 2. API 与请求

```http
POST /api/actions/request-credit-report/execute
X-API-Key: <MVP_API_KEY>
Content-Type: application/json
```

请求合同：

```json
{
  "application_id": "APP20260801000001",
  "subject_id_number": "110101198803120011",
  "authorization_reference": "REAL-CONSENT-REFERENCE",
  "query_purpose": "贷款审批",
  "request_id": "REQ-CREDIT-001"
}
```

- 当前只允许 `贷款审批` 用途。
- 查询主体必须是该申请中的借款人、担保人或共借人，不能查询申请外自然人。
- 执行身份不由客户端提交；服务端通过 `MVP_CREDIT_REPORT_ACTOR_ID` 与 `MVP_CREDIT_REPORT_ACTOR_ROLE` 注入。
- 允许角色为 `系统适配器`、`信审员`；`Agent` 被拒绝。
- API、Action 审计和供应商收据均不保存授权引用明文；审计只保留 SHA-256 摘要。

## 3. 真实供应商与本地沙箱边界

默认配置为：

```dotenv
CREDIT_PROVIDER_MODE=disabled
CREDIT_CONSENT_MODE=disabled
```

因此，在没有真实授权核验器和供应商合同的环境中，Action 会失败关闭，不会把演示响应冒充真实征信。当前代码定义了供应商中立接口；正式接入需要实现：

- 授权引用的真实性、主体、用途、有效期和撤回状态校验；
- 供应商认证、签名验签、超时和错误映射；
- 对同一 `provider_request_id` 的幂等保证，避免重复计费；
- 响应字段到 `CreditReport` 的确定性映射。

本地演示可显式把两个 mode 都设为 `sandbox`，授权引用必须为 `SANDBOX-CONSENT:<application_id>`。沙箱报告编号以 `SBXCR` 开头，供应商收据中 `synthetic=true`。沙箱数据只能用于开发验证，不能参与真实信审或模型评估。

## 4. 供应商幂等收据与本地写入

`mvp_control.credit_provider_receipts` 在调用前预留稳定 `provider_request_id`，成功响应先写入收据，再写业务表。收据保存主体身份证的 SHA-256 摘要，不保存身份证明文；缓存响应不包含主体证件号。

供应商最多尝试2次，两次必须复用相同 `provider_request_id`。本地 PostgreSQL 写入失败时，最多重试2次并复用已缓存的供应商响应，禁止为补偿本地故障重新发起可计费查询。

征信报告写入规则：

- 同主体恰好存在一份未开始的全空占位报告时，保留其 `report_id` 并回填；
- 不存在占位报告时，使用供应商报告编号创建新报告；
- 存在多份空占位报告或供应商报告编号已存在时失败关闭；
- 报告行的 `subject_id_number` 同时是 ABox `HOLDS` 的映射来源，因此报告与关系来源在同一 PostgreSQL 事务提交。

## 5. Outbox、恢复与补偿

`CREDIT_REPORT_GRAPH_SYNC` 由独立 Worker 消费，不与进件或风险评估事件竞争。支持数据库租约、`FOR UPDATE SKIP LOCKED`、超时接管、指数退避、重启续跑与死信补偿。

图同步重试耗尽时：

- 本次新建报告仅在内容指纹未变化时删除；
- 回填的空占位报告仅在内容指纹未变化时恢复原值；
- 报告已被后续业务修改时拒绝自动补偿，并进入 `COMPENSATION_FAILED`；
- 补偿后再次同步并验证 NebulaGraph，避免残留孤立 `HOLDS`。

## 6. 返回与错误

成功结果返回 `report_id`、`provider_request_id`、供应商模式、是否为合成数据、主体角色、`HOLDS` 和 PostgreSQL/NebulaGraph 持久化状态，但不返回征信计数明细。

主要受控错误包括：

- `ACTION_ROLE_DENIED`
- `ACTION_SUBJECT_NOT_FOUND`
- `ACTION_CREDIT_CONSENT_MISSING`
- `ACTION_CREDIT_PROVIDER_NOT_CONFIGURED`
- `ACTION_IDEMPOTENCY_CONFLICT`
- `ACTION_CREDIT_REPORT_ALREADY_EXISTS`
- `ACTION_EXTERNAL_TIMEOUT`
- `ACTION_EXTERNAL_RESPONSE_INVALID`
- `ACTION_WRITE_FAILED`
- `ACTION_GRAPH_SYNC_FAILED`

## 7. 验证结果

- 9个专项 Action 测试覆盖请求合同、权限、主体范围、授权、幂等重放、供应商超时、响应主体校验、沙箱标识，以及“本地写重试但供应商只调用一次”。
- 2个专项 Outbox 测试覆盖征信图同步成功和重试耗尽后的精确补偿。
- 当前全量后端回归为195个测试。
- 生产默认关闭真实供应商调用；本轮不生成或遗留沙箱业务报告。
