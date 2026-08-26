import pathlib
import unittest
from dataclasses import replace

from app.action import (
    ActionExecutionNotFound,
    ActionExecutionRecord,
    ActionIdempotencyConflict,
    ActionReservation,
    ActionStateMachine,
    GenerateRiskAssessmentActionService,
    GenerateRiskAssessmentError,
    OutboxEvent,
    RiskAssessmentAlreadyExists,
    RiskAssessmentEffect,
    action_idempotency_key_hash,
    action_request_digest,
    deterministic_assessment_id,
)
from app.ontology.baseline import build_personal_credit_baseline


ROOT = pathlib.Path(__file__).resolve().parents[2]
FUNCTIONS = (
    "calculate_dti",
    "calculate_ltv",
    "evaluate_credit_risk",
    "evaluate_fraud_risk",
    "assess_application_risk",
)


class MemoryExecutionRepository:
    def __init__(self):
        self.records = {}
        self.keys = {}
        self.events = {}
        self.sequence = 0

    def reserve(self, **values):
        key = (values["action_name"], values["idempotency_key_hash"])
        existing_id = self.keys.get(key)
        if existing_id:
            existing = self.records[existing_id]
            if existing.request_digest != values["request_digest"]:
                raise ActionIdempotencyConflict("ACTION_IDEMPOTENCY_CONFLICT")
            return ActionReservation(existing, False)
        self.sequence += 1
        execution_id = f"execution-{self.sequence}"
        record = ActionExecutionRecord(
            execution_id=execution_id,
            action_name=values["action_name"],
            ontology_version=values["ontology_version"],
            status="RECEIVED",
            actor_id=values["actor_id"],
            actor_role=values["actor_role"],
            idempotency_key_hash=values["idempotency_key_hash"],
            request_digest=values["request_digest"],
            correlation_id=values.get("correlation_id"),
            result_summary=None,
            error_code=None,
            error_message=None,
            version=1,
        )
        self.records[execution_id] = record
        self.keys[key] = execution_id
        self.events[execution_id] = ["action_received"]
        return ActionReservation(record, True)

    def get(self, execution_id):
        try:
            return self.records[execution_id]
        except KeyError as exc:
            raise ActionExecutionNotFound(execution_id) from exc

    def transition(
        self,
        execution_id,
        target_status,
        *,
        expected_version,
        event_type,
        actor_id,
        details=None,
        result_summary=None,
        error_code=None,
        error_message=None,
    ):
        current = self.get(execution_id)
        self.assert_version(current, expected_version)
        ActionStateMachine.require_transition(current.status, target_status)
        updated = replace(
            current,
            status=target_status,
            version=current.version + 1,
            result_summary=result_summary or current.result_summary,
            error_code=error_code,
            error_message=error_message,
        )
        self.records[execution_id] = updated
        self.events[execution_id].append(event_type)
        return updated

    @staticmethod
    def assert_version(current, expected_version):
        if current.version != expected_version:
            raise AssertionError("stale version")

    def record_event(self, execution_id, *, event_type, actor_id, details=None):
        self.get(execution_id)
        self.events[execution_id].append(event_type)

    def verify_audit_chain(self, execution_id):
        return bool(self.events.get(execution_id))


class FakeReportRepository:
    def __init__(self, *, completed=False):
        self.completed = completed
        self.persist_count = 0
        self.compensate_count = 0

    def inspect_target(self, application_id):
        if self.completed:
            raise RiskAssessmentAlreadyExists("already completed")
        return None

    def persist(self, *, application_id, assessment_version, report):
        self.persist_count += 1
        assessment_id = deterministic_assessment_id(application_id, assessment_version)
        stored = {
            "assessment_id": assessment_id,
            "application_id": application_id,
            **report,
        }
        return stored, RiskAssessmentEffect(
            operation="created",
            assessment_id=assessment_id,
            application_id=application_id,
            previous_row=None,
        )

    def persist_and_enqueue(
        self,
        *,
        application_id,
        assessment_version,
        report,
        outbox_repository,
        execution_id,
        ontology_version,
        outbox_payload,
        max_attempts,
    ):
        stored, effect = self.persist(
            application_id=application_id,
            assessment_version=assessment_version,
            report=report,
        )
        event = OutboxEvent(
            event_id=f"outbox-{execution_id}",
            execution_id=execution_id,
            event_type="RISK_ASSESSMENT_GRAPH_SYNC",
            aggregate_type="RiskAssessmentReport",
            aggregate_id=effect.assessment_id,
            ontology_version=ontology_version,
            deduplication_key=execution_id,
            payload={
                **dict(outbox_payload),
                "assessment_version": assessment_version,
                "report": {
                    "assessment_id": stored["assessment_id"],
                    "application_id": application_id,
                    "risk_level": stored["risk_level"],
                    "pd_value": stored["pd_value"],
                    "check_result": stored["check_result"],
                },
                "effect": {
                    "operation": effect.operation,
                    "assessment_id": effect.assessment_id,
                    "application_id": effect.application_id,
                    "previous_row": effect.previous_row,
                },
            },
            status="PENDING",
            attempt_count=0,
            max_attempts=max_attempts,
        )
        outbox_repository.events[event.event_id] = event
        return stored, effect, event

    def compensate(self, effect):
        self.compensate_count += 1


