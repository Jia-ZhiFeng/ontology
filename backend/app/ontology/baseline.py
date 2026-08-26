from .models import OntologyProposal, ObjectType, Property, LinkType, Interface, Function, ActionType, DynamicSecurity
from .scope import PERSONAL_CREDIT_SCOPE
from .attribute_catalog import PERSONAL_CREDIT_ATTRIBUTE_CATALOG
from .relation_catalog import PERSONAL_CREDIT_RELATION_CATALOG
from .interface_catalog import PERSONAL_CREDIT_INTERFACE_CATALOG
from .capability_catalog import PERSONAL_CREDIT_CAPABILITY_CATALOG, PERSONAL_CREDIT_SECURITY_CATALOG


def build_personal_credit_baseline():
    object_types=[
        ObjectType(name=item["name"],label=item["label"],description=item["purpose"])
        for item in PERSONAL_CREDIT_SCOPE["object_types"]
    ]
    properties=[]
    for domain,group in PERSONAL_CREDIT_ATTRIBUTE_CATALOG["objects"].items():
        for spec in group["properties"]:
            properties.append(Property(
                domain=domain,
                source_table=group["table"],
                source_column=spec["name"],
                description=f'{spec["label"]}；来源：{spec["data_source"]}',
                **spec,
            ))
    link_types=[LinkType(**item) for item in PERSONAL_CREDIT_RELATION_CATALOG["relations"]]
    interfaces=[Interface(**item) for item in PERSONAL_CREDIT_INTERFACE_CATALOG["interfaces"]]
    functions=[Function(**item) for item in PERSONAL_CREDIT_CAPABILITY_CATALOG["functions"]]
    action_types=[ActionType(**item) for item in PERSONAL_CREDIT_CAPABILITY_CATALOG["actions"]]
    dynamic_security=[DynamicSecurity(**item) for item in PERSONAL_CREDIT_SECURITY_CATALOG["policies"]]
    counts={
        "object_types":len(object_types), "properties":len(properties), "link_types":len(link_types),
        "interfaces":len(interfaces), "axioms":sum(len(item.constraints) for item in interfaces),
        "functions":len(functions), "action_types":len(action_types), "dynamic_security":len(dynamic_security),
    }
    return OntologyProposal(
        version="1.1.0",
        object_types=object_types,
        properties=properties,
        link_types=link_types,
        interfaces=interfaces,
        functions=functions,
        action_types=action_types,
        dynamic_security=dynamic_security,
        metadata={
            "name":"个人汽车信审本体", "status":"approved", "generation_method":"deterministic_catalog",
            "scope_version":PERSONAL_CREDIT_SCOPE["version"],
            "attribute_catalog_version":PERSONAL_CREDIT_ATTRIBUTE_CATALOG["version"],
            "relation_catalog_version":PERSONAL_CREDIT_RELATION_CATALOG["version"],
            "interface_catalog_version":PERSONAL_CREDIT_INTERFACE_CATALOG["version"],
            "capability_catalog_version":PERSONAL_CREDIT_CAPABILITY_CATALOG["version"],
            "security_catalog_version":PERSONAL_CREDIT_SECURITY_CATALOG["version"],
            "counts":counts,
            "excluded_database_fields":PERSONAL_CREDIT_ATTRIBUTE_CATALOG["excluded_database_fields"],
            "runtime_capabilities_enabled":False,
        },
    )
