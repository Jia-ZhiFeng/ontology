const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "workbench.html"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "workbench.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "workbench.css"), "utf8");

new vm.Script(script, {filename: "workbench.js"});
assert.ok(html.includes("workbench.css?v=20260903-local-session")&&html.includes("workbench.js?v=20260903-local-session"),"兼容工作台必须提升本机会话静态资源版本");
assert.ok(html.includes("CREDIT / FLOW"), "应提供独立的业务工作台品牌入口");
assert.ok(html.includes("当前待办") && html.includes("新申请进件"), "首屏应围绕日常业务任务");
assert.ok(html.includes("申请处理") && html.includes("已完成业务"), "应提供申请队列与归档业务");
assert.ok(html.includes('id="agent-dock"'), "业务 Agent 应跟随业务工作台提供辅助");
assert.equal(html.includes("人工验收结论"), false, "不得退化为验收管理平台");
assert.equal(html.includes("验收任务"), false, "不得展示验收会话管理概念");

for (const route of [
  "/workflows/personal-credit/summary",
  "/workflows/personal-credit/definition",
  "/workflows/personal-credit?limit=100",
  "/steps/",
  "/execute",
  "/refresh",
  "/skip",
  "/agents/personal-credit/run",
  "/auth/session",
  "/auth/check",
]) assert.ok(script.includes(route), `业务工作台缺少真实流程接口：${route}`);

assert.ok(script.includes("buildStepPayload"), "业务表单必须按字段构造 Action 请求");
assert.ok(script.includes("GROUP_META"), "进件必须使用分组业务表单");
assert.ok(script.includes("currentStep"), "业务 Agent 必须基于真实流程判断下一任务");
assert.ok(html.includes('href="credit-review.html#models"')&&html.includes("模型由砺信 Agent 模型管理统一配置"),"兼容工作台应统一跳转到砺信 Agent 模型管理");
assert.ok(script.includes("sessionStorage.removeItem('personalCreditAgentLLM')"),"兼容工作台应清理旧版会话中的模型密钥");
assert.ok(script.includes("discardLegacyLocalAccessToken()")&&script.includes("JSON.stringify({base:connection.base})"),"兼容工作台必须物理清除本机旧版访问令牌");
assert.ok(script.includes("credentials='include'")&&script.includes("establishWorkbenchAccess"),"兼容工作台必须自动建立并携带本机安全会话");
assert.ok(html.includes('id="workbench-connection-state"')&&html.includes("正在建立本机安全会话"),"兼容工作台应显示自动连接状态");
assert.equal(/agentLLMConfig|saveAgentModel|clearAgentModel|openAgentModelSettings|agent-llm-key/.test(script+html),false,"兼容工作台不得保留内联模型配置入口");
const agentStart=script.indexOf("async function sendAgent");
const agentEnd=script.indexOf("document.addEventListener",agentStart);
const agentSource=script.slice(agentStart,agentEnd);
assert.equal(/payload\.llm|api_key|base_url/.test(agentSource),false,"兼容工作台 Agent 请求不得再携带模型配置或密钥");
assert.ok(script.includes("lixin_mvp_connection")&&script.includes("X-API-Key"),"兼容工作台应复用主工作台的 MVP 访问配置");
assert.equal(script.includes("function agentReply"), false, "不得继续使用前端关键词模拟 Agent");
assert.ok(html.includes("Agent 仅执行只读 Function"), "必须明确 Agent 的执行边界");
assert.ok(css.includes(".agent-dock") && css.includes(".case-list-item"), "应具备业务队列与 Agent 工作区样式");

console.log(JSON.stringify({valid:true,workbench:true,no_acceptance_console:true}));
