const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const vm=require("node:vm");

const html=fs.readFileSync(path.join(__dirname,"credit-review.html"),"utf8");
const css=fs.readFileSync(path.join(__dirname,"credit-review.css"),"utf8");
const script=fs.readFileSync(path.join(__dirname,"credit-review.js"),"utf8");

new vm.Script(script,{filename:"credit-review.js"});
assert.ok(html.includes('credit-review.css?v=9')&&html.includes('credit-review.js?v=9'),"结果优先与真实 trace 升级必须提升静态资源版本，避免旧页面缓存");

assert.ok(html.includes("多轮对话 · 自主规划 · Action 编排"),"应保留 Agent 主导的多轮科技感工作台");
assert.ok(html.includes("多轮记忆")&&html.includes("Query 拆解")&&html.includes("真实执行"),"应说明多轮 Agent 与真实执行能力");
for(const id of ["conversation-state-chip","context-turn","context-window"])assert.ok(html.includes(`id="${id}"`),`缺少多轮上下文状态：${id}`);
assert.ok(html.includes("服务重启后失效")&&html.includes("只读 Action 按整轮执行"),"应明确后端上下文的临时边界与整轮执行方式");
assert.match(html,/id="auto-read"[^>]*checked[^>]*disabled/,"多轮模式必须锁定后端整轮执行只读 Action");
for(const view of ["models","runs","catalog","knowledge"])assert.ok(html.includes(`data-view-panel="${view}"`),`缺少工作区：${view}`);
for(const modal of ["connection-modal","case-modal","evidence-modal","safety-modal","workflow-action-modal","run-modal","session-modal"])assert.ok(html.includes(`id="${modal}"`),`缺少交互弹窗：${modal}`);
assert.equal(html.includes('id="model-modal"'),false,"模型配置必须迁移到左侧独立模块，不能继续使用顶部弹窗");
assert.ok(html.includes('data-view="models"')&&html.includes("模型管理")&&html.includes('id="model-management-form"'),"左侧应提供完整的模型管理模块");
assert.ok(script.includes('location.hash||""')&&script.includes('==="#models"')&&script.includes("openHashView"),"模型管理应支持从兼容工作台通过 #models 直达");

