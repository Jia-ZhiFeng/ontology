import json
import re

from .audit import proposal_digest
from .axiom_executor import execute_axioms
from .data_quality import validate_catalog_data_quality
from .mapping import build_mapping_contract,prepare_mapped_entities
from .models import OntologyProposal
from .validator import validate


VERSION_PATTERN=re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")
WORKFLOW_TRANSITIONS={
    "draft":{"reviewed"},
    "reviewed":{"approved"},
    "approved":{"published"},
    "published":set(),
}


class VersionWorkflowError(ValueError):
    pass


def json_safe_payload(value):
    """Normalize audit/profile values (notably PostgreSQL datetimes) for JSONB."""
    return json.loads(json.dumps(value,ensure_ascii=False,default=str))


def validate_version_identifier(version:str)->str:
    value=(version or "").strip()
    if not VERSION_PATTERN.fullmatch(value):
        raise VersionWorkflowError("版本号必须使用 SemVer 格式，例如 1.1.0 或 1.1.0-rc.1")
    return value


def require_transition(current:str,target:str):
    if target not in WORKFLOW_TRANSITIONS.get(current,set()):
        raise VersionWorkflowError(f"不允许从 {current} 转换到 {target}")


def proposal_counts(proposal:OntologyProposal)->dict:
    return {
        "object_types":len(proposal.object_types),
        "properties":len(proposal.properties),
        "link_types":len(proposal.link_types),
        "interfaces":len(proposal.interfaces),
        "axioms":sum(len(item.constraints) for item in proposal.interfaces),
        "functions":len(proposal.functions),
        "action_types":len(proposal.action_types),
        "dynamic_security":len(proposal.dynamic_security),
    }


def normalize_draft_proposal(proposal:OntologyProposal,version:str)->OntologyProposal:
    value=proposal.model_copy(deep=True)
    value.version=validate_version_identifier(version)
    value.metadata={**value.metadata,"counts":proposal_counts(value)}
    return value


def proposal_diff(previous:OntologyProposal|None,current:OntologyProposal)->dict:
    sections=(
        ("object_types",lambda item:item.name),
        ("properties",lambda item:f"{item.domain}.{item.name}"),
        ("link_types",lambda item:item.name),
        ("interfaces",lambda item:item.name),
        ("functions",lambda item:item.name),
        ("action_types",lambda item:item.name),
        ("dynamic_security",lambda item:item.action),
    )
    output={}
    for field,key in sections:
        before={key(item):item.model_dump(mode="json") for item in getattr(previous,field,[])} if previous else {}
        after={key(item):item.model_dump(mode="json") for item in getattr(current,field,[])}
        before_keys=set(before); after_keys=set(after)
        output[field]={
            "added":sorted(after_keys-before_keys),
            "removed":sorted(before_keys-after_keys),
            "modified":sorted(identifier for identifier in before_keys&after_keys if before[identifier]!=after[identifier]),
        }
    output["previous_version"]=previous.version if previous else None
    output["candidate_version"]=current.version
    output["changed_item_count"]=sum(
        len(values[name])
        for field,values in output.items() if isinstance(values,dict)
        for name in ("added","removed","modified")
    )
    return output


