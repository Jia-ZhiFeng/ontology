import json
import threading
import time
from nebula3.Config import Config
from nebula3.gclient.net import ConnectionPool
from ..ontology.mapping import (
    abox_vid,
    build_mapping_contract,
    derive_shared_contact_links,
    prepare_mapped_entities,
    relation_rank,
    required_tbox_vertices,
    type_relation,
)

def q(value):
    return json.dumps("" if value is None else str(value),ensure_ascii=False)


def edge_parts(edge):
    if len(edge)==3:
        src,dst,kind=edge
        return str(src),str(dst),0,str(kind)
    src,dst,rank,kind=edge
    return str(src),str(dst),int(rank),str(kind)


def stale_edge_pairs(existing_edges,desired_edges,managed):
    """Return stale ranked edge keys without deleting an overwritten replacement."""
    desired={edge_parts(edge) for edge in desired_edges}
    desired_keys={(src,dst,rank) for src,dst,rank,_ in desired}
    return sorted({
        (src,dst,rank)
        for edge in existing_edges
        for src,dst,rank,kind in [edge_parts(edge)]
        if managed(src,dst,kind)
        and (src,dst,rank,kind) not in desired
        and (src,dst,rank) not in desired_keys
    })


def tbox_graph_spec(proposal):
    items=[("Ontology","credit_ontology",{
        "version":proposal.version,
        "status":"published",
        "metadata":proposal.metadata,
    })]
    groups=[("ObjectType",proposal.object_types),("Property",proposal.properties),("LinkType",proposal.link_types),
            ("Interface",proposal.interfaces),("ActionType",proposal.action_types),("Function",proposal.functions),
            ("DynamicSecurity",proposal.dynamic_security)]
    for kind,values in groups:
        for value in values:
            data=value.model_dump(); name=data.get("name") or data.get("action")
            items.append((kind,name,data))

    def item_key(kind,name,data):
        return (kind,name,data.get("domain",""),data.get("source",""),data.get("target",""))
    def item_vid(kind,name,data):
        parts=[kind]
        if data.get("domain"): parts.append(str(data["domain"]))
        if data.get("source"): parts.append(str(data["source"]))
        parts.append(str(name))
        if data.get("target"): parts.append(str(data["target"]))
        return "tbox:"+":".join(parts)

    vids={item_key(kind,name,data):item_vid(kind,name,data) for kind,name,data in items}
    root=vids[item_key("Ontology","credit_ontology",items[0][2])]
    links=[]
    for kind,name,data in items[1:]: links.append((root,vids[item_key(kind,name,data)],"CONTAINS",{}))
    for prop in proposal.properties:
        source=vids.get(item_key("ObjectType",prop.domain,{})); target=vids.get(item_key("Property",prop.name,prop.model_dump()))
        if source and target: links.append((source,target,"HAS_PROPERTY",{}))
    for link in proposal.link_types:
        link_vid=vids.get(item_key("LinkType",link.name,link.model_dump())); source=vids.get(item_key("ObjectType",link.source,{})); target=vids.get(item_key("ObjectType",link.target,{}))
        if source and link_vid: links.append((source,link_vid,"HAS_LINK_TYPE",{}))
        if link_vid and target: links.append((link_vid,target,"TARGET",{}))
    return {
        "items":items,
        "vids":vids,
        "item_key":item_key,
        "links":links,
        "vertices":set(vids.values()),
        "edges":{(str(src),str(dst),0,str(kind)) for src,dst,kind,_ in links},
    }

