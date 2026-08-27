import pathlib
import unittest

from app.action.workflow import (
    OPTIONAL_WORKFLOW_ACTIONS,
    WORKFLOW_ACTIONS,
    WORKFLOW_DEPENDENCIES,
    PostgresPersonalCreditWorkflowRepository,
    _safe_mapping,
    ready_workflow_actions,
    workflow_request_digest,
)


ROOT = pathlib.Path(__file__).resolve().parents[2]


class PersonalCreditWorkflowTests(unittest.TestCase):
    def test_workflow_covers_all_eight_actions_without_final_approval(self):
        self.assertEqual(len(WORKFLOW_ACTIONS), 8)
        self.assertEqual(len(set(WORKFLOW_ACTIONS)), 8)
        self.assertNotIn("approve_application", WORKFLOW_ACTIONS)
        self.assertNotIn("execute_approval", WORKFLOW_ACTIONS)

    def test_credit_and_fraud_become_parallel_after_existing_entry(self):
        statuses = {name: "PENDING" for name in WORKFLOW_ACTIONS}
        statuses["create_loan_application"] = "SKIPPED"

        self.assertEqual(
            set(ready_workflow_actions(statuses)),
            {"request_credit_report", "request_fraud_report"},
        )

    def test_risk_and_downstream_dependencies_are_ordered(self):
        statuses = {name: "PENDING" for name in WORKFLOW_ACTIONS}
        statuses.update(
            {
                "create_loan_application": "COMPLETED",
                "request_credit_report": "COMPLETED",
                "request_fraud_report": "COMPLETED",
            }
        )
        self.assertEqual(
            ready_workflow_actions(statuses), ("generate_risk_assessment",)
        )
        self.assertEqual(
            WORKFLOW_DEPENDENCIES["record_decision_recommendation"],
            ("record_phone_verification", "trigger_manual_review"),
        )

    def test_only_manual_review_is_optional(self):
        self.assertEqual(OPTIONAL_WORKFLOW_ACTIONS, {"trigger_manual_review"})
        statuses = {name: "PENDING" for name in WORKFLOW_ACTIONS}
        for name in WORKFLOW_ACTIONS[:-2]:
            statuses[name] = "COMPLETED"
        statuses["record_phone_verification"] = "COMPLETED"
        statuses["trigger_manual_review"] = "SKIPPED"
        self.assertEqual(
            ready_workflow_actions(statuses),
            ("record_decision_recommendation",),
        )

    def test_dispatch_ids_do_not_change_business_request_digest(self):
        first = workflow_request_digest(
            {
                "application_id": "APP-1",
                "request_id": "wf-first",
                "callback_id": "callback-first",
            }
        )
        second = workflow_request_digest(
            {
                "application_id": "APP-1",
                "request_id": "wf-second",
                "callback_id": "callback-second",
            }
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_workflow_status_derivation_is_fail_closed(self):
        steps = [
            {"action_name": name, "status": "PENDING"}
            for name in WORKFLOW_ACTIONS
        ]
        steps[0]["status"] = "FAILED"
        self.assertEqual(
            PostgresPersonalCreditWorkflowRepository._derive_workflow_status(steps),
            "FAILED",
        )
        steps[0]["status"] = "COMPLETED"
        steps[-1]["status"] = "COMPLETED"
        self.assertEqual(
            PostgresPersonalCreditWorkflowRepository._derive_workflow_status(steps),
            "COMPLETED",
        )

    def test_persistent_schema_is_append_only_and_stores_no_raw_payload(self):
        source = (ROOT / "backend/app/action/workflow.py").read_text(
            encoding="utf-8"
        )
        lowered = source.lower()
        self.assertIn("personal_credit_workflows", lowered)
        self.assertIn("personal_credit_workflow_steps", lowered)
        self.assertIn("personal_credit_workflow_events", lowered)
        self.assertIn("is append-only", lowered)
        self.assertIn("request_digest char(64)", lowered)
        self.assertIn("input_summary jsonb", lowered)
        self.assertIn("safe_input = _safe_mapping(input_summary)", lowered)
        self.assertIn("jsonb(safe_input)", lowered)
        self.assertNotIn("request_payload jsonb", lowered)

    def test_persisted_workflow_input_summary_is_masked(self):
        summary = _safe_mapping(
            {
                "request_id": "wf-test-001",
                "borrower": {
                    "name": "庚某某",
                    "id_number": "110101199001011234",
                    "phone": "13812345678",
                },
                "application": {"loan_amount": 120000},
            }
        )
        self.assertEqual(summary["request_id"], "wf-test-001")
        self.assertEqual(summary["borrower"]["name"], "庚**")
        self.assertEqual(summary["borrower"]["id_number"], "110*************34")
        self.assertEqual(summary["borrower"]["phone"], "138****5678")
        self.assertEqual(summary["application"]["loan_amount"], "[已脱敏]")

    def test_routes_and_background_reconciliation_are_integrated(self):
        routes = (ROOT / "backend/app/api/routes.py").read_text(encoding="utf-8")
        main = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
        frontend = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
        frontend_adapter = (ROOT / "frontend/live-backend.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("/workflows/personal-credit", routes)
        self.assertIn("/steps/{action_name}/execute", routes)
        self.assertIn("/workflows/personal-credit/data-search", routes)
        self.assertIn("PersonalCreditBusinessLookup", routes)
        self.assertIn('input_summary=model.model_dump(mode="json",exclude_none=True)', routes)
        self.assertIn("input_summary=input_summary", routes)
        self.assertIn('response.headers["Cache-Control"]="no-store"', routes)
        self.assertIn("workflow_repository.reconcile_active", main)
        self.assertIn('id="page-runtime"', frontend)
        self.assertIn("window.renderRuntime", frontend_adapter)
        self.assertIn("/workflows/personal-credit/summary", frontend_adapter)


if __name__ == "__main__":
    unittest.main()
