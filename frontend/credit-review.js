(function(){
"use strict";

const $=(selector,root)=>(root||document).querySelector(selector);
const $$=(selector,root)=>Array.from((root||document).querySelectorAll(selector));
const icon=name=>'<svg aria-hidden="true"><use href="#i-'+name+'"></use></svg>';
const STORAGE={connection:"lixin_mvp_connection",runs:"lixin_agent_real_runs",safety:"lixin_agent_safety",conversation:"lixin_agent_backend_conversation"};
const STATUS_LABELS={WAITING_INPUT:"待补充资料",RUNNING:"处理中",FAILED:"处理异常",COMPLETED:"已完成",CANCELLED:"已取消",PENDING:"等待前置",READY:"可处理",WAITING:"等待系统回写",SKIPPED:"已跳过",BLOCKED:"证据不足"};
const FIELD_META={
 assessment_version:{label:"评估版本",default:"risk-v1"},data_snapshot_policy:{label:"数据快照策略",default:"repeatable_read_read_only"},
 verification_round:{label:"电核轮次",type:"integer",default:"1"},trigger_reason:{label:"触发原因",default:"Agent 根据当前风险证据建议进行电话核验",long:true},
 recommendation_version:{label:"建议版本",default:"recommendation-v1"},approval_assertion:{label:"建议边界声明",readonly:true,long:true},
 verification_id:{label:"电核任务编号"},verifier_identity:{label:"电核执行身份",default:"mvp-phone-result-adapter"},call_time:{label:"通话时间",type:"datetime-local"},
 duration_seconds:{label:"通话时长（秒）",type:"integer",default:"60"},conclusion:{label:"电核结论"},doubts:{label:"疑点说明",long:true},recording_file_id:{label:"录音文件编号"},
 review_reason:{label:"人工复核原因",default:"Agent 识别到需要人工复核的风险信号",long:true},source_execution_id:{label:"来源执行编号"},
 subject_id_number:{label:"申请人身份证号"},authorization_reference:{label:"授权凭证编号"},query_purpose:{label:"查询用途",default:"贷款审批",choices:["贷款审批"]},
 submit_date:{label:"提交时间",type:"datetime-local"},channel:{label:"申请渠道",default:"线上H5",choices:["线上H5","线下门店","经销商APP"]},loan_amount:{label:"贷款金额",type:"number"},loan_term:{label:"贷款期限（月）",type:"integer"},interest_rate:{label:"年利率",type:"number",default:"0.045"},application_status:{label:"申请状态",default:"评估中",choices:["资料待补充","评估中"]},
 id_number:{label:"身份证号"},name:{label:"姓名"},phone:{label:"手机号"},gender:{label:"性别",choices:["男","女"]},address:{label:"地址"},marital_status:{label:"婚姻状态",choices:["未婚","已婚","离异","丧偶"]},monthly_income:{label:"月收入",type:"number"},employer:{label:"工作单位"},
 vin:{label:"车辆 VIN"},brand:{label:"车辆品牌"},model:{label:"车辆型号"},invoice_price:{label:"发票价",type:"number"},guide_price:{label:"指导价",type:"number"},pledge_status:{label:"抵押状态",choices:["未抵押","抵押中","已解押"]},
 credit_code:{label:"经销商统一信用代码"},dealer_name:{label:"经销商名称"},location:{label:"经销商所在地"},guarantee_rating:{label:"担保评级",choices:["优","良","中","受限"]},guarantee_count:{label:"担保笔数",type:"integer"},guarantee_total:{label:"担保总额",type:"number"}
};
const GROUP_META={
 application:{label:"贷款申请",fields:["submit_date","channel","loan_amount","loan_term","interest_rate","application_status"]},
 borrower:{label:"借款人资料",fields:["id_number","name","phone","gender","address","marital_status","monthly_income","employer"]},
 vehicle:{label:"车辆资料",fields:["vin","brand","model","invoice_price","guide_price","pledge_status"]},
 dealer:{label:"经销商资料（选填）",fields:["credit_code","dealer_name","location"]},
 guarantor:{label:"担保人资料（选填）",fields:["id_number","name","phone","gender","address","marital_status","monthly_income","employer","guarantee_rating","guarantee_count","guarantee_total"]},
 co_borrower:{label:"共同借款人资料（选填）",fields:["id_number","name","phone","gender","address","marital_status","monthly_income","employer"]}
};
const SYSTEM_FIELDS=new Set(["application_id","request_id","callback_id","approver_id"]);
const CLARIFICATION_EXAMPLES=[
 {label:"预览综合风险",prompt:"评估这笔申请的综合风险并解释主要因素"},
 {label:"生成电核问题",prompt:"根据当前风险证据生成电核问题"},
 {label:"生成决策建议",prompt:"聚合当前证据并生成信审决策建议"}
];

class ApiError extends Error{
 constructor(message,status,detail){super(message);this.name="ApiError";this.status=status;this.detail=detail}
}

function esc(value){return String(value==null?"":value).replace(/[&<>"']/g,char=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"})[char])}
function nowTime(){return new Date().toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false})}
function localTime(value){if(!value)return "—";const date=new Date(value);return Number.isNaN(date.getTime())?"—":date.toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"})}
function uid(prefix){return prefix+"-"+(crypto.randomUUID?crypto.randomUUID().slice(0,8):Date.now().toString(36))}
function readSession(key,fallback){try{const value=JSON.parse(sessionStorage.getItem(key)||"null");return value==null?fallback:value}catch(_error){return fallback}}
function writeSession(key,value){sessionStorage.setItem(key,JSON.stringify(value))}
function toast(message,type){const node=document.createElement("div");node.className="toast"+(type==="error"?" error":"");node.innerHTML="<i></i><span>"+esc(message)+"</span>";$("#toast-region").appendChild(node);setTimeout(()=>{node.style.opacity="0";setTimeout(()=>node.remove(),180)},3200)}
function modal(selector,open){const node=$(selector);if(!node)return;node.hidden=!open;document.body.style.overflow=open?"hidden":""}
function scrollChat(){const chat=$("#chat");chat.scrollTop=chat.scrollHeight}
function setResult(selector,message,type){const node=$(selector);if(!node)return;node.hidden=false;node.className="test-result "+type;node.textContent=message}
function on(id,event,handler){const node=$("#"+id);if(node)node.addEventListener(event,handler)}
function normalizeBase(value){return String(value||"").trim().replace(/\/+$/,"")}
function defaultApiBase(){if(location.protocol==="file:")return "http://127.0.0.1:8000/api";if(location.port==="8000")return location.origin+"/api";return location.protocol+"//"+(location.hostname||"127.0.0.1")+":8000/api"}
function isLoopbackHost(value){return["localhost","127.0.0.1","::1","[::1]"].includes(String(value||"").toLowerCase())}
function isLocalApiBase(value){try{return isLoopbackHost(new URL(normalizeBase(value),location.href).hostname)}catch(_error){return false}}
function alignLocalApiBase(value){const base=normalizeBase(value);try{const url=new URL(base,location.href),pageHost=String(location.hostname||"");if(isLoopbackHost(url.hostname)&&isLoopbackHost(pageHost)&&url.hostname!==pageHost)url.hostname=pageHost;return normalizeBase(url.href)}catch(_error){return base}}
function connectionConfig(){const saved=readSession(STORAGE.connection,{}),base=alignLocalApiBase(saved.base||defaultApiBase()),local=isLocalApiBase(base);return{base,token:local?"":String(saved.token||"").trim(),local}}
function persistConnection(config){const base=alignLocalApiBase(config&&config.base||defaultApiBase()),local=isLocalApiBase(base),saved={base};if(!local&&config&&config.token)saved.token=String(config.token).trim();writeSession(STORAGE.connection,saved);return{base,token:local?"":String(saved.token||""),local}}
function errorMessage(payload,status){const detail=payload&&payload.detail;if(typeof detail==="string")return detail;if(detail&&typeof detail.message==="string")return detail.message;if(Array.isArray(detail))return detail.map(item=>item.msg||item.type).filter(Boolean).join("；")||"请求参数无效";if(payload&&typeof payload.message==="string")return payload.message;return status===0?"无法连接 MVP 后端，请检查地址、服务状态与 CORS 配置":"MVP 后端请求失败（HTTP "+status+"）"}

function requestTimeout(path,options){if(options&&Number.isFinite(Number(options.timeoutMs)))return Math.max(1000,Number(options.timeoutMs));if(path==="/agents/personal-credit/run")return 125000;if(/^\/llm\/configs\/[^/]+\/test$/.test(path))return 65000;return 15000}
function timeoutMessage(path){if(/^\/llm\/configs\/[^/]+\/test$/.test(path))return"模型连接测试超过 65 秒，请检查模型服务状态后重试";if(path==="/agents/personal-credit/run")return"Agent 本轮处理超过 125 秒，请稍后确认后端状态再重试";return"连接 MVP 后端超时"}
async function requestWith(config,path,options){
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),requestTimeout(path,options)),headers=Object.assign({Accept:"application/json"},options&&options.headers||{}),fetchOptions=Object.assign({},options||{});delete fetchOptions.timeoutMs;
 if(options&&options.body!=null)headers["Content-Type"]="application/json";
 if(config.token)headers["X-API-Key"]=config.token;
 try{
  const response=await fetch(normalizeBase(config.base)+path,Object.assign(fetchOptions,{headers,signal:controller.signal,credentials:"include"}));
  let payload=null;try{payload=await response.json()}catch(_error){}
  if(!response.ok)throw new ApiError(errorMessage(payload,response.status),response.status,payload);
  return payload;
 }catch(error){
  if(error instanceof ApiError)throw error;
  if(error&&error.name==="AbortError")throw new ApiError(timeoutMessage(path),0,null);
  throw new ApiError(errorMessage(null,0),0,error);
 }finally{clearTimeout(timer)}
}
let localSessionRefresh=null;
function authenticationResult(payload,fallbackMode){const source=payload&&typeof payload==="object"?payload:{};return{authenticated:source.authenticated!==false,mode:String(source.mode||source.auth_mode||fallbackMode||"mvp_api_key"),expiresAt:source.expires_at||source.expiresAt||null}}
async function establishAuthentication(config){
 if(config.token){const checked=await requestWith(config,"/auth/check");return authenticationResult(checked,"mvp_api_key")}
 if(!config.local)return{authenticated:false,mode:"anonymous",expiresAt:null,error:null};
 let bootstrapError=null,session=null;
 try{session=await requestWith(config,"/auth/session",{method:"POST"})}catch(error){bootstrapError=error}
 try{const checked=await requestWith(config,"/auth/check");const result=authenticationResult(checked,session&&session.mode||"local_session");if(session&&session.expires_at&&!result.expiresAt)result.expiresAt=session.expires_at;return result}catch(error){
  if([401,403,404,405,503].includes(error.status)){const unavailable=bootstrapError&&(bootstrapError.status===404||bootstrapError.status===405),misconfigured=bootstrapError&&bootstrapError.status===503;return{authenticated:false,mode:unavailable?"local_unsupported":"local_session_failed",expiresAt:null,error:unavailable?"当前 MVP 后端尚未提供本机安全会话，请更新或重启后端。":misconfigured?"MVP 后端尚未配置本机会话签名密钥，请检查后端配置后重启。":"本机安全会话建立失败，请确认页面与 MVP 后端均从受信任的本机地址访问。"}}
  throw bootstrapError&&bootstrapError.status!==404&&bootstrapError.status!==405?bootstrapError:error
 }
}
function applyAuthentication(result){state.authenticated=!!(result&&result.authenticated);state.authMode=state.authenticated?String(result.mode||"api_key"):String(result&&result.mode||"anonymous");state.sessionExpiresAt=result&&result.expiresAt||null;state.authError=result&&result.error||null}
async function refreshLocalAuthentication(config){if(!localSessionRefresh)localSessionRefresh=establishAuthentication(config).finally(()=>{localSessionRefresh=null});const result=await localSessionRefresh;applyAuthentication(result);return result}
async function api(path,options){const config=connectionConfig();try{return await requestWith(config,path,options)}catch(error){if(error.status!==401||!config.local||config.token||path==="/auth/session"||path==="/auth/check")throw error;const result=await refreshLocalAuthentication(config);if(result.authenticated)return requestWith(config,path,options);throw new ApiError(result.error||"本机安全会话已失效，自动重连失败。",401,error.detail)}}
function backendAuthGuidance(){return connectionConfig().local?"本机安全会话未建立。请确认后端已更新并正在运行，然后点击 MVP Backend 重新连接。":"请在 MVP Backend 连接设置中填写有效的 X-API-Key；它与外部模型 API Key 不同。"}