class FakeOutboxRepository:
    def __init__(self):
        self.events = {}

    def ensure_schema(self):
        return None

    def get(self, event_id):
        return self.events[event_id]


class FakeOutboxWorker:
    def __init__(self, executions, outbox, reports, *, sync_failure=False):
        self.executions = executions
        self.outbox = outbox
        self.reports = reports
        self.sync_failure = sync_failure

    def process_event(self, event_id):
        event = self.outbox.get(event_id)
        event = replace(event, status="PROCESSING", attempt_count=1)
        self.outbox.events[event_id] = event
        current = self.executions.get(event.execution_id)
        if self.sync_failure:
            current = self.executions.transition(
                current.execution_id,
                "COMPENSATING",
                expected_version=current.version,
                event_type="compensation_started",
                actor_id="outbox-test",
                details={"outbox_event_id": event_id},
            )
            effect_raw = event.payload["effect"]
            self.reports.compensate(RiskAssessmentEffect(**effect_raw))
            self.executions.transition(
                current.execution_id,
                "COMPENSATED",
                expected_version=current.version,
                event_type="action_compensated",
                actor_id="outbox-test",
                result_summary={
                    "compensated": True,
                    "original_error_code": "ACTION_GRAPH_SYNC_FAILED",
                },
            )
            event = replace(event, status="COMPENSATED")
            self.outbox.events[event_id] = event
            return event

        report = event.payload["report"]
        self.executions.record_event(
            event.execution_id,
            event_type="risk_assessment_recorded",
            actor_id="outbox-test",
        )
        current = self.executions.get(event.execution_id)
        self.executions.transition(
            current.execution_id,
            "SUCCEEDED",
            expected_version=current.version,
            event_type="action_succeeded",
            actor_id="outbox-test",
            result_summary={
                "application_id": report["application_id"],
                "assessment_id": report["assessment_id"],
                "evidence_summary": event.payload["evidence_summary"],
                "decision_boundary": "risk_assessment_only_not_final_approval",
            },
        )
        event = replace(event, status="SUCCEEDED")
        self.outbox.events[event_id] = event
        return event


class FakeFunctionAdapter:
    def __init__(self, *, incomplete=False):
        self.call_count = 0
        self.incomplete = incomplete

    def execute_risk_assessment(self, application_id, **_context):
        self.call_count += 1
        functions = {}
        for name in FUNCTIONS:
            result = {"ok": True}
            if name == "assess_application_risk":
                result = {
                    "pd_value": 0.17,
                    "risk_level": "中",
                    "risk_factors": [{"factor": "INQUIRY_MEDIUM"}],
                    "check_result": "待人工复核",
                    "model": {"calibrated_pd": False, "version": "risk-v1"},
                    "calculation_policy": "mvp_rule_proxy_not_calibrated_pd",
                    "decision_boundary": "risk_assessment_only_not_final_approval",
                }
            functions[name] = {
                "function_name": name,
                "status": "success",
                "result": result,
                "evidence": [{"source": "unit-test"}],
                "confidence": 0.75,
                "implementation_version": f"{name}-v1",
                "data_snapshot_time": "2026-08-24T12:00:00+08:00",
                "complete": not (self.incomplete and name == "evaluate_fraud_risk"),
                "warnings": [],
                "duration_ms": 1,
            }
        return {
            "application_id": application_id,
            "status": "success",
            "read_only": True,
            "postgres_snapshot_id": "300:300:",
            "postgres_snapshot_time": "2026-08-24T12:00:00+08:00",
            "graph_snapshot_time": None,
            "cross_store_atomic": True,
            "execution_order": list(functions),
            "functions": functions,
        }


class TestActionService(GenerateRiskAssessmentActionService):
    def __init__(self, *, sync_failure=False, completed=False, incomplete=False):
        self.fake_execution_repository = MemoryExecutionRepository()
        self.fake_reports = FakeReportRepository(completed=completed)
        self.fake_functions = FakeFunctionAdapter(incomplete=incomplete)
        self.fake_outbox = FakeOutboxRepository()
        self.fake_worker = FakeOutboxWorker(
            self.fake_execution_repository,
            self.fake_outbox,
            self.fake_reports,
            sync_failure=sync_failure,
        )
        proposal = build_personal_credit_baseline()
        super().__init__(
            postgres_service=object(),
            nebula_factory=lambda: None,
            proposal=proposal,
            execution_repository=self.fake_execution_repository,
            report_repository=self.fake_reports,
            function_adapter=self.fake_functions,
            outbox_repository=self.fake_outbox,
            outbox_worker=self.fake_worker,
            outbox_max_attempts=1,
        )


