import sys
import types
import unittest
from pathlib import Path


# The workstation runtime used for fast unit tests does not install the
# NebulaGraph driver. Supply only the import surface needed to load the service;
# these tests never open a real connection.
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

from app.kg.nebula import NebulaGraphService,stale_edge_pairs
from app.ontology.baseline import build_personal_credit_baseline
from app.ontology.mapping import (
    abox_vid,
    build_mapping_contract,
    prepare_mapped_entities,
    relation_rank,
    required_tbox_vertices,
)


class RecordingNebula(NebulaGraphService):
    def __init__(self,vertices=None,edges=None,fail_after_insert=None,fail_verification=False):
        self.vertices={key:set(value) for key,value in (vertices or {}).items()}
        self.edges=set(edges or set())
        self.fail_after_insert=fail_after_insert
        self.fail_verification=fail_verification
        self.insert_count=0
        self.events=[]

    def ensure_schema(self):
        self.events.append("ENSURE_SCHEMA")

    def _vertex_ids(self,tag):
        return sorted(self.vertices.get(tag,set()))

    def _edge_rows(self):
        return sorted(self.edges)

    def _assert_graph_contains(self,tag,expected_vertices,expected_edges,attempts=10):
        self.events.append(("VERIFY",tag,len(expected_vertices),len(expected_edges)))
        if self.fail_verification:
            raise RuntimeError("injected verification failure")

    def _run(self,statement,use_space=True):
        self.events.append(statement)
        if statement.startswith("INSERT"):
            self.insert_count+=1
            if self.fail_after_insert==self.insert_count:
                raise RuntimeError("injected insert failure")
        return None


