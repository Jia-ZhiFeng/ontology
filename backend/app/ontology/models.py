from pydantic import BaseModel, Field
from typing import Optional, Any
class ObjectType(BaseModel):
    name:str; label:Optional[str]=None; description:Optional[str]=None
class Property(BaseModel):
    name:str; domain:str; range:str="string"; label:Optional[str]=None; description:Optional[str]=None
    required:bool=False; business_key:bool=False; required_when:Optional[str]=None
    source_table:Optional[str]=None; source_column:Optional[str]=None; data_source:Optional[str]=None
    enum_values:list[str]=Field(default_factory=list); unit:Optional[str]=None
class RelationshipProperty(BaseModel):
    name:str; label:Optional[str]=None; range:str="string"; source:Optional[str]=None
    required:bool=False; derived:bool=False; enum_values:list[str]=Field(default_factory=list)
class LinkType(BaseModel):
    name:str; source:str; target:str; label:Optional[str]=None; description:Optional[str]=None
    direction:str="directed"; cardinality:Optional[str]=None
    source_cardinality:Optional[str]=None; target_cardinality:Optional[str]=None
    source_columns:list[str]=Field(default_factory=list)
    relationship_properties:list[RelationshipProperty]=Field(default_factory=list)
    constraints:list[str]=Field(default_factory=list)
class Axiom(BaseModel):
    id:str; name:str; description:Optional[str]=None; expression:str
    applies_to:list[str]=Field(default_factory=list); severity:str="error"
    validation_phase:str="ontology_and_data_validation"
class Interface(BaseModel):
    name:str; label:Optional[str]=None; description:Optional[str]=None
    implementing_types:list[str]=Field(default_factory=list)
    required_properties:dict[str,list[str]]=Field(default_factory=dict)
    required_relations:list[str]=Field(default_factory=list)
    constraints:list[Axiom]=Field(default_factory=list)
    input:list[str]=Field(default_factory=list); output:list[str]=Field(default_factory=list)
class ActionType(BaseModel):
    name:str; label:Optional[str]=None; description:Optional[str]=None
    input_types:list[str]=Field(default_factory=list); output_types:list[str]=Field(default_factory=list)
    invokes_functions:list[str]=Field(default_factory=list); effects:list[str]=Field(default_factory=list)
    preconditions:list[str]=Field(default_factory=list); external_system:Optional[str]=None
    idempotency_key:Optional[str]=None; audit_required:bool=True; execution_mode:str="declaration_only"
class Function(BaseModel):
    name:str; label:Optional[str]=None; description:Optional[str]=None
    input_types:list[str]=Field(default_factory=list); reads_properties:list[str]=Field(default_factory=list)
    reads_relations:list[str]=Field(default_factory=list); returns:list[str]=Field(default_factory=list)
    deterministic:bool=True; side_effects:bool=False; implementation_status:str="not_bound"
    parameters:list[str]=Field(default_factory=list)
class DynamicSecurity(BaseModel):
    action:str; allowed_roles:list[str]=Field(default_factory=list); denied_roles:list[str]=Field(default_factory=list)
    approval_required:bool=False; execution_boundary:Optional[str]=None
    sensitive_fields:list[str]=Field(default_factory=list); conditions:list[str]=Field(default_factory=list)
    audit_required:bool=True
class OntologyProposal(BaseModel):
    version:str="0.1"
    object_types:list[ObjectType]=Field(default_factory=list)
    properties:list[Property]=Field(default_factory=list)
    link_types:list[LinkType]=Field(default_factory=list)
    interfaces:list[Interface]=Field(default_factory=list)
    action_types:list[ActionType]=Field(default_factory=list)
    functions:list[Function]=Field(default_factory=list)
    dynamic_security:list[DynamicSecurity]=Field(default_factory=list)
    metadata:dict[str,Any]=Field(default_factory=dict)