class NebulaGraphService:
    _mutation_lock=threading.RLock()

    def __init__(self,host,port,user,password,space):
        from ..core.config import NEBULA_REQUEST_TIMEOUT_MS
        self.user=user; self.password=password; self.space=space
        self.pool=ConnectionPool()
        config=Config()
        config.timeout=NEBULA_REQUEST_TIMEOUT_MS
        if not self.pool.init([(host,port)],config):
            self.pool.close()
            raise RuntimeError("NebulaGraph connection pool initialization failed")
    def close(self): self.pool.close()
    def _run(self,statement,use_space=True):
        with self.pool.session_context(self.user,self.password) as session:
            if use_space:
                used=session.execute(f"USE `{self.space}`")
                if not used.is_succeeded(): raise RuntimeError(used.error_msg())
            result=session.execute(statement)
            if not result.is_succeeded(): raise RuntimeError(result.error_msg())
            return result
    def ping(self):
        return self._run("SHOW HOSTS",use_space=False).is_succeeded()
    def ensure_schema(self):
        self._run(f"CREATE SPACE IF NOT EXISTS `{self.space}`(vid_type=FIXED_STRING(128),partition_num=5,replica_factor=1)",False)
        # Meta changes are asynchronous; wait until the new space is visible to graphd.
        for attempt in range(20):
            try:
                self._run("YIELD 1")
                break
            except RuntimeError:
                if attempt==19: raise
                time.sleep(1)
        statements=[
            "CREATE TAG IF NOT EXISTS entity(kind string, props string)",
            "CREATE TAG IF NOT EXISTS ontology(kind string, name string, props string)",
            "CREATE EDGE IF NOT EXISTS related(kind string, props string)",
            "CREATE TAG INDEX IF NOT EXISTS entity_kind_idx ON entity(kind(64))",
        ]
        for statement in statements: self._run(statement)
    def _vertex_ids(self,tag):
        result=self._run(f"MATCH (v:`{tag}`) RETURN id(v) AS id")
        return [str(result.row_values(index)[0].cast()) for index in range(result.row_size())]
    def _edge_rows(self):
        result=self._run("MATCH (s)-[e]->(d) RETURN id(s) AS source,id(d) AS target,rank(e) AS rank,properties(e).kind AS kind")
        rows=[]
        for index in range(result.row_size()):
            values=result.row_values(index)
            rows.append((str(values[0].cast()),str(values[1].cast()),int(values[2].cast()),str(values[3].cast())))
        return rows
    def _delete_vertex_ids(self,ids):
        ids=sorted({str(vid) for vid in ids})
        for start in range(0,len(ids),100):
            values=",".join(q(vid) for vid in ids[start:start+100])
            if values: self._run(f"DELETE VERTEX {values} WITH EDGE")
        return len(ids)
    def _delete_vertices(self,tag):
        return self._delete_vertex_ids(self._vertex_ids(tag))
    def _delete_edge_pairs(self,pairs):
        keys=sorted({(str(src),str(dst),int(rank)) for src,dst,rank in pairs})
        for src,dst,rank in keys:
            self._run(f"DELETE EDGE related {q(src)}->{q(dst)}@{rank}")
        return len(keys)
    def _assert_graph_contains(self,tag,expected_vertices,expected_edges,attempts=10):
        expected_vertices={str(vid) for vid in expected_vertices}
        expected_edges={edge_parts(edge) for edge in expected_edges}
        missing_vertices=expected_vertices
        missing_edges=expected_edges
        for attempt in range(attempts):
            actual_vertices=set(self._vertex_ids(tag))
            actual_edges={edge_parts(edge) for edge in self._edge_rows()}
            missing_vertices=expected_vertices-actual_vertices
            missing_edges=expected_edges-actual_edges
            if not missing_vertices and not missing_edges:
                return
            if attempt<attempts-1: time.sleep(0.2)
        raise RuntimeError(
            "NebulaGraph 写入校验失败: "
            f"缺少顶点 {sorted(missing_vertices)}, 缺少关系 {sorted(missing_edges)}"
        )
    def write_tbox(self,p,audit_digest=None):
        spec=tbox_graph_spec(p); items=spec["items"]; vids=spec["vids"]; item_key=spec["item_key"]
        links=spec["links"]; desired_vertices=spec["vertices"]; desired_edges=spec["edges"]
        root_data=items[0][2]
        root_data["audit_digest"]=audit_digest
        with self._mutation_lock:
            self.ensure_schema()
            previous_vertices=set(self._vertex_ids("ontology"))
            previous_edges={edge_parts(edge) for edge in self._edge_rows()}

            # Upsert and verify the complete replacement before removing anything.
            # Existing ObjectType VIDs remain available, so INSTANCE_OF survives a
            # same-identity TBox publication.
            for kind,name,data in items:
                vid=vids[item_key(kind,name,data)]
                self._run(f"INSERT VERTEX ontology(kind,name,props) VALUES {q(vid)}:({q(kind)},{q(name)},{q(json.dumps(data,ensure_ascii=False,default=str))})")
            for src,dst,kind,data in links:
                self._run(f"INSERT EDGE related(kind,props) VALUES {q(src)}->{q(dst)}:({q(kind)},{q(json.dumps(data,ensure_ascii=False,default=str))})")
            self._assert_graph_contains("ontology",desired_vertices,desired_edges)

            stale_tbox_edges=stale_edge_pairs(
                previous_edges,
                desired_edges,
                lambda src,dst,kind: src in previous_vertices and dst in previous_vertices,
            )
            deleted_stale_tbox_edges=self._delete_edge_pairs(stale_tbox_edges)
            stale_vertices=previous_vertices-desired_vertices
            deleted_stale_tbox_vertices=self._delete_vertex_ids(stale_vertices)
            preserved_instance_of_edges=sum(
                kind=="INSTANCE_OF" and dst in desired_vertices
                for src,dst,rank,kind in previous_edges
            )
            return {
                "previous_tbox_vertices":len(previous_vertices),
                "deleted_previous_tbox_vertices":deleted_stale_tbox_vertices,
                "deleted_stale_tbox_vertices":deleted_stale_tbox_vertices,
                "deleted_stale_tbox_edges":deleted_stale_tbox_edges,
                "preserved_instance_of_edges":preserved_instance_of_edges,
                "ontology_vertices":len(items),
                "ontology_edges":len(links),
                "publication_mode":"upsert_verify_cleanup",
            }

    def abox_compatibility_report(self,proposal):
        """Check that replacing TBox will not remove vertices referenced by current ABox."""
        target_vertices=tbox_graph_spec(proposal)["vertices"]
        snapshot=self.snapshot(1000); stats=self.stats()
        entity_ids={str(node["id"]) for node in snapshot["nodes"] if "entity" in node.get("tags",[])}
        references=set()
        for node in snapshot["nodes"]:
            if str(node.get("id")) not in entity_ids: continue
            raw=(node.get("properties") or {}).get("props","{}")
            try: data=json.loads(raw) if isinstance(raw,str) else raw
            except Exception: data={}
            mapping=(data or {}).get("_ontology") or {}
            if mapping.get("object_type_vid"): references.add(str(mapping["object_type_vid"]))
            for item in (mapping.get("property_mappings") or {}).values():
                if item.get("property_vid"): references.add(str(item["property_vid"]))
        for edge in snapshot["edges"]:
            props=edge.get("properties") or {}; kind=props.get("kind") or props.get("related.kind")
            if kind=="INSTANCE_OF": references.add(str(edge.get("target")))
            raw=props.get("props") or props.get("related.props") or "{}"
            try: data=json.loads(raw) if isinstance(raw,str) else raw
            except Exception: data={}
            mapping=(data or {}).get("_ontology") or {}
            if mapping.get("link_type_vid"): references.add(str(mapping["link_type_vid"]))
        missing=sorted(references-target_vertices)
        complete=stats["vertices"]<=len(snapshot["nodes"]) and stats["edges"]<=len(snapshot["edges"])
        return {
            "valid":complete and not missing,
            "snapshot_complete":complete,
            "current_reference_count":len(references),
            "target_tbox_vertex_count":len(target_vertices),
            "missing_references":missing,
        }
    def sync_postgres_profile(self,profile,proposal):
        self.ensure_schema(); samples=profile.get("samples",{})
        contract=build_mapping_contract(proposal)
        entities,issues=prepare_mapped_entities(profile,contract)
        if issues: raise RuntimeError(f"ABox-TBox 映射准备失败: {json.dumps(issues,ensure_ascii=False)}")
        required_tbox=required_tbox_vertices(contract)
        links=[]
        for row in samples.get("loan_application",[]):
            context={"submit_date":row.get("submit_date"),"channel":row.get("channel")}
            if row.get("borrower_id"): links.append((row["borrower_id"],row["application_id"],"SUBMITS",{**context,"role":"借款人"}))
            if row.get("guarantor_id"): links.append((row["guarantor_id"],row["application_id"],"GUARANTEES",{"role":"担保人"}))
            if row.get("co_borrower_id"): links.append((row["co_borrower_id"],row["application_id"],"CO_BORROWS",{"role":"共借人"}))
            if row.get("vehicle_vin"): links.append((row["application_id"],row["vehicle_vin"],"TARGETS",{}))
            if row.get("dealer_credit_code"): links.append((row["application_id"],row["dealer_credit_code"],"SOLD_BY",{}))
        for row in samples.get("credit_report",[]):
            if row.get("subject_id_number"): links.append((row["subject_id_number"],row["report_id"],"HOLDS",{"query_time":row.get("query_time"),"reporting_agency":row.get("reporting_agency")}))
        for row in samples.get("external_debt",[]):
            if row.get("borrower_id"): links.append((row["borrower_id"],row["debt_id"],"OWES",{"start_date":row.get("start_date"),"overdue_status":row.get("overdue_status")}))
        for row in samples.get("risk_assessment_report",[]):
            if row.get("application_id"): links.append((row["application_id"],row["assessment_id"],"GENERATES",{"assessment_time":row.get("assessment_time")}))
        for row in samples.get("anti_fraud_report",[]):
            if row.get("application_id"): links.append((row["application_id"],row["fraud_report_id"],"HAS_FRAUD_REPORT",{}))
        for row in samples.get("phone_verification_record",[]):
            if row.get("application_id"): links.append((row["application_id"],row["verification_id"],"HAS_PHONE_VERIFICATION",{}))
        links.extend(derive_shared_contact_links(samples.get("natural_person",[])))
        entity_types={entity["vid"]:entity["object_type"] for entity in entities}
        entity_by_business_key={
            (entity["object_type"],entity["business_id"]):entity["vid"]
            for entity in entities
        }
        typed_links=[]
        for raw_src,raw_dst,kind,data in links:
            relation_spec=contract["relations"].get(kind)
            if not relation_spec:
                raise RuntimeError(f"ABox 关系没有对应 LinkType: {kind}")
            src=entity_by_business_key.get((relation_spec["source"],str(raw_src)))
            dst=entity_by_business_key.get((relation_spec["target"],str(raw_dst)))
            if not src or not dst:
                raise RuntimeError(f"ABox 关系端点未同步: {kind} {raw_src}->{raw_dst}")
            actual_types=(entity_types[src],entity_types[dst])
            expected_types=(relation_spec["source"],relation_spec["target"])
            if actual_types!=expected_types:
                raise RuntimeError(
                    f"ABox 关系端点类型错误: {kind} {src}->{dst}, "
                    f"实际 {actual_types}, 预期 {expected_types}"
                )
            typed_links.append((src,dst,relation_rank(kind),kind,type_relation(kind,data,contract)))

        desired_vertices={str(entity["vid"]) for entity in entities}
        desired_business_edges={(src,dst,rank,kind) for src,dst,rank,kind,_ in typed_links}
        desired_instance_edges={
            (str(entity["vid"]),str(entity["object_type_vid"]),relation_rank("INSTANCE_OF"),"INSTANCE_OF")
            for entity in entities
        }
        desired_edges=desired_business_edges|desired_instance_edges

        with self._mutation_lock:
            actual_tbox=set(self._vertex_ids("ontology"))
            missing_tbox=sorted(required_tbox-actual_tbox)
            if missing_tbox: raise RuntimeError(f"NebulaGraph 缺少映射所需 TBox 节点: {missing_tbox}")
            previous_vertices=set(self._vertex_ids("entity"))
            previous_edges={edge_parts(edge) for edge in self._edge_rows()}

            # Write and verify the complete new ABox first. If any INSERT or
            # verification fails, cleanup is never reached and the old ABox stays
            # queryable (possibly alongside harmless upserts/new rows).
            for entity in entities:
                props=json.dumps(entity["data"],ensure_ascii=False,default=str)
                self._run(f"INSERT VERTEX entity(kind,props) VALUES {q(entity['vid'])}:({q(entity['table'])},{q(props)})")
            for src,dst,rank,kind,typed in typed_links:
                self._run(f"INSERT EDGE related(kind,props) VALUES {q(src)}->{q(dst)}@{rank}:({q(kind)},{q(json.dumps(typed,ensure_ascii=False,default=str))})")
            for entity in entities:
                mapping={
                    "mapping_version":contract["mapping_version"],
                    "tbox_version":contract["tbox_version"],
                    "object_type":entity["object_type"],
                    "vid_scheme":contract["vid_scheme"],
                    "edge_rank_scheme":contract["edge_rank_scheme"],
                }
                rank=relation_rank("INSTANCE_OF")
                self._run(f"INSERT EDGE related(kind,props) VALUES {q(entity['vid'])}->{q(entity['object_type_vid'])}@{rank}:({q('INSTANCE_OF')},{q(json.dumps(mapping,ensure_ascii=False))})")
            self._assert_graph_contains("entity",desired_vertices,desired_edges)

            stale_abox_edges=stale_edge_pairs(
                previous_edges,
                desired_edges,
                lambda src,dst,kind: (
                    (src in previous_vertices and dst in previous_vertices)
                    or (src in previous_vertices and kind=="INSTANCE_OF")
                ),
            )
            deleted_stale_abox_edges=self._delete_edge_pairs(stale_abox_edges)
            stale_vertices=previous_vertices-desired_vertices
            deleted_stale_abox_vertices=self._delete_vertex_ids(stale_vertices)
            return {
                "mapping_version":contract["mapping_version"],
                "tbox_version":contract["tbox_version"],
                "vid_scheme":contract["vid_scheme"],
                "edge_rank_scheme":contract["edge_rank_scheme"],
                "previous_abox_vertices":len(previous_vertices),
                "deleted_previous_abox_vertices":deleted_stale_abox_vertices,
                "deleted_stale_abox_vertices":deleted_stale_abox_vertices,
                "deleted_stale_abox_edges":deleted_stale_abox_edges,
                "entity_count":len(entities),
                "instance_of_count":len(entities),
                "business_relation_count":len(typed_links),
                "derived_relation_counts":{
                    "SHARES_CONTACT":sum(kind=="SHARES_CONTACT" for _,_,_,kind,_ in typed_links),
                },
                "tables":list(contract["table_objects"]),
                "synchronization_mode":"upsert_verify_cleanup",
            }

    def mapping_report(self,proposal):
        contract=build_mapping_contract(proposal); snapshot=self.snapshot(1000)
        ontology_nodes=[node for node in snapshot["nodes"] if "ontology" in node["tags"]]
        entity_nodes=[node for node in snapshot["nodes"] if "entity" in node["tags"]]
        ontology_ids={str(node["id"]) for node in ontology_nodes}; entity_ids={str(node["id"]) for node in entity_nodes}
        edge_rows=[]
        for edge in snapshot["edges"]:
            props=edge.get("properties") or {}; kind=props.get("kind") or props.get("related.kind")
            raw=props.get("props") or props.get("related.props") or "{}"
            try: data=json.loads(raw) if isinstance(raw,str) else raw
            except Exception: data={}
            edge_rows.append({**edge,"kind":kind,"data":data})
        instance_edges={(str(edge["source"]),str(edge["target"])) for edge in edge_rows if edge["kind"]=="INSTANCE_OF"}
        missing_instance_of=[]; unmapped_entities=[]; unmapped_fields=[]; missing_tbox_references=[]
        mismatched_entity_tbox_versions=[]; mismatched_relation_tbox_versions=[]
        invalid_entity_vids=[]; invalid_edge_ranks=[]; mismatched_vid_schemes=[]; mismatched_edge_rank_schemes=[]
        mapped_field_entries=0; mapped_null_entries=0; excluded_field_entries=0; object_type_counts={}
        for node in entity_nodes:
            raw=(node.get("properties") or {}).get("props","{}")
            try: data=json.loads(raw) if isinstance(raw,str) else raw
            except Exception: data={}
            mapping=data.get("_ontology") or {}; object_type=mapping.get("object_type"); object_vid=mapping.get("object_type_vid")
            if not object_type or not object_vid:
                unmapped_entities.append(str(node["id"])); continue
            if mapping.get("tbox_version")!=proposal.version:
                mismatched_entity_tbox_versions.append(str(node["id"]))
            business_id=mapping.get("business_id")
            expected_vid=abox_vid(object_type,business_id) if business_id not in (None,"") else None
            if not expected_vid or str(node["id"])!=expected_vid:
                invalid_entity_vids.append(str(node["id"]))
            if mapping.get("vid_scheme")!=contract["vid_scheme"]:
                mismatched_vid_schemes.append(str(node["id"]))
            object_type_counts[object_type]=object_type_counts.get(object_type,0)+1
            if object_vid not in ontology_ids: missing_tbox_references.append(object_vid)
            if (str(node["id"]),str(object_vid)) not in instance_edges: missing_instance_of.append(str(node["id"]))
            property_map=mapping.get("property_mappings") or {}
            mapped_field_entries+=len(property_map)
            mapped_null_entries+=sum(item.get("status")=="mapped_null" for item in property_map.values())
            excluded_field_entries+=len(mapping.get("excluded_fields") or [])
            for column,item in property_map.items():
                property_ref=item.get("property_vid")
                if property_ref not in ontology_ids: missing_tbox_references.append(property_ref)
            for column in mapping.get("unmapped_fields") or []:
                unmapped_fields.append(f"{mapping.get('source_table')}.{column}")
        business_edges=[edge for edge in edge_rows if str(edge["source"]) in entity_ids and str(edge["target"]) in entity_ids and edge["kind"]!="INSTANCE_OF"]
        unmapped_relations=[]; relation_type_counts={}
        abox_edges=[edge for edge in edge_rows if str(edge["source"]) in entity_ids]
        for edge in abox_edges:
            if int(edge.get("rank",0))!=relation_rank(edge["kind"]):
                invalid_edge_ranks.append(f"{edge['source']}-[{edge['kind']}@{edge.get('rank',0)}]->{edge['target']}")
            edge_payload=edge["data"] or {}
            edge_mapping=edge_payload.get("_ontology") or edge_payload
            if edge_mapping.get("edge_rank_scheme")!=contract["edge_rank_scheme"]:
                mismatched_edge_rank_schemes.append(
                    f"{edge['source']}-[{edge['kind']}@{edge.get('rank',0)}]->{edge['target']}"
                )
        for edge in business_edges:
            mapping=(edge["data"] or {}).get("_ontology") or {}; kind=edge["kind"]
            relation_type_counts[kind]=relation_type_counts.get(kind,0)+1
            if mapping.get("tbox_version")!=proposal.version:
                mismatched_relation_tbox_versions.append(f"{edge['source']}-[{kind}]->{edge['target']}")
            relation_ref=mapping.get("link_type_vid")
            if mapping.get("link_type")!=kind or not relation_ref:
                unmapped_relations.append(f"{edge['source']}-[{kind}]->{edge['target']}")
            elif relation_ref not in ontology_ids: missing_tbox_references.append(relation_ref)
        expected_types=set(contract["relations"]); actual_types=set(relation_type_counts)
        root_version=None
        for node in ontology_nodes:
            if (node.get("properties") or {}).get("kind")=="Ontology":
                raw=(node.get("properties") or {}).get("props","{}")
                try: root_version=(json.loads(raw) if isinstance(raw,str) else raw).get("version")
                except Exception: pass
        errors={
            "unmapped_entities":sorted(set(unmapped_entities)),
            "missing_instance_of":sorted(set(missing_instance_of)),
            "unmapped_fields":sorted(set(unmapped_fields)),
            "unmapped_relations":sorted(set(unmapped_relations)),
            "missing_tbox_references":sorted(set(missing_tbox_references)),
            "mismatched_entity_tbox_versions":sorted(set(mismatched_entity_tbox_versions)),
            "mismatched_relation_tbox_versions":sorted(set(mismatched_relation_tbox_versions)),
            "invalid_entity_vids":sorted(set(invalid_entity_vids)),
            "invalid_edge_ranks":sorted(set(invalid_edge_ranks)),
            "mismatched_vid_schemes":sorted(set(mismatched_vid_schemes)),
            "mismatched_edge_rank_schemes":sorted(set(mismatched_edge_rank_schemes)),
        }
        valid=root_version==proposal.version and not any(errors.values())
        return {
            "valid":valid,
            "status":"mapped" if valid else "mapping_incomplete",
            "mapping_version":contract["mapping_version"],
            "vid_scheme":contract["vid_scheme"],
            "edge_rank_scheme":contract["edge_rank_scheme"],
            "tbox_version":root_version,
            "expected_tbox_version":proposal.version,
            "summary":{
                "entity_total":len(entity_nodes),
                "entity_mapped":len(entity_nodes)-len(set(unmapped_entities)),
                "instance_of_edges":len(instance_edges),
                "mapped_field_entries":mapped_field_entries,
                "mapped_null_entries":mapped_null_entries,
                "intentional_exclusion_entries":excluded_field_entries,
                "business_relation_total":len(business_edges),
                "business_relation_mapped":len(business_edges)-len(set(unmapped_relations)),
                "typed_vid_entities":len(entity_nodes)-len(set(invalid_entity_vids)),
                "ranked_abox_edges":len(abox_edges)-len(set(invalid_edge_ranks)),
                "object_types_with_instances":len(object_type_counts),
                "link_types_with_instances":len(actual_types),
            },
            "object_type_counts":dict(sorted(object_type_counts.items())),
            "relation_type_counts":dict(sorted(relation_type_counts.items())),
            "link_types_without_instances":sorted(expected_types-actual_types),
            "errors":errors,
        }
    def related(self,node_id,hops=1):
        hops=max(1,min(int(hops),3))
        resolved=self.resolve_node_id(node_id)
        result=self._run(f"GO {hops} STEPS FROM {q(resolved)} OVER related BIDIRECT YIELD src(edge) AS src,dst(edge) AS dst,rank(edge) AS rank,properties(edge).kind AS kind")
        rows=[]
        # rows() exposes raw Thrift Value objects. row_values() returns
        # ValueWrapper instances, whose cast() method is the public API.
        for index in range(result.row_size()):
            values=result.row_values(index)
            rows.append({"src":values[0].cast(),"dst":values[1].cast(),"rank":int(values[2].cast()),"kind":values[3].cast()})
        return rows
    def stats(self):
        vertices=self._run("MATCH (v) RETURN count(v) AS count").row_values(0)[0].as_int()
        edges=self._run("MATCH ()-[e]->() RETURN count(e) AS count").row_values(0)[0].as_int()
        return {"vertices":vertices,"edges":edges}

    @staticmethod
    def _nodes_from_result(result):
        nodes=[]
        for index in range(result.row_size()):
            values=result.row_values(index)
            nodes.append({"id":values[0].cast(),"tags":values[1].cast(),"properties":values[2].cast()})
        return nodes

    @staticmethod
    def _edges_from_result(result):
        edges=[]
        for index in range(result.row_size()):
            values=result.row_values(index)
            edges.append({
                "source":values[0].cast(),"target":values[1].cast(),
                "type":values[2].cast(),"rank":int(values[3].cast()),
                "properties":values[4].cast(),
            })
        return edges

    def _count_tag_nodes(self,tag,where=""):
        statement=f"MATCH (v:`{tag}`)"
        if where: statement+=f" WHERE {where}"
        result=self._run(statement+" RETURN count(v) AS count")
        return result.row_values(0)[0].as_int()

    def _fetch_tag_nodes(self,tag,where="",limit=300,offset=0):
        statement=f"MATCH (v:`{tag}`)"
        if where: statement+=f" WHERE {where}"
        statement+=(
            " RETURN id(v) AS id,labels(v) AS labels,properties(v) AS props"
            f" ORDER BY id SKIP {int(offset)} LIMIT {int(limit)}"
        )
        return self._nodes_from_result(self._run(statement))

    def _paged_tag_nodes(self,where_by_tag,limit,offset):
        counts={tag:self._count_tag_nodes(tag,where) for tag,where in where_by_tag.items()}
        remaining=limit; local_offset=offset; nodes=[]
        for tag,where in where_by_tag.items():
            tag_count=counts[tag]
            if local_offset>=tag_count:
                local_offset-=tag_count
                continue
            take=min(remaining,tag_count-local_offset)
            nodes.extend(self._fetch_tag_nodes(tag,where,take,local_offset))
            remaining-=take; local_offset=0
            if remaining<=0: break
        return nodes,sum(counts.values())

    @staticmethod
    def _mode_node_filters(mode):
        if mode=="entity": return {"entity":""}
        if mode=="ontology": return {"ontology":""}
        if mode=="mapping": return {"entity":"","ontology":""}
        return {"entity":"","ontology":""}

    def _all_nodes_page(self,limit,offset,mode):
        if mode=="mapping":
            entity_count=self._count_tag_nodes("entity")
            object_types=[
                node for node in self._fetch_tag_nodes("ontology","",10000,0)
                if (node.get("properties") or {}).get("kind")=="ObjectType"
            ]
            total=entity_count+len(object_types); nodes=[]; remaining=limit
            if offset<entity_count:
                take=min(remaining,entity_count-offset)
                nodes.extend(self._fetch_tag_nodes("entity","",take,offset)); remaining-=take
                ontology_offset=0
            else:
                ontology_offset=offset-entity_count
            if remaining>0: nodes.extend(object_types[ontology_offset:ontology_offset+remaining])
            return nodes,total
        return self._paged_tag_nodes(self._mode_node_filters(mode),limit,offset)

    @staticmethod
    def _search_where(tag,term):
        variants=[]
        for value in (term,term.lower(),term.upper()):
            if value not in variants: variants.append(value)
        fields=[f"v.{tag}.kind",f"v.{tag}.props"]
        if tag=="ontology": fields.append("v.ontology.name")
        return " OR ".join(
            f"{field} CONTAINS {q(value)}"
            for value in variants
            for field in ["id(v)",*fields]
        )

    @staticmethod
    def _entity_business_id(node):
        raw=(node.get("properties") or {}).get("props","{}")
        try: data=json.loads(raw) if isinstance(raw,str) else raw
        except Exception: return None
        return str(((data or {}).get("_ontology") or {}).get("business_id") or "") or None

    def _entity_business_id_matches(self,term,limit=1000):
        candidates=self._fetch_tag_nodes("entity",self._search_where("entity",term),limit,0)
        return [node for node in candidates if self._entity_business_id(node)==str(term)]

    def resolve_node_id(self,node_id):
        value=str(node_id)
        if value.startswith(("abox:","tbox:")): return value
        matches=self._entity_business_id_matches(value)
        if len(matches)==1: return str(matches[0]["id"])
        if len(matches)>1:
            raise ValueError("业务编号对应多个本体类型，请使用完整类型化 VID")
        return value

    def _matching_nodes_page(self,term,limit,offset,mode):
        exact=" OR ".join(f"id(v)=={q(value)}" for value in dict.fromkeys((term,term.lower(),term.upper())))
        include_entity=mode in {"all","entity","mapping"}
        include_ontology=mode in {"all","ontology","mapping"}
        ontology_nodes=self._fetch_tag_nodes("ontology","",10000,0) if include_ontology else []
        if mode=="mapping":
            ontology_nodes=[node for node in ontology_nodes if (node.get("properties") or {}).get("kind")=="ObjectType"]

        def ontology_matches(exact_only):
            lowered=term.lower(); matches=[]
            for node in ontology_nodes:
                identifier=str(node.get("id",""))
                if exact_only:
                    matched=identifier.lower()==lowered
                else:
                    matched=lowered in json.dumps(node,ensure_ascii=False,default=str).lower()
                if matched: matches.append(node)
            return matches

        def partition(entity_where,ontology_values):
            entity_total=self._count_tag_nodes("entity",entity_where) if include_entity else 0
            total=entity_total+len(ontology_values); nodes=[]; remaining=limit
            if offset<entity_total:
                take=min(remaining,entity_total-offset)
                nodes.extend(self._fetch_tag_nodes("entity",entity_where,take,offset)); remaining-=take
                ontology_offset=0
            else:
                ontology_offset=offset-entity_total
            if remaining>0: nodes.extend(ontology_values[ontology_offset:ontology_offset+remaining])
            return nodes,total

        exact_ontology=ontology_matches(True)
        exact_entity=[]
        if include_entity:
            direct=self._fetch_tag_nodes("entity",exact,1000,0)
            exact_entity=direct+self._entity_business_id_matches(term)
            exact_entity=list({str(node["id"]):node for node in exact_entity}.values())
        exact_values=exact_entity+exact_ontology
        if exact_values:
            return exact_values[offset:offset+limit],len(exact_values),True
        search=self._search_where("entity",term) if include_entity else ""
        nodes,total=partition(search,ontology_matches(False))
        return nodes,total,False

    def _nodes_by_ids(self,ids):
        ids=sorted({str(value) for value in ids}); nodes=[]
        for start in range(0,len(ids),100):
            values=",".join(q(value) for value in ids[start:start+100])
            if not values: continue
            result=self._run(
                "MATCH (v) "
                f"WHERE id(v) IN [{values}] "
                "RETURN id(v) AS id,labels(v) AS labels,properties(v) AS props ORDER BY id"
            )
            nodes.extend(self._nodes_from_result(result))
        return nodes

    @staticmethod
    def _mode_edge_condition(mode):
        if mode in {"entity","ontology"}: return f"properties(e).kind!={q('INSTANCE_OF')}"
        if mode=="mapping": return f"properties(e).kind=={q('INSTANCE_OF')}"
        return ""

    def _edges_touching(self,ids,limit,mode):
        values=",".join(q(value) for value in sorted({str(value) for value in ids}))
        if not values: return []
        where=f"(id(s) IN [{values}] OR id(d) IN [{values}])"
        edge_condition=self._mode_edge_condition(mode)
        if edge_condition: where+=f" AND {edge_condition}"
        result=self._run(
            "MATCH (s)-[e:related]->(d) "
            f"WHERE {where} "
            "RETURN id(s) AS source,id(d) AS target,type(e) AS type,rank(e) AS edge_rank,properties(e) AS props "
            f"ORDER BY source,target,edge_rank LIMIT {int(limit)}"
        )
        return self._edges_from_result(result)

    def _edges_between(self,ids,limit,mode):
        values=",".join(q(value) for value in sorted({str(value) for value in ids}))
        if not values: return [],0
        where=f"id(s) IN [{values}] AND id(d) IN [{values}]"
        edge_condition=self._mode_edge_condition(mode)
        if edge_condition: where+=f" AND {edge_condition}"
        count=self._run(f"MATCH (s)-[e:related]->(d) WHERE {where} RETURN count(e) AS count").row_values(0)[0].as_int()
        result=self._run(
            f"MATCH (s)-[e:related]->(d) WHERE {where} "
            "RETURN id(s) AS source,id(d) AS target,type(e) AS type,rank(e) AS edge_rank,properties(e) AS props "
            f"ORDER BY source,target,edge_rank LIMIT {int(limit)}"
        )
        return self._edges_from_result(result),count

    def _count_mode_edges(self,mode):
        if mode=="entity": statement="MATCH (s:entity)-[e:related]->(d:entity)"
        elif mode=="ontology": statement="MATCH (s:ontology)-[e:related]->(d:ontology)"
        elif mode=="mapping": statement="MATCH (s:entity)-[e:related]->(d:ontology)"
        else: statement="MATCH (s)-[e:related]->(d)"
        edge_condition=self._mode_edge_condition(mode)
        if edge_condition: statement+=f" WHERE {edge_condition}"
        return self._run(statement+" RETURN count(e) AS count").row_values(0)[0].as_int()

    def _full_graph_page(self,limit,offset,mode):
        nodes,total_nodes=self._all_nodes_page(limit,offset,mode)
        ids={str(node["id"]) for node in nodes}
        edge_limit=min(max(limit*6,100),5000)
        edges,page_edge_total=self._edges_between(ids,edge_limit,mode)
        total_edges=self._count_mode_edges(mode)
        has_previous=offset>0; has_next=offset+len(nodes)<total_nodes
        return {
            "nodes":nodes,"edges":edges,"scope":"full_graph_page",
            "query":{"applied":False,"hops":None,"mode":mode,"matched_total":None,"exact_match":False},
            "page":{
                "offset":offset,"limit":limit,"node_total":total_nodes,"node_returned":len(nodes),
                "edge_total":total_edges,"page_edge_total":page_edge_total,"edge_returned":len(edges),
                "has_previous":has_previous,"has_next":has_next,
                "previous_offset":max(0,offset-limit) if has_previous else None,
                "next_offset":offset+len(nodes) if has_next else None,
                "node_truncated":has_previous or has_next,
                "edge_truncated":page_edge_total>len(edges) or has_previous or has_next,
                "result_truncated":has_previous or has_next or page_edge_total>len(edges),
            },
        }

    def query_graph(self,query="",hops=2,limit=300,offset=0,mode="all"):
        term=str(query or "").strip()[:200]
        hops=max(1,min(int(hops),3)); limit=max(1,min(int(limit),500)); offset=max(0,min(int(offset),1000000))
        mode=mode if mode in {"all","entity","ontology","mapping"} else "all"
        if not term:
            return self._full_graph_page(limit,offset,mode)

        seed_limit=min(limit,100)
        seeds,matched_total,exact=self._matching_nodes_page(term,seed_limit,offset,mode)
        seed_ids={str(node["id"]) for node in seeds}; visited=set(seed_ids); frontier=set(seed_ids)
        edge_budget=min(max(limit*6,100),2000); expansion_truncated=False
        for _ in range(hops):
            if not frontier: break
            touching=self._edges_touching(frontier,edge_budget+1,mode)
            if len(touching)>edge_budget:
                touching=touching[:edge_budget]; expansion_truncated=True
            next_frontier=set()
            for edge in touching:
                for endpoint in (str(edge["source"]),str(edge["target"])):
                    if endpoint in visited: continue
                    if len(visited)>=limit:
                        expansion_truncated=True
                        continue
                    visited.add(endpoint); next_frontier.add(endpoint)
            frontier=next_frontier

        nodes=self._nodes_by_ids(visited)
        for node in nodes: node["matched"]=str(node["id"]) in seed_ids
        ids={str(node["id"]) for node in nodes}
        edges,page_edge_total=self._edges_between(ids,edge_budget,mode)
        has_previous=offset>0; has_next=offset+len(seeds)<matched_total
        edge_truncated=page_edge_total>len(edges)
        return {
            "nodes":nodes,"edges":edges,"scope":"search_neighborhood",
            "query":{
                "applied":True,"hops":hops,"mode":mode,"matched_total":matched_total,
                "matched_returned":len(seeds),"exact_match":exact,
            },
            "page":{
                "offset":offset,"limit":limit,"node_total":None,"node_returned":len(nodes),
                "edge_total":None,"page_edge_total":page_edge_total,"edge_returned":len(edges),
                "has_previous":has_previous,"has_next":has_next,
                "previous_offset":max(0,offset-seed_limit) if has_previous else None,
                "next_offset":offset+len(seeds) if has_next else None,
                "node_truncated":expansion_truncated or has_previous or has_next,
                "edge_truncated":edge_truncated,
                "result_truncated":expansion_truncated or has_previous or has_next or edge_truncated,
            },
        }

    def snapshot(self,limit=300,offset=0):
        # Internal mapping and compatibility checks may request a larger page.
        limit=max(1,min(int(limit),5000)); offset=max(0,int(offset))
        return self._full_graph_page(limit,offset,"all")