def request(**updates):
    value = {
        "application_id": "APP20260824000001",
        "assessment_version": "risk-v1",
        "data_snapshot_policy": "repeatable_read_read_only",
        "request_id": "REQ-1",
    }
    value.update(updates)
    return value


class GenerateRiskAssessmentActionTests(unittest.TestCase):
    def execute(self, service, values=None):
        return service.execute(
            values or request(),
            ontology_version=service.proposal.version,
            actor_id="risk-user-1",
            actor_role="风控专员",
        )

    def test_success_runs_declared_functions_persists_and_audits(self):
        service = TestActionService()
        response = self.execute(service)
        self.assertEqual(response["status"], "SUCCEEDED")
        self.assertEqual(response["result"]["evidence_summary"]["function_count"], 5)
        self.assertEqual(service.fake_functions.call_count, 1)
        self.assertEqual(service.fake_reports.persist_count, 1)
        self.assertEqual(service.fake_reports.compensate_count, 0)
        events = service.fake_execution_repository.events[response["execution_id"]]
        for required in (
            "action_received",
            "authorization_checked",
            "idempotency_checked",
            "assessment_snapshot_loaded",
            "assessment_functions_completed",
            "risk_assessment_recorded",
            "action_succeeded",
        ):
            self.assertIn(required, events)
        self.assertEqual(
            response["result"]["decision_boundary"],
            "risk_assessment_only_not_final_approval",
        )

    def test_same_request_replays_without_second_business_write(self):
        service = TestActionService()
        first = self.execute(service)
        second = self.execute(service)
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["execution_id"], second["execution_id"])
        self.assertEqual(service.fake_reports.persist_count, 1)
        self.assertEqual(service.fake_functions.call_count, 1)

    def test_existing_formal_report_fails_closed_without_overwrite(self):
        service = TestActionService(completed=True)
        with self.assertRaises(GenerateRiskAssessmentError) as context:
            self.execute(service)
        self.assertEqual(context.exception.code, "ACTION_ASSESSMENT_ALREADY_EXISTS")
        self.assertEqual(context.exception.status, "FAILED")
        self.assertEqual(service.fake_functions.call_count, 0)
        self.assertEqual(service.fake_reports.persist_count, 0)

    def test_failed_idempotent_replay_returns_original_failure(self):
        service = TestActionService(completed=True)
        with self.assertRaises(GenerateRiskAssessmentError) as first:
            self.execute(service)
        with self.assertRaises(GenerateRiskAssessmentError) as replay:
            self.execute(service)
        self.assertEqual(replay.exception.execution_id, first.exception.execution_id)
        self.assertEqual(replay.exception.code, "ACTION_ASSESSMENT_ALREADY_EXISTS")
        self.assertEqual(service.fake_execution_repository.sequence, 1)
        self.assertEqual(service.fake_reports.persist_count, 0)

    def test_incomplete_function_output_never_writes_report(self):
        service = TestActionService(incomplete=True)
        with self.assertRaises(GenerateRiskAssessmentError) as context:
            self.execute(service)
        self.assertEqual(context.exception.code, "ACTION_FUNCTION_OUTPUT_INVALID")
        self.assertEqual(service.fake_reports.persist_count, 0)
        self.assertEqual(context.exception.status, "FAILED")

    def test_graph_failure_restores_postgres_and_finishes_compensated(self):
        service = TestActionService(sync_failure=True)
        with self.assertRaises(GenerateRiskAssessmentError) as context:
            self.execute(service)
        self.assertEqual(context.exception.code, "ACTION_GRAPH_SYNC_FAILED")
        self.assertEqual(context.exception.status, "COMPENSATED")
        self.assertEqual(service.fake_reports.persist_count, 1)
        self.assertEqual(service.fake_reports.compensate_count, 1)

    def test_deterministic_id_is_stable_and_within_database_limit(self):
        first = deterministic_assessment_id("APP-1", "risk-v1")
        self.assertEqual(first, deterministic_assessment_id("APP-1", "risk-v1"))
        self.assertNotEqual(first, deterministic_assessment_id("APP-1", "risk-v2"))
        self.assertLessEqual(len(first), 32)

    def test_business_repository_uses_application_lock_and_scoped_compensation(self):
        source = (
            ROOT / "backend/app/action/risk_assessment_repository.py"
        ).read_text(encoding="utf-8")
        self.assertIn("WHERE application_id=%s FOR UPDATE", source)
        self.assertIn("DELETE FROM public.risk_assessment_report", source)
        self.assertIn("WHERE assessment_id=%s AND application_id=%s", source)
        self.assertNotIn("UPDATE public.loan_application", source)

    def test_api_identity_is_server_configured_not_request_controlled(self):
        routes = (ROOT / "backend/app/api/routes.py").read_text(encoding="utf-8")
        request_fields = request()
        self.assertNotIn("actor_role", request_fields)
        self.assertIn("actor_role=MVP_ACTION_ACTOR_ROLE", routes)
        self.assertIn("actor_id=MVP_ACTION_ACTOR_ID", routes)


if __name__ == "__main__":
    unittest.main()
