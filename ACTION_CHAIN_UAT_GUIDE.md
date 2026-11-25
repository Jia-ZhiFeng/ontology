# 个人信审八 Action 业务验收数据

## 目标与边界

本数据集验证 8 个 Action 的真实写入、依赖推进、outbox、图同步和最终业务闭环。所有人员、联系方式、车辆和机构均为合成数据；场景定义存放在独立的 `mvp_uat` Schema，不得替换为真实客户数据。

基础准备不会预填 `loan_application`、`credit_report`、`anti_fraud_report`、`risk_assessment_report`、`phone_verification_record`、人工复核或决策建议记录。上述事实必须由各自 Action 产生，否则验收无效。

## 数据与脚本

- `scripts/action_chain_uat_scenario.json`：当前可重复执行的合成场景和每步预期。
- `postgres/action_chain_uat_schema.sql`：幂等创建 `mvp_uat.action_chain_scenarios` 与逐步观测表。
- `scripts/action_chain_uat.py`：准备、运行和状态查询工具；使用本地 psycopg 连接控制存储、使用工作流 API 执行 Action，不直写业务结果，不需要 Docker CLI。
- `docker-compose.uat.yml`：仅为本地验收启用征信、授权、反欺诈和电核合成沙箱。

## 执行方式

```powershell
.\start.cmd
.\.venv\Scripts\python.exe scripts/action_chain_uat.py prepare
.\.venv\Scripts\python.exe scripts/action_chain_uat.py run
.\.venv\Scripts\python.exe scripts/action_chain_uat.py status
```

本地 `.env.local` 中 `CREDIT_PROVIDER_MODE`、`CREDIT_CONSENT_MODE`、`FRAUD_PROVIDER_MODE` 和 `PHONE_TASK_DISPATCH_MODE` 必须均为 `sandbox`。修改后先 `stop.cmd` 再 `start.cmd`，配置与运行中状态不一致时验收脚本会拒绝执行。Windows 运行方式见 `WINDOWS_LOCAL_RUNTIME.md`。

`prepare` 和 `run` 均为幂等入口。已完成的工作流步骤只读取验证，不会再次派发；要进行一轮全新的验收，应复制场景并更换场景号、申请号、人员业务主键、VIN、经销商编号和工作流请求号。

## 当前正向场景

场景 `UAT-ACTION-CHAIN-002` 使用申请 `UATAC202608260007`。反欺诈沙箱结果可确定性复算为 63.48/人工复核；风险评估进入待人工复核，电话核验记录“存疑”，人工复核 Action 将申请路由为“转人工”，最后由风险经理身份记录非最终审批的决策建议。

验收通过必须同时满足：

- 工作流 8 步全部 `COMPLETED`，8 个 Action 执行全部 `SUCCEEDED`。
- 八类业务/控制结果各一条，且没有未成功的关联 outbox 事件。
- 反欺诈为人工复核，风险为中或高且待人工复核，电核有存疑点。
- 申请状态为待人工复核、路由结果为转人工；最终记录仍是建议，不是审批。

## 首轮缺陷留痕

场景 `UAT-ACTION-CHAIN-001` 在第 6 步发现 `AX-PHN-001` 实现把“尚未决策”误判为违规，造成电核 Action 无法进入后续人工复核。该失败执行和重放审计被保留在数据库中，没有覆盖或删除；规则已修正为“电核存疑时不得直接通过”，并由场景 002 完整回归通过。

此记录属于 UAT 缺陷证据，不应计入正向通过率。
