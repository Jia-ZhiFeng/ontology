import pathlib
import unittest
from dataclasses import replace

from app.action import (
    ActionAuthorizationError,
    ActionIdempotencyConflict,
    ManualReviewContext,
    ManualReviewEffect,
    ManualReviewEvidenceIncomplete,
    ManualReviewRuleNotMet,
    OutboxEvent,
    TriggerManualReviewActionService,
    TriggerManualReviewError,
    deterministic_manual_review_id,
    validate_manual_review_request,
)
from app.ontology.baseline import build_personal_credit_baseline
from tests.test_request_credit_report_action import MemoryExecutionRepository


ROOT = pathlib.Path(__file__).resolve().parents[2]


class SourceAwareMemoryExecutionRepository(MemoryExecutionRepository):
    def verify_audit_chain(self, execution_id):
        if execution_id == "source-execution-1":
            return True
        return super().verify_audit_chain(execution_id)


def valid_request():
    return {
        "application_id": "APP20260801000001",
        "review_reason": "风险评估及电核证据要求人工复核",
        "source_execution_id": "source-execution-1",
        "request_id": "REQ-MANUAL-REVIEW-1",
    }


class FakeOutboxRepository:
    def __init__(self):
        self.event = None

    def ensure_schema(self):
        return None

    def get(self, event_id):
        if not self.event or self.event.event_id != event_id:
            raise KeyError(event_id)
        return self.event


class FakeReviewRepository:
    def __init__(self, *, inspect_error=None):
        self.inspect_error = inspect_error
        self.inspect_count = 0
        self.persist_count = 0
        self.context = ManualReviewContext(
            application_id="APP20260801000001",
            application_status="评估中",
            decision_result=None,
            risk_assessment_id="RA20260001",
            risk_level="中",
            risk_check_result="待人工复核",
            fraud_report_id="FR20260001",
            fraud_result="通过",
            fraud_hit=False,
            phone_verification_id="PV20260001",
            phone_conclusion="信息基本一致",
            phone_has_doubts=True,
            source_execution_id="source-execution-1",
            source_action_name="generate_risk_assessment",
            source_execution_status="SUCCEEDED",
            matched_rules=("RISK_REVIEW_REQUIRED", "PHONE_DOUBT"),
        )

    def ensure_schema(self):
        return None

    def inspect_target(self, application_id, source_execution_id):
        self.inspect_count += 1
        if self.inspect_error:
            raise self.inspect_error
        return replace(
            self.context, application_id=application_id,
            source_execution_id=source_execution_id,
        )

    def persist_and_enqueue(
        self, *, context, review_reason, outbox_repository, execution_id,
        ontology_version, decision_support, **_values
    ):
        self.persist_count += 1
        review_id = deterministic_manual_review_id(context.application_id, review_reason)
        effect = ManualReviewEffect(
            review_id=review_id,
            application_id=context.application_id,
            previous_application_status=context.application_status,
            previous_decision_result=context.decision_result,
        )
        event = OutboxEvent(
            event_id="manual-review-outbox-1",
            execution_id=execution_id,
            event_type="MANUAL_REVIEW_GRAPH_SYNC",
            aggregate_type="LoanApplication",
            aggregate_id=context.application_id,
            ontology_version=ontology_version,
            deduplication_key=execution_id,
            payload={
                "application_id": context.application_id,
                "review_id": review_id,
                "matched_rules": list(context.matched_rules),
                "effect": effect.__dict__,
            },
            status="PENDING",
            attempt_count=0,
            max_attempts=5,
        )
        outbox_repository.event = event
        return (
            {"application_status": "待人工复核", "decision_result": "转人工"},
            {"review_id": review_id, "evidence_reference": "EVD-TEST-1"},
            effect,
            event,
        )


