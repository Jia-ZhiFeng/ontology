import hashlib
import re
import unicodedata
from itertools import combinations

from .models import OntologyProposal


MAPPING_VERSION = "personal-credit-abox-tbox-mapping-2.0"
VID_SCHEME = "typed-abox-vid-v1"
EDGE_RANK_SCHEME = "relation-kind-sha256-v1"

ID_KEYS = {
    "natural_person": "id_number",
    "dealer": "credit_code",
    "vehicle": "vin",
    "loan_application": "application_id",
    "credit_report": "report_id",
    "external_debt": "debt_id",
    "risk_assessment_report": "assessment_id",
    "anti_fraud_report": "fraud_report_id",
    "phone_verification_record": "verification_id",
}


def object_type_vid(name: str) -> str:
    return f"tbox:ObjectType:{name}"


def property_vid(domain: str, name: str) -> str:
    return f"tbox:Property:{domain}:{name}"


def link_type_vid(source: str, name: str, target: str) -> str:
    return f"tbox:LinkType:{source}:{name}:{target}"


def abox_vid(object_type: str, business_key) -> str:
    return f"abox:{object_type}:{business_key}"


def relation_rank(kind: str) -> int:
    """Return a deterministic non-negative int64 rank for one relation kind."""
    digest = hashlib.sha256(f"related:{kind}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8],"big") & 0x7FFFFFFFFFFFFFFF


def build_mapping_contract(proposal: OntologyProposal) -> dict:
    table_objects = {}
    property_mappings = {}
    for prop in proposal.properties:
        if not prop.source_table or not prop.source_column:
            continue
        existing = table_objects.setdefault(prop.source_table, prop.domain)
        if existing != prop.domain:
            raise ValueError(f"同一数据表映射到多个 ObjectType：{prop.source_table}")
        property_mappings.setdefault(prop.source_table, {})[prop.source_column] = {
            "property": f"{prop.domain}.{prop.name}",
            "property_name": prop.name,
            "property_vid": property_vid(prop.domain, prop.name),
            "range": prop.range,
            "required": prop.required,
            "required_when": prop.required_when,
        }

    excluded = {}
    for item in proposal.metadata.get("excluded_database_fields", []):
        excluded.setdefault(item["table"], {})[item["column"]] = item.get("reason", "按范围排除")

    relations = {}
    for relation in proposal.link_types:
        relations[relation.name] = {
            "name": relation.name,
            "label": relation.label,
            "source": relation.source,
            "target": relation.target,
            "direction": relation.direction,
            "link_type_vid": link_type_vid(relation.source, relation.name, relation.target),
        }

    missing_id_tables = sorted(set(table_objects) - set(ID_KEYS))
    if missing_id_tables:
        raise ValueError(f"缺少 ABox 主键映射：{', '.join(missing_id_tables)}")

    return {
        "mapping_version": MAPPING_VERSION,
        "tbox_version": proposal.version,
        "table_objects": table_objects,
        "property_mappings": property_mappings,
        "excluded_fields": excluded,
        "relations": relations,
        "id_keys": {table: ID_KEYS[table] for table in table_objects},
        "vid_scheme": VID_SCHEME,
        "edge_rank_scheme": EDGE_RANK_SCHEME,
    }


def required_tbox_vertices(contract: dict) -> set[str]:
    required = {object_type_vid(name) for name in contract["table_objects"].values()}
    required.update(
        item["property_vid"]
        for mappings in contract["property_mappings"].values()
        for item in mappings.values()
    )
    required.update(item["link_type_vid"] for item in contract["relations"].values())
    return required


def prepare_mapped_entities(profile: dict, contract: dict) -> tuple[list[dict], list[dict]]:
    entities = []
    issues = []
    samples = profile.get("samples", {})
    for table, object_type in contract["table_objects"].items():
        id_key = contract["id_keys"][table]
        approved = contract["property_mappings"].get(table, {})
        excluded = contract["excluded_fields"].get(table, {})
        for row_index, row in enumerate(samples.get(table, [])):
            data = dict(row)
            identifier = data.get(id_key)
            if identifier in (None, ""):
                issues.append({"table": table, "row": row_index, "issue": f"主键 {id_key} 为空"})
                continue
            row_fields = set(data)
            unmapped_fields = sorted(row_fields - set(approved) - set(excluded))
            property_map = {
                column: {
                    **spec,
                    "status": "mapped_null" if data.get(column) is None else "mapped_value",
                }
                for column, spec in approved.items()
                if column in data
            }
            missing_columns = sorted(set(approved) - row_fields)
            if missing_columns:
                issues.append({"table": table, "row": row_index, "issue": "本体字段未出现在数据行", "fields": missing_columns})
            if unmapped_fields:
                issues.append({"table": table, "row": row_index, "issue": "存在未决字段", "fields": unmapped_fields})
            ontology_meta = {
                "mapping_version": contract["mapping_version"],
                "tbox_version": contract["tbox_version"],
                "source_table": table,
                "object_type": object_type,
                "object_type_vid": object_type_vid(object_type),
                "business_key": id_key,
                "business_id": str(identifier),
                "vid_scheme": contract["vid_scheme"],
                "edge_rank_scheme": contract["edge_rank_scheme"],
                "property_mappings": property_map,
                "excluded_fields": [
                    {"column": column, "reason": reason}
                    for column, reason in excluded.items()
                    if column in data
                ],
                "unmapped_fields": unmapped_fields,
            }
            data["_ontology"] = ontology_meta
            entities.append({
                "vid": abox_vid(object_type,str(identifier)),
                "legacy_vid": str(identifier),
                "business_id": str(identifier),
                "table": table,
                "object_type": object_type,
                "object_type_vid": ontology_meta["object_type_vid"],
                "data": data,
            })
    return entities, issues


def type_relation(kind: str, data: dict, contract: dict) -> dict:
    spec = contract["relations"].get(kind)
    if not spec:
        raise ValueError(f"ABox 关系没有对应 LinkType：{kind}")
    result = dict(data)
    result["_ontology"] = {
        "mapping_version": contract["mapping_version"],
        "tbox_version": contract["tbox_version"],
        "link_type": kind,
        "link_type_vid": spec["link_type_vid"],
        "source_object_type": spec["source"],
        "target_object_type": spec["target"],
        "edge_rank_scheme": contract["edge_rank_scheme"],
    }
    return result


def _normalize_contact(value, match_type: str) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", str(value)).strip()
    if not normalized:
        return None
    if match_type == "phone":
        digits = re.sub(r"\D", "", normalized)
        if digits.startswith("86") and len(digits) == 13:
            digits = digits[2:]
        return digits if 7 <= len(digits) <= 15 else None
    if match_type == "address":
        normalized = re.sub(r"\s+", "", normalized).casefold()
        return normalized or None
    raise ValueError(f"不支持的联系方式类型：{match_type}")


def derive_shared_contact_links(person_rows: list[dict]) -> list[tuple[str, str, str, dict]]:
    pair_matches = {}
    for column, match_type in (("phone", "phone"), ("address", "address")):
        groups = {}
        for row in person_rows:
            identifier = row.get("id_number")
            normalized = _normalize_contact(row.get(column), match_type)
            if normalized is None or identifier in (None, ""):
                continue
            groups.setdefault(normalized, set()).add(str(identifier))
        for normalized, identifiers in groups.items():
            if len(identifiers) < 2:
                continue
            value_hash = hashlib.sha256(
                f"SHARES_CONTACT:{match_type}:{normalized}".encode("utf-8")
            ).hexdigest()
            for source, target in combinations(sorted(identifiers), 2):
                pair_matches.setdefault((source, target), []).append({
                    "match_type": match_type,
                    "source_column": f"natural_person.{column}",
                    "rule_id": f"SHC-EXACT-{match_type.upper()}-V1",
                    "matched_value_hash": value_hash,
                })
    links = []
    for (source, target), matches in pair_matches.items():
        primary = matches[0]
        links.append((source, target, "SHARES_CONTACT", {
            "match_type": primary["match_type"],
            "matched_value": f"sha256:{primary['matched_value_hash']}",
            "evidence_types": [item["match_type"] for item in matches],
            "evidence_count": len(matches),
            "generation_rule": "exact_normalized_contact_match_v1",
            "matches": matches,
        }))
    return links
