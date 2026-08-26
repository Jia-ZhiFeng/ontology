from .models import OntologyProposal
def validate(p:OntologyProposal):
    e=[]; classes=[x.name for x in p.object_types]; cs=set(classes)
    if len(classes)!=len(cs): e.append("ObjectType 名称重复")
    for x in p.properties:
        if x.domain not in cs: e.append(f"Property domain 不存在: {x.domain}")
    for x in p.link_types:
        if x.source not in cs: e.append(f"LinkType source 不存在: {x.source}")
        if x.target not in cs: e.append(f"LinkType target 不存在: {x.target}")
        if x.direction not in {"directed","undirected"}: e.append(f"LinkType direction 非法: {x.name}")
        relationship_properties=[item.name for item in x.relationship_properties]
        if len(relationship_properties)!=len(set(relationship_properties)): e.append(f"LinkType 关系属性重复: {x.name}")
    link_names=[x.name for x in p.link_types]
    if len(link_names)!=len(set(link_names)): e.append("LinkType 名称重复")
    link_set=set(link_names); property_pairs={(x.domain,x.name) for x in p.properties}; axiom_ids=[]
    for interface in p.interfaces:
        for object_type in interface.implementing_types:
            if object_type not in cs: e.append(f"Interface 实现类型不存在: {interface.name}.{object_type}")
        for object_type,names in interface.required_properties.items():
            if object_type not in cs: e.append(f"Interface 属性所属类型不存在: {interface.name}.{object_type}")
            for name in names:
                if (object_type,name) not in property_pairs: e.append(f"Interface 必需属性不存在: {interface.name}.{object_type}.{name}")
        for name in interface.required_relations:
            if name not in link_set: e.append(f"Interface 必需关系不存在: {interface.name}.{name}")
        for axiom in interface.constraints:
            axiom_ids.append(axiom.id)
            if axiom.severity not in {"error","warning"}: e.append(f"公理严重程度非法: {axiom.id}")
    if len(axiom_ids)!=len(set(axiom_ids)): e.append("公理 ID 重复")
    action_names=[x.name for x in p.action_types]; actions=set(action_names)
    if len(action_names)!=len(actions): e.append("ActionType 名称重复")
    function_names=[x.name for x in p.functions]; functions=set(function_names)
    if len(function_names)!=len(functions): e.append("Function 名称重复")
    for function in p.functions:
        if function.side_effects: e.append(f"Function 不允许副作用: {function.name}")
        for object_type in function.input_types:
            if object_type not in cs: e.append(f"Function 输入类型不存在: {function.name}.{object_type}")
        for name in function.reads_relations:
            if name not in link_set: e.append(f"Function 读取关系不存在: {function.name}.{name}")
    for action in p.action_types:
        if not action.audit_required: e.append(f"ActionType 必须审计: {action.name}")
        if action.execution_mode!="declaration_only": e.append(f"ActionType 当前只能为声明态: {action.name}")
        for object_type in action.input_types+action.output_types:
            if object_type not in cs: e.append(f"ActionType 对象类型不存在: {action.name}.{object_type}")
        for name in action.invokes_functions:
            if name not in functions: e.append(f"ActionType 调用函数不存在: {action.name}.{name}")
    security_actions=[]
    for x in p.dynamic_security:
        security_actions.append(x.action)
        if x.action not in actions: e.append(f"Security action 不存在: {x.action}")
        if not x.audit_required: e.append(f"Security 必须审计: {x.action}")
    if len(security_actions)!=len(set(security_actions)): e.append("Security action 重复")
    if actions and set(security_actions)!=actions: e.append("每个 ActionType 必须且只能有一条 DynamicSecurity")
    return e
