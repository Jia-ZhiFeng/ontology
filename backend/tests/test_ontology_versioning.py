import unittest
from datetime import datetime,timezone

from app.ontology.attribute_catalog import PERSONAL_CREDIT_ATTRIBUTE_CATALOG
from app.ontology.baseline import build_personal_credit_baseline
from app.ontology.versioning import (
    VersionWorkflowError,
    json_safe_payload,
    normalize_draft_proposal,
    proposal_diff,
    require_transition,
    review_release_candidate,
    validate_version_identifier,
)


def empty_complete_profile():
    tables={}
    for group in PERSONAL_CREDIT_ATTRIBUTE_CATALOG["objects"].values():
        tables.setdefault(group["table"],[]).extend(
            {"column_name":item["name"]} for item in group["properties"]
        )
    for item in PERSONAL_CREDIT_ATTRIBUTE_CATALOG["excluded_database_fields"]:
        tables.setdefault(item["table"],[]).append({"column_name":item["column"]})
    return {
        "database":"credit_ontology",
        "tables":tables,
        "samples":{table:[] for table in tables},
        "row_counts":{table:0 for table in tables},
        "truncated_tables":[],
        "snapshot_mode":"repeatable_read_read_only",
        "snapshot_consistent":True,
        "snapshot_id":"test-snapshot",
        "snapshot_started_at":"2000-01-01T00:00:00Z",
    }


class OntologyVersioningTests(unittest.TestCase):
    def test_version_repository_normalizes_snapshot_datetime_for_jsonb(self):
        payload=json_safe_payload({"snapshot_started_at":datetime(2026,8,24,tzinfo=timezone.utc)})
        self.assertEqual(payload["snapshot_started_at"],"2026-08-24 00:00:00+00:00")

    def test_version_identifier_requires_semver(self):
        self.assertEqual(validate_version_identifier("1.2.0"),"1.2.0")
        self.assertEqual(validate_version_identifier("2.0.0-rc.1"),"2.0.0-rc.1")
        for value in ("1.2","v1.2.0","01.2.0",""):
            with self.subTest(value=value),self.assertRaises(VersionWorkflowError):
                validate_version_identifier(value)

    def test_workflow_transitions_are_strict_and_one_way(self):
        require_transition("draft","reviewed")
        require_transition("reviewed","approved")
        require_transition("approved","published")
        for current,target in (("draft","published"),("reviewed","draft"),("published","approved")):
            with self.subTest(current=current,target=target),self.assertRaises(VersionWorkflowError):
                require_transition(current,target)

    def test_normalize_draft_updates_version_and_counts_without_mutating_source(self):
        source=build_personal_credit_baseline()
        draft=normalize_draft_proposal(source,"1.2.0")
        self.assertEqual(source.version,"1.1.0")
        self.assertEqual(draft.version,"1.2.0")
        self.assertEqual(draft.metadata["counts"]["properties"],74)
        self.assertEqual(draft.metadata["counts"]["axioms"],26)

    def test_diff_records_added_removed_and_modified_items(self):
        previous=build_personal_credit_baseline()
        candidate=previous.model_copy(deep=True)
        candidate.version="1.1.0"
        candidate.object_types[0].label="自然人（修订）"
        candidate.link_types.pop()
        difference=proposal_diff(previous,candidate)
        self.assertIn(candidate.object_types[0].name,difference["object_types"]["modified"])
        self.assertEqual(len(difference["link_types"]["removed"]),1)
        self.assertGreaterEqual(difference["changed_item_count"],2)

    def test_complete_baseline_passes_version_candidate_review(self):
        proposal=normalize_draft_proposal(build_personal_credit_baseline(),"1.1.0")
        audit=review_release_candidate(proposal,empty_complete_profile(),build_personal_credit_baseline())
        self.assertTrue(audit["valid"],audit)
        self.assertEqual(audit["summary"]["failed"],0)
        self.assertEqual(audit["summary"]["total"],9)
        self.assertIn("data_quality.catalog",{item["id"] for item in audit["checks"]})
        self.assertTrue(audit["data_quality"]["valid"])
        self.assertEqual(audit["axiom_validation"]["proposal_version"],"1.1.0")

    def test_unmapped_database_field_blocks_review(self):
        proposal=normalize_draft_proposal(build_personal_credit_baseline(),"1.1.0")
        proposal.properties.pop()
        proposal=normalize_draft_proposal(proposal,"1.1.0")
        audit=review_release_candidate(proposal,empty_complete_profile())
        self.assertFalse(audit["valid"])
        failed={item["id"] for item in audit["checks"] if item["status"]=="failed"}
        self.assertIn("postgresql.field_decisions",failed)

    def test_executable_action_blocks_review(self):
        proposal=normalize_draft_proposal(build_personal_credit_baseline(),"1.1.0")
        proposal.action_types[0].execution_mode="automatic"
        audit=review_release_candidate(proposal,empty_complete_profile())
        self.assertFalse(audit["valid"])
        failed={item["id"] for item in audit["checks"] if item["status"]=="failed"}
        self.assertIn("capabilities.boundary",failed)


if __name__=="__main__":
    unittest.main()
