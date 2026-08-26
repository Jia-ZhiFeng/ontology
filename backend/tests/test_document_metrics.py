import json
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ontology.attribute_catalog import PERSONAL_CREDIT_ATTRIBUTE_CATALOG
from app.ontology.audit import audit_personal_credit_proposal
from app.ontology.baseline import build_personal_credit_baseline


METRICS_PATH = PROJECT_ROOT / "personal_credit_release_metrics.json"
METRICS = json.loads(METRICS_PATH.read_text(encoding="utf-8"))


class DocumentMetricsTests(unittest.TestCase):
    def test_metric_totals_are_internally_consistent(self):
        database = METRICS["database"]
        graph = METRICS["graph"]
        self.assertEqual(
            database["fields_total"],
            database["fields_modeled"] + database["fields_intentionally_excluded"],
        )
        self.assertEqual(
            graph["abox_edges_total"],
            graph["business_relation_edges"] + graph["instance_of_edges"],
        )
        self.assertEqual(
            graph["total_vertices"],
            graph["tbox_vertices"] + graph["abox_entities"],
        )
        self.assertEqual(
            graph["total_edges"],
            graph["tbox_edges"] + graph["abox_edges_total"],
        )

    def test_metrics_match_deterministic_baseline_and_audit(self):
        proposal = build_personal_credit_baseline()
        model = METRICS["model"]
        self.assertEqual(len(proposal.object_types), model["object_types"])
        self.assertEqual(len(proposal.properties), model["properties"])
        self.assertEqual(len(proposal.link_types), model["link_types"])
        self.assertEqual(len(proposal.interfaces), model["interfaces"])
        self.assertEqual(
            sum(len(interface.constraints) for interface in proposal.interfaces),
            model["axioms"],
        )
        self.assertEqual(len(proposal.functions), model["functions"])
        self.assertEqual(len(proposal.action_types), model["action_types"])
        self.assertEqual(len(proposal.dynamic_security), model["dynamic_security"])

        schema = {}
        for prop in proposal.properties:
            schema.setdefault(prop.source_table, []).append({"column_name": prop.source_column})
        for item in PERSONAL_CREDIT_ATTRIBUTE_CATALOG["excluded_database_fields"]:
            schema.setdefault(item["table"], []).append({"column_name": item["column"]})
        axiom_execution = {
            "valid": True,
            "executability": {"valid": True},
            "summary": {"total": model["axioms"], "passed": model["axioms"]},
            "results": [],
            "dataset_complete": True,
            "truncated_tables": [],
        }
        audit = audit_personal_credit_proposal(proposal, schema, axiom_execution)
        self.assertEqual(audit["summary"]["total"], METRICS["audit"]["checks_total"])
        self.assertEqual(audit["summary"]["passed"], METRICS["audit"]["checks_passed"])
        self.assertEqual(audit["audited_digest"], METRICS["audit"]["digest"])

    def test_authoritative_documents_use_current_release_counts(self):
        audit_text = (PROJECT_ROOT / "PERSONAL_CREDIT_PREPUBLICATION_AUDIT.md").read_text(encoding="utf-8")
        release_version = METRICS["release_version"]
        release_text = (PROJECT_ROOT / f"PERSONAL_CREDIT_ONTOLOGY_RELEASE_{release_version}.md").read_text(encoding="utf-8")
        guide_text = (PROJECT_ROOT / "MVP_FULL_REAUDIT_AND_FILE_GUIDE.md").read_text(encoding="utf-8")
        audit = METRICS["audit"]
        graph = METRICS["graph"]
        self.assertIn(f"通过（{audit['checks_passed']}/{audit['checks_total']}）", audit_text)
        self.assertIn(f"查看 {audit['checks_total']} 项审核明细", audit_text)
        self.assertIn(f"| 全部图谱 | {graph['total_vertices']} | {graph['total_edges']} |", release_text)
        self.assertIn(f"{graph['business_relation_edges']} 条业务关系", release_text)
        self.assertIn(f"{graph['instance_of_edges']} 条 `INSTANCE_OF`", release_text)
        self.assertIn("P1-8 文档统计已经出现多套口径（已修复）", guide_text)

    def test_legacy_release_counts_cannot_reappear(self):
        targets = [
            *PROJECT_ROOT.glob("*.md"),
            PROJECT_ROOT / "scripts" / "build_eight_tasks_doc.py",
        ]
        forbidden = ("14/14", "13 项审核明细", "150/243", "150 个节点和 243", "24 条 ABox 关系")
        for path in targets:
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                with self.subTest(path=path.name, token=token):
                    self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