const savedSafety=readSession(STORAGE.safety,{}),savedConversation=readSession(STORAGE.conversation,{});
const state={
 online:false,authenticated:false,authMode:"anonymous",authError:null,sessionExpiresAt:null,connecting:false,busy:false,currentView:"chat",runFilter:"all",actionFilter:"all",
 summary:null,definition:null,workflows:[],currentWorkflow:null,workflowDetail:null,readiness:null,
 agentTools:[],executionBoundary:null,actionCatalog:{},lastAgentResponse:null,lastPrompt:"",pendingWorkflowAction:null,activeRun:null,
 models:[],activeModelId:null,selectedModelId:null,modelProviders:[],modelLoading:false,modelActionBusy:false,modelsLoaded:false,modelAccessError:null,
 conversationId:typeof savedConversation.conversation_id==="string"?savedConversation.conversation_id:null,conversationApplicationId:typeof savedConversation.application_id==="string"?savedConversation.application_id:null,conversationOntologyVersion:typeof savedConversation.ontology_version==="string"?savedConversation.ontology_version:null,turnIndex:Number.isFinite(Number(savedConversation.turn_index))?Number(savedConversation.turn_index):0,contextWindow:[],contextWindowSize:Number.isFinite(Number(savedConversation.context_count))?Number(savedConversation.context_count):0,contextPending:false,
 safety:{autoRead:true,writePolicy:savedSafety.writePolicy==="deny"?"deny":"confirm"},
 runs:readSession(STORAGE.runs,[]).filter(item=>item&&item.source==="mvp-backend"&&Array.isArray(item.actions)&&item.actions.length>0).slice(0,30)
};

function persistConversation(){
 if(!state.conversationId)return sessionStorage.removeItem(STORAGE.conversation);
 writeSession(STORAGE.conversation,{conversation_id:state.conversationId,application_id:state.conversationApplicationId,ontology_version:state.conversationOntologyVersion,turn_index:state.turnIndex,context_count:state.contextWindowSize})
}
function updateConversationStatus(){
 const active=!!state.conversationId,turn=Math.max(0,Number(state.turnIndex)||0),count=Math.max(0,Number(state.contextWindowSize)||0),chip=$("#conversation-state-chip");
 if(chip){chip.dataset.state=state.contextPending?"pending":active?"active":"new";chip.textContent=state.contextPending?"CONTEXT · SYNC":active?"TURN "+turn+" · "+count+" CTX":"CONTEXT · NEW"}
 if($("#context-turn"))$("#context-turn").textContent=active?"第 "+turn+" 轮":"新会话";
 if($("#context-window"))$("#context-window").textContent=active?count+" 轮已记忆":"等待首轮";
 if($("#agent-mode-copy"))$("#agent-mode-copy").innerHTML="<i></i>"+(state.online?(state.contextPending?"正在同步后端对话上下文":active?"后端临时记忆 · 第 "+turn+" 轮":"后端多轮对话 · 等待首轮"):state.connecting?"正在读取后端能力":"等待后端连接")
}
function resetConversationContext(){
 state.conversationId=null;state.conversationApplicationId=null;state.conversationOntologyVersion=null;state.turnIndex=0;state.contextWindow=[];state.contextWindowSize=0;state.contextPending=false;sessionStorage.removeItem(STORAGE.conversation);updateConversationStatus()
}
function ensureConversationApplication(applicationId,ontologyVersion){
 if(state.conversationId&&(state.conversationApplicationId!==applicationId||state.conversationOntologyVersion!==String(ontologyVersion||"")))resetConversationContext()
}
function conversationEnvelope(response){return response&&response.conversation&&typeof response.conversation==="object"?response.conversation:{}}
function updateConversationFromResponse(response){
 const envelope=conversationEnvelope(response),conversationId=response&&response.conversation_id||envelope.conversation_id||envelope.id,turnValue=response&&response.turn_index!=null?response.turn_index:envelope.turn_index,windowValue=Array.isArray(response&&response.context_window)?response.context_window:Array.isArray(envelope.context_window)?envelope.context_window:null;
 if(typeof conversationId==="string"&&conversationId.trim()){state.conversationId=conversationId.trim();state.conversationApplicationId=state.currentWorkflow&&state.currentWorkflow.application_id||state.conversationApplicationId;state.conversationOntologyVersion=String(state.currentWorkflow&&state.currentWorkflow.ontology_version||state.conversationOntologyVersion||"")}
 if(Number.isFinite(Number(turnValue))&&Number(turnValue)>=0)state.turnIndex=Number(turnValue);
 if(windowValue){state.contextWindow=windowValue.slice();state.contextWindowSize=windowValue.length}else{const size=response&&response.context_window_size!=null?response.context_window_size:envelope.context_window_size;if(Number.isFinite(Number(size))&&Number(size)>=0)state.contextWindowSize=Number(size)}
 state.contextPending=false;persistConversation();updateConversationStatus()
}

function introMarkup(){
 const copy=state.online?(state.conversationId?"已恢复当前申请的后端对话标识。可以继续追问；历史消息不会由前端伪造或重复回放。":"当前已连接 MVP 后端。可以直接连续对话；目标明确时才会规划和调用 Action，需要补充信息时只会追问。") : "尚未连接 MVP 后端。请先检查连接配置；离线时不会生成或展示任何伪造业务结果。";
 return '<article class="message agent-message intro-message"><span class="avatar">'+icon("spark")+'</span><div class="message-body"><div class="message-meta"><b>信审 Agent</b><span>现在</span></div><div class="bubble"><p>'+esc(copy)+'</p><div class="ability-row"><span>'+icon("chat")+'多轮记忆</span><span>'+icon("search")+'Query 拆解</span><span>'+icon("run")+'后端执行</span><span>'+icon("shield")+'权限门禁</span></div></div></div></article>'
}
function newTask(announce,options){if(state.busy){toast("当前 Agent 请求仍在执行");return}if(!(options&&options.preserveConversation))resetConversationContext();showView("chat");$("#chat").innerHTML=introMarkup();$("#agent-input").value="";state.lastAgentResponse=null;if(announce!==false)toast("已创建新的后端 Agent 对话")}
function refreshIdleIntro(){const chat=$("#chat");if(chat&&chat.children.length===1&&chat.firstElementChild.classList.contains("intro-message"))chat.innerHTML=introMarkup()}
function addUser(text){const article=document.createElement("article");article.className="message user-message";article.innerHTML='<div class="message-body"><div class="message-meta"><span>'+esc(nowTime())+'</span><b>我</b></div><div class="bubble"><p>'+esc(text).replace(/\n/g,"<br>")+'</p></div></div>';$("#chat").appendChild(article);scrollChat()}
function addTyping(){const article=document.createElement("article");article.className="message agent-message typing-message";article.innerHTML='<span class="avatar">'+icon("spark")+'</span><div class="typing"><i></i><i></i><i></i></div>';$("#chat").appendChild(article);scrollChat();return article}
function addAgentNotice(title,message,type){const article=document.createElement("article");article.className="message agent-message";article.innerHTML='<span class="avatar">'+icon(type==="error"?"shield":"spark")+'</span><div class="message-body"><div class="message-meta"><b>'+esc(title)+'</b><span>'+esc(nowTime())+'</span></div><div class="bubble notice-bubble '+esc(type||"")+'"><p>'+esc(message)+'</p></div></div>';$("#chat").appendChild(article);scrollChat();return article}
function validateGoalInput(raw){
 const text=String(raw==null?"":raw).trim(),compact=text.replace(/[\s\u200B-\u200D\uFEFF]/g,"");
 if(!compact)return{valid:false,text:"",message:"请描述希望查询或完成的信审任务。"};
 const semantic=compact.replace(/[^\p{L}\p{N}]/gu,"");
 if(!semantic)return{valid:false,text,message:"这条消息只有标点或符号，暂时无法判断信审目标。"};
 if(/^\p{N}+$/u.test(semantic))return{valid:false,text,message:"仅有数字不足以判断信审目标，请补充希望查询或完成的事项。"};
 if(semantic.length<2||/^(.)(?:\1)+$/u.test(semantic))return{valid:false,text,message:"这条消息信息不足，请补充一个明确的信审目标。"};
 return{valid:true,text,message:""}
}
function clarificationExamplesMarkup(){return '<div class="clarification-prompts"><span>可以这样问</span><div>'+CLARIFICATION_EXAMPLES.map(item=>'<button type="button" data-prompt="'+esc(item.prompt)+'">'+esc(item.label)+'</button>').join("")+'</div></div>'}
function addClarificationNotice(title,message,meta){
 const article=document.createElement("article");article.className="message agent-message clarification-message";if(meta&&meta.runId)article.dataset.runId=meta.runId;
 article.innerHTML='<span class="avatar">'+icon("spark")+'</span><div class="message-body"><div class="message-meta"><b>'+esc(title)+'</b><span>'+esc(meta&&meta.duration?"未调用 Action · "+meta.duration:nowTime())+'</span></div><div class="bubble notice-bubble clarification-bubble"><p>'+esc(message)+'</p><div class="clarification-state"><i></i>未调用任何 Action</div>'+clarificationExamplesMarkup()+'</div></div>';
 $("#chat").appendChild(article);scrollChat();return article
}
function addEvent(title,detail,status){const li=document.createElement("li");li.innerHTML='<i class="'+esc(status||"done")+'"></i><span><b>'+esc(title)+'</b><small>'+esc(nowTime()+(detail?" · "+detail:""))+'</small></span>';$("#event-log").prepend(li);while($("#event-log").children.length>6)$("#event-log").lastElementChild.remove()}

function setConnectionState(status,message){
 state.connecting=status==="connecting";state.online=status==="online";
 const label=status==="online"?"已连接":status==="connecting"?"连接中":"离线";
 $("#connection-label").textContent=label;$("#connection-button").dataset.status=status;$("#backend-notice").dataset.status=status;
 $("#backend-badge").textContent=status==="online"?"MVP CONNECTED":status==="connecting"?"CONNECTING":"BACKEND OFFLINE";
 $("#backend-title").textContent=status==="online"?"已连接现有 MVP 后端":status==="connecting"?"正在连接 MVP 后端":"MVP 后端未连接";
 $("#backend-copy").textContent=message||"";$("#agent-status").innerHTML="<i></i>"+(status==="online"?"READY":status==="connecting"?"CONNECTING":"OFFLINE");$("#agent-status").dataset.status=status;updateConversationStatus();
}

