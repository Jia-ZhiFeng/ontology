import json
import sys
import types
import unittest
from pathlib import Path


config_module=types.ModuleType("nebula3.Config")
config_module.Config=type("Config",(),{})
net_module=types.ModuleType("nebula3.gclient.net")
net_module.ConnectionPool=type("ConnectionPool",(),{})
sys.modules.setdefault("nebula3",types.ModuleType("nebula3"))
sys.modules.setdefault("nebula3.Config",config_module)
sys.modules.setdefault("nebula3.gclient",types.ModuleType("nebula3.gclient"))
sys.modules.setdefault("nebula3.gclient.net",net_module)

BACKEND=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BACKEND))

from app.kg.nebula import NebulaGraphService


class InMemoryQueryNebula(NebulaGraphService):
    def __init__(self):
        self.nodes={
            "A":{"id":"A","tags":["entity"],"properties":{"kind":"loan_application","props":json.dumps({"application_id":"APP-A","channel":"线上H5","_ontology":{"business_id":"APP-A"}})}},
            "B":{"id":"B","tags":["entity"],"properties":{"kind":"natural_person","props":json.dumps({"name":"张伟"},ensure_ascii=False)}},
            "C":{"id":"C","tags":["entity"],"properties":{"kind":"credit_report","props":json.dumps({"report_id":"CR-C"})}},
            "D":{"id":"D","tags":["entity"],"properties":{"kind":"dealer","props":json.dumps({"dealer_name":"示例经销商"},ensure_ascii=False)}},
            "tbox:ObjectType:LoanApplication":{"id":"tbox:ObjectType:LoanApplication","tags":["ontology"],"properties":{"kind":"ObjectType","name":"LoanApplication","props":"{}"}},
            "tbox:Property:channel":{"id":"tbox:Property:channel","tags":["ontology"],"properties":{"kind":"Property","name":"channel","props":"{}"}},
        }
        self.edges=[
            {"source":"A","target":"B","type":"related","properties":{"kind":"SUBMITS"}},
            {"source":"B","target":"C","type":"related","properties":{"kind":"HOLDS"}},
            {"source":"C","target":"D","type":"related","properties":{"kind":"OWES"}},
            {"source":"A","target":"tbox:ObjectType:LoanApplication","type":"related","properties":{"kind":"INSTANCE_OF"}},
            {"source":"tbox:ObjectType:LoanApplication","target":"tbox:Property:channel","type":"related","properties":{"kind":"HAS_PROPERTY"}},
        ]

    @staticmethod
    def _kind(edge):
        return edge["properties"]["kind"]

    def _allowed_nodes(self,mode):
        values=list(self.nodes.values())
        if mode=="entity": return [node for node in values if "entity" in node["tags"]]
        if mode=="ontology": return [node for node in values if "ontology" in node["tags"]]
        if mode=="mapping":
            return [node for node in values if "entity" in node["tags"] or node["properties"].get("kind")=="ObjectType"]
        return values

    def _allowed_edges(self,mode):
        node_ids={node["id"] for node in self._allowed_nodes(mode)}
        edges=[edge for edge in self.edges if edge["source"] in node_ids and edge["target"] in node_ids]
        if mode=="mapping": return [edge for edge in edges if self._kind(edge)=="INSTANCE_OF"]
        if mode in {"entity","ontology"}: return [edge for edge in edges if self._kind(edge)!="INSTANCE_OF"]
        return edges

    def _all_nodes_page(self,limit,offset,mode):
        nodes=sorted(self._allowed_nodes(mode),key=lambda node:node["id"])
        return nodes[offset:offset+limit],len(nodes)

    def _matching_nodes_page(self,term,limit,offset,mode):
        nodes=self._allowed_nodes(mode); lowered=term.lower()
        exact=[node for node in nodes if node["id"].lower()==lowered]
        if mode in {"all","entity","mapping"}:
            exact.extend(self._entity_business_id_matches(term))
            exact=list({node["id"]:node for node in exact}.values())
        matches=exact or [node for node in nodes if lowered in json.dumps(node,ensure_ascii=False).lower()]
        matches=sorted(matches,key=lambda node:node["id"])
        return matches[offset:offset+limit],len(matches),bool(exact)

    def _entity_business_id_matches(self,term,limit=1000):
        matches=[node for node in self._allowed_nodes("entity") if self._entity_business_id(node)==str(term)]
        return matches[:limit]

    def _nodes_by_ids(self,ids):
        return [self.nodes[node_id].copy() for node_id in sorted(ids) if node_id in self.nodes]

    def _edges_touching(self,ids,limit,mode):
        ids=set(ids)
        return [edge.copy() for edge in self._allowed_edges(mode) if edge["source"] in ids or edge["target"] in ids][:limit]

    def _edges_between(self,ids,limit,mode):
        ids=set(ids)
        edges=[edge.copy() for edge in self._allowed_edges(mode) if edge["source"] in ids and edge["target"] in ids]
        return edges[:limit],len(edges)

    def _count_mode_edges(self,mode):
        return len(self._allowed_edges(mode))


class NebulaQueryTests(unittest.TestCase):
    def setUp(self):
        self.service=InMemoryQueryNebula()

    def test_empty_query_returns_endpoint_safe_page_with_totals(self):
        result=self.service.query_graph("",limit=2,offset=0,mode="all")
        node_ids={node["id"] for node in result["nodes"]}
        self.assertEqual(result["scope"],"full_graph_page")
        self.assertEqual(result["page"]["node_total"],6)
        self.assertEqual(result["page"]["edge_total"],5)
        self.assertTrue(result["page"]["has_next"])
        self.assertTrue(all(edge["source"] in node_ids and edge["target"] in node_ids for edge in result["edges"]))

    def test_exact_search_expands_requested_hops(self):
        result=self.service.query_graph("A",hops=2,limit=10,mode="entity")
        self.assertTrue(result["query"]["exact_match"])
        self.assertEqual({node["id"] for node in result["nodes"]},{"A","B","C"})
        self.assertEqual({edge["properties"]["kind"] for edge in result["edges"]},{"SUBMITS","HOLDS"})

    def test_property_search_is_server_side_seed_selection(self):
        result=self.service.query_graph("application_id",hops=1,limit=10,mode="entity")
        matched=[node["id"] for node in result["nodes"] if node.get("matched")]
        self.assertEqual(matched,["A"])
        self.assertEqual(result["query"]["matched_total"],1)

    def test_legacy_business_id_remains_an_exact_search_key(self):
        result=self.service.query_graph("APP-A",hops=1,limit=10,mode="entity")
        matched=[node["id"] for node in result["nodes"] if node.get("matched")]
        self.assertEqual(matched,["A"])
        self.assertTrue(result["query"]["exact_match"])
        self.assertEqual(self.service.resolve_node_id("APP-A"),"A")

    def test_mapping_mode_contains_only_instance_links_and_object_types(self):
        result=self.service.query_graph("",limit=20,mode="mapping")
        self.assertNotIn("tbox:Property:channel",{node["id"] for node in result["nodes"]})
        self.assertEqual([edge["properties"]["kind"] for edge in result["edges"]],["INSTANCE_OF"])

    def test_result_limit_sets_explicit_truncation(self):
        result=self.service.query_graph("A",hops=3,limit=2,mode="entity")
        self.assertEqual(len(result["nodes"]),2)
        self.assertTrue(result["page"]["node_truncated"])
        self.assertTrue(result["page"]["result_truncated"])


if __name__=="__main__":
    unittest.main()
