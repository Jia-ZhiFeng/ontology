# 个人信审动力层与安全策略 v1.0

## 阶段边界

当前 TBox 仍输出静态能力声明，所有 Action 在本体中保持 `declaration_only`，所有 Function 保持 `not_bound` 且无副作用。独立运行层可显式绑定全部9个 Function；8个 Action 的独立执行层均已实现权限、幂等、证据、审计和可靠 outbox。决策建议 Action 只写 `recommendation_only` 建议，不能把申请改为已完成，也不执行最终审批；生产征信、反欺诈及电核系统仍默认关闭。

## Function（9个）

`calculate_dti`、`calculate_ltv`、`evaluate_credit_risk`、`evaluate_fraud_risk`、`assess_application_risk`、`generate_phone_questions`、`detect_association_risk`、`aggregate_decision_support`、`build_evidence_explanation`。

## ActionType（8个）

`create_loan_application`、`request_credit_report`、`request_fraud_report`、`generate_risk_assessment`、`create_phone_verification_task`、`record_phone_verification`、`trigger_manual_review`、`record_decision_recommendation`。

没有定义`execute_approval`。`record_decision_recommendation`只记录带证据的建议，最终审批必须由有权人员在业务系统完成。

## DynamicSecurity

- 8个Action各有且仅有一条安全策略。
- 每个Action必须审计，并限制允许角色、禁止角色、敏感字段和执行边界。
- Agent不能创建申请、发起征信/反欺诈查询、生成评估或触发人工复核。
- Agent仅可参与决策建议，并且必须附证据链、经过有权人员确认。
- 身份证、手机号、地址、征信、负债、风险因子、欺诈命中和电核录音均按敏感数据处理。

## 当前 MVP 运行时安全基线

- 除健康检查外，后端 API 使用服务端 `MVP_API_KEY` 进行单用户访问令牌校验。
- PostgreSQL、NebulaGraph、映射报告、关联查询和公理违规响应在 API 出口默认脱敏，不改变库内原值。
- 发送给 LLM 的样本被替换为保留字段、类型和空值状态的匿名占位符，原始业务值不会进入提示词。
- LLM Base URL 必须精确匹配服务端白名单；HTTP 仅可在显式启用时用于白名单内的本机地址。
- 浏览器只在当前会话保存 MVP 访问令牌；LLM API Key 仍由用户临时输入，不做长期存储。
- Function 注册中心只加载 9 个无副作用的静态契约，默认全部未绑定；实现必须显式绑定，重复绑定必须显式替换。
- 可选比率注册中心只绑定 DTI/LTV；可选完整只读注册中心显式绑定全部 9 个 Function。两者均不会在模块导入时自动创建，也未挂载到后端 API。
- DTI 数据适配器只在 `REPEATABLE READ / READ ONLY` 快照中读取必需字段；月供缺失时失败关闭，不用负债余额替代。
- 完整 Function 适配器在单个 PostgreSQL 一致性快照中读取最小字段，再独立读取 NebulaGraph 关联邻域；跨库存储不宣称原子，关联查询截断时综合评估失败关闭。
- `pd_value` 是未校准的 MVP 规则代理值；所有建议保持 `decision_support_only`，不能作为生产模型或最终审批。
- 8 个 Action 已具备机器可读运行设计和统一状态机合同，但当前仅用于设计校验，不执行权限、外部调用或副作用。
- Action 控制服务会根据静态安全策略拒绝未授权角色，并且只保存业务请求 SHA-256 摘要；当前尚无真实身份认证入口，调用方角色必须由未来服务端身份层提供。
- Action 审计采用追加表、前序哈希链和数据库防更新/删除触发器；审计详情进入 PostgreSQL 前再次脱敏。
- Function 执行会隔离输入，校验声明返回字段和统一结果信封，并将下游异常包装为受控错误；风险评估 Action API 只接受业务请求字段，角色与操作者由服务端配置注入，不信任调用方自报身份。
- 风险评估 Action 不覆盖已有正式报告；仅允许创建缺失报告或回填未开始的预创建空报告。报告和图同步意图在同一 PostgreSQL 事务提交；worker 使用数据库租约、幂等事件和指数退避重试，耗尽后精确恢复本次变更。最终审批字段不在允许写集内。

以上是本地单用户 MVP 的安全底座，不代表已经具备生产级登录、RBAC、租户隔离、字段授权、密钥托管或完整操作审计。