function workflowStatus(item){if(!item)return "—";if(item.status==="FAILED"||Number(item.failed_steps)>0)return "FAILED";if(item.status==="COMPLETED")return "COMPLETED";if(item.status==="CANCELLED")return "CANCELLED";if(Number(item.ready_steps)>0)return "READY";return item.status||"WAITING_INPUT"}
function statusLabel(value){return STATUS_LABELS[value]||value||"—"}
function buildActionCatalog(){
 const catalog={};
 state.agentTools.forEach(tool=>{catalog[tool.name]={name:tool.name,label:tool.label||tool.name,type:"READ",source:"MVP Agent 白名单",description:tool.description||"后端只读 Agent 工具",input:JSON.stringify({application_id:"string",ontology_version:"string"},null,2),output:tool.output||"object",requiredEvidence:tool.required_evidence||[],suggestedWrite:tool.suggested_write_action||null}});
 ((state.definition&&state.definition.actions)||[]).forEach(action=>{catalog[action.action_name]={name:action.action_name,label:action.label||action.action_name,type:"WRITE",source:"MVP 业务工作流",description:"工作流第 "+action.position+" 步；执行会改变业务状态，必须人工确认。",input:JSON.stringify({fields:action.request_fields||[],dependencies:action.dependencies||[]},null,2),output:"workflow_detail",requestFields:action.request_fields||[],optional:!!action.optional}});
 state.actionCatalog=catalog;renderCatalog();renderInspectorCatalog()
}
function renderInspectorCatalog(){
 const entries=Object.values(state.actionCatalog),reads=entries.filter(item=>item.type==="READ"),writes=entries.filter(item=>item.type==="WRITE");
 $("#action-count").textContent=String(entries.length);$("#catalog-count").textContent=String(entries.length);
 const group=(label,items,write)=>'<div class="catalog-group '+(write?'write-group':'')+'"><label>'+esc(label)+'</label>'+items.map(item=>'<button type="button" data-action="'+esc(item.name)+'"><i class="'+(write?'write':'read')+'">'+icon(write?'lock':'search')+'</i><span><b>'+esc(item.label)+'</b><code>'+esc(item.name)+'</code></span><em>'+(write?'WRITE':'READ')+'</em></button>').join("")+'</div>';
 $("#inspector-catalog").innerHTML=entries.length?group("只读 Agent 工具 · 后端执行",reads,false)+group("业务工作流 · 人工确认",writes,true):'<div class="sidebar-empty">后端未返回可用能力</div>'
}
function renderCases(filter){
 const query=String(filter||"").trim().toLowerCase();
 const rows=state.workflows.filter(item=>!query||String(item.application_id).toLowerCase().includes(query)||String(item.status).toLowerCase().includes(query));
 $("#case-options").innerHTML=rows.length?rows.map(item=>{const status=workflowStatus(item);return '<button class="case-option '+(state.currentWorkflow&&item.workflow_id===state.currentWorkflow.workflow_id?'selected':'')+'" type="button" data-case-id="'+esc(item.workflow_id)+'"><span>信</span><span><b>'+esc(item.application_id)+'</b><small>本体 '+esc(item.ontology_version||"—")+' · '+esc(statusLabel(status))+'</small></span><em class="'+(status==="FAILED"?'high':status==="COMPLETED"?'low':'medium')+'">'+esc(statusLabel(status))+'</em>'+icon("chevron")+'</button>'}).join(""):'<div class="empty-row">没有匹配的真实申请</div>'
}
function renderContext(){
 const item=state.currentWorkflow,readiness=state.readiness,detail=state.workflowDetail;
 if(!item){$("#case-context b").textContent=state.online?"后端暂无申请":"未连接";$("#context-app-id").textContent="—";$("#context-applicant").textContent="—";$("#context-ontology").textContent=state.definition&&state.definition.ontology_version||"—";$("#context-evidence").textContent="—";return}
 $("#case-context b").textContent=item.application_id+" · "+statusLabel(workflowStatus(item));$("#context-app-id").textContent=item.application_id;$("#context-applicant").textContent=statusLabel((detail&&detail.workflow&&detail.workflow.status)||item.status);$("#context-ontology").textContent=item.ontology_version||"—";
 if(readiness&&readiness.evidence){const total=Object.keys(readiness.evidence).length,ready=Object.values(readiness.evidence).filter(value=>value&&value.available).length;$("#context-evidence").textContent=ready+" / "+total+" 就绪"}else $("#context-evidence").textContent=readiness&&readiness.snapshot_error?"快照不可用":"读取失败";
 renderCases($("#case-search").value)
}
async function loadWorkflowContext(workflowId,options){
 const item=state.workflows.find(row=>row.workflow_id===workflowId);if(!item)return;
 if(options&&options.resetConversation)resetConversationContext();else ensureConversationApplication(item.application_id,item.ontology_version);
 state.currentWorkflow=item;state.workflowDetail=null;state.readiness=null;renderContext();
 try{
  state.workflowDetail=await api("/workflows/personal-credit/"+encodeURIComponent(workflowId));
  try{state.readiness=await api("/agents/personal-credit/"+encodeURIComponent(item.application_id)+"/readiness?ontology_version="+encodeURIComponent(item.ontology_version||""))}catch(error){state.readiness={status:"BLOCKED",snapshot_error:error.message,evidence:{},available_tools:[],missing_evidence:[]}}
  renderContext();addEvent("已加载申请上下文",item.application_id,"done");
  if(options&&options.resetConversation)newTask(false,{preserveConversation:true});
 }catch(error){toast(error.message,"error");addEvent("申请上下文加载失败",error.message,"wait")}
}

async function connectBackend(silent,overrideConfig){
 if(state.connecting)return false;state.authenticated=false;state.authMode="anonymous";state.authError=null;state.sessionExpiresAt=null;setConnectionState("connecting","建立本机安全会话并读取后端能力…");
 const config=overrideConfig||connectionConfig();
 try{
  await requestWith(config,"/health");
  let authError=null,authResult;try{authResult=await establishAuthentication(config)}catch(error){if(config.token&&error.status===401){authError=error;authResult={authenticated:false,mode:"api_key_failed",expiresAt:null,error:"MVP X-API-Key 无效"}}else throw error}applyAuthentication(authResult);
  const values=await Promise.all([
   requestWith(config,"/workflows/personal-credit/summary"),requestWith(config,"/workflows/personal-credit/definition"),requestWith(config,"/workflows/personal-credit?limit=100"),requestWith(config,"/agents/personal-credit/tools")
  ]);
  state.summary=values[0];state.definition=values[1];state.workflows=(values[2]&&values[2].workflows)||[];state.agentTools=(values[3]&&values[3].tools)||[];state.executionBoundary=values[3]&&values[3].execution_boundary||null;
  const localSession=state.authenticated&&state.authMode==="local_session",accessCopy=localSession?"本机安全会话":state.authenticated?"API Key 已验证":config.local?"本机会话不可用，仅只读":config.token?"令牌无效，仅只读":"匿名只读";setConnectionState("online",state.workflows.length+" 笔工作流 · "+state.agentTools.length+" 个 Agent 工具 · "+accessCopy);$("#connection-label").textContent=localSession?"本机会话":state.authenticated?"API Key":"只读";await loadModels(true);refreshIdleIntro();buildActionCatalog();renderCases("");renderRuns();
  const selected=state.currentWorkflow&&state.workflows.find(item=>item.workflow_id===state.currentWorkflow.workflow_id)||state.conversationApplicationId&&state.workflows.find(item=>item.application_id===state.conversationApplicationId&&String(item.ontology_version||"")===String(state.conversationOntologyVersion||""))||state.workflows[0];
  if(selected)await loadWorkflowContext(selected.workflow_id,{resetConversation:false});else renderContext();
  addEvent(localSession?"本机安全会话已建立":state.authenticated?"MVP API Key 验证成功":"MVP 后端只读连接成功",config.base,state.authenticated?"done":"wait");if(authError||state.authError){const message=state.authError||"MVP X-API-Key 无效，当前为只读模式";addEvent("MVP 受保护能力不可用",message,"wait");if(!silent)toast(message,"error")}else if(!silent)toast(localSession?"本机安全会话已自动建立":state.authenticated?"MVP 后端连接成功":"MVP 后端只读连接成功");return true;
 }catch(error){
  state.authenticated=false;state.authMode="anonymous";state.authError=error.message;setConnectionState("offline",error.message);state.models=[];state.activeModelId=null;state.modelsLoaded=false;state.modelAccessError="MVP 后端未连接";updateModel();renderModels();refreshIdleIntro();state.summary=null;state.definition=null;state.workflows=[];state.agentTools=[];state.actionCatalog={};state.currentWorkflow=null;state.workflowDetail=null;state.readiness=null;renderContext();renderCases("");renderCatalog();renderInspectorCatalog();addEvent("MVP 后端连接失败",error.message,"wait");if(!silent)toast(error.message,"error");return false
 }
}