assert.match(script,/\bfetch\s*\(/,"应通过 Fetch API 连接 MVP 后端");
for(const endpoint of [
  "/health","/workflows/personal-credit/summary","/workflows/personal-credit/definition",
  "/auth/session","/auth/check","/agents/personal-credit/tools","/agents/personal-credit/run","/readiness?ontology_version=",
])assert.ok(script.includes(endpoint),`缺少真实接口：${endpoint}`);

assert.ok(script.includes("requestWith")&&script.includes("ApiError")&&script.includes("AbortController"),"应具备统一 API、错误和超时处理");
assert.ok(script.includes("function requestTimeout")&&script.includes("return 65000")&&script.includes("return 125000")&&script.includes("模型连接测试超过 65 秒")&&script.includes("Agent 本轮处理超过 125 秒"),"模型测试与 Agent 应使用覆盖后端处理时长的独立超时");
const timeoutStart=script.indexOf("function requestTimeout");
const timeoutEnd=script.indexOf("async function requestWith",timeoutStart);
const timeoutContext={};
vm.runInNewContext(script.slice(timeoutStart,timeoutEnd),timeoutContext);
assert.equal(timeoutContext.requestTimeout("/health"),15000,"普通后端请求应保持短超时");
assert.equal(timeoutContext.requestTimeout("/agents/personal-credit/run"),125000,"Agent 请求应覆盖规划与总结两个模型窗口");
assert.equal(timeoutContext.requestTimeout("/llm/configs/abc/test"),65000,"模型连接测试应覆盖服务端处理窗口");
assert.ok(script.includes("setConnectionState")&&script.includes("BACKEND OFFLINE"),"应明确呈现在线与离线状态");
assert.ok(script.includes("sessionStorage")&&script.includes("lixin_mvp_connection"),"MVP 连接地址与远程兼容配置应只保存在当前会话");
assert.ok(script.includes('credentials:"include"'),"所有后端请求必须携带 HttpOnly 本机会话 Cookie");
assert.ok(script.includes('requestWith(config,"/auth/session",{method:"POST"})')&&script.includes('requestWith(config,"/auth/check")'),"本机连接必须先签发安全会话再校验状态");
assert.ok(script.includes('mode:"local_session"')||script.includes('"local_session"'),"应识别后端本机会话模式");
const persistConnectionSource=script.slice(script.indexOf("function persistConnection"),script.indexOf("function errorMessage"));
assert.ok(persistConnectionSource.includes("if(!local&&config&&config.token)"),"本机连接不得将访问密钥写入浏览器存储");
const connectionHelpersStart=script.indexOf("function normalizeBase");
const connectionHelpersEnd=script.indexOf("function errorMessage",connectionHelpersStart);
const migratedStorage={lixin_mvp_connection:JSON.stringify({base:"http://localhost:8000/api",token:"legacy-secret"})};
const connectionContext={
  URL,
  location:{protocol:"http:",hostname:"127.0.0.1",port:"4173",origin:"http://127.0.0.1:4173",href:"http://127.0.0.1:4173/credit-review.html"},
  STORAGE:{connection:"lixin_mvp_connection"},
  readSession(key,fallback){try{return JSON.parse(migratedStorage[key]||"null")||fallback}catch(_error){return fallback}},
  writeSession(key,value){migratedStorage[key]=JSON.stringify(value)},
};
vm.runInNewContext(script.slice(connectionHelpersStart,connectionHelpersEnd),connectionContext);
const migratedConfig=connectionContext.connectionConfig();
assert.equal(migratedConfig.token,"","本机旧版 X-API-Key 不得进入请求配置");
assert.equal(migratedConfig.base,"http://127.0.0.1:8000/api","本机 API 主机应与页面主机对齐，确保 SameSite Cookie 可用");
connectionContext.persistConnection(migratedConfig);
assert.equal(Object.hasOwn(JSON.parse(migratedStorage.lixin_mvp_connection),"token"),false,"启动迁移必须从 sessionStorage 物理清除本机旧令牌");
assert.ok(script.includes('sessionStorage.removeItem("lixin_agent_model")'),"升级后应主动清理旧版浏览器会话中残留的模型密钥");
assert.ok(script.includes('sessionStorage.removeItem("personalCreditAgentLLM")'),"主工作台也应清理兼容页面遗留的模型密钥");
assert.equal(/STORAGE\s*=\s*\{[^}]*model\s*:/.test(script),false,"模型配置不得继续写入浏览器存储");
assert.ok(script.includes("lixin_agent_backend_conversation")&&script.includes("persistConversation")&&script.includes("updateConversationFromResponse"),"应维护后端签发的 conversation_id");
assert.ok(script.includes("openWorkflowAction")&&script.includes("executeWorkflowAction"),"业务写入应通过独立确认门禁");
assert.ok(script.includes("step.status!==\"READY\"")&&script.includes("writePolicy===\"deny\""),"写入前必须校验工作流状态和前端安全策略");
assert.ok(script.includes("/steps/\"+encodeURIComponent(pending.name)+\"/execute"),"确认后应调用真实工作流步骤接口");
for(const marker of ["/llm/configs",'"/test"','"activate"','"deactivate"','method:"DELETE"'])assert.ok(script.includes(marker),`缺少后端模型管理调用：${marker}`);
assert.equal(script.includes("llmPayload"),false,"Agent 请求不得再携带浏览器中的模型配置");
assert.equal(script.includes('api("/llm/test"'),false,"模型测试必须针对后端配置 ID，不得再直接提交临时密钥");
assert.ok(html.includes("密钥仅提交到后端")&&html.includes("浏览器不再保存或随 Agent 请求发送密钥"),"模型管理应明确单一后端配置源");
assert.ok(html.includes('id="model-open-connection"')&&html.includes('id="model-auth-gate"'),"模型管理应提供 MVP 会话门禁与直达入口");
assert.ok(html.includes('id="connection-local-info"')&&html.includes("HttpOnly")&&html.includes('id="connection-token-field" hidden'),"本机连接应默认展示无感安全会话并隐藏手工 API Key");
assert.ok(html.includes('id="model-save-and-test"')&&script.includes("saveModelConfig(true)"),"首次配置应支持一次填写后保存并测试");
assert.ok(script.includes("verification_status")&&script.includes("modelStatusLabel")&&["草稿","验证中","可用","失败","启用"].every(value=>script.includes(value)),"应展示完整的模型验证与启用状态");
assert.ok(script.includes("api_key_masked")&&script.includes("api_key_configured"),"列表只能消费后端脱敏密钥元数据");
const managedTestSource=script.slice(script.indexOf("async function testModelConfig"),script.indexOf("async function activateModelConfig"));
assert.equal(managedTestSource.includes("api_key"),false,"连接测试只允许引用后端配置 ID，不得再次发送模型密钥");
assert.ok(script.includes("value.version=current.version")&&script.includes("error.status===409"),"编辑模型必须携带乐观锁版本并处理并发冲突");
assert.ok(script.includes('"?version="+encodeURIComponent(model.version)')&&script.includes("当前启用模型不能直接删除"),"删除模型必须携带版本并阻止直接删除启用配置");
assert.ok(script.includes("default_base_url")&&script.includes("base_url_editable")&&script.includes("applyProviderPreset"),"提供方选择应使用后端返回的默认地址与可编辑策略");
const connectionTestSource=script.slice(script.indexOf("async function testConnection"),script.indexOf("async function saveConnection"));
assert.ok(connectionTestSource.includes("establishAuthentication(value)"),"后端连接测试必须兼容本机会话与远程 API Key");
assert.ok(script.includes("非本机 MVP API 必须使用 HTTPS"),"非本机后端不得通过明文 HTTP 传输双重凭据");
assert.ok(script.includes("max_steps:requestedMaxSteps(text)")&&script.includes("execution_summary")&&script.includes("proposed_actions"),"应按本轮明确意图选择步骤上限并消费后端多步骤契约");
assert.ok(script.includes("context_window")&&script.includes("turn_index")&&script.includes("conversation_id"),"应消费后端多轮会话契约");

const validationStart=script.indexOf("function validateGoalInput");
const validationEnd=script.indexOf("function clarificationExamplesMarkup",validationStart);
assert.ok(validationStart>0&&validationEnd>validationStart,"应提供提交前输入有效性门禁");
const validationContext={};
vm.runInNewContext(script.slice(validationStart,validationEnd),validationContext);
for(const input of ["", "   ", "...", "！！！", "1", "12345", "１２３", "? 1 ?", "a"]){
  assert.equal(validationContext.validateGoalInput(input).valid,false,`无意义输入不应进入 Agent：${JSON.stringify(input)}`);
}
for(const input of ["风险", "电核", "评估这笔申请的风险", "生成电核问题", "risk assessment"]){
  assert.equal(validationContext.validateGoalInput(input).valid,true,`明确目标应允许提交：${input}`);
}
const runGoalSource=script.slice(script.indexOf("async function runGoal"),script.indexOf("function renderRuns"));
assert.ok(runGoalSource.indexOf("validateGoalInput")<runGoalSource.indexOf('api("/agents/personal-credit/run"'),"输入门禁必须在 Agent API 调用之前");
assert.ok(runGoalSource.includes("body.conversation_id=state.conversationId"),"后续轮次必须携带后端 conversation_id");
assert.ok(runGoalSource.includes("execute:true"),"多轮模式下只读 Action 必须由后端按整轮原子执行");
assert.equal(/\bllm\s*:|body\.llm/.test(runGoalSource),false,"Agent 请求必须由后端自动选择当前启用模型");
assert.ok(runGoalSource.indexOf('api("/agents/personal-credit/run"')<runGoalSource.indexOf("state.runs.unshift(run)"),"只有收到后端 Action 响应后才能创建运行记录");
assert.ok(runGoalSource.includes("!actionResponse")&&runGoalSource.includes("renderConversationResponse"),"CHAT/澄清响应不得进入 Action 运行分支");
const renderResponseSource=script.slice(script.indexOf("function renderAgentResponse"),script.indexOf("function saveRuns"));
assert.equal(renderResponseSource.includes("data-approve-read"),false,"多轮模式不得暴露无法回写会话的前端逐工具确认入口");
assert.equal(/approveRead|cancelRead|pendingRead/.test(script),false,"不得保留绕过后端多轮提交的旧只读执行路径");
assert.ok(script.includes("未调用任何 Action")&&script.includes("data-prompt="),"澄清消息应明确未调用 Action 并提供可点击示例");

const clarificationStart=script.indexOf("function isClarificationResponse");
const clarificationEnd=script.indexOf("function clarificationMessage",clarificationStart);
const clarificationContext={};
vm.runInNewContext(script.slice(clarificationStart,clarificationEnd),clarificationContext);
assert.equal(clarificationContext.isClarificationResponse({response_type:"NO_ACTION"}),true,"应识别后端权威 NO_ACTION 响应");
assert.equal(clarificationContext.isClarificationResponse({response_type:"NEEDS_CLARIFICATION"}),true,"应识别后端权威 NEEDS_CLARIFICATION 响应");
assert.equal(clarificationContext.isClarificationResponse({response_type:"CHAT",plan:{steps:[]},executions:[],executed:false}),false,"CHAT 即使没有 Action 也不得误标为澄清");
assert.equal(clarificationContext.isClarificationResponse({status:"NEEDS_CLARIFICATION"}),true,"应识别顶层澄清状态");
assert.equal(clarificationContext.isClarificationResponse({plan:{status:"NEEDS_CLARIFICATION"}}),true,"应识别计划澄清状态");
assert.equal(clarificationContext.isClarificationResponse({execution_summary:{status:"NEEDS_CLARIFICATION"}}),true,"应识别执行摘要澄清状态");
assert.equal(clarificationContext.isClarificationResponse({plan:{steps:[]},executions:[],executed:false}),true,"空计划和空执行不得渲染 Action");
assert.equal(clarificationContext.isClarificationResponse({plan:{steps:[{tool_name:"inspect_case_readiness"}]},executions:[]}),false,"真实计划不得被误判为澄清");
assert.ok(script.includes("if(isClarificationResponse(response))return[]"),"澄清响应必须产生零个计划步骤");

const typeStart=script.indexOf("function isClarificationResponse");
const typeEnd=script.indexOf("function normalPlanSteps",typeStart);
const typeContext={};
vm.runInNewContext(script.slice(typeStart,typeEnd),typeContext);
assert.equal(typeContext.agentResponseType({response_type:"CHAT"}),"CHAT","应消费自然对话响应");
assert.equal(typeContext.agentResponseType({response_type:"ACTION_PLAN"}),"ACTION_PLAN","应消费待执行 Action 计划响应");
assert.equal(typeContext.agentResponseType({response_type:"ACTION_RUN"}),"ACTION_RUN","应消费 Action 执行响应");
assert.equal(typeContext.isActionResponse({response_type:"NO_ACTION"}),false,"NO_ACTION 不得被当成任务");
assert.equal(typeContext.isActionResponse({response_type:"ACTION_RUN"}),true,"ACTION_RUN 应被当成任务");

const traceStart=script.indexOf("function traceStatusMeta");
const traceEnd=script.indexOf("function planCard",traceStart);
assert.ok(traceStart>0&&traceEnd>traceStart,"应提供后端 trace 归一化逻辑");
const traceContext={};
vm.runInNewContext(script.slice(traceStart,traceEnd),traceContext);
const authoritativeTrace=traceContext.normalTraceStages({
  trace:[
    {stage:"planner",label:"服务端 Planner",status:"COMPLETED",duration_ms:12,source:"local_router"},
    {stage:"action",tool_name:"preview_risk_assessment",label:"真实风险工具",status:"EXECUTED",duration_ms:34,source:"backend_read_only"},
  ],
  synthesis:{mode:"llm",status:"COMPLETED",reason_code:null},
});
assert.equal(authoritativeTrace[0].label,"服务端 Planner","存在后端 trace 时必须优先采用后端阶段，不得伪造固定步骤");
assert.equal(authoritativeTrace[1].durationMs,34,"应消费后端返回的真实阶段耗时");
assert.equal(authoritativeTrace[1].source,"backend_read_only","应消费后端 trace 的真实来源");
assert.equal(authoritativeTrace.at(-1).phase,"synthesis","后端 trace 未含总结阶段时应追加真实 synthesis 状态");
const legacyTrace=traceContext.normalTraceStages({
  plan:{planner_mode:"llm",reason:"模型已规划",readiness:{status:"READY",missing_evidence:[]},steps:[{tool_name:"preview_risk_assessment",tool_label:"风险预览",reason:"查询风险",status:"READY"}]},
  executions:[{tool_name:"preview_risk_assessment",tool_label:"风险预览",status:"EXECUTED",answer:"已执行"}],
  synthesis:{mode:"deterministic_fallback",status:"FALLBACK",reason_code:"MODEL_SYNTHESIS_FAILED"},
});
assert.deepEqual(Array.from(legacyTrace,item=>item.phase),["planner","readiness","action","synthesis"],"旧后端响应只能按可证实顺序推导 Planner、readiness、Action、synthesis");
assert.equal(traceContext.traceStatusMeta("FALLBACK").kind,"degraded","总结回退必须标记为降级");
assert.equal(traceContext.traceStageMeta({phase:"fallback",status:"COMPLETED"}).kind,"degraded","后端 fallback 阶段即使执行完成也必须展示为回答降级");
const planCardSource=script.slice(script.indexOf("function planCard"),script.indexOf("function resultFacts"));
assert.ok(planCardSource.includes('<details class="task-card dynamic-plan execution-trace">'),"真实执行过程应使用默认折叠的 details");
assert.equal(planCardSource.includes("<details open"),false,"过程不得默认展开遮挡最终结果");
assert.equal(/解析申请与证据上下文|拆解 Query 并选择 Action|index<2/.test(planCardSource),false,"不得伪造固定已完成步骤或按位置强制标记完成");

const answerStateStart=script.indexOf("function actionAnswerState");
const answerStateEnd=script.indexOf("function answerCard",answerStateStart);
const answerStateContext={};
vm.runInNewContext(script.slice(answerStateStart,answerStateEnd),answerStateContext);
assert.deepEqual({...answerStateContext.actionAnswerState({final_answer:"最终答案",answer:"旧答案"})},{answer:"最终答案",missing:false},"应优先显示后端 final_answer");
assert.equal(answerStateContext.actionAnswerState({final_answer:" ",answer:"兼容答案"}).answer,"兼容答案","空 final_answer 应兼容回退到非空 answer");
const missingAnswer=answerStateContext.actionAnswerState({executions:[{status:"EXECUTED"}]});
assert.equal(missingAnswer.missing,true,"Action 响应缺失答案时必须显式标错");
assert.ok(missingAnswer.answer.includes("没有返回 final_answer 或 answer"),"空答案不得泛化成后端成功");
const degradedSynthesis=answerStateContext.synthesisState({synthesis:{mode:"deterministic_fallback",status:"FALLBACK",reason_code:"MODEL_SYNTHESIS_FAILED"}});
assert.equal(degradedSynthesis.degraded,true,"模型总结回退必须对用户可见");
assert.equal(degradedSynthesis.reasonCode,"MODEL_SYNTHESIS_FAILED","应保留安全的后端降级原因代码");
assert.equal(script.includes("MVP 后端已返回结果。"),false,"缺失答案时不得展示泛化成功文案");
const conversationRenderSource=script.slice(script.indexOf("function renderConversationResponse"),script.indexOf("function renderAgentResponse"));
assert.ok(conversationRenderSource.includes("synthesisState(response)")&&conversationRenderSource.includes("conversation-quality-alert")&&conversationRenderSource.includes("DEGRADED"),"CHAT/澄清发生模型降级时也必须向用户显示安全告警");
const resultFirstSource=script.slice(script.indexOf("function renderAgentResponse"),script.indexOf("function saveRuns"));
assert.ok(resultFirstSource.indexOf("answerCard(response)")<resultFirstSource.indexOf("planCard(response,run)"),"最终结论必须渲染在执行过程之前");

const maxStepsStart=script.indexOf("function requestedMaxSteps");
const maxStepsEnd=script.indexOf("async function runGoal",maxStepsStart);
const maxStepsContext={};
vm.runInNewContext(script.slice(maxStepsStart,maxStepsEnd),maxStepsContext);
for(const input of ["查风险","生成电核问题","给出决策建议","检查材料"]){assert.equal(maxStepsContext.requestedMaxSteps(input),1,`单一目标默认只允许一个步骤：${input}`)}
for(const [input,count] of [["查风险并生成电核问题",2],["检查材料、风险和决策建议",3]]){assert.equal(maxStepsContext.requestedMaxSteps(input),count,`明确多意图应按目标数量开放拆解：${input}`)}
for(const input of ["完整审查这笔申请","全部检查","综合信审","full review"]){assert.equal(maxStepsContext.requestedMaxSteps(input),4,`明确全量意图才允许四步：${input}`)}

const newTaskSource=script.slice(script.indexOf("function newTask"),script.indexOf("function refreshIdleIntro"));
const loadContextSource=script.slice(script.indexOf("async function loadWorkflowContext"),script.indexOf("async function connectBackend"));
assert.ok(newTaskSource.includes("resetConversationContext"),"新建或清空对话必须重置 conversation_id");
assert.ok(loadContextSource.includes("resetConversationContext")&&loadContextSource.includes("ensureConversationApplication"),"切换申请或本体版本必须重置 conversation_id");
assert.ok(script.includes("conversationOntologyVersion")&&script.includes("conversationApplicationId"),"后端会话必须同时绑定申请和本体版本");
assert.ok(script.includes('newTask(false,{preserveConversation:true})'),"页面刷新时应允许恢复同一申请的后端会话标识");
assert.ok(script.includes('item.source==="mvp-backend"&&Array.isArray(item.actions)&&item.actions.length>0'),"最近任务只能恢复包含真实 Action 的运行");
const exportSource=script.slice(script.indexOf('on("export-session"'),script.indexOf('on("clear-session"'));
assert.ok(script.includes("function exportRunRecord")&&exportSource.includes("state.runs.map(exportRunRecord)"),"导出应使用裁剪后的 Action 运行摘要");
assert.equal(exportSource.includes(",response,error"),false,"导出不得包含完整后端 response 或原始 result");

assert.equal(script.includes("planFor("),false,"不得保留前端规则模拟 Planner");
assert.equal(script.includes("executePlan("),false,"不得保留前端模拟 Action 状态机");
assert.equal(/林知夏|赵清和|周南嘉|陈屿|42\.7%|170,000|TASK-0902/.test(html+script),false,"不得保留静态假案件或假结论");
assert.equal(/所有执行均为前端模拟|纯前端演示统计|模拟成功率/.test(html+script),false,"不得宣称模拟执行结果");

assert.ok(css.includes(".connection-button")&&css.includes('[data-status=offline]'),"应具备后端连接状态样式");
assert.ok(css.includes(".approval-box")&&css.includes("#workflow-action-form"),"应具备写入确认与表单样式");
assert.ok(css.includes(".clarification-prompts")&&css.includes(".clarification-state"),"应具备澄清提示和示例样式");
assert.ok(css.includes(".conversation-context-chip")&&css.includes(".conversation-response"),"应具备多轮状态和紧凑自然回复样式");
assert.ok(css.includes(".result-first")&&css.includes(".final-answer")&&css.includes(".execution-trace"),"应具备结果优先和折叠真实过程样式");
assert.ok(css.includes(".synthesis-alert")&&css.includes(".answer-degraded")&&css.includes(".answer-missing")&&css.includes(".conversation-quality-alert"),"应明显展示 Action 与自然对话的总结降级和结果缺失状态");

console.log(JSON.stringify({valid:true,backendIntegrated:true,multiTurn:true,mockFallback:false,agentRun:true,writeConfirmation:true}));