class FakeFunctionAdapter:
    def __init__(self, *, recommendation="转人工", complete=True):
        self.recommendation = recommendation
        self.complete = complete
        self.calls = 0

    def execute_decision_support(
        self, application_id, *, ontology_version, action_execution_id, **_values
    ):
        self.calls += 1
        timestamp = "2026-08-25T08:00:00+00:00"
        decision = {
            "recommended_decision": self.recommendation,
            "decision_reasons": [{"code": "PHONE_DOUBT", "description": "电核存疑"}],
            "decision_boundary": "decision_support_only_not_final_approval",
        }
        explanation = {
            "evidence_chain": [
                {"source_type": value, "reference_id": f"REF-{index}"}
                for index, value in enumerate(
                    ("LoanApplication", "RiskAssessmentReport", "AntiFraudReport",
                     "PhoneVerificationRecord", "DecisionSupport"), 1
                )
            ],
            "lineage": {
                "application_id": application_id,
                "ontology_version": ontology_version,
                "action_execution_id": action_execution_id,
                "missing_sources": [] if self.complete else ["PhoneVerificationRecord"],
                "source_references": [{"source_type": "LoanApplication", "reference_id": application_id}],
            },
        }
        envelope = lambda result, version: {
            "status": "success", "result": result,
            "evidence": [{"source": "test"}], "complete": self.complete,
            "implementation_version": version, "data_snapshot_time": timestamp,
        }
        return {
            "application_id": application_id,
            "status": "success", "read_only": True,
            "postgres_snapshot_id": "400:400:",
            "postgres_snapshot_time": timestamp,
            "execution_order": [
                "aggregate_decision_support", "build_evidence_explanation",
            ],
            "functions": {
                "aggregate_decision_support": envelope(decision, "decision-support-v1"),
                "build_evidence_explanation": envelope(explanation, "evidence-v1"),
            },
        }


class SuccessfulWorker:
    def __init__(self, executions, outbox):
        self.executions = executions
        self.outbox = outbox

    def process_event(self, event_id):
        current = self.executions.get(self.outbox.event.execution_id)
        self.executions.transition(
            current.execution_id, "SUCCEEDED", expected_version=current.version,
            event_type="action_succeeded", actor_id="worker",
            result_summary={
                "application_id": self.outbox.event.aggregate_id,
                "application_status": "待人工复核",
                "routing_result": "转人工",
                "review_evidence_reference": "EVD-TEST-1",
                "persistence": {
                    "postgres": "manual_review_status_and_reason_recorded",
                    "nebulagraph": "synchronized",
                },
            },
        )
        self.outbox.event = replace(self.outbox.event, status="SUCCEEDED")
        return self.outbox.event


class PendingWorker:
    def __init__(self, outbox):
        self.outbox = outbox

    def process_event(self, event_id):
        self.outbox.event = replace(
            self.outbox.event, status="RETRY", attempt_count=1,
            last_error_code="ACTION_GRAPH_SYNC_FAILED",
            last_error_message="graph unavailable",
        )
        return self.outbox.event