const MODEL_PROVIDER_LABELS={openai_compatible:"OpenAI Compatible",deepseek:"DeepSeek",aliyun_bailian:"阿里云百炼",azure_openai:"Azure OpenAI",custom:"自定义服务"};
function modelStatusKey(model){if(model&&model.is_active)return"active";const value=String(model&&model.status||"unverified").toLowerCase();if(["testing","verifying","pending"].includes(value))return"testing";if(["verified","available","ready","success"].includes(value))return"verified";if(["failed","error","invalid"].includes(value))return"failed";return"draft"}
function modelStatusLabel(model){return{active:"启用",testing:"验证中",verified:"可用",failed:"失败",draft:"草稿"}[modelStatusKey(model)]}
function modelIsVerified(model){return!!(model&&model.is_active)||modelStatusKey(model)==="verified"}
function normalizeModelItem(item,activeId){
 const source=item&&typeof item==="object"?item:{},id=String(source.id||source.config_id||source.model_config_id||"");
 return{id,name:String(source.name||source.display_name||source.label||source.model||"未命名模型"),provider:String(source.provider||"openai_compatible"),base_url:normalizeBase(source.base_url||source.base||source.endpoint),model:String(source.model||source.model_name||""),temperature:Number.isFinite(Number(source.temperature))?Number(source.temperature):0.1,api_version:String(source.api_version||""),version:Number.isInteger(Number(source.version))?Number(source.version):null,status:String(source.verification_status||source.status||"unverified"),is_active:!!(source.is_active||source.active||activeId&&String(activeId)===id),api_key_masked:String(source.api_key_masked||source.masked_api_key||source.api_key_hint||source.api_key_configured&&"••••••••"||"未配置"),api_key_configured:!!(source.api_key_configured||source.api_key_masked||source.masked_api_key||source.api_key_hint),last_tested_at:source.last_tested_at||source.last_test_at||source.verified_at||null,last_error:String(source.last_error||source.error||"")}
}
function modelFromResponse(payload){const item=payload&&typeof payload==="object"&&(payload.item||payload.config||payload.model_config||payload);return item&&typeof item==="object"?normalizeModelItem(item,state.activeModelId):null}
function activeModel(){return state.models.find(item=>item.is_active)||state.models.find(item=>String(item.id)===String(state.activeModelId||""))||null}
function selectedModel(){return state.models.find(item=>String(item.id)===String(state.selectedModelId||""))||null}
function providerLabel(value){return MODEL_PROVIDER_LABELS[value]||String(value||"OpenAI Compatible")}
function providerPreset(id){const found=state.modelProviders.find(item=>String(item&&item.id||item&&item.provider||"")===String(id||""));if(found)return found;const defaults={openai_compatible:"https://api.openai.com/v1",deepseek:"https://api.deepseek.com",aliyun_bailian:"https://dashscope.aliyuncs.com/compatible-mode/v1",azure_openai:"",custom:""};return{id,label:providerLabel(id),default_base_url:defaults[id]||"",base_url_editable:id!=="deepseek"}}
function renderProviderOptions(selected){const catalog=state.modelProviders.length?state.modelProviders:Object.keys(MODEL_PROVIDER_LABELS).map(id=>providerPreset(id)),select=$("#model-provider");select.innerHTML=catalog.map(item=>{const id=String(item.id||item.provider||""),label=String(item.label||providerLabel(id));return'<option value="'+esc(id)+'">'+esc(label)+'</option>'}).join("");select.value=catalog.some(item=>String(item.id||item.provider||"")===String(selected||""))?String(selected):"openai_compatible"}
function applyProviderPreset(replaceBase){const id=$("#model-provider").value,preset=providerPreset(id),base=$("#model-base-url"),editable=preset.base_url_editable!==false;if(replaceBase||!base.value.trim())base.value=preset.default_base_url||"";base.readOnly=!editable;base.title=editable?"":"该提供方使用固定服务端点";$("#model-base-hint").textContent=editable?(preset.requires_admin_allowlist?"需位于后端允许列表":"提供方预设，可按需调整"):"提供方固定端点";const azure=id==="azure_openai";$("#model-api-version-field").hidden=!azure;if(azure&&!$("#model-api-version").value)$("#model-api-version").value="2024-10-21"}
function updateModel(){
 const model=activeModel(),unknown=!state.authenticated;
 if($("#planner-model-name"))$("#planner-model-name").textContent=unknown?(connectionConfig().local?"等待本机会话":"模型状态需鉴权"):model?model.model:"后端规则规划";
 if($("#planner-model-source"))$("#planner-model-source").textContent=unknown?(connectionConfig().local?"连接后自动同步，无需填写令牌":"完成 MVP 鉴权后同步"):model?providerLabel(model.provider)+" · 后端统一配置":"MVP 确定性 Planner · 无启用模型";
 if($("#planner-mode"))$("#planner-mode").textContent=unknown?"LOCKED":model?"LLM":"BACKEND";
 if($("#model-count-badge"))$("#model-count-badge").textContent=unknown?"—":String(Math.min(state.models.length,99));
}
function setModelFormDisabled(disabled){$$('#model-management-form input,#model-management-form select').forEach(node=>node.disabled=disabled);for(const id of["model-new","model-refresh","model-save-draft","model-save-and-test","model-test","model-activate","model-delete"]){const node=$("#"+id);if(node)node.disabled=disabled||state.modelActionBusy}}
function modelCardMarkup(model){const status=modelStatusKey(model),host=(()=>{try{return new URL(model.base_url).host}catch(_error){return model.base_url||"未配置地址"}})();return '<button class="model-list-item '+(String(model.id)===String(state.selectedModelId||"")?'selected ':'')+(model.is_active?'active':'')+'" type="button" data-model-id="'+esc(model.id)+'"><span class="model-provider-mark">'+esc(providerLabel(model.provider).slice(0,2).toUpperCase())+'</span><span class="model-list-main"><span><b>'+esc(model.name)+'</b><em class="model-status-pill '+esc(status)+'">'+esc(modelStatusLabel(model))+'</em></span><code>'+esc(model.model||"未填写模型")+'</code><small>'+esc(host)+' · Key '+esc(model.api_key_masked)+'</small></span>'+icon("chevron")+'</button>'}
function renderModelEditor(){
 const model=selectedModel(),locked=!state.authenticated||state.modelLoading;
 if(!model&&state.selectedModelId)state.selectedModelId=null;
 const current=selectedModel();
 if($("#model-editor-title"))$("#model-editor-title").textContent=current?"编辑 "+current.name:"添加模型";
 if($("#model-editor-copy"))$("#model-editor-copy").textContent=current?(current.is_active?"当前 Agent 正在使用此模型；请先停用或切换后再删除。":"API Key 留空将保留后端已保存的密钥。"):'保存后为草稿，连接测试通过后才可启用。';
 const pill=$("#model-editor-status");if(pill){const status=current?modelStatusKey(current):"draft";pill.className="model-status-pill "+status;pill.textContent=current?modelStatusLabel(current):"草稿"}
 const testButton=$("#model-test"),activateButton=$("#model-activate"),deleteButton=$("#model-delete"),saveDraftButton=$("#model-save-draft"),saveTestButton=$("#model-save-and-test");
 if(testButton)testButton.disabled=locked||state.modelActionBusy||!current;
 if(activateButton){activateButton.disabled=locked||state.modelActionBusy||!current||!current.is_active&&!modelIsVerified(current);activateButton.textContent=current&&current.is_active?"停用并改用规则":"设为启用";activateButton.classList.toggle("deactivate",!!(current&&current.is_active))}
 if(deleteButton){deleteButton.disabled=locked||state.modelActionBusy||!current||!!current.is_active;deleteButton.title=current&&current.is_active?"请先停用或启用其他模型":"删除后端模型配置"}
 if(saveDraftButton)saveDraftButton.disabled=locked||state.modelActionBusy;if(saveTestButton){saveTestButton.disabled=locked||state.modelActionBusy;saveTestButton.textContent=current?"保存修改并测试":"保存并测试"}
}
function populateModelForm(model){
 const current=model||{};$("#model-config-id").value=current.id||"";$("#model-display-name").value=current.name||"";renderProviderOptions(current.provider||"openai_compatible");$("#model-base-url").value=current.base_url||"";$("#model-model-name").value=current.model||"";$("#model-api-key").value="";$("#model-api-key").placeholder=current.api_key_configured?"已由后端保存（留空即保留）":"新建配置时必填";$("#model-api-version").value=current.api_version||"";$("#model-temperature").value=current.temperature==null?0.1:current.temperature;$("#model-temp-value").textContent=$("#model-temperature").value;applyProviderPreset(!model);if($("#model-management-result"))$("#model-management-result").hidden=true;renderModelEditor()
}
function selectModel(id){state.selectedModelId=String(id||"");const model=selectedModel();renderModels();populateModelForm(model)}
function beginNewModel(){state.selectedModelId=null;populateModelForm(null);renderModels();$("#model-display-name").focus()}
function renderModels(){
 const gate=$("#model-auth-gate"),grid=$("#model-management-grid");if(!gate||!grid)return;
 const gated=!state.authenticated;gate.hidden=!gated;grid.hidden=gated;
 const local=connectionConfig().local;if($("#model-auth-kicker"))$("#model-auth-kicker").textContent=local?"LOCAL SESSION REQUIRED":"AUTHENTICATION REQUIRED";if($("#model-auth-title"))$("#model-auth-title").textContent=local?"本机安全会话尚未建立":"需要 MVP 后端鉴权";if($("#model-auth-copy"))$("#model-auth-copy").textContent=local?(state.authError||"工作台会在本机自动建立安全会话，不需要填写 X-API-Key。点击重新连接即可恢复模型管理。 "):"远程模型管理需要有效的 MVP X-API-Key。模型密钥仍仅提交到后端，不会保存在浏览器中。";if($("#model-auth-action"))$("#model-auth-action").textContent=local?"重新建立本机会话":"配置远程 API Key";
 if(!gated){const list=$("#model-list");if(state.modelLoading)list.innerHTML='<div class="model-empty loading"><span class="model-empty-icon">'+icon("refresh")+'</span><b>正在同步后端配置</b><p>读取模型及验证状态…</p></div>';else if(state.modelAccessError)list.innerHTML='<div class="model-empty failed"><span class="model-empty-icon">'+icon("shield")+'</span><b>模型配置读取失败</b><p>'+esc(state.modelAccessError)+'</p></div>';else list.innerHTML=state.models.length?state.models.map(modelCardMarkup).join(""):'<div class="model-empty"><span class="model-empty-icon">'+icon("spark")+'</span><b>尚未配置大模型</b><p>Agent 将使用后端确定性 Planner；可从右侧添加第一个模型。</p></div>'}
 const verified=state.models.filter(modelIsVerified).length,failed=state.models.filter(item=>modelStatusKey(item)==="failed").length,active=activeModel();
 if($("#model-metric-total"))$("#model-metric-total").textContent=gated?"—":String(state.models.length);if($("#model-metric-verified"))$("#model-metric-verified").textContent=gated?"—":String(verified);if($("#model-metric-failed"))$("#model-metric-failed").textContent=gated?"—":String(failed);if($("#model-metric-active"))$("#model-metric-active").textContent=gated?(local?"待连接":"需鉴权"):active?active.model:"规则";if($("#model-active-copy"))$("#model-active-copy").textContent=gated?(local?"本机会话连接后自动同步":"鉴权后从 MVP 后端同步"):active?active.name+" · "+providerLabel(active.provider):"无启用模型时使用确定性 Planner";
 updateModel();setModelFormDisabled(gated||state.modelLoading);renderModelEditor()
}
async function loadModels(silent){
 if(!state.authenticated){state.models=[];state.activeModelId=null;state.modelsLoaded=false;state.modelAccessError=connectionConfig().local?"本机安全会话尚未建立":"需要 MVP X-API-Key 鉴权";renderModels();updateModel();return false}
 state.modelLoading=true;state.modelAccessError=null;renderModels();
 try{const payload=await api("/llm/configs"),items=Array.isArray(payload)?payload:Array.isArray(payload&&payload.items)?payload.items:Array.isArray(payload&&payload.configs)?payload.configs:[];state.activeModelId=payload&&payload.active_config_id||null;state.modelProviders=Array.isArray(payload&&payload.providers)?payload.providers:[];state.models=items.map(item=>normalizeModelItem(item,state.activeModelId)).filter(item=>item.id);state.modelsLoaded=true;if(!state.models.some(item=>String(item.id)===String(state.selectedModelId||"")))state.selectedModelId=state.models[0]&&state.models[0].id||null;state.modelLoading=false;renderModels();populateModelForm(selectedModel());return true}catch(error){state.modelLoading=false;state.modelsLoaded=false;state.modelAccessError=error.status===401?backendAuthGuidance():error.message;if(error.status===401){state.authenticated=false;state.authMode=connectionConfig().local?"local_session_failed":"anonymous";state.authError=state.modelAccessError}renderModels();updateModel();if(!silent)toast(state.modelAccessError,"error");return false}
}
function modelValues(){
 const editing=!!$("#model-config-id").value,current=selectedModel(),value={name:$("#model-display-name").value.trim(),provider:$("#model-provider").value,base_url:normalizeBase($("#model-base-url").value),model:$("#model-model-name").value.trim(),api_key:$("#model-api-key").value.trim(),temperature:Number($("#model-temperature").value),api_version:$("#model-provider").value==="azure_openai"?$("#model-api-version").value.trim()||"2024-10-21":null};
 if(editing){if(!current||!Number.isInteger(current.version)||current.version<1)return{error:"配置版本缺失，请刷新后重新编辑"};value.version=current.version}if(!value.name||!value.base_url||!value.model||!editing&&!value.api_key)return{error:editing?"请完整填写配置名称、Base URL 和模型名称":"新建配置必须填写名称、Base URL、模型名称和 API Key"};try{const url=new URL(value.base_url),local=["localhost","127.0.0.1","::1"].includes(url.hostname);if(url.protocol!=="https:"&&!(url.protocol==="http:"&&local))return{error:"模型地址必须使用 HTTPS；仅本机地址可填写 HTTP"}}catch(_error){return{error:"模型 Base URL 格式无效"}}if(editing&&!value.api_key)delete value.api_key;return value
}
function requireModelAccess(){if(state.authenticated)return true;showView("models");renderModels();toast(backendAuthGuidance(),"error");return false}
async function saveModelConfig(testAfterSave){
 if(state.modelActionBusy||!requireModelAccess())return;const value=modelValues();if(value.error)return setResult("#model-management-result",value.error,"error");const id=$("#model-config-id").value,path=id?"/llm/configs/"+encodeURIComponent(id):"/llm/configs",method=id?"PUT":"POST",requestBody=JSON.stringify(value);$("#model-api-key").value="";if(value.api_key)value.api_key="";state.modelActionBusy=true;renderModelEditor();setResult("#model-management-result",testAfterSave?"正在保存到后端并执行连接测试…":id?"正在保存修改；连接字段变化后将恢复为草稿状态…":"正在将草稿保存到 MVP 后端…","success");try{const response=await api(path,{method,body:requestBody}),saved=modelFromResponse(response),savedId=saved&&saved.id||id;if(!savedId)throw new ApiError("后端未返回模型配置 ID",0,response);state.selectedModelId=savedId;if(testAfterSave){const optimistic=saved||state.models.find(item=>String(item.id)===String(savedId));if(optimistic){optimistic.status="testing";optimistic.is_active=false;const index=state.models.findIndex(item=>String(item.id)===String(savedId));if(index>=0)state.models[index]=optimistic;else state.models.unshift(optimistic);renderModels();setResult("#model-management-result","配置已保存，MVP 后端正在验证模型连接…","success")}try{await api("/llm/configs/"+encodeURIComponent(savedId)+"/test",{method:"POST"})}catch(error){await loadModels(true);setResult("#model-management-result","配置已保存，但连接测试失败："+error.message,"error");toast("配置已保存，模型验证失败","error");return}await loadModels(true);setResult("#model-management-result","配置已保存且连接测试通过，现在可以设为启用模型。","success");toast("模型配置已保存并验证通过")}else{await loadModels(true);setResult("#model-management-result","已保存到后端。保存不代表可用，请执行连接测试。","success");toast("模型配置已保存为草稿")}}catch(error){if(error.status===409){await loadModels(true);setResult("#model-management-result","配置已被其他操作更新，列表已刷新；请核对最新版本后重新编辑。","error")}else setResult("#model-management-result",error.message,"error")}finally{$("#model-api-key").value="";state.modelActionBusy=false;renderModels()}
}
async function testModelConfig(){
 const model=selectedModel();if(state.modelActionBusy||!model||!requireModelAccess())return;const previousStatus=model.status;state.modelActionBusy=true;model.status="testing";renderModels();setResult("#model-management-result","MVP 后端正在验证模型连接与响应格式…","success");try{await api("/llm/configs/"+encodeURIComponent(model.id)+"/test",{method:"POST"});await loadModels(true);setResult("#model-management-result","连接测试通过，模型现在可启用。","success");toast("模型连接测试通过")}catch(error){const refreshed=await loadModels(true);if(!refreshed)model.status=previousStatus;setResult("#model-management-result",error.message,"error");toast("模型连接测试失败","error")}finally{$("#model-api-key").value="";state.modelActionBusy=false;renderModels()}
}
async function activateModelConfig(){
 const model=selectedModel();if(state.modelActionBusy||!model||!requireModelAccess())return;if(!model.is_active&&!modelIsVerified(model)){setResult("#model-management-result","模型尚未通过连接测试，不能启用。","error");return}const deactivating=!!model.is_active,operation=deactivating?"deactivate":"activate";state.modelActionBusy=true;renderModelEditor();setResult("#model-management-result",deactivating?"正在停用模型并切换到后端规则规划…":"正在切换后端启用模型…","success");try{await api("/llm/configs/"+encodeURIComponent(model.id)+"/"+operation,{method:"POST"});state.activeModelId=deactivating?null:model.id;await loadModels(true);setResult("#model-management-result",deactivating?"模型已停用，后续 Agent 对话将使用确定性 Planner。":"已设为启用模型，后续 Agent 对话将自动使用该配置。","success");toast(deactivating?"已改用后端规则规划":"Agent 已切换到 "+model.model)}catch(error){setResult("#model-management-result",error.message,"error")}finally{state.modelActionBusy=false;renderModels()}
}
async function deleteModelConfig(){
 const model=selectedModel();if(state.modelActionBusy||!model||!requireModelAccess())return;if(model.is_active){setResult("#model-management-result","当前启用模型不能直接删除，请先停用或切换其他模型。","error");return}if(!Number.isInteger(model.version)||model.version<1){await loadModels(true);setResult("#model-management-result","配置版本缺失，列表已刷新；请重新选择后再删除。","error");return}if(!window.confirm("确定删除模型配置“"+model.name+"”吗？后端保存的密钥也会一并删除。"))return;state.modelActionBusy=true;renderModelEditor();try{await api("/llm/configs/"+encodeURIComponent(model.id)+"?version="+encodeURIComponent(model.version),{method:"DELETE"});state.selectedModelId=null;await loadModels(true);if(!state.selectedModelId)beginNewModel();toast("模型配置已从后端删除")}catch(error){if(error.status===409){await loadModels(true);setResult("#model-management-result","配置已被其他操作更新，列表已刷新；请重新确认后再删除。","error")}else setResult("#model-management-result",error.message,"error")}finally{state.modelActionBusy=false;renderModels()}
}

