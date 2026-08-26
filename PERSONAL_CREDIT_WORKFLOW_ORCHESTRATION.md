# 个人信审流程编排

日期：2026-08-25  
版本：`personal-credit-workflow-v1`

## 1. 目标与边界

流程编排以 `application_id` 为业务主线，把 8 个既有 Action 组织成可持久化、可查询、可暂停和可审计的申请级流程。编排器不复制 Action 业务逻辑，也不绕过权限、幂等、公理、Function 输出门禁、Outbox、熔断或补偿。

流程终点是 `record_decision_recommendation`，输出固定为 `recommendation_only`。系统没有新增最终审批、放款或拒绝执行能力。

## 2. 步骤和依赖

```text
创建贷款申请
       │
       ├── 发起征信查询 ──┐
       └── 发起反欺诈查询 ─┤
                          ↓
                    生成风险评估
                          ↓
                    创建电核任务
                          ↓
                    记录电核结果
                          ↓
             触发人工复核（条件步骤，可跳过）
                          ↓
                    记录决策建议
```

征信和反欺诈在申请建立后同时进入 `READY`，可以并行执行。风险评估必须等待二者完成。电核记录是当前决策建议 Function 的必要证据，因此不能跳过。人工复核只有命中转人工规则时执行；未命中时必须填写原因后显式跳过。

## 3. 流程与步骤状态

流程状态：

- `WAITING_INPUT`：至少一个步骤已就绪，等待用户提供该 Action 请求；
- `RUNNING`：Action 正在执行或等待 Outbox 收敛；
- `FAILED`：某个步骤受控失败；
- `COMPLETED`：决策建议步骤已完成；
- `CANCELLED`：用户填写原因后取消编排。

步骤状态：

- `PENDING`：依赖尚未完成；
- `READY`：可以执行；
- `RUNNING`：正在派发 Action；
- `WAITING`：Action 已接收，等待外部响应或 Outbox；
- `COMPLETED`：对应 Action 已进入 `SUCCEEDED`；
- `FAILED`：对应 Action 失败或补偿终止；
- `SKIPPED`：允许跳过的条件步骤已由用户说明原因。

## 4. 两种入口

- `NEW`：申请尚不存在，第一步从 `create_loan_application` 开始；
- `EXISTING`：申请已存在，创建申请步骤记为 `SKIPPED`，征信和反欺诈直接就绪。

同一申请只允许一个 `WAITING_INPUT`、`RUNNING` 或 `FAILED` 流程。`request_id` 是流程创建幂等键：相同申请和入口重复提交返回原流程，不同请求复用同一编号会被拒绝。

## 5. 持久化与恢复

PostgreSQL `mvp_control` 新增：

- `personal_credit_workflows`：流程实例；
- `personal_credit_workflow_steps`：8 个步骤、Action 执行引用和脱敏结果摘要；
- `personal_credit_workflow_events`：追加式事件及 SHA-256 前序哈希链。

流程表不保存 Action 原始请求。身份证号、征信授权、电核内容等只用于当次派发；步骤仅保存请求摘要 SHA-256、服务端派发编号和脱敏结果。

派发前会建立短租约。如果服务在 Action 建立前崩溃，租约到期且找不到相同 `correlation_id` 的 Action 执行时，步骤自动恢复为 `READY`；如果已经建立 Action 执行，后台根据 `action_executions` 自动接管并同步状态。Action 已进入失败终态时不会被编排器静默重发，防止重复征信计费、重复写入或重复外部投递。

## 6. API

- `POST /api/workflows/personal-credit`：创建流程；
- `GET /api/workflows/personal-credit/summary`：流程摘要；
- `GET /api/workflows/personal-credit/definition`：步骤定义和请求字段；
- `GET /api/workflows/personal-credit`：按状态、申请编号分页查询；
- `GET /api/workflows/personal-credit/{workflow_id}`：流程、步骤和审计详情；
- `POST /api/workflows/personal-credit/{workflow_id}/refresh`：立即对账 Action 状态；
- `POST /api/workflows/personal-credit/{workflow_id}/steps/{action_name}/execute`：执行就绪步骤；
- `POST /api/workflows/personal-credit/{workflow_id}/steps/{action_name}/skip`：跳过条件步骤；
- `POST /api/workflows/personal-credit/{workflow_id}/cancel`：取消未运行 Action 的流程。

所有接口继续受 `MVP_API_KEY` 保护。编排身份来自服务端 `MVP_WORKFLOW_ACTOR_ID`，Action 仍使用各自服务端角色，不接受调用方伪造身份。

## 7. 前端

“流程编排”页面提供：

- 新建/已有申请入口；
- 状态和申请编号筛选；
- 8 步时间线与并行就绪展示；
- 每一步的 JSON 请求编辑和执行确认；
- 人工复核条件跳过；
- Action 状态同步、流程取消和审计事件查看；
- 决策建议“不是最终审批”的二次确认。

## 8. 生产边界

当前属于单申请、人工驱动的 MVP 编排。征信、反欺诈和电核生产适配器仍默认关闭；正式接入前还需要供应商合同、回调签名、计费幂等、对账策略和外部 SLA。最终审批仍由独立业务系统和有权人员完成。

