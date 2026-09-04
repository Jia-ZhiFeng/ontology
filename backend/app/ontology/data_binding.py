from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .models import OntologyProposal


SQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class OntologyDataBindingError(ValueError):
    """本体属性无法安全绑定到只读关系数据源。"""


def quote_identifier(value: str) -> str:
    identifier = str(value or "").strip()
    if not SQL_IDENTIFIER_PATTERN.fullmatch(identifier):
        raise OntologyDataBindingError(f"非法数据库标识符：{identifier or '<empty>'}")
    return f'"{identifier}"'


@dataclass(frozen=True)
class BoundOntologyProperty:
    object_type: str
    property_name: str
    source_table: str
    source_column: str
    data_source: str | None


class OntologyDataBinding:
    """从已发布本体构造只读、白名单化的 SQL 标识符绑定。"""

    def __init__(self, proposal: OntologyProposal, *, schema: str = "public"):
        if not isinstance(proposal, OntologyProposal):
            raise OntologyDataBindingError("proposal 必须是 OntologyProposal")
        self.ontology_version = str(proposal.version or "").strip()
        if not self.ontology_version:
            raise OntologyDataBindingError("本体版本不能为空")
        self.schema = str(schema or "").strip()
        quote_identifier(self.schema)
        self._properties: dict[tuple[str, str], BoundOntologyProperty] = {}
        self._tables: dict[str, str] = {}
        table_owners: dict[str, str] = {}

        for prop in proposal.properties:
            table = str(prop.source_table or "").strip()
            column = str(prop.source_column or "").strip()
            if bool(table) != bool(column):
                raise OntologyDataBindingError(
                    f"属性数据绑定不完整：{prop.domain}.{prop.name}"
                )
            if not table:
                continue
            quote_identifier(table)
            quote_identifier(column)
            key = (prop.domain, prop.name)
            if key in self._properties:
                raise OntologyDataBindingError(
                    f"属性数据绑定重复：{prop.domain}.{prop.name}"
                )
            existing_table = self._tables.setdefault(prop.domain, table)
            if existing_table != table:
                raise OntologyDataBindingError(
                    f"同一 ObjectType 映射到多个数据表：{prop.domain}"
                )
            existing_owner = table_owners.setdefault(table, prop.domain)
            if existing_owner != prop.domain:
                raise OntologyDataBindingError(
                    f"同一数据表映射到多个 ObjectType：{table}"
                )
            self._properties[key] = BoundOntologyProperty(
                object_type=prop.domain,
                property_name=prop.name,
                source_table=table,
                source_column=column,
                data_source=prop.data_source,
            )

        canonical = [
            {
                "object_type": item.object_type,
                "property": item.property_name,
                "table": item.source_table,
                "column": item.source_column,
                "data_source": item.data_source,
            }
            for item in sorted(
                self._properties.values(),
                key=lambda value: (value.object_type, value.property_name),
            )
        ]
        self.binding_digest = hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def property(self, object_type: str, property_name: str) -> BoundOntologyProperty:
        key = (str(object_type or "").strip(), str(property_name or "").strip())
        binding = self._properties.get(key)
        if binding is None:
            raise OntologyDataBindingError(
                f"本体属性缺少数据绑定：{key[0]}.{key[1]}"
            )
        return binding

    def require(self, requirements: dict[str, tuple[str, ...]]) -> None:
        for object_type, property_names in requirements.items():
            for property_name in property_names:
                self.property(object_type, property_name)

    def table_sql(self, object_type: str) -> str:
        normalized = str(object_type or "").strip()
        table = self._tables.get(normalized)
        if table is None:
            raise OntologyDataBindingError(
                f"ObjectType 缺少数据表绑定：{normalized}"
            )
        return f"{quote_identifier(self.schema)}.{quote_identifier(table)}"

    def column_sql(self, object_type: str, property_name: str, *, alias: str) -> str:
        binding = self.property(object_type, property_name)
        return f"{quote_identifier(alias)}.{quote_identifier(binding.source_column)}"

    def select_sql(self, object_type: str, property_name: str, *, alias: str) -> str:
        return (
            f"{self.column_sql(object_type, property_name, alias=alias)} "
            f"AS {quote_identifier(property_name)}"
        )
