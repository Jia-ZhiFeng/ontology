import pathlib
import unittest
from dataclasses import replace
from decimal import Decimal

from app.action import (
    ActionIdempotencyConflict,
    DecisionRecommendationContext,
    DecisionRecommendationEffect,
    OutboxEvent,
    RECOMMENDATION_ONLY_ASSERTION,
    RecordDecisionRecommendationActionService,
    RecordDecisionRecommendationError,
    deterministic_recommendation_id,
    validate_decision_recommendation_request,
)
from app.ontology.baseline import build_personal_credit_baseline
from tests.test_request_credit_report_action import MemoryExecutionRepository


ROOT = pathlib.Path(__file__).resolve().parents[2]


def valid_request():
    return {
        "application_id": "APP20260801000001",
        "recommendation_version": "rec-v1",
        "approver_id": "risk-manager-1",
        "approval_assertion": RECOMMENDATION_ONLY_ASSERTION,
        "request_id": "REQ-DECISION-REC-1",
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


class FakeRecommendationRepository:
    def __init__(self, *, context=None, inspect_error=None):
        self.inspect_error = inspect_error
        self.inspect_count = 0
        self.persist_count = 0
        self.context = context or DecisionRecommendationContext(
            application_id="APP20260801000001",
            application_status="待人工复核",
            decision_result="转人工",
            risk_assessment_id="RA20260001",
            risk_level="中",
            pd_value=Decimal("0.170000"),
            threshold=Decimal("0.150000"),
            risk_check_result="待人工复核",
            fraud_report_id="FR20260001",
            fraud_result="通过",
            phone_verification_id="PV20260001",
            phone_conclusion="信息基本一致",
            phone_doubts_digest="d" * 64,
        )

    def ensure_schema(self):
        return None

    def inspect_target(self, application_id):
        self.inspect_count += 1
        if self.inspect_error:
            raise self.inspect_error
        return replace(self.context, application_id=application_id)

    def persist_and_enqueue(
        self,
        *,
        context,
        recommendation_version,
        decision_support,
        outbox_repository,
        execution_id,
        ontology_version,
        **_values,
    ):
        self.persist_count += 1
        recommendation_id = deterministic_recommendation_id(
            context.application_id, recommendation_version
        )
        recommendation = decision_support["recommended_decision"]
        effect = DecisionRecommendationEffect(
            recommendation_id=recommendation_id,
            application_id=context.application_id,
            previous_application_status=context.application_status,
            previous_decision_result=context.decision_result,
            applied_application_status=context.application_status,
            applied_decision_result=recommendation,
        )
        event = OutboxEvent(
            event_id="decision-rec-outbox-1",
            execution_id=execution_id,
            event_type="DECISION_RECOMMENDATION_GRAPH_SYNC",
            aggregate_type="LoanApplication",
            aggregate_id=context.application_id,
            ontology_version=ontology_version,
            deduplication_key=execution_id,
            payload={
                "application_id": context.application_id,
                "recommendation_id": recommendation_id,
                "recommendation_version": recommendation_version,
                "recommended_decision": recommendation,
                "effect": effect.__dict__,
            },
            status="PENDING",
            attempt_count=0,
            max_attempts=5,
        )
        outbox_repository.event = event
        return (
            {
                "application_status": context.application_status,
                "decision_result": recommendation,
            },
            {
                "recommendation_id": recommendation_id,
                "evidence_reference": "EVD-DECISION-1",
            },
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
            "decision_reasons": [
                {"code": "RISK_REVIEW", "description": "综合风险要求人工判断"}
            ],
            "decision_boundary": "decision_support_only_not_final_approval",
        }
        explanation = {
            "evidence_chain": [
                {
                    "source_type": "LoanApplication",
                    "reference_id": application_id,
                },
                {
                    "source_type": "RiskAssessmentReport",
                    "reference_id": "RA20260001",
                },
                {
                    "source_type": "AntiFraudReport",
                    "reference_id": "FR20260001",
                },
                {
                    "source_type": "PhoneVerificationRecord",
                    "reference_id": "PV20260001",
                },
                {
                    "source_type": "DecisionSupport",
                    "reference_id": application_id,
                },
            ],
            "lineage": {
                "application_id": application_id,
                "ontology_version": ontology_version,
                "action_execution_id": action_execution_id,
                "missing_sources": [] if self.complete else ["PhoneVerificationRecord"],
                "source_references": [
                    {"source_type": "LoanApplication", "reference_id": application_id}
                ],
            },
        }

        def envelope(result, version):
            return {
                "status": "success",
                "result": result,
                "evidence": [{"source": "test"}],
                "complete": self.complete,
                "implementation_version": version,
                "data_snapshot_time": timestamp,
            }

        return {
            "application_id": application_id,
            "status": "success",
            "read_only": True,
            "postgres_snapshot_id": "500:500:",
            "postgres_snapshot_time": timestamp,
            "execution_order": list((
                "aggregate_decision_support",
                "build_evidence_explanation",
            )),
            "functions": {
                "aggregate_decision_support": envelope(
                    decision, "decision-support-v1"
                ),
                "build_evidence_explanation": envelope(
                    explanation, "evidence-v1"
                ),
            },
        }


class SuccessfulWorker:
    def __init__(self, executions, outbox):
        self.executions = executions
        self.outbox = outbox

    def process_event(self, event_id):
        current = self.executions.get(self.outbox.event.execution_id)
        self.executions.transition(
            current.execution_id,
            "SUCCEEDED",
            expected_version=current.version,
            event_type="action_succeeded",
            actor_id="worker",
            result_summary={
                "application_id": self.outbox.event.aggregate_id,
                "recommended_decision": self.outbox.event.payload[
                    "recommended_decision"
                ],
                "decision_authority": "recommendation_only",
                "evidence_reference": "EVD-DECISION-1",
                "persistence": {
                    "postgres": "recommendation_evidence_and_confirmation_recorded",
                    "nebulagraph": "synchronized",
                },
                "decision_boundary": "recommendation_only_not_final_approval",
            },
        )
        self.outbox.event = replace(self.outbox.event, status="SUCCEEDED")
        return self.outbox.event


class PendingWorker:
    def __init__(self, outbox):
        self.outbox = outbox

    def process_event(self, event_id):
        self.outbox.event = replace(
            self.outbox.event,
            status="RETRY",
            attempt_count=1,
            last_error_code="ACTION_GRAPH_SYNC_FAILED",
            last_error_message="graph unavailable",
        )
        return self.outbox.event


class RecordDecisionRecommendationActionTests(unittest.TestCase):
    def service(self, *, recommendations=None, functions=None, pending=False):
        proposal = build_personal_credit_baseline()
        executions = MemoryExecutionRepository()
        outbox = FakeOutboxRepository()
        recommendations = recommendations or FakeRecommendationRepository()
        functions = functions or FakeFunctionAdapter()
        worker = (
            PendingWorker(outbox)
            if pending
            else SuccessfulWorker(executions, outbox)
        )
        service = RecordDecisionRecommendationActionService(
            postgres_service=object(),
            nebula_factory=lambda: None,
            proposal=proposal,
            execution_repository=executions,
            recommendation_repository=recommendations,
            function_adapter=functions,
            outbox_repository=outbox,
            outbox_worker=worker,
        )
        return service, proposal, executions, recommendations, functions

    @staticmethod
    def execute(
        service,
        proposal,
        request=None,
        *,
        actor_id="risk-manager-1",
        role="风险经理",
    ):
        return service.execute(
            request or valid_request(),
            ontology_version=proposal.version,
            actor_id=actor_id,
            actor_role=role,
        )

    def test_request_contract_requires_fixed_non_final_assertion(self):
        request = valid_request()
        request["approval_assertion"] = "confirmed"
        with self.assertRaisesRegex(Exception, "必须为"):
            validate_decision_recommendation_request(request)
        request = valid_request()
        request["final_approval"] = True
        with self.assertRaisesRegex(Exception, "未声明字段"):
            validate_decision_recommendation_request(request)

    def test_success_and_same_request_replay_write_once(self):
        service, proposal, _executions, recommendations, functions = self.service()
        first = self.execute(service, proposal)
        second = self.execute(service, proposal)
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertEqual(first["result"]["decision_authority"], "recommendation_only")
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(recommendations.persist_count, 1)
        self.assertEqual(functions.calls, 1)

    def test_same_version_changed_request_conflicts(self):
        service, proposal, _executions, recommendations, _functions = self.service()
        self.execute(service, proposal)
        changed = valid_request()
        changed["approver_id"] = "risk-manager-2"
        with self.assertRaises(ActionIdempotencyConflict):
            self.execute(
                service, proposal, changed, actor_id="risk-manager-2"
            )
        self.assertEqual(recommendations.persist_count, 1)

    def test_agent_can_generate_candidate_but_cannot_confirm(self):
        request = valid_request()
        request["approver_id"] = "agent-1"
        service, proposal, executions, recommendations, functions = self.service()
        with self.assertRaises(RecordDecisionRecommendationError) as raised:
            self.execute(
                service,
                proposal,
                request,
                actor_id="agent-1",
                role="Agent",
            )
        self.assertEqual(raised.exception.code, "ACTION_APPROVAL_REQUIRED")
        self.assertEqual(functions.calls, 1)
        self.assertEqual(recommendations.persist_count, 0)
        self.assertEqual(next(iter(executions.records.values())).status, "FAILED")

    def test_approver_must_equal_authenticated_actor(self):
        service, proposal, _executions, recommendations, functions = self.service()
        with self.assertRaises(RecordDecisionRecommendationError) as raised:
            self.execute(service, proposal, actor_id="different-manager")
        self.assertEqual(raised.exception.code, "ACTION_APPROVAL_REQUIRED")
        self.assertEqual(functions.calls, 1)
        self.assertEqual(recommendations.persist_count, 0)

    def test_high_risk_context_forbids_pass_recommendation(self):
        functions = FakeFunctionAdapter(recommendation="通过")
        service, proposal, _executions, recommendations, _functions = self.service(
            functions=functions
        )
        with self.assertRaises(RecordDecisionRecommendationError) as raised:
            self.execute(service, proposal)
        self.assertEqual(raised.exception.code, "ACTION_HIGH_RISK_PASS_FORBIDDEN")
        self.assertEqual(recommendations.persist_count, 0)

    def test_clean_context_can_record_pass_as_recommendation_only(self):
        clean = replace(
            FakeRecommendationRepository().context,
            risk_level="低",
            pd_value=Decimal("0.030000"),
            threshold=Decimal("0.150000"),
            risk_check_result="通过",
            phone_doubts_digest=None,
        )
        recommendations = FakeRecommendationRepository(context=clean)
        functions = FakeFunctionAdapter(recommendation="通过")
        service, proposal, _executions, recommendations, _functions = self.service(
            recommendations=recommendations, functions=functions
        )
        response = self.execute(service, proposal)
        self.assertEqual(response["result"]["recommended_decision"], "通过")
        self.assertEqual(response["result"]["decision_authority"], "recommendation_only")
        self.assertEqual(recommendations.persist_count, 1)

    def test_incomplete_evidence_never_writes_recommendation(self):
        functions = FakeFunctionAdapter(complete=False)
        service, proposal, _executions, recommendations, _functions = self.service(
            functions=functions
        )
        with self.assertRaises(RecordDecisionRecommendationError) as raised:
            self.execute(service, proposal)
        self.assertEqual(raised.exception.code, "ACTION_RECOMMENDATION_INVALID")
        self.assertEqual(recommendations.persist_count, 0)

    def test_graph_retry_keeps_atomic_postgres_result_committing(self):
        service, proposal, executions, recommendations, _functions = self.service(
            pending=True
        )
        response = self.execute(service, proposal)
        self.assertEqual(response["status"], "COMMITTING")
        self.assertEqual(response["outbox"]["status"], "RETRY")
        self.assertEqual(response["result"]["decision_authority"], "recommendation_only")
        self.assertEqual(recommendations.persist_count, 1)
        self.assertEqual(next(iter(executions.records.values())).status, "COMMITTING")

    def test_repository_is_atomic_locked_evidenced_and_compensatable(self):
        repository = (
            ROOT / "backend/app/action/decision_recommendation_repository.py"
        ).read_text(encoding="utf-8")
        outbox = (ROOT / "backend/app/action/outbox.py").read_text(encoding="utf-8")
        self.assertIn("decision_authority='recommendation_only'", repository)
        self.assertIn("FOR UPDATE", repository)
        self.assertIn("DECISION_RECOMMENDATION_GRAPH_SYNC", repository)
        self.assertIn("approval_assertion_digest", repository)
        self.assertIn("decision_recommendation_effect_from_payload", outbox)


if __name__ == "__main__":
    unittest.main()
