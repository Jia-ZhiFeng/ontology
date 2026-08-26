import unittest

from app.ontology.axiom_executor import execute_axioms
from app.ontology.baseline import build_personal_credit_baseline


def profile(decision_result):
    samples = {
        "loan_application": [
            {
                "application_id": "UAT001",
                "decision_result": decision_result,
            }
        ],
        "phone_verification_record": [
            {
                "verification_id": "PVUAT001",
                "application_id": "UAT001",
                "conclusion": "存疑",
                "doubts": "合成验收存疑点",
            }
        ],
    }
    return {
        "samples": samples,
        "row_counts": {table: len(rows) for table, rows in samples.items()},
        "truncated_tables": [],
        "snapshot_mode": "repeatable_read_read_only",
        "snapshot_consistent": True,
        "snapshot_id": "test-snapshot",
        "snapshot_started_at": "2000-01-01T00:00:00Z",
    }


def phone_axiom(report):
    return next(item for item in report["results"] if item["id"] == "AX-PHN-001")


class PhoneVerificationAxiomTests(unittest.TestCase):
    def setUp(self):
        self.proposal = build_personal_credit_baseline()

    def test_doubtful_result_can_remain_undecided_until_manual_review_action(self):
        result = phone_axiom(execute_axioms(self.proposal, profile(None)))

        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["violation_count"], 0)

    def test_doubtful_result_cannot_be_passed_directly(self):
        result = phone_axiom(execute_axioms(self.proposal, profile("通过")))

        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["violation_count"], 1)
        self.assertIn("不得直接通过", result["violations"][0]["message"])


if __name__ == "__main__":
    unittest.main()