function isClarificationResponse(response){
 const plan=response&&response.plan||{},responseType=String(response&&response.response_type||"").toUpperCase(),statuses=[response&&response.status,plan.status,response&&response.execution_summary&&response.execution_summary.status].map(value=>String(value||"").toUpperCase()),explicitEmptyPlan=Array.isArray(plan.steps)&&plan.steps.length===0,explicitEmptyExecutions=Array.isArray(response&&response.executions)&&response.executions.length===0;
 if(["CHAT","ACTION_PLAN","ACTION_RUN"].includes(responseType))return false;
 return responseType==="NO_ACTION"||responseType==="NEEDS_CLARIFICATION"||statuses.includes("NEEDS_CLARIFICATION")||(explicitEmptyPlan&&explicitEmptyExecutions&&!(response&&response.executed))
}
function clarificationMessage(response){const plan=response&&response.plan||{},value=response&&response.answer||response&&response.message||plan.clarification_question||plan.reason;return String(value||"我还不能确定你希望完成哪项信审任务，请补充要查询、评估或生成的内容。")}
function agentResponseType(response){
 const explicit=String(response&&response.response_type||"").trim().toUpperCase();
 if(["CHAT","NO_ACTION","NEEDS_CLARIFICATION","ACTION_PLAN","ACTION_RUN"].includes(explicit))return explicit;
 if(isClarificationResponse(response))return "NEEDS_CLARIFICATION";
 const plan=response&&response.plan||{},hasDeclared=Array.isArray(plan.steps)?plan.steps.length>0:!!plan.selected_tool,hasExecutions=Array.isArray(response&&response.executions)&&response.executions.length>0;
 return hasDeclared||hasExecutions?(response&&response.executed||hasExecutions?"ACTION_RUN":"ACTION_PLAN"):"CHAT"
}
function isActionResponse(response){return ["ACTION_PLAN","ACTION_RUN"].includes(agentResponseType(response))}
function conversationMessage(response){const plan=response&&response.plan||{},value=response&&response.answer||response&&response.message||response&&response.reply||plan.clarification_question||plan.reason;return String(value||"我已收到。请继续说明你希望了解或处理的信审事项。")}
function normalPlanSteps(response){
 if(isClarificationResponse(response))return[];
 const plan=response&&response.plan||{},executions=Array.isArray(response&&response.executions)?response.executions:[],executionMap=new Map(executions.map(item=>[item.tool||item.tool_name||item.action_name,item]));
 const declared=Array.isArray(plan.steps)&&plan.steps.length?plan.steps:(plan.selected_tool?[{tool:plan.selected_tool,label:plan.tool_label,reason:plan.reason,status:plan.status,executable:plan.executable,suggested_write_action:plan.suggested_write_action}]:executions);
 return declared.map((step,index)=>{const tool=step&&step.tool||step&&step.tool_name||step&&step.action_name,execution=executionMap.get(tool);if(!tool)return null;return{stepId:step.step_id||step.id||"step-"+(index+1),tool,label:step.label||step.tool_label||(state.actionCatalog[tool]&&state.actionCatalog[tool].label)||tool,reason:step.reason||step.summary||plan.reason||"后端规划器选择该工具",status:execution&&execution.status||step.status||(response.executed?"COMPLETED":step.executable||plan.executable?"READY":"BLOCKED"),result:execution&&execution.result,executable:step.executable!=null?!!step.executable:plan.executable!=null?!!plan.executable:true,suggestedWriteAction:step.suggested_write_action}}).filter(Boolean)
}
function traceStatusMeta(value){
 const status=String(value||"UNKNOWN").trim().toUpperCase();
 if(["COMPLETED","EXECUTED","SUCCEEDED","SUCCESS"].includes(status))return{status,label:"已完成",kind:"done",icon:"check"};
 if(["FAILED","ERROR","REJECTED"].includes(status))return{status,label:status==="REJECTED"?"结果被拒绝":"执行失败",kind:"failed",icon:"shield"};
 if(status==="BLOCKED")return{status,label:"证据不足",kind:"blocked",icon:"lock"};
 if(["FALLBACK","DEGRADED"].includes(status))return{status,label:"已降级",kind:"degraded",icon:"shield"};
 if(status==="READY")return{status,label:"已就绪",kind:"ready",icon:"check"};
 if(status==="PLANNED")return{status,label:"已规划",kind:"planned",icon:"nodes"};
 if(["RUNNING","IN_PROGRESS","PROCESSING"].includes(status))return{status,label:"执行中",kind:"running",icon:"run"};
 return{status,label:"后端未声明状态",kind:"unknown",icon:"nodes"}
}
function traceStageMeta(stage){
 const meta=traceStatusMeta(stage&&stage.status),phase=String(stage&&stage.phase||"").toLowerCase();
 if(phase==="fallback")return{...meta,label:meta.status==="COMPLETED"?"回退完成":"回答已回退",kind:"degraded",icon:"shield"};
 if(phase==="synthesis"&&meta.status==="REJECTED")return{...meta,label:"模型总结被拒绝",kind:"degraded",icon:"shield"};
 return meta
}
function backendTraceStages(response){
 const source=Array.isArray(response&&response.trace)?response.trace:Array.isArray(response&&response.execution_trace)?response.execution_trace:[];
 const labels={planner:"Query Planner",readiness:"案件就绪度检查",action:"只读 Action",synthesis:"结果总结",fallback:"确定性回退"};
 return source.map((item,index)=>{if(!item||typeof item!=="object")return null;const phase=String(item.stage||item.phase||item.kind||item.type||"").trim().toLowerCase(),tool=String(item.tool_name||item.tool||item.action_name||phase||"trace-"+(index+1)),origin=String(item.source||"").trim(),label=String(item.label||item.name||item.tool_label||item.action_label||(phase==="action"&&item.tool_name?item.tool_name:labels[phase])||phase||tool),reason=String(item.reason||item.message||item.summary||item.detail||(origin?"执行来源："+origin:"后端返回该阶段，但未提供说明。"));return{phase:phase||"trace",tool,label,reason,status:String(item.status||"UNKNOWN"),durationMs:item.duration_ms,source:origin}}).filter(Boolean)
}
function normalTraceStages(response){
 const authoritative=backendTraceStages(response),synthesis=response&&response.synthesis&&typeof response.synthesis==="object"?response.synthesis:null;
 if(authoritative.length){
  if(synthesis&&!authoritative.some(item=>/synth|总结|合成/i.test(item.phase+" "+item.tool+" "+item.label)))authoritative.push({phase:"synthesis",tool:"result_synthesis",label:"结果总结",reason:synthesis.reason_code?"后端总结状态："+synthesis.reason_code:"后端已返回结果总结状态。",status:String(synthesis.status||"UNKNOWN")});
  return authoritative
 }
 const plan=response&&response.plan&&typeof response.plan==="object"?response.plan:{},stages=[],plannerMode=String(plan.planner_mode||"").toLowerCase();
 if(Object.keys(plan).length)stages.push({phase:"planner",tool:plannerMode==="llm"?"llm_planner":"deterministic_planner",label:"Query Planner",reason:String(plan.reason||"后端已返回本轮规划结果。"),status:"COMPLETED"});
 const readiness=plan.readiness&&typeof plan.readiness==="object"?plan.readiness:response&&response.readiness&&typeof response.readiness==="object"?response.readiness:null;
 if(readiness){const readinessState=String(readiness.status||"UNKNOWN").toUpperCase(),missing=Array.isArray(readiness.missing_evidence)?readiness.missing_evidence:[];stages.push({phase:"readiness",tool:"inspect_case_readiness",label:"案件就绪度检查",reason:"后端就绪度："+readinessState+(missing.length?"；缺失 "+missing.join("、"):""),status:readinessState==="BLOCKED"?"BLOCKED":"COMPLETED"})}
 const executions=Array.isArray(response&&response.executions)?response.executions:[];
 if(executions.length){
  executions.forEach((item,index)=>{if(!item||typeof item!=="object")return;const tool=String(item.tool_name||item.tool||item.action_name||"action-"+(index+1)),planned=Array.isArray(plan.steps)?plan.steps.find(step=>String(step&&step.tool_name||step&&step.tool||step&&step.action_name||"")===tool):null;stages.push({phase:"action",tool,label:String(item.tool_label||item.label||planned&&planned.tool_label||tool),reason:String(item.answer||planned&&planned.reason||"后端未提供该 Action 的执行说明。"),status:String(item.status||"UNKNOWN")})})
 }else if(Array.isArray(plan.steps)){
  plan.steps.forEach((item,index)=>{if(!item||typeof item!=="object")return;const tool=String(item.tool_name||item.tool||item.action_name||"action-"+(index+1));stages.push({phase:"action",tool,label:String(item.tool_label||item.label||tool),reason:String(item.reason||"后端已规划该 Action，尚未返回执行记录。"),status:String(item.status||"PLANNED")})})
 }
 if(synthesis&&String(synthesis.status||"").toUpperCase()!=="NOT_REQUIRED")stages.push({phase:"synthesis",tool:"result_synthesis",label:"结果总结",reason:synthesis.reason_code?"后端总结状态："+synthesis.reason_code:String(synthesis.mode||"").includes("llm")?"外部大模型已总结 Action 结果。":"后端已生成确定性结果摘要。",status:String(synthesis.status||"UNKNOWN")});
 return stages
}
function planCard(response,run){
 const plan=response.plan||{},stages=normalTraceStages(response),summary=response.execution_summary||{},planner=plan.planner_mode==="llm"?"LLM":"RULE",actionStages=stages.filter(item=>item.phase==="action"),completed=actionStages.filter(item=>["COMPLETED","EXECUTED","SUCCEEDED","SUCCESS"].includes(String(item.status).toUpperCase())).length,degraded=stages.some(item=>traceStageMeta(item).kind==="degraded"),failed=stages.some(item=>traceStageMeta(item).kind==="failed"),blocked=stages.some(item=>traceStageMeta(item).kind==="blocked"),stateKind=degraded||failed||blocked?"waiting":completed?"success":"running",stateIcon=degraded||failed?"shield":blocked?"lock":completed?"check":"nodes",headline=degraded?"结果已返回，回答发生降级":summary.status==="PARTIAL"?"部分 Action 完成":completed?"后端 Action 已执行":blocked?"执行被证据阻塞":"后端已返回规划";
 return '<details class="task-card dynamic-plan execution-trace"><summary class="task-card-head"><div><span class="task-state '+stateKind+'">'+icon(stateIcon)+'</span><span><small>'+esc(headline)+'</small><b>查看真实执行过程</b></span></div><span class="trace-disclosure">'+esc(stages.length+' 个可证实阶段 · '+run.duration)+icon("chevron")+'</span></summary><div class="trace-body"><div class="plan-summary"><span><b>'+stages.length+'</b><small>真实阶段</small></span><span><b>'+esc(planner)+'</b><small>Planner</small></span><span><b>'+completed+'/'+actionStages.length+'</b><small>Action 完成</small></span><span><b>'+esc(run.duration)+'</b><small>真实耗时</small></span></div><div class="query-plan">'+(stages.length?stages.map((step,index)=>{const meta=traceStageMeta(step),duration=Number.isFinite(Number(step.durationMs))?" · "+Number(step.durationMs)+"ms":"",source=step.source?" · "+step.source:"";return '<div class="plan-step '+esc(meta.kind)+'"><span class="step-line"></span><i>'+icon(meta.icon)+'</i><div><header><b>'+String(index+1).padStart(2,"0")+' · '+esc(step.label)+'</b><em>'+esc(meta.label+duration)+'</em></header><code>'+esc(step.tool+source)+'</code><p>'+esc(step.reason)+'</p></div></div>'}).join(""):'<div class="trace-empty">后端未返回可展示的 trace、plan、executions 或 synthesis。</div>')+'</div><footer class="trace-footer"><code>'+esc(run.id)+'</code><span>阶段与状态均来自本轮后端响应</span></footer></div></details>'
}
function resultFacts(result){
 if(!result||typeof result!=="object")return [];
 const labels={risk_level:"风险等级",pd_value:"违约概率",check_result:"评估结论",recommended_decision:"建议结果",decision_boundary:"决策边界",question_count:"问题数量",template_version:"模板版本",status:"状态",snapshot_available:"数据快照",data_binding_version:"数据绑定版本"};
 return Object.keys(labels).filter(key=>result[key]!=null&&typeof result[key]!=="object").slice(0,6).map(key=>[labels[key],typeof result[key]==="boolean"?(result[key]?"可用":"不可用"):String(result[key])])
}
function suggestedWrites(response){if(response&&Array.isArray(response.proposed_actions))return Array.from(new Set(response.proposed_actions.map(item=>item&&item.action_name).filter(Boolean)));const plan=response&&response.plan||{},values=plan.suggested_write_actions||plan.suggested_write_action||[];return Array.from(new Set((Array.isArray(values)?values:[values]).filter(Boolean)))}
function writeSuggestionMarkup(response){
 const names=suggestedWrites(response);if(!names.length)return"";
 return names.map(name=>{const meta=state.actionCatalog[name]||{label:name},step=state.workflowDetail&&state.workflowDetail.steps&&state.workflowDetail.steps.find(item=>item.action_name===name),ready=step&&step.status==="READY",denied=state.safety.writePolicy==="deny";return '<div class="approval-box"><p><b>模型建议后续业务 Action：'+esc(meta.label)+'</b><br>本次 Agent 只读调用没有执行写入。'+(ready&&!denied?'当前工作流步骤已就绪，可在人工确认并补全字段后执行。':denied?'当前前端策略禁止写入。':'当前工作流尚未到达该步骤。')+'</p><div>'+(ready&&!denied?'<button class="approve" type="button" data-open-workflow-action="'+esc(name)+'">填写并确认执行</button>':'')+'<button class="reject" type="button" data-action="'+esc(name)+'">查看 Action 契约</button></div></div>'}).join("")
}
function actionAnswerState(response){
 const finalAnswer=typeof (response&&response.final_answer)==="string"?response.final_answer.trim():"",legacyAnswer=typeof (response&&response.answer)==="string"?response.answer.trim():"",answer=finalAnswer||legacyAnswer;
 return answer?{answer,missing:false}:{answer:"结果异常：后端已返回 Action 过程，但没有返回 final_answer 或 answer。请重新运行本轮任务；若持续出现，请检查结果总结状态。",missing:true}
}
function synthesisState(response){
 const synthesis=response&&response.synthesis&&typeof response.synthesis==="object"?response.synthesis:{},status=String(synthesis.status||"").trim().toUpperCase(),mode=String(synthesis.mode||"").trim().toLowerCase(),reasonCode=String(synthesis.reason_code||"").trim(),degraded=!!(response&&response.degraded)||!!synthesis.degraded||["FALLBACK","DEGRADED","FAILED","ERROR"].includes(status)||mode.includes("fallback");
 return{status,mode,reasonCode,degraded,label:degraded?(mode.includes("fallback")?"模型总结失败，当前结论来自后端确定性回退。":"本轮结果总结已降级。"):(mode==="llm"?"外部大模型总结":"后端结果总结")}
}
function answerCard(response){
 const executionResults=(response.executions||[]).filter(item=>item&&item.status==="EXECUTED"&&item.result).map(item=>item.result),result=response.result,facts=(executionResults.length?executionResults:[result]).filter(Boolean).flatMap(resultFacts).slice(0,6),summary=response.execution_summary||{},blocked=summary.status==="BLOCKED"||(!response.executed&&response.plan&&response.plan.status==="BLOCKED"),conclusion=actionAnswerState(response),synthesis=synthesisState(response),title=conclusion.missing?"结果字段缺失":blocked?"当前无法执行":"最终结论",badge=conclusion.missing?"响应异常":synthesis.degraded?"降级结果 · 非最终审批":"真实后端结果 · 非最终审批";
 return '<div class="answer-card result-first '+(conclusion.missing?'answer-missing ':'')+(synthesis.degraded?'answer-degraded':'')+'"><header><span>'+icon(conclusion.missing||synthesis.degraded||blocked?'shield':'file')+'<b>'+esc(title)+'</b></span><em>'+esc(badge)+'</em></header><p class="final-answer" '+(conclusion.missing?'role="alert"':'')+'>'+esc(conclusion.answer)+'</p>'+(synthesis.degraded?'<div class="synthesis-alert" role="status"><b>'+icon("shield")+'回答已降级</b><span>'+esc(synthesis.label+(synthesis.reasonCode?" 原因代码："+synthesis.reasonCode:""))+'</span></div>':'')+(facts.length?'<div class="answer-grid">'+facts.slice(0,3).map(item=>'<div><span>'+esc(item[0])+'</span><b>'+esc(item[1])+'</b></div>').join("")+'</div>':'')+'<footer><span>'+icon("database")+'来源：MVP 后端只读 Agent 工具</span><button type="button" data-evidence>'+icon("chevron")+'查看证据与原始摘要</button></footer></div>'+writeSuggestionMarkup(response)
}
function renderConversationResponse(response,duration){
 const type=agentResponseType(response),needsInput=type==="NEEDS_CLARIFICATION"||type==="NO_ACTION"||isClarificationResponse(response),quality=synthesisState(response),turn=Number(response&&response.turn_index||conversationEnvelope(response).turn_index||state.turnIndex||0),windowValue=Array.isArray(response&&response.context_window)?response.context_window:Array.isArray(conversationEnvelope(response).context_window)?conversationEnvelope(response).context_window:null,count=windowValue?windowValue.length:state.contextWindowSize,label=needsInput?"CLARIFY":type==="NO_ACTION"?"NO ACTION":"CHAT",article=document.createElement("article"),qualityNotice=quality.degraded?'<div class="conversation-quality-alert" role="status"><b>'+icon("shield")+'回答已降级</b><span>'+esc(quality.label+(quality.reasonCode?" 原因代码："+quality.reasonCode:""))+'</span></div>':"";
 article.className="message agent-message conversation-response"+(needsInput?" needs-input":"")+(quality.degraded?" degraded":"");article.dataset.responseType=type;
 article.innerHTML='<span class="avatar">'+icon(quality.degraded?"shield":"spark")+'</span><div class="message-body compact"><div class="message-meta"><b>信审 Agent</b><em>'+esc(quality.degraded?"DEGRADED":label)+'</em><span>'+esc(duration||nowTime())+'</span></div><div class="bubble conversation-reply-bubble"><p>'+esc(conversationMessage(response))+'</p>'+qualityNotice+'<div class="conversation-turn-state"><i></i><span>'+esc("未调用 Action"+(turn?" · 第 "+turn+" 轮":"")+(count?" · 记忆 "+count+" 轮":""))+'</span></div></div></div>';
 $("#chat").appendChild(article);scrollChat();return article
}
function renderAgentResponse(response,run){
 if(!isActionResponse(response))return renderConversationResponse(response,run&&run.duration);
 const article=document.createElement("article");article.className="message agent-message";article.dataset.runId=run.id;const summary=response.execution_summary||{};article.innerHTML='<span class="avatar">'+icon("spark")+'</span><div class="message-body wide"><div class="message-meta"><b>信审 Agent</b><span>'+esc(Number(summary.executed||0)>0?'真实执行完成':'后端规划完成')+' · 第 '+esc(state.turnIndex||response.turn_index||1)+' 轮 · '+esc(run.duration)+'</span></div><div class="run-result">'+answerCard(response)+planCard(response,run)+'<div class="message-tools"><button type="button" data-copy-result>'+icon("copy")+'复制结果</button><button type="button" data-rerun>'+icon("refresh")+'重新运行</button></div></div></div>';$("#chat").appendChild(article);scrollChat();return article
}
function saveRuns(){state.runs=state.runs.slice(0,30);writeSession(STORAGE.runs,state.runs);renderRuns();renderRecentRuns()}