class SafePublicationTests(unittest.TestCase):
    def setUp(self):
        self.proposal=build_personal_credit_baseline()

    def assert_cleanup_follows_all_inserts(self,events):
        insert_indexes=[index for index,event in enumerate(events) if isinstance(event,str) and event.startswith("INSERT")]
        delete_indexes=[index for index,event in enumerate(events) if isinstance(event,str) and event.startswith("DELETE")]
        self.assertTrue(insert_indexes)
        self.assertTrue(delete_indexes)
        self.assertLess(max(insert_indexes),min(delete_indexes))

    def natural_person_profile(self):
        row={
            prop.source_column:None
            for prop in self.proposal.properties
            if prop.source_table=="natural_person"
        }
        row["id_number"]="110101198803120011"
        return {"samples":{"natural_person":[row]}}

    def test_replacement_edge_pair_is_not_deleted(self):
        existing={("a","b","OLD"),("x","y","STALE")}
        desired={("a","b","NEW")}
        pairs=stale_edge_pairs(existing,desired,lambda *_:True)
        self.assertEqual(pairs,[("x","y",0)])

    def test_distinct_rank_allows_stale_relation_cleanup_without_deleting_replacement(self):
        old_rank=relation_rank("SUBMITS")
        new_rank=relation_rank("GUARANTEES")
        existing={("a","b",old_rank,"SUBMITS")}
        desired={("a","b",new_rank,"GUARANTEES")}
        pairs=stale_edge_pairs(existing,desired,lambda *_:True)
        self.assertEqual(pairs,[("a","b",old_rank)])

    def test_typed_vid_prevents_cross_object_business_key_collision(self):
        contract=build_mapping_contract(self.proposal)
        samples={}
        for table in ("natural_person","loan_application"):
            row={column:None for column in contract["property_mappings"][table]}
            row[contract["id_keys"][table]]="SAME-ID"
            samples[table]=[row]
        entities,issues=prepare_mapped_entities({"samples":samples},contract)
        self.assertEqual(issues,[])
        self.assertEqual({entity["vid"] for entity in entities},{
            abox_vid("NaturalPerson","SAME-ID"),
            abox_vid("LoanApplication","SAME-ID"),
        })

    def test_relation_rank_is_stable_and_separates_relation_kinds(self):
        self.assertEqual(relation_rank("SUBMITS"),relation_rank("SUBMITS"))
        self.assertNotEqual(relation_rank("SUBMITS"),relation_rank("GUARANTEES"))
        self.assertGreaterEqual(relation_rank("SUBMITS"),0)
        self.assertLessEqual(relation_rank("SUBMITS"),0x7FFFFFFFFFFFFFFF)

    def test_tbox_upserts_before_cleanup_and_preserves_instance_edge(self):
        root="tbox:Ontology:credit_ontology"
        natural="tbox:ObjectType:NaturalPerson"
        legacy="tbox:Legacy:old"
        service=RecordingNebula(
            vertices={"ontology":{root,natural,legacy}},
            edges={
                ("person-1",natural,"INSTANCE_OF"),
                (root,legacy,"CONTAINS"),
            },
        )

        result=service.write_tbox(self.proposal,"digest")

        self.assert_cleanup_follows_all_inserts(service.events)
        self.assertEqual(result["preserved_instance_of_edges"],1)
        self.assertEqual(result["deleted_stale_tbox_vertices"],1)
        self.assertFalse(any(
            isinstance(event,str) and event.startswith("DELETE EDGE") and "person-1" in event
            for event in service.events
        ))

    def test_tbox_insert_failure_never_reaches_cleanup(self):
        service=RecordingNebula(
            vertices={"ontology":{"tbox:Legacy:old"}},
            fail_after_insert=2,
        )

        with self.assertRaisesRegex(RuntimeError,"injected insert failure"):
            service.write_tbox(self.proposal,"digest")

        self.assertFalse(any(
            isinstance(event,str) and event.startswith("DELETE")
            for event in service.events
        ))

    def test_tbox_verification_failure_never_reaches_cleanup(self):
        service=RecordingNebula(
            vertices={"ontology":{"tbox:Legacy:old"}},
            fail_verification=True,
        )

        with self.assertRaisesRegex(RuntimeError,"injected verification failure"):
            service.write_tbox(self.proposal,"digest")

        self.assertFalse(any(
            isinstance(event,str) and event.startswith("DELETE")
            for event in service.events
        ))

    def test_abox_upserts_and_verifies_before_removing_stale_data(self):
        required=required_tbox_vertices(build_mapping_contract(self.proposal))
        service=RecordingNebula(
            vertices={"ontology":required,"entity":{"old-person"}},
            edges={("old-person","tbox:ObjectType:NaturalPerson","INSTANCE_OF")},
        )

        result=service.sync_postgres_profile(self.natural_person_profile(),self.proposal)

        self.assert_cleanup_follows_all_inserts(service.events)
        self.assertEqual(result["entity_count"],1)
        self.assertEqual(result["instance_of_count"],1)
        self.assertEqual(result["vid_scheme"],"typed-abox-vid-v1")
        self.assertEqual(result["edge_rank_scheme"],"relation-kind-sha256-v1")
        self.assertTrue(any(
            isinstance(event,str)
            and abox_vid("NaturalPerson","110101198803120011") in event
            and f"@{relation_rank('INSTANCE_OF')}" in event
            for event in service.events
        ))
        self.assertEqual(result["deleted_stale_abox_vertices"],1)
        self.assertEqual(result["synchronization_mode"],"upsert_verify_cleanup")

    def test_abox_insert_failure_never_reaches_cleanup(self):
        required=required_tbox_vertices(build_mapping_contract(self.proposal))
        service=RecordingNebula(
            vertices={"ontology":required,"entity":{"old-person"}},
            edges={("old-person","tbox:ObjectType:NaturalPerson","INSTANCE_OF")},
            fail_after_insert=2,
        )

        with self.assertRaisesRegex(RuntimeError,"injected insert failure"):
            service.sync_postgres_profile(self.natural_person_profile(),self.proposal)

        self.assertFalse(any(
            isinstance(event,str) and event.startswith("DELETE")
            for event in service.events
        ))


if __name__=="__main__":
    unittest.main()
