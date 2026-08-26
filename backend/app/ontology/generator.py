import json
import asyncio
import logging
import time
from .models import OntologyProposal
from .scope import PERSONAL_CREDIT_SCOPE
from .attribute_catalog import PERSONAL_CREDIT_ATTRIBUTE_CATALOG
from .relation_catalog import PERSONAL_CREDIT_RELATION_CATALOG
from .interface_catalog import PERSONAL_CREDIT_INTERFACE_CATALOG
from .capability_catalog import PERSONAL_CREDIT_CAPABILITY_CATALOG, PERSONAL_CREDIT_SECURITY_CATALOG
from ..llm.client import LLMTransientError
from ..core.security import anonymize_profile_for_llm

logger=logging.getLogger(__name__)

SYSTEM = """你是金融信审领域的本体工程师。根据“PRD 业务要求 + PostgreSQL Schema + 数据样本”生成可人工审核的候选本体。
必须覆盖 ObjectType、Property、LinkType、Interface、ActionType、Function、DynamicSecurity。
优先使用来源中的业务术语，不要凭空扩展业务事实；domain/source/target 必须引用已有 ObjectType。
对象类型必须来自业务概念而不是机械复制数据库表；但每个对象及属性必须能溯源到 PostgreSQL 表和字段。
借款人、担保人、共借人不是独立 ObjectType，统一建模为 NaturalPerson 与 LoanApplication 之间的关系语义。
关系应服务于业务查询或函数运行路径，至少覆盖提交申请、担保、共同借款、车辆、经销商、征信、负债、风险评估、反欺诈和电核。
Function 只计算不写图；ActionType 表示有副作用且必须审计的动作；DynamicSecurity 必须坚持人工复核和最小权限。
当前是一期静态本体：不得声称已具备 CDC、自动推理、自动执行、MCP 或正式 FIBO 对齐能力。
每个 ObjectType 的 label 必须填写准确的中文名称；每个 Property 的 description 必须以简短中文说明该属性含义，便于图谱汉化展示。
这是 MVP 候选本体，请保持精炼：ObjectType 不超过 15 个，LinkType 不超过 20 个，Interface、ActionType、Function、DynamicSecurity 各不超过 10 个。
Property 必须来自 PostgreSQL 的真实字段，不要臆造来源中不存在的字段。数量限制是生成约束，不得截断被其他元素引用的 ObjectType。
Property 必须严格按照 attribute_catalog 中列出的本体属性生成；不得加入 excluded_database_fields。
label、required、business_key、required_when、data_source、enum_values、unit 必须采用目录定义；source_table/source_column 必须填写实际 PostgreSQL 来源。
required 表示业务流程完成时是否必须具备，不等同于数据库当前是否允许 NULL；NULL 可以表示尚未采集。
LinkType 必须严格采用 relation_catalog 中定义的 11 条关系，不得增加装饰性关系。
方向、两端基数、关系属性、来源字段和结构约束必须采用目录定义；实体自身属性不得为了展示而重复定义为关系属性。
Interface 必须严格采用 interface_catalog 中定义的 6 个公共约束接口，并完整保留其中的公理。
26条公理已绑定同步前批量校验器；不得将该能力描述为持续推理、流式规则引擎、自动审批或自动执行。
ActionType、Function、DynamicSecurity 必须严格采用 capability_catalog 和 security_catalog。
Function 的 side_effects 必须为 false；ActionType 必须 audit_required 且 execution_mode=declaration_only。
不得生成自动审批执行动作；record_decision_recommendation 只能提供 decision_support_only 建议。
只输出符合 JSON Schema 的 JSON，不要输出 Markdown。"""

async def generate(llm,profile):
    def prompt(current_profile):
        return json.dumps({
        "prd_context":PERSONAL_CREDIT_SCOPE,
        "attribute_catalog":PERSONAL_CREDIT_ATTRIBUTE_CATALOG,
        "relation_catalog":PERSONAL_CREDIT_RELATION_CATALOG,
        "interface_catalog":PERSONAL_CREDIT_INTERFACE_CATALOG,
        "capability_catalog":PERSONAL_CREDIT_CAPABILITY_CATALOG,
        "security_catalog":PERSONAL_CREDIT_SECURITY_CATALOG,
        "postgres_profile":current_profile,
        "output_schema":OntologyProposal.model_json_schema()
        },ensure_ascii=False,default=str)
    safe_profile=anonymize_profile_for_llm(profile)
    started=time.monotonic()
    try:
        raw=await llm.chat_json(SYSTEM,prompt(safe_profile))
        logger.info("ontology generation completed in %.2fs without retry",time.monotonic()-started)
    except LLMTransientError as first_error:
        logger.warning("ontology generation transient failure after %.2fs; retrying once: %s",time.monotonic()-started,first_error)
        compact={**safe_profile,"samples":{table:rows[:1] for table,rows in safe_profile.get("samples",{}).items()}}
        await asyncio.sleep(2)
        retry_started=time.monotonic()
        raw=await llm.chat_json(SYSTEM,prompt(compact))
        logger.info("ontology generation retry completed in %.2fs",time.monotonic()-retry_started)
    return OntologyProposal.model_validate(json.loads(raw))