function requestedMaxSteps(instruction){
 const text=String(instruction||"").trim().toLowerCase();
 if(/全量|完整|全部|综合|全面|全流程|端到端|逐步|所有环节|full\s*(review|assessment)|comprehensive/.test(text))return 4;
 const intents=[
  /风险|评估|征信|反欺诈|欺诈|负债|偿债|收入|车辆|risk|credit|fraud|dti/,
  /电核|核验|电话|问题|question|verify|verification/,
  /决策|信审建议|审批建议|建议|decision|review/,
  /就绪|缺少|材料|证据状态|案件状态|申请状态|可用能力|readiness/
 ];
 return Math.max(1,Math.min(4,intents.filter(pattern=>pattern.test(text)).length))
}
async function runGoal(rawText){
 if(state.busy)return;
 const input=rawText==null?$("#agent-input").value:rawText,validation=validateGoalInput(input),text=validation.text;
 showView("chat");
 if(!validation.valid){if(text)addUser(text);$("#agent-input").value="";$("#agent-input").style.height="auto";addClarificationNotice("请明确任务目标",validation.message);return}
 if(text.length>500){toast("后端 Agent 指令最多 500 个字符","error");return}if(!state.online){addAgentNotice("MVP 后端未连接","无法执行该目标。请先打开顶部“MVP Backend”配置并恢复连接。","error");return}if(!state.currentWorkflow){addAgentNotice("缺少申请上下文","后端当前没有可供 Agent 使用的个人信审工作流。","error");return}
 ensureConversationApplication(state.currentWorkflow.application_id,state.currentWorkflow.ontology_version);showView("chat");state.busy=true;state.lastPrompt=text;state.contextPending=true;updateConversationStatus();$("#send-button").disabled=true;addUser(text);$("#agent-input").value="";$("#agent-input").style.height="auto";const typing=addTyping(),started=performance.now(),requestContext={caseId:state.currentWorkflow.application_id,workflowId:state.currentWorkflow.workflow_id,ontologyVersion:state.currentWorkflow.ontology_version,time:new Date().toISOString()};addEvent("多轮消息已发送",requestContext.caseId,"running");
 try{
  const body={application_id:requestContext.caseId,instruction:text,ontology_version:requestContext.ontologyVersion,execute:true,max_steps:requestedMaxSteps(text)};if(state.conversationId&&state.conversationApplicationId===requestContext.caseId&&state.conversationOntologyVersion===String(requestContext.ontologyVersion||""))body.conversation_id=state.conversationId;
  const response=await api("/agents/personal-credit/run",{method:"POST",body:JSON.stringify(body)})||{},duration=((performance.now()-started)/1000).toFixed(2)+"s";updateConversationFromResponse(response);typing.remove();
  const steps=normalPlanSteps(response),actionResponse=isActionResponse(response);
  if(!actionResponse){renderConversationResponse(response,duration);addEvent(isClarificationResponse(response)?"Agent 请求补充信息":"Agent 完成自然对话","第 "+(state.turnIndex||"—")+" 轮 · 未调用 Action",isClarificationResponse(response)?"wait":"done")}
  else{
   const summaryStatus=String(response.execution_summary&&response.execution_summary.status||((response.executed)?"COMPLETED":agentResponseType(response)==="ACTION_PLAN"?"PLANNED":response.plan&&response.plan.status)||"").toUpperCase(),run={id:String(response.run_id||response.request_id||uid("RUN")),source:"mvp-backend",goal:text,caseId:requestContext.caseId,workflowId:requestContext.workflowId,ontologyVersion:requestContext.ontologyVersion,actions:steps.map(item=>item.tool),status:"completed",statusLabel:"已完成",duration,time:requestContext.time,response,conversationId:state.conversationId,turnIndex:state.turnIndex,responseType:agentResponseType(response)};
   run.status=summaryStatus==="BLOCKED"?"blocked":summaryStatus==="FAILED"?"failed":["PLANNED","READY","WAITING_CONFIRMATION"].includes(summaryStatus)||agentResponseType(response)==="ACTION_PLAN"?"waiting":"completed";run.statusLabel=summaryStatus==="PARTIAL"?"部分完成":run.status==="completed"?"已完成":run.status==="blocked"?"证据不足":run.status==="failed"?"执行失败":"待确认";state.runs.unshift(run);state.lastAgentResponse=response;renderAgentResponse(response,run);saveRuns();addEvent(run.status==="completed"?"Agent 真实 Action 完成":run.status==="blocked"?"Agent 被证据阻塞":run.status==="failed"?"Agent Action 失败":"Agent Action 已规划",run.actions.join(" → "),run.status==="completed"?"done":"wait")
  }
 }catch(error){typing.remove();state.contextPending=false;updateConversationStatus();const message=error.status===401?backendAuthGuidance():error.message;addAgentNotice(error.status===401?(connectionConfig().local?"本机安全会话已失效":"MVP 访问令牌无效"):"Agent 请求失败",message,"error");addEvent("Agent 请求失败",message,"wait");if(error.status===401&&!connectionConfig().local){openConnection();setResult("#connection-result","当前 API Key 未通过后端校验，请重新填写。","error")}
 }finally{state.busy=false;state.contextPending=false;updateConversationStatus();$("#send-button").disabled=false}
}

