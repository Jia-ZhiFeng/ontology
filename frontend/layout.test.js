const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const adapter = fs.readFileSync(path.join(__dirname, "live-backend.js"), "utf8");
const inlineScripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match => match[1]);

assert.equal(inlineScripts.length, 1, "候选 HTML 原生脚本应保持单一入口");
new vm.Script(inlineScripts[0], {filename: "candidate-ui.js"});
new vm.Script(adapter, {filename: "live-backend.js"});
assert.ok(html.includes('<script src="live-backend.js"></script>'), "候选页面应加载真实后端适配层");
assert.ok(html.includes("OntoStudio · 本体建模平台"), "应保留候选 HTML 标题");
assert.ok(html.includes('id="sidebar"'), "应保留候选侧栏");
assert.ok(html.includes('id="page-model"'), "应保留候选树形建模页面");
assert.ok(html.includes('id="page-actions"'), "应保留候选动作页面");
assert.ok(html.includes('id="page-axioms"'), "应保留候选公理页面");
assert.ok(html.includes('id="page-graph"'), "应保留候选星图页面");
assert.ok(html.includes('id="page-data"'), "应保留候选数据关联页面");
assert.ok(html.includes('id="page-models"'), "应保留候选模型管理页面");
assert.equal(html.includes('data-page="accounts"'), false, "账号管理导航必须删除");
assert.equal(html.includes('data-page="perms"'), false, "权限管理导航必须删除");
assert.equal(html.includes('id="page-accounts"'), false, "账号管理页面必须删除");
assert.equal(html.includes('id="page-perms"'), false, "权限管理页面必须删除");
assert.equal(html.includes('id="role-sel"'), false, "顶栏角色切换必须删除");
assert.equal(html.includes("function renderAccounts"), false, "账号管理实现必须从源码删除");
assert.equal(html.includes("function renderPerms"), false, "权限管理实现必须从源码删除");
assert.equal(html.includes("PLATFORM.users"), false, "账号演示数据必须从源码删除");
assert.equal(html.includes("ROLE_NAMES"), false, "角色字典必须从源码删除");
assert.equal(html.includes("state.role"), false, "角色状态必须从源码删除");
assert.equal(html.includes("role-sel"), false, "角色切换样式和遗留命名必须删除");
assert.ok(html.includes('data-page="runtime"'), "应启用候选预留的实例图谱/动作运行入口");
assert.ok(html.includes('id="page-runtime"'), "应增加统一业务运行页面");

for (const route of [
  "/ontology/baseline", "/ontology/generate", "/ontology/versions/drafts",
  "/ontology/git/status", "/postgres/schema", "/llm/test", "/kg/snapshot",
  "/kg/query", "/kg/sync", "/workflows/personal-credit/definition",
  "/workflows/personal-credit/summary", "/workflows/personal-credit/data-search",
  "/operations/summary",
  "/operations/alerts", "/operations/circuits", "/actions/outbox/status",
  "/actions/outbox/alerts", "/replay", "/ontology/version-events",
  "/ontology/git/branches", "/ontology/git/tags",
]) assert.ok(adapter.includes(route), `后端适配缺少接口：${route}`);

assert.ok(adapter.includes("proposalToOntology"), "应将后端本体转换为候选页面数据模型");
assert.ok(adapter.includes("ontologyToProposal"), "应将候选编辑结果转换回后端本体");
assert.ok(adapter.includes("candidateRenderModels"), "模型配置应扩展候选页面而不是覆盖页面");
assert.ok(adapter.includes("candidateDrawTab"), "版本治理应扩展候选标签而不是覆盖建模页面");
assert.equal(adapter.includes("renderList=refreshHome"), false, "不得整体替换候选本体列表");
assert.equal(adapter.includes("renderModel=window"), false, "不得整体替换候选建模页面");
assert.equal(adapter.includes("JSON.stringify(data,null,2)"), false, "默认界面不得铺开接口 JSON");
assert.equal(adapter.includes('id="runtime-payload"'), false, "工作流执行不得要求手写 JSON");
assert.ok(adapter.includes("workflowDefinition"), "工作流表单必须读取后端工作流定义");
assert.ok(adapter.includes("data-workflow-field"), "工作流执行必须使用字段化表单");
assert.ok(adapter.includes("buildWorkflowPayload"), "字段化表单必须受控构造 Action 请求");
assert.ok(adapter.includes("workflow-dashboard"), "信审流程必须提供申请队列与流程详情工作台");
assert.ok(adapter.includes("workflowStepFacts"), "信审流程必须展示关键业务结果而非原始响应");
assert.ok(adapter.includes("WORKFLOW_SYSTEM_FIELDS"), "服务端生成字段不得要求业务人员手工填写");
assert.ok(adapter.includes("verification_id") && adapter.includes("source_execution_id"), "后续 Action 应自动承接前序执行标识");
assert.ok(adapter.includes("WORKFLOW_LOOKUP_CONFIG"), "Action 表单必须声明受控数据库检索范围");
assert.ok(adapter.includes("searchWorkflowData"), "Action 表单必须接入数据库搜索");
assert.ok(adapter.includes("applyWorkflowLookup"), "数据库搜索结果必须支持字段映射回填");
assert.ok(adapter.includes("覆盖已填写字段"), "自动回填必须让用户控制字段覆盖行为");
assert.ok(adapter.includes("保留当前申请编号、提交时间和初始状态"), "历史资料复用不得覆盖当前业务事件身份");
assert.ok(adapter.includes("data-prefill-source"), "数据库回填字段必须显示来源标记");
assert.ok(adapter.includes(".workflow-form [hidden]{display:none!important}"), "可选业务分组默认必须真正隐藏");
assert.ok(adapter.includes("governance-stage-rail"), "版本治理必须展示草稿到发布的阶段路径");
assert.ok(adapter.includes("governanceVersionActions"), "版本操作必须跟随真实状态显示");
assert.ok(adapter.includes("governanceEventRow"), "版本治理必须显示发布与回滚动态");
assert.ok(adapter.includes("review_failed"), "版本治理必须显式反馈审核门禁失败");
assert.ok(adapter.includes("action==='rollback'"), "版本治理必须接入受控回滚动作");
assert.ok(adapter.includes("trustedCell"), "运行表格必须显式区分可信控件与后端文本");
assert.equal(adapter.includes("value.indexOf('<')"), false, "不得把含尖括号的后端文本直接当 HTML");
assert.equal(html.includes("function tryTpl"), false, "不得保留提示词模板模拟试运行");
assert.equal(html.includes("模型返回结构化候选 JSON（演示）"), false, "不得展示模拟模型结果");
assert.ok(html.includes("待后端接口"), "未接入的提示词模板能力必须明确标注");
assert.ok(adapter.includes("不代表最终审批"), "流程页必须保留决策建议安全边界");
assert.ok(adapter.includes("不会改写原 Action 终态"), "Outbox 重放必须保留历史终态语义");

console.log(JSON.stringify({valid:true,candidate_scripts:inlineScripts.length,adapter:true}));