def review_release_candidate(
    proposal:OntologyProposal,
    profile:dict,
    previous:OntologyProposal|None=None,
)->dict:
    """Review a mutable candidate without requiring equality to the code baseline."""
    checks=[]

    def add(identifier,category,passed,message,details=None):
        item={
            "id":identifier,
            "category":category,
            "status":"passed" if passed else "failed",
            "severity":"error",
            "message":message,
        }
        if details is not None:
            item["details"]=details
        checks.append(item)

    try:
        validate_version_identifier(proposal.version); version_error=None
    except VersionWorkflowError as exc:
        version_error=str(exc)
    add("release.version","版本标识",not version_error,"版本号符合 SemVer" if not version_error else version_error)

    structural_errors=validate(proposal)
    add(
        "structure.references","结构与引用完整性",not structural_errors,
        "名称、引用、动作和安全约束结构有效" if not structural_errors else "本体结构存在错误",
        {"errors":structural_errors} if structural_errors else None,
    )

    actual_counts=proposal_counts(proposal)
    metadata_counts=proposal.metadata.get("counts")
    counts_valid=metadata_counts==actual_counts
    add(
        "metadata.counts","统计元数据",counts_valid,
        "统计元数据与候选内容一致" if counts_valid else "metadata.counts 与候选内容不一致",
        {"expected":actual_counts,"actual":metadata_counts} if not counts_valid else actual_counts,
    )

    execution=execute_axioms(proposal,profile)
    add(
        "axioms.execution","公理执行",bool(execution.get("valid")),
        "26 条公理均有执行器且当前数据通过错误级校验" if execution.get("valid") else "公理覆盖、执行或当前数据校验未通过",
        {"executability":execution.get("executability"),"summary":execution.get("summary"),"dataset_complete":execution.get("dataset_complete")},
    )

    data_quality=validate_catalog_data_quality(proposal,profile)
    add(
        "data_quality.catalog","目录驱动数据质量",bool(data_quality.get("valid")),
        "当前一致性快照通过目录必填、条件、枚举、类型和业务主键校验" if data_quality.get("valid") else "当前数据未通过目录驱动通用质量校验",
        {
            "summary":data_quality.get("summary"),
            "snapshot":data_quality.get("snapshot"),
            "condition_coverage":data_quality.get("condition_coverage"),
        },
    )

    live_fields={
        f"{table}.{column['column_name']}"
        for table,columns in profile.get("tables",{}).items()
        for column in columns
    }
    incomplete_sources=[]; missing_sources=[]
    proposal_sources=set()
    for item in proposal.properties:
        if bool(item.source_table)!=bool(item.source_column):
            incomplete_sources.append(f"{item.domain}.{item.name}")
        elif item.source_table and item.source_column:
            source=f"{item.source_table}.{item.source_column}"
            proposal_sources.add(source)
            if source not in live_fields: missing_sources.append(source)
    excluded_fields={
        f"{item.get('table')}.{item.get('column')}"
        for item in proposal.metadata.get("excluded_database_fields",[])
        if item.get("table") and item.get("column")
    }
    undecided_fields=sorted(live_fields-proposal_sources-excluded_fields)
    field_valid=not incomplete_sources and not missing_sources and not undecided_fields
    add(
        "postgresql.field_decisions","PostgreSQL 字段决策",field_valid,
        "全部业务字段均已映射或明确排除" if field_valid else "存在无效来源或尚未决策的 PostgreSQL 字段",
        {
            "incomplete_sources":sorted(incomplete_sources),
            "missing_sources":sorted(set(missing_sources)),
            "undecided_fields":undecided_fields,
            "mapped_field_count":len(proposal_sources),
            "excluded_field_count":len(excluded_fields),
        },
    )

    relation_sources={source for relation in proposal.link_types for source in relation.source_columns}
    missing_relation_sources=sorted(relation_sources-live_fields)
    add(
        "postgresql.relation_sources","关系来源字段",not missing_relation_sources,
        "全部关系来源字段存在" if not missing_relation_sources else "存在无效的关系来源字段",
        {"missing":missing_relation_sources,"source_count":len(relation_sources)},
    )

    mapping_error=None; mapping_issues=[]; mapped_tables=[]; mapped_objects=set()
    try:
        contract=build_mapping_contract(proposal)
        _,mapping_issues=prepare_mapped_entities(profile,contract)
        mapped_tables=sorted(contract["table_objects"])
        mapped_objects=set(contract["table_objects"].values())
    except Exception as exc:
        mapping_error=str(exc)
    expected_objects={item.name for item in proposal.object_types}
    missing_core_objects=sorted(expected_objects-mapped_objects)
    mapping_valid=not mapping_error and not mapping_issues and not missing_core_objects
    add(
        "abox.mapping_preflight","ABox–TBox 映射预检",mapping_valid,
        "全部核心 ObjectType 可无歧义映射到候选 TBox" if mapping_valid else "候选版本无法安全承接当前 ABox 映射",
        {
            "error":mapping_error,
            "issues":mapping_issues,
            "mapped_tables":mapped_tables,
            "missing_core_object_types":missing_core_objects,
        },
    )

    forbidden_actions=sorted(item.name for item in proposal.action_types if item.name in {"execute_approval","approve_application"})
    unsafe_functions=sorted(item.name for item in proposal.functions if item.side_effects or item.implementation_status!="not_bound")
    executable_actions=sorted(item.name for item in proposal.action_types if item.execution_mode!="declaration_only" or not item.audit_required)
    runtime_enabled=proposal.metadata.get("runtime_capabilities_enabled") is not False
    boundary_valid=not forbidden_actions and not unsafe_functions and not executable_actions and not runtime_enabled
    add(
        "capabilities.boundary","静态能力与安全边界",boundary_valid,
        "候选仍保持只读函数、声明态动作、人工复核和运行能力关闭" if boundary_valid else "候选超出当前静态个人信审本体边界",
        {
            "forbidden_actions":forbidden_actions,
            "unsafe_or_bound_functions":unsafe_functions,
            "executable_or_unaudited_actions":executable_actions,
            "runtime_capabilities_enabled":runtime_enabled,
        },
    )

    difference=proposal_diff(previous,proposal)
    failed=sum(item["status"]=="failed" for item in checks)
    return {
        "valid":failed==0,
        "status":"passed" if failed==0 else "failed",
        "proposal_version":proposal.version,
        "audited_digest":proposal_digest(proposal),
        "audit_scope":"个人汽车信审本体版本候选审核",
        "summary":{"total":len(checks),"passed":len(checks)-failed,"failed":failed},
        "checks":checks,
        "change_set":difference,
        "axiom_validation":execution,
        "data_quality":data_quality,
    }