function renderRuns(){
 const query=String($("#run-search")&&$("#run-search").value||"").trim().toLowerCase(),filter=state.runFilter;
 const rows=state.runs.filter(run=>(filter==="all"||filter==="completed"&&run.status==="completed"||filter==="running"&&run.status==="running"||filter==="waiting"&&["waiting","blocked"].includes(run.status))&&(!query||[run.goal,run.id,run.caseId].concat(run.actions||[]).join(" ").toLowerCase().includes(query)));
 $("#runs-table").innerHTML=rows.length?rows.map(run=>'<article class="run-row" data-run-record="'+esc(run.id)+'"><span><b>'+esc(run.goal)+'</b><small class="run-id">'+esc(run.id)+' · '+esc(run.caseId)+'</small></span><span><b>'+esc(run.caseId)+'</b><small>真实 MVP Agent 调用</small></span><span class="run-actions">'+(run.actions||[]).slice(0,4).map(name=>'<i>'+esc(String(name).split("_").map(part=>part[0]).join("").slice(0,3).toUpperCase())+'</i>').join("")+'</span><span class="run-status '+esc(run.status)+'">'+esc(run.statusLabel)+'</span><small>'+esc(run.duration)+' · '+esc(localTime(run.time))+'</small>'+icon("chevron")+'</article>').join(""):'<div class="empty-row">本次会话还没有真实 Agent 运行记录</div>';
 const completed=state.runs.filter(item=>item.status==="completed").length,blocked=state.runs.filter(item=>item.status==="blocked").length,running=state.runs.filter(item=>item.status==="running").length;$("#metric-total").textContent=String(state.runs.length);$("#metric-running").textContent=String(running);$("#metric-blocked").textContent=String(blocked);$("#metric-completed").textContent=String(completed);$("#run-count-badge").textContent=String(Math.min(state.runs.length,99))
}
function renderRecentRuns(){const rows=state.runs.slice(0,4);$("#recent-runs").innerHTML=rows.length?rows.map((run,index)=>'<button class="session-item '+(index===0?'active':'')+'" type="button" data-run-record="'+esc(run.id)+'"><span class="session-dot '+(run.status==="completed"?'done':run.status==="running"?'running':'wait')+'"></span><span><b>'+esc(run.goal)+'</b><small>'+esc(run.actions.length+' 个 Action · '+localTime(run.time))+'</small></span></button>').join(""):'<div class="sidebar-empty">本次会话暂无真实运行</div>'}
function renderCatalog(){if(!$("#action-grid"))return;const query=String($("#catalog-search").value||"").trim().toLowerCase(),entries=Object.values(state.actionCatalog).filter(item=>(state.actionFilter==="all"||item.type===state.actionFilter)&&(!query||[item.name,item.label,item.description].join(" ").toLowerCase().includes(query)));$("#action-grid").innerHTML=entries.length?entries.map(item=>'<article class="action-card '+(item.type==="WRITE"?'write':'')+'" data-action="'+esc(item.name)+'"><header><span class="action-card-icon">'+icon(item.type==="WRITE"?'lock':'search')+'</span><em class="type-badge">'+esc(item.type)+'</em></header><h3>'+esc(item.label)+'</h3><code>'+esc(item.name)+'</code><p>'+esc(item.description)+'</p><footer><span>'+icon("database")+esc(item.source)+'</span><span>查看契约 '+icon("chevron")+'</span></footer></article>').join(""):'<div class="empty-row">没有匹配的后端能力</div>';$("#catalog-count").textContent=String(Object.keys(state.actionCatalog).length)}
function showAction(name){const item=state.actionCatalog[name];if(!item)return;const step=item.type==="WRITE"&&state.workflowDetail&&state.workflowDetail.steps&&state.workflowDetail.steps.find(value=>value.action_name===name),ready=step&&step.status==="READY";$("#action-title").textContent=item.label;$("#action-subtitle").textContent=item.name;$("#action-detail").innerHTML='<div class="action-detail-grid"><div><span>执行类型</span><b>'+esc(item.type)+'</b></div><div><span>能力来源</span><b>'+esc(item.source)+'</b></div><div style="grid-column:1/-1"><span>用途说明</span><b>'+esc(item.description)+'</b></div></div><div class="schema-box">INPUT\n'+esc(typeof item.input==="string"?item.input:JSON.stringify(item.input,null,2))+'\n\nOUTPUT\n'+esc(typeof item.output==="string"?item.output:JSON.stringify(item.output,null,2))+'</div>'+(ready?'<div class="approval-box"><p><b>当前工作流步骤已就绪</b><br>填写业务字段并再次确认后，才会调用真实写入接口。</p><div><button class="approve" type="button" data-open-workflow-action="'+esc(name)+'">办理当前 Action</button></div></div>':'');modal("#action-modal",true)}
function showRunDetail(id){const run=state.runs.find(item=>item.id===id);if(!run)return;state.activeRun=run;$("#run-detail-title").textContent=run.goal;$("#run-detail-subtitle").textContent=run.id+" · "+run.caseId;$("#run-detail-body").innerHTML='<div class="run-detail-summary"><div><span>申请编号</span><b>'+esc(run.caseId)+'</b></div><div><span>状态</span><b>'+esc(run.statusLabel)+'</b></div><div><span>Action</span><b>'+esc(run.actions.length)+" 个"+'</b></div><div><span>真实耗时</span><b>'+esc(run.duration)+'</b></div></div><ul class="run-timeline">'+run.actions.map(name=>'<li><i class="'+(run.status==="blocked"?'waiting':'')+'">'+(run.status==="blocked"?'!':'✓')+'</i><span><b>'+esc(state.actionCatalog[name]&&state.actionCatalog[name].label||name)+'</b><small>'+esc(name)+'</small></span><em>'+esc(run.statusLabel)+'</em></li>').join("")+'</ul>';modal("#run-modal",true)}

function renderEvidence(){
 const readiness=state.lastAgentResponse&&state.lastAgentResponse.plan&&state.lastAgentResponse.plan.readiness||state.readiness||{},evidence=readiness.evidence||{},available=Object.entries(evidence).filter(([,value])=>value&&value.available),missing=Object.entries(evidence).filter(([,value])=>!value||!value.available),tools=(readiness.available_tools||[]);
 $("#evidence-ready-count").textContent=String(available.length);$("#evidence-missing-count").textContent=String(missing.length);$("#evidence-tools-count").textContent=String(tools.length);$("#evidence-status").textContent=readiness.status||"—";
 const group=(label,action,items,positive)=>'<section class="evidence-group"><header><b>'+esc(label)+'</b><code>'+esc(action)+'</code></header>'+items.map(item=>'<div class="evidence-item"><i>'+icon(positive?'check':'lock')+'</i><span><b>'+esc(item[1]&&item[1].label||item[0])+'</b><small>'+esc(item[0])+'</small></span><em>'+(positive?'已就绪':'缺失')+'</em></div>').join("")+'</section>';
 let html=group("可用业务证据","inspect_case_readiness",available,true)+group("缺失或未完成证据","inspect_case_readiness",missing,false);
 const outputs=state.lastAgentResponse&&state.lastAgentResponse.executions&&state.lastAgentResponse.executions.length?state.lastAgentResponse.executions.filter(item=>item.result).map(item=>({tool:item.tool_name,result:item.result})):(state.lastAgentResponse&&state.lastAgentResponse.result?[{tool:state.lastAgentResponse.plan&&state.lastAgentResponse.plan.selected_tool||"agent_result",result:state.lastAgentResponse.result}]:[]);
 outputs.forEach(output=>{html+='<section class="evidence-group"><header><b>真实 Action 输出摘要</b><code>'+esc(output.tool)+'</code></header><div class="schema-box evidence-json">'+esc(JSON.stringify(output.result,null,2))+'</div></section>'});
 $("#evidence-chain").innerHTML=html||'<div class="empty-row">后端未返回证据信息</div>';modal("#evidence-modal",true)
}

function findScalar(source,name,depth){if(depth>5||source==null)return undefined;if(Array.isArray(source)){for(const item of source){const value=findScalar(item,name,depth+1);if(value!==undefined)return value}return undefined}if(typeof source!=="object")return undefined;if(Object.prototype.hasOwnProperty.call(source,name))return source[name];for(const value of Object.values(source)){const found=findScalar(value,name,depth+1);if(found!==undefined)return found}return undefined}
function fieldDefault(name){if(name==="approval_assertion")return state.definition&&state.definition.recommendation_only_assertion||"";if(name==="verification_id"||name==="source_execution_id")return findScalar(state.workflowDetail,name,0)||"";if(name==="submit_date"){const now=new Date(Date.now()-new Date().getTimezoneOffset()*60000);return now.toISOString().slice(0,16)}return FIELD_META[name]&&FIELD_META[name].default||""}
function workflowFieldMarkup(field,group){const meta=FIELD_META[field]||{label:field},value=fieldDefault(field),readonly=meta.readonly||field==="approval_assertion",type=meta.type==="integer"?"number":meta.type||"text",data='data-workflow-field="'+esc(field)+'"'+(group?' data-workflow-group="'+esc(group)+'"':'');let control;if(meta.choices)control='<select '+data+' '+(readonly?'disabled':'')+'><option value="">请选择</option>'+meta.choices.map(choice=>'<option value="'+esc(choice)+'" '+(String(value)===choice?'selected':'')+'>'+esc(choice)+'</option>').join("")+'</select>';else if(meta.long)control='<textarea '+data+' rows="3" '+(readonly?'readonly':'')+'>'+esc(value)+'</textarea>';else control='<input '+data+' type="'+esc(type)+'" value="'+esc(value)+'" '+(type==="number"?'step="any" ':'')+(readonly?'readonly':'')+'>';return '<label><span>'+esc(meta.label||field)+' <code>'+esc(field)+'</code></span>'+control+'</label>'}
function openWorkflowAction(name){
 const definition=((state.definition&&state.definition.actions)||[]).find(item=>item.action_name===name),step=state.workflowDetail&&state.workflowDetail.steps&&state.workflowDetail.steps.find(item=>item.action_name===name);if(!definition){toast("后端流程定义中不存在该 Action","error");return}if(!step||step.status!=="READY"){toast("该业务 Action 当前不是 READY 状态，不能执行","error");return}if(state.safety.writePolicy==="deny"){toast("当前安全策略禁止执行写入 Action","error");return}
 if(!state.authenticated){if(connectionConfig().local){toast("正在重新建立本机安全会话，请连接成功后重试","error");connectBackend(false)}else{openConnection();setResult("#connection-result","远程业务写入必须先填写并验证 MVP X-API-Key。","error")}return}
 state.pendingWorkflowAction={name,definition};modal("#action-modal",false);$("#workflow-action-title").textContent="确认执行："+definition.label;$("#workflow-action-copy").textContent="申请 "+state.currentWorkflow.application_id+" · 该操作会写入真实业务状态";
 const fields=(definition.request_fields||[]).filter(field=>!SYSTEM_FIELDS.has(field));$("#workflow-action-form").innerHTML=fields.length?fields.map(field=>{const group=GROUP_META[field];return group?'<fieldset class="workflow-fieldset"><legend>'+esc(group.label)+'</legend>'+group.fields.map(child=>workflowFieldMarkup(child,field)).join("")+'</fieldset>':'<div class="workflow-scalar">'+workflowFieldMarkup(field,null)+'</div>'}).join(""):'<div class="demo-warning"><p>该 Action 不需要额外业务字段。</p></div>';$("#workflow-action-result").hidden=true;modal("#workflow-action-modal",true)
}
async function executeWorkflowAction(){const pending=state.pendingWorkflowAction;if(!pending||state.busy)return;const payload={};$$('[data-workflow-field]',$("#workflow-action-form")).forEach(node=>{let value=node.value.trim();if(value!==""){const meta=FIELD_META[node.dataset.workflowField]||{};if(meta.type==="integer")value=Number.parseInt(value,10);else if(meta.type==="number")value=Number(value);const group=node.dataset.workflowGroup;if(group){if(!payload[group])payload[group]={};payload[group][node.dataset.workflowField]=value}else payload[node.dataset.workflowField]=value}});state.busy=true;$("#execute-workflow-action").disabled=true;setResult("#workflow-action-result","正在调用真实工作流 Action…","success");try{state.workflowDetail=await api("/workflows/personal-credit/"+encodeURIComponent(state.currentWorkflow.workflow_id)+"/steps/"+encodeURIComponent(pending.name)+"/execute",{method:"POST",body:JSON.stringify({payload})});await loadWorkflowContext(state.currentWorkflow.workflow_id,{resetConversation:false});setResult("#workflow-action-result","Action 已提交，工作流状态已刷新","success");addEvent("业务 Action 已人工确认执行",pending.name,"done");addAgentNotice("业务 Action 已执行","“"+pending.definition.label+"”已通过工作流接口提交，最新状态已从后端刷新。","success");setTimeout(()=>modal("#workflow-action-modal",false),650)}catch(error){setResult("#workflow-action-result",error.message,"error");addEvent("业务 Action 执行失败",error.message,"wait")}finally{state.busy=false;$("#execute-workflow-action").disabled=false}}