class TriggerManualReviewActionTests(unittest.TestCase):
    def service(self, *, reviews=None, functions=None, pending=False):
        proposal = build_personal_credit_baseline()
        executions = SourceAwareMemoryExecutionRepository()
        outbox = FakeOutboxRepository()
        reviews = reviews or FakeReviewRepository()
        functions = functions or FakeFunctionAdapter()
        worker = PendingWorker(outbox) if pending else SuccessfulWorker(executions, outbox)
        service = TriggerManualReviewActionService(
            postgres_service=object(), nebula_factory=lambda: None,
            proposal=proposal, execution_repository=executions,
            review_repository=reviews, function_adapter=functions,
            outbox_repository=outbox, outbox_worker=worker,
        )
        return service, proposal, executions, reviews, functions

    @staticmethod
    def execute(service, proposal, request=None, *, role="系统适配器"):
        return service.execute(
            request or valid_request(), ontology_version=proposal.version,
            actor_id="manual-review-router", actor_role=role,
        )

    def test_request_contract_rejects_unknown_fields_and_invalid_source(self):
        request = valid_request(); request["approval"] = True
        with self.assertRaisesRegex(Exception, "未声明字段"):
            validate_manual_review_request(request)
        request = valid_request(); request["source_execution_id"] = "?"
        with self.assertRaisesRegex(Exception, "格式无效"):
            validate_manual_review_request(request)

    def test_success_and_same_request_replay_persist_once(self):
        service, proposal, _executions, reviews, functions = self.service()
        first = self.execute(service, proposal)
        second = self.execute(service, proposal)
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(reviews.persist_count, 1)
        self.assertEqual(functions.calls, 1)

    def test_same_business_key_with_different_source_conflicts(self):
        service, proposal, _executions, reviews, _functions = self.service()
        self.execute(service, proposal)
        changed = valid_request(); changed["source_execution_id"] = "source-execution-2"
        with self.assertRaises(ActionIdempotencyConflict):
            self.execute(service, proposal, changed)
        self.assertEqual(reviews.persist_count, 1)

    def test_role_denied_before_rule_or_function(self):
        service, proposal, _executions, reviews, functions = self.service()
        with self.assertRaises(ActionAuthorizationError):
            self.execute(service, proposal, role="Agent")
        self.assertEqual(reviews.inspect_count, 0)
        self.assertEqual(functions.calls, 0)

    def test_rule_not_met_and_invalid_source_fail_without_side_effect(self):
        for error, code in (
            (ManualReviewRuleNotMet("no rule"), "ACTION_MANUAL_REVIEW_RULE_NOT_MET"),
            (ManualReviewEvidenceIncomplete("bad source"), "ACTION_EVIDENCE_INCOMPLETE"),
        ):
            reviews = FakeReviewRepository(inspect_error=error)
            service, proposal, executions, reviews, functions = self.service(reviews=reviews)
            with self.assertRaises(TriggerManualReviewError) as raised:
                self.execute(service, proposal)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(reviews.persist_count, 0)
            self.assertEqual(functions.calls, 0)
            self.assertEqual(next(iter(executions.records.values())).status, "FAILED")

    def test_incomplete_or_non_review_function_output_never_updates_status(self):
        for functions, code in (
            (FakeFunctionAdapter(complete=False), "ACTION_EVIDENCE_INCOMPLETE"),
            (FakeFunctionAdapter(recommendation="通过"), "ACTION_MANUAL_REVIEW_RULE_NOT_MET"),
        ):
            service, proposal, _executions, reviews, _functions = self.service(functions=functions)
            with self.assertRaises(TriggerManualReviewError) as raised:
                self.execute(service, proposal)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(reviews.persist_count, 0)

    def test_graph_retry_keeps_atomic_postgres_result_committing(self):
        service, proposal, executions, reviews, _functions = self.service(pending=True)
        response = self.execute(service, proposal)
        self.assertEqual(response["status"], "COMMITTING")
        self.assertEqual(response["outbox"]["status"], "RETRY")
        self.assertEqual(response["result"]["routing_result"], "转人工")
        self.assertEqual(reviews.persist_count, 1)
        self.assertEqual(next(iter(executions.records.values())).status, "COMMITTING")

    def test_repository_has_source_scope_row_lock_atomic_evidence_and_compensation(self):
        repository = (ROOT / "backend/app/action/manual_review_repository.py").read_text(encoding="utf-8")
        outbox = (ROOT / "backend/app/action/outbox.py").read_text(encoding="utf-8")
        self.assertIn("source_execution_id", repository)
        self.assertIn("_source_application_id", repository)
        self.assertIn("FOR UPDATE", repository)
        self.assertIn("MANUAL_REVIEW_GRAPH_SYNC", repository)
        self.assertIn("application_status='待人工复核',decision_result='转人工'", repository)
        self.assertIn("default=str", repository)
        self.assertIn("manual_review_effect_from_payload", outbox)


if __name__ == "__main__":
    unittest.main()
