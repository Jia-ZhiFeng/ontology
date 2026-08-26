import hashlib
import json

from .attribute_catalog import PERSONAL_CREDIT_ATTRIBUTE_CATALOG
from .baseline import build_personal_credit_baseline
from .models import OntologyProposal
from .validator import validate


def proposal_digest(proposal: OntologyProposal) -> str:
    payload = json.dumps(
        proposal.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _indexed(items, key):
    result = {}
    duplicates = []
    for item in items:
        identifier = key(item)
        if identifier in result:
            duplicates.append(identifier)
        result[identifier] = item.model_dump(mode="json")
    return result, sorted(set(duplicates))


def _catalog_diff(actual_items, expected_items, key):
    actual, duplicates = _indexed(actual_items, key)
    expected, _ = _indexed(expected_items, key)
    actual_keys = set(actual)
    expected_keys = set(expected)
    return {
        "duplicates": duplicates,
        "missing": sorted(expected_keys - actual_keys),
        "unexpected": sorted(actual_keys - expected_keys),
        "mismatched": sorted(
            identifier
            for identifier in actual_keys & expected_keys
            if actual[identifier] != expected[identifier]
        ),
    }


def audit_personal_credit_proposal(proposal: OntologyProposal, schema: dict, axiom_execution: dict | None = None) -> dict:
    """对当前个人信审本体执行发布前、目录级的确定性深度审核。"""
    expected = build_personal_credit_baseline()
    checks = []

    def add(identifier, category, passed, message, details=None, severity="error"):
        item = {
            "id": identifier,
            "category": category,
            "status": "passed" if passed else "failed",
            "severity": severity,
            "message": message,
        }
        if details:
            item["details"] = details
        checks.append(item)

    structural_errors = validate(proposal)
    add(
        "structure.references",
        "结构与引用完整性",
        not structural_errors,
        "基础结构、名称唯一性和跨目录引用均有效" if not structural_errors else "基础结构或跨目录引用存在错误",
        {"errors": structural_errors} if structural_errors else None,
    )

    version_ok = proposal.version == expected.version
    add(
        "release.identity",
        "发布版本标识",
        version_ok,
        f"发布版本固定为 {expected.version}" if version_ok else "发布版本与已批准版本不一致",
        None if version_ok else {"expected": expected.version, "actual": proposal.version},
    )

    sections = [
        ("catalog.objects", "对象目录一致性", proposal.object_types, expected.object_types, lambda item: item.name),
        ("catalog.properties", "属性目录一致性", proposal.properties, expected.properties, lambda item: f"{item.domain}.{item.name}"),
        ("catalog.relations", "关系目录一致性", proposal.link_types, expected.link_types, lambda item: item.name),
        ("catalog.interfaces", "接口与公理目录一致性", proposal.interfaces, expected.interfaces, lambda item: item.name),
        ("catalog.functions", "只读函数目录一致性", proposal.functions, expected.functions, lambda item: item.name),
        ("catalog.actions", "动作声明目录一致性", proposal.action_types, expected.action_types, lambda item: item.name),
        ("catalog.security", "动态安全目录一致性", proposal.dynamic_security, expected.dynamic_security, lambda item: item.action),
    ]
    for identifier, category, actual_items, expected_items, key in sections:
        diff = _catalog_diff(actual_items, expected_items, key)
        passed = not any(diff.values())
        add(
            identifier,
            category,
            passed,
            f"与已审核目录完全一致，共 {len(expected_items)} 项" if passed else "与已审核目录存在差异",
            diff if not passed else {"count": len(expected_items)},
        )

    actual_axioms, axiom_duplicates = _indexed(
        [axiom for interface in proposal.interfaces for axiom in interface.constraints],
        lambda item: item.id,
    )
    expected_axioms, _ = _indexed(
        [axiom for interface in expected.interfaces for axiom in interface.constraints],
        lambda item: item.id,
    )
    missing_axioms = sorted(set(expected_axioms) - set(actual_axioms))
    unexpected_axioms = sorted(set(actual_axioms) - set(expected_axioms))
    mismatched_axioms = sorted(
        identifier
        for identifier in set(actual_axioms) & set(expected_axioms)
        if actual_axioms[identifier] != expected_axioms[identifier]
    )
    axiom_details = {
        "duplicates": axiom_duplicates,
        "missing": missing_axioms,
        "unexpected": unexpected_axioms,
        "mismatched": mismatched_axioms,
    }
    axiom_ok = not any(axiom_details.values())
    add(
        "axioms.coverage",
        "公理覆盖",
        axiom_ok,
        f"26 条已审核公理全部覆盖" if axiom_ok else "公理覆盖不完整或内容被修改",
        {"count": len(actual_axioms)} if axiom_ok else axiom_details,
    )

    if axiom_execution is not None:
        executable = axiom_execution.get("executability", {})
        add(
            "axioms.executability",
            "公理可执行覆盖",
            bool(executable.get("valid")),
            "26 条公理均已绑定受控执行器" if executable.get("valid") else "存在未绑定、重复或多余的公理执行器",
            executable,
        )
        runtime_summary = axiom_execution.get("summary", {})
        runtime_failures = [
            item["id"] for item in axiom_execution.get("results", [])
            if item.get("status") in {"failed", "execution_error", "not_executable"}
        ]
        runtime_warnings = [
            item["id"] for item in axiom_execution.get("results", [])
            if item.get("status") == "warning"
        ]
        add(
            "axioms.runtime_data",
            "PostgreSQL 运行时公理校验",
            bool(axiom_execution.get("valid")),
            "当前 PostgreSQL 数据通过全部错误级公理" if axiom_execution.get("valid") else "当前 PostgreSQL 数据存在错误级公理违规或数据集不完整",
            {
                "summary": runtime_summary,
                "failed_axioms": runtime_failures,
                "warning_axioms": runtime_warnings,
                "dataset_complete": axiom_execution.get("dataset_complete"),
                "truncated_tables": axiom_execution.get("truncated_tables", []),
            },
        )

    live_fields = {
        f"{table}.{column['column_name']}"
        for table, columns in schema.items()
        for column in columns
    }
    catalog_fields = {
        f"{group['table']}.{spec['name']}"
        for group in PERSONAL_CREDIT_ATTRIBUTE_CATALOG["objects"].values()
        for spec in group["properties"]
    }
    excluded_fields = {
        f"{item['table']}.{item['column']}"
        for item in PERSONAL_CREDIT_ATTRIBUTE_CATALOG["excluded_database_fields"]
    }
    proposal_fields = {
        f"{item.source_table}.{item.source_column}"
        for item in proposal.properties
        if item.source_table and item.source_column
    }
    field_details = {
        "database_field_count": len(live_fields),
        "catalog_field_count": len(catalog_fields),
        "intentional_exclusion_count": len(excluded_fields),
        "proposal_source_count": len(proposal_fields),
        "database_fields_without_catalog_decision": sorted(live_fields - catalog_fields - excluded_fields),
        "catalog_fields_missing_in_database": sorted(catalog_fields - live_fields),
        "excluded_fields_missing_in_database": sorted(excluded_fields - live_fields),
        "catalog_fields_missing_in_proposal": sorted(catalog_fields - proposal_fields),
        "proposal_sources_outside_catalog": sorted(proposal_fields - catalog_fields),
    }
    field_ok = not any(
        field_details[name]
        for name in (
            "database_fields_without_catalog_decision",
            "catalog_fields_missing_in_database",
            "excluded_fields_missing_in_database",
            "catalog_fields_missing_in_proposal",
            "proposal_sources_outside_catalog",
        )
    )
    add(
        "postgresql.field_coverage",
        "PostgreSQL 字段覆盖",
        field_ok,
        f"{len(catalog_fields)} 个本体字段和 {len(excluded_fields)} 个有意排除字段完整覆盖 PostgreSQL" if field_ok else "PostgreSQL 与属性目录的字段覆盖不一致",
        field_details,
    )

    relation_sources = {
        source
        for relation in proposal.link_types
        for source in relation.source_columns
    }
    missing_relation_sources = sorted(relation_sources - live_fields)
    add(
        "postgresql.relation_sources",
        "关系来源字段",
        not missing_relation_sources,
        "全部关系来源字段均存在于 PostgreSQL" if not missing_relation_sources else "存在无效的关系来源字段",
        {"source_count": len(relation_sources), "missing": missing_relation_sources},
    )

    forbidden_actions = sorted(
        item.name for item in proposal.action_types if item.name in {"execute_approval", "approve_application"}
    )
    unsafe_functions = sorted(
        item.name
        for item in proposal.functions
        if item.side_effects or item.implementation_status != "not_bound"
    )
    executable_actions = sorted(
        item.name
        for item in proposal.action_types
        if item.execution_mode != "declaration_only" or not item.audit_required
    )
    security_actions = {item.action for item in proposal.dynamic_security}
    action_names = {item.name for item in proposal.action_types}
    metadata_runtime_enabled = proposal.metadata.get("runtime_capabilities_enabled") is not False
    boundary_details = {
        "forbidden_actions": forbidden_actions,
        "unsafe_or_bound_functions": unsafe_functions,
        "executable_or_unaudited_actions": executable_actions,
        "actions_without_exactly_one_security_policy": sorted(action_names ^ security_actions),
        "runtime_capabilities_enabled": metadata_runtime_enabled,
    }
    boundary_ok = not any(
        (
            forbidden_actions,
            unsafe_functions,
            executable_actions,
            boundary_details["actions_without_exactly_one_security_policy"],
            metadata_runtime_enabled,
        )
    )
    add(
        "capabilities.boundary",
        "能力与安全边界",
        boundary_ok,
        "函数只读未绑定、动作仅声明且受审计控制，不含自动审批" if boundary_ok else "动力层超出当前静态本体范围",
        boundary_details,
    )

    expected_metadata = expected.metadata
    metadata_keys = [
        "name",
        "status",
        "generation_method",
        "scope_version",
        "attribute_catalog_version",
        "relation_catalog_version",
        "interface_catalog_version",
        "capability_catalog_version",
        "security_catalog_version",
        "counts",
        "excluded_database_fields",
        "runtime_capabilities_enabled",
    ]
    mismatched_metadata = {
        key: {"expected": expected_metadata.get(key), "actual": proposal.metadata.get(key)}
        for key in metadata_keys
        if proposal.metadata.get(key) != expected_metadata.get(key)
    }
    add(
        "metadata.provenance",
        "版本与来源元数据",
        not mismatched_metadata,
        "范围、六份目录版本及统计元数据完整" if not mismatched_metadata else "来源或统计元数据与已审核基线不一致",
        mismatched_metadata or {"catalog_versions_verified": 6},
    )

    failed = sum(item["status"] == "failed" for item in checks)
    passed = len(checks) - failed
    return {
        "valid": failed == 0,
        "status": "passed" if failed == 0 else "failed",
        "proposal_version": proposal.version,
        "audited_digest": proposal_digest(proposal),
        "audit_scope": "个人汽车信审静态本体发布前深度审核",
        "summary": {"total": len(checks), "passed": passed, "failed": failed},
        "checks": checks,
        "axiom_validation": axiom_execution,
    }