function openSafety(){$("#auto-read").checked=true;$("#write-policy").value=state.safety.writePolicy;modal("#safety-modal",true)}
function saveSafety(){state.safety={autoRead:true,writePolicy:$("#write-policy").value};writeSession(STORAGE.safety,state.safety);const labels=$$(".strategy-row b");if(labels[0])labels[0].textContent="开启";if(labels[1])labels[1].textContent=state.safety.writePolicy==="deny"?"禁止":"强制";modal("#safety-modal",false);toast("安全执行设置已保存")}
function showView(view){state.currentView=view||"chat";$$('[data-view-panel]').forEach(panel=>panel.hidden=panel.dataset.viewPanel!==state.currentView);$$('.main-nav [data-view]').forEach(button=>button.classList.toggle("active",button.dataset.view===state.currentView));if(state.currentView==="runs")renderRuns();if(state.currentView==="catalog")renderCatalog();if(state.currentView==="models"){renderModels();if(state.authenticated&&!state.modelsLoaded&&!state.modelLoading)loadModels(true)}document.body.classList.remove("sidebar-open")}
function openHashView(){if(String(location.hash||"").toLowerCase()==="#models")showView("models")}
function copyText(text,label){if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(String(text)).catch(()=>{});toast(label||"已复制")}
function exportRunRecord(run){
 const response=run.response&&typeof run.response==="object"?run.response:{},summary=response.execution_summary&&typeof response.execution_summary==="object"?response.execution_summary:{},answer=String(response.answer||"").replace(/\s+/g," ").trim().slice(0,300),executions=Array.isArray(response.executions)?response.executions.map(item=>({tool_name:item&&item.tool_name||item&&item.tool||null,status:item&&item.status||null,executed:!!(item&&item.executed)})).filter(item=>item.tool_name):[],proposedActions=Array.isArray(response.proposed_actions)?response.proposed_actions.map(item=>item&&item.action_name).filter(Boolean):[];
 return{id:run.id,goal:run.goal,application_id:run.caseId,ontology_version:run.ontologyVersion,conversation_id:run.conversationId||null,turn_index:run.turnIndex||null,response_type:run.responseType||agentResponseType(response),actions:(run.actions||[]).slice(),status:run.status,status_label:run.statusLabel,duration:run.duration,time:run.time,answer_summary:answer,execution_summary:{planned:Number(summary.planned||0),executed:Number(summary.executed||0),blocked:Number(summary.blocked||0),failed:Number(summary.failed||0),status:summary.status||null},executions,proposed_actions:proposedActions}
}

function updateConnectionFormMode(){const base=normalizeBase($("#connection-base")&&$("#connection-base").value),local=isLocalApiBase(base),tokenField=$("#connection-token-field"),localInfo=$("#connection-local-info");if(tokenField)tokenField.hidden=local;if(localInfo)localInfo.hidden=!local;if(local&&$("#connection-token"))$("#connection-token").value="";if($("#connection-description"))$("#connection-description").textContent=local?"本机地址会自动建立 HttpOnly 安全会话，不需要填写 X-API-Key。":"远程地址继续使用 MVP X-API-Key，并且必须通过 HTTPS 连接。";if($("#test-connection"))$("#test-connection").textContent=local?"测试自动连接":"测试连接与 API Key";if($("#save-connection"))$("#save-connection").textContent=local?"保存并自动连接":"保存并重新连接"}
function openConnection(){const config=connectionConfig();$("#connection-base").value=config.base;$("#connection-token").value=config.token;$("#connection-result").hidden=true;updateConnectionFormMode();modal("#connection-modal",true)}
function connectionValues(){const value={base:normalizeBase($("#connection-base").value),token:$("#connection-token").value.trim()};if(!value.base)return{error:"请填写 MVP API Base URL"};try{const url=new URL(value.base,location.href),local=isLoopbackHost(url.hostname);if(!["http:","https:"].includes(url.protocol))return{error:"MVP API 地址必须使用 HTTP(S)"};if(url.protocol!=="https:"&&!local)return{error:"非本机 MVP API 必须使用 HTTPS，以保护访问令牌和模型密钥"};value.local=local;if(local){value.base=alignLocalApiBase(value.base);value.token=""}else if(!value.token)return{error:"远程 MVP API 需要填写 X-API-Key"}}catch(_error){return{error:"MVP API Base URL 格式无效"}}return value}
async function testConnection(){
 const value=connectionValues();if(value.error)return setResult("#connection-result",value.error,"error");setResult("#connection-result",value.local?"正在建立本机安全会话…":"正在验证远程连接与 API Key…","success");try{await requestWith(value,"/health");const auth=await establishAuthentication(value);if(!auth.authenticated)return setResult("#connection-result",auth.error||"未能建立受保护会话。","error");const tools=await requestWith(value,"/agents/personal-credit/tools");setResult("#connection-result",value.local?"本机安全会话已建立；无需保存或填写访问令牌。后端返回 "+String((tools.tools||[]).length)+" 个 Agent 工具。":"远程连接成功，MVP X-API-Key 有效。后端返回 "+String((tools.tools||[]).length)+" 个 Agent 工具。","success")}catch(error){setResult("#connection-result",error.status===401?"MVP X-API-Key 无效，请核对远程服务配置。":error.message,"error")}
}
async function saveConnection(){
 const value=connectionValues();if(value.error)return setResult("#connection-result",value.error,"error");setResult("#connection-result",value.local?"正在建立本机安全会话…":"正在验证 MVP X-API-Key…","success");try{await requestWith(value,"/health");const auth=await establishAuthentication(value);if(!auth.authenticated){setResult("#connection-result",auth.error||"未能建立受保护会话，连接地址未保存。","error");return}}catch(error){setResult("#connection-result",error.status===401?"MVP X-API-Key 无效，未保存。":"连接验证失败："+error.message,"error");return}
 resetConversationContext();const saved=persistConnection(value);modal("#connection-modal",false);newTask(false,{preserveConversation:true});await connectBackend(false,saved);toast(value.local?"本机安全会话已建立":"MVP 后端与 API Key 已保存到当前浏览器会话")
}

document.addEventListener("click",event=>{
 const target=event.target.closest("button,[data-action],[data-run-record]");if(!target)return;
 if(target.dataset.view)showView(target.dataset.view);
 if(target.dataset.prompt)runGoal(target.dataset.prompt);
 if(target.dataset.action)showAction(target.dataset.action);
 if(target.dataset.modelId)selectModel(target.dataset.modelId);
 if(target.dataset.runRecord)showRunDetail(target.dataset.runRecord);
 if(target.dataset.caseId){modal("#case-modal",false);loadWorkflowContext(target.dataset.caseId,{resetConversation:true})}
 if(target.dataset.copyTask)copyText(target.dataset.copyTask,"运行 ID 已复制");
 if(target.hasAttribute("data-copy-result")){const node=$(".answer-card .final-answer",target.closest(".message-body"));copyText(node&&node.textContent||"","结果摘要已复制")}
 if(target.hasAttribute("data-copy-policy")){const node=$(".prompt-card pre");copyText(node&&node.innerText||"","规划边界已复制")}
 if(target.hasAttribute("data-evidence"))renderEvidence();
 if(target.hasAttribute("data-close-modal"))modal("#"+target.closest(".modal-backdrop").id,false);
 if(target.dataset.openWorkflowAction)openWorkflowAction(target.dataset.openWorkflowAction);
 if(target.hasAttribute("data-rerun"))runGoal(state.lastPrompt);
 if(target.dataset.runFilter){state.runFilter=target.dataset.runFilter;$$('[data-run-filter]').forEach(node=>node.classList.toggle("active",node===target));renderRuns()}
 if(target.dataset.actionFilter){state.actionFilter=target.dataset.actionFilter;$$('[data-action-filter]').forEach(node=>node.classList.toggle("active",node===target));renderCatalog()}
});
$$('.modal-backdrop').forEach(node=>node.addEventListener("click",event=>{if(event.target===node)modal("#"+node.id,false)}));
on("new-task","click",()=>newTask(true));on("send-button","click",()=>runGoal());on("agent-input","keydown",event=>{if(event.key==="Enter"&&!event.shiftKey){event.preventDefault();runGoal()}});on("agent-input","input",event=>{event.target.style.height="auto";event.target.style.height=Math.min(event.target.scrollHeight,90)+"px"});
on("connection-button","click",openConnection);on("connection-base","input",updateConnectionFormMode);on("test-connection","click",testConnection);on("save-connection","click",saveConnection);
on("planner-settings","click",()=>showView("models"));on("model-new","click",beginNewModel);on("model-refresh","click",()=>loadModels(false));on("model-open-connection","click",openConnection);on("model-auth-action","click",()=>connectionConfig().local?connectBackend(false):openConnection());on("model-save-draft","click",()=>saveModelConfig(false));on("model-save-and-test","click",()=>saveModelConfig(true));on("model-test","click",testModelConfig);on("model-activate","click",activateModelConfig);on("model-delete","click",deleteModelConfig);on("model-temperature","input",event=>$("#model-temp-value").textContent=event.target.value);
on("model-provider","change",()=>applyProviderPreset(true));
on("case-context","click",()=>{renderCases("");modal("#case-modal",true)});on("case-search","input",event=>renderCases(event.target.value));on("scope-button","click",()=>toast("现有 MVP Agent 按单笔申请上下文运行"));
on("safety-button","click",openSafety);on("open-safety-from-policy","click",openSafety);on("save-safety","click",saveSafety);on("execute-workflow-action","click",executeWorkflowAction);
on("run-search","input",renderRuns);on("catalog-search","input",renderCatalog);on("conversation-more","click",()=>modal("#session-modal",true));
on("rename-session","click",()=>{const run=state.activeRun||state.runs[0];if(!run){toast("当前没有可重命名的真实运行");return}const name=window.prompt("输入新的任务名称",run.goal);if(name&&name.trim()){run.goal=name.trim();saveRuns();modal("#session-modal",false);toast("任务名称已更新")}});
on("export-session","click",()=>{const payload={environment:"mvp-backend",api_base:connectionConfig().base,application_id:state.currentWorkflow&&state.currentWorkflow.application_id,conversation:{conversation_id:state.conversationId,turn_index:state.turnIndex,context_count:state.contextWindowSize},exported_at:new Date().toISOString(),runs:state.runs.map(exportRunRecord)},blob=new Blob([JSON.stringify(payload,null,2)],{type:"application/json;charset=utf-8"}),url=URL.createObjectURL(blob),link=document.createElement("a");link.href=url;link.download="lixin-agent-action-runs.json";link.click();URL.revokeObjectURL(url);modal("#session-modal",false);toast("已导出裁剪后的 Action 运行摘要")});
on("clear-session","click",()=>{modal("#session-modal",false);newTask(false)});on("continue-run","click",()=>{const run=state.activeRun;if(!run)return;modal("#run-modal",false);const workflow=state.workflows.find(item=>item.application_id===run.caseId&&String(item.ontology_version||"")===String(run.ontologyVersion||""))||state.workflows.find(item=>item.application_id===run.caseId),switching=!!(workflow&&state.currentWorkflow&&(workflow.application_id!==state.currentWorkflow.application_id||String(workflow.ontology_version||"")!==String(state.currentWorkflow.ontology_version||"")));if(workflow)loadWorkflowContext(workflow.workflow_id,{resetConversation:switching});$("#agent-input").value="继续处理："+run.goal;$("#agent-input").focus()});
on("restore-policy","click",()=>{$$('[data-policy]').forEach(node=>node.checked=node.dataset.policy!=="experimental");toast("已恢复界面默认策略显示")});
$$('[data-policy]').forEach(node=>node.addEventListener("change",()=>{const enabled=$$('[data-policy]:checked').length;$(".policy-list>header>em").textContent=enabled+" / 5";toast((node.checked?"已启用：":"已停用：")+node.closest("label").querySelector("b").textContent)}));
on("mobile-menu","click",()=>document.body.classList.toggle("sidebar-open"));document.addEventListener("keydown",event=>{if(event.key==="Escape"){$$('.modal-backdrop:not([hidden])').forEach(node=>modal("#"+node.id,false));document.body.classList.remove("sidebar-open")}if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==="k"){event.preventDefault();newTask(true)}});
window.addEventListener("hashchange",openHashView);

sessionStorage.removeItem("lixin_agent_model");sessionStorage.removeItem("personalCreditAgentLLM");persistConnection(connectionConfig());newTask(false,{preserveConversation:true});updateModel();renderModels();updateConversationStatus();renderRuns();renderRecentRuns();renderCatalog();renderCases("");openHashView();connectBackend(true);
})();
