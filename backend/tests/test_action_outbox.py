import pathlib
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from app.action import (
    ActionExecutionRecord,
    ActionStateMachine,
    LoanApplicationOutboxWorker,
    CreditReportOutboxWorker,
    AntiFraudReportOutboxWorker,
    PhoneTaskDispatchRequest,
    PhoneTaskDispatchResponse,
    PhoneVerificationTaskOutboxWorker,
    ManualReviewOutboxWorker,
    DecisionRecommendationOutboxWorker,
    OutboxEvent,
    RiskAssessmentEffect,
    RiskAssessmentOutboxWorker,
)
from app.ontology.baseline import build_personal_credit_baseline


ROOT = pathlib.Path(__file__).resolve().parents[2]


class MemoryExecutionRepository:
    def __init__(self, status="COMMITTING"):
        self.record = ActionExecutionRecord(
            execution_id="execution-outbox-1",
            action_name="generate_risk_assessment",
            ontology_version="1.1.0",
            status=status,
            actor_id="risk-user",
            actor_role="风控专员",
            idempotency_key_hash="a" * 64,
            request_digest="b" * 64,
            correlation_id="REQ-OUTBOX",
            result_summary=None,
            error_code=None,
            error_message=None,
            version=6,
        )
        self.events = [{"event_type": "graph_sync_enqueued"}]

    def get(self, execution_id):
        if execution_id != self.record.execution_id:
            raise KeyError(execution_id)
        return self.record

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
        if current.version != expected_version:
            raise AssertionError("stale action version")
        ActionStateMachine.require_transition(current.status, target_status)
        self.record = replace(
            current,
            status=target_status,
            version=current.version + 1,
            result_summary=result_summary or current.result_summary,
            error_code=error_code,
            error_message=error_message,
        )
        self.events.append({"event_type": event_type, "details": details or {}})
        return self.record

    def record_event(self, execution_id, *, event_type, actor_id, details=None):
        self.get(execution_id)
        self.events.append({"event_type": event_type, "details": details or {}})

    def list_audit_events(self, execution_id):
        self.get(execution_id)
        return list(self.events)


class MemoryOutboxRepository:
    def __init__(self, event):
        self.event = event
        self.stale_rows = []

    def claim(self, *, worker_id, lease_seconds, event_id=None, event_type=None):
        if event_id and event_id != self.event.event_id:
            return None
        if event_type and event_type != self.event.event_type:
            return None
        if self.event.status not in {"PENDING", "RETRY", "PROCESSING"}:
            return None
        self.event = replace(
            self.event,
            status="PROCESSING",
            attempt_count=self.event.attempt_count + 1,
            lock_owner=worker_id,
        )
        return self.event

    def get(self, event_id):
        if event_id != self.event.event_id:
            raise KeyError(event_id)
        return self.event

    def claim_dead_letter(self, event_id, worker_id):
        current = self.get(event_id)
        if current.status != "DEAD_LETTER":
            raise AssertionError("not a dead letter")
        self.event = replace(
            current,
            status="PROCESSING",
            attempt_count=current.attempt_count + 1,
            lock_owner=worker_id,
        )
        return self.event

    def restore_dead_letter(
        self, event_id, worker_id, *, error_code, error_message
    ):
        current = self.get(event_id)
        if current.status != "PROCESSING" or current.lock_owner != worker_id:
            raise AssertionError("invalid replay lease")
        self.event = replace(
            current,
            status="DEAD_LETTER",
            lock_owner=None,
            last_error_code=error_code,
            last_error_message=error_message,
        )
        return self.event

    def mark_succeeded(self, event_id, worker_id):
        self.event = replace(
            self.get(event_id), status="SUCCEEDED", lock_owner=None
        )
        return self.event

    def mark_failed(
        self,
        event_id,
        worker_id,
        *,
        error_code,
        error_message,
        retry_delay_seconds,
    ):
        current = self.get(event_id)
        status = (
            "DEAD_LETTER"
            if current.attempt_count >= current.max_attempts
            else "RETRY"
        )
        self.event = replace(
            current,
            status=status,
            lock_owner=None,
            last_error_code=error_code,
            last_error_message=error_message,
        )
        return self.event

    def mark_compensated(self, event_id):
        self.event = replace(self.get(event_id), status="COMPENSATED")
        return self.event

    def cancel(self, event_id, reason):
        self.event = replace(
            self.get(event_id), status="CANCELLED", last_error_message=reason
        )
        return self.event

    def stale_executions_without_event(
        self, *, action_name="generate_risk_assessment", older_than_seconds, limit=100
    ):
        return list(self.stale_rows[:limit])


class FakeReportRepository:
    def __init__(self, *, fail_compensation=False):
        self.compensation_count = 0
        self.fail_compensation = fail_compensation

    def compensate(self, effect):
        self.compensation_count += 1
        if self.fail_compensation:
            raise RuntimeError("postgres compensation unavailable")


class FakeApplicationRepository:
    def __init__(self):
        self.compensation_count = 0

    def compensate(self, effect):
        self.compensation_count += 1
        self.last_effect = effect


class FakeCreditReportRepository(FakeApplicationRepository):
    pass


class FakeAntiFraudReportRepository(FakeApplicationRepository):
    pass


class FakePhoneTaskRepository:
    def __init__(self):
        self.failures = []
        self.dispatched = []

    def ensure_schema(self):
        return None

    def load_dispatch_request(self, verification_id):
        return PhoneTaskDispatchRequest(
            dispatch_id="PVD-OUTBOX-1",
            verification_id=verification_id,
            application_id="APP-OUTBOX-1",
            verification_round=2,
            verification_target="测试用户",
            trigger_reason="风险评估待人工复核",
            questions=({
                "question_id": "Q01",
                "category": "征信查询",
                "question": "请确认近期查询。",
                "source_factors": ["INQUIRY_HIGH"],
                "required": True,
            },),
        )

    def mark_dispatched(self, response, *, response_cached):
        self.dispatched.append(response.external_task_id)
        return {
            "external_task_id": response.external_task_id,
            "provider_name": response.provider_name,
            "provider_mode": response.provider_mode,
            "synthetic": response.synthetic,
            "provider_response_cached": response_cached,
        }

    def mark_dispatch_failure(
        self, verification_id, *, error_code, error_message, dead_letter
    ):
        self.failures.append((verification_id, error_code, dead_letter))


class FakePhoneDispatchCoordinator:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def dispatch(self, request, *, timeout_seconds, max_attempts):
        self.calls += 1
        if self.error:
            raise self.error
        return PhoneTaskDispatchResponse(
            dispatch_id=request.dispatch_id,
            verification_id=request.verification_id,
            external_task_id="PHONE-EXTERNAL-1",
            accepted_at=datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc),
            provider_name="test-phone-system",
            provider_mode="test",
            synthetic=False,
        ), False


class FakeManualReviewRepository(FakeApplicationRepository):
    def ensure_schema(self):
        return None


class FakeDecisionRecommendationRepository(FakeApplicationRepository):
    def ensure_schema(self):
        return None


def outbox_event(*, status="PENDING", attempts=0, max_attempts=3):
    return OutboxEvent(
        event_id="outbox-event-1",
        execution_id="execution-outbox-1",
        event_type="RISK_ASSESSMENT_GRAPH_SYNC",
        aggregate_type="RiskAssessmentReport",
        aggregate_id="RAOUTBOX001",
        ontology_version="1.1.0",
        deduplication_key="execution-outbox-1",
        payload={
            "actor_id": "risk-user",
            "assessment_version": "risk-v1",
            "report": {
                "assessment_id": "RAOUTBOX001",
                "application_id": "APP-OUTBOX-1",
                "risk_level": "中",
                "pd_value": "0.170000",
                "check_result": "待人工复核",
            },
            "effect": {
                "operation": "created",
                "assessment_id": "RAOUTBOX001",
                "application_id": "APP-OUTBOX-1",
                "previous_row": None,
            },
            "evidence_summary": {"function_count": 5},
            "warnings": ["MVP proxy"],
        },
        status=status,
        attempt_count=attempts,
        max_attempts=max_attempts,
        lock_owner="dead-worker" if status == "PROCESSING" else None,
    )


def graph_success(_proposal):
    return {
        "synchronization": {"entity_count": 1},
        "mapping_summary": {"valid": True},
        "data_quality": "passed",
        "axioms": "passed",
    }


def application_outbox_event(*, max_attempts=3):
    return replace(
        outbox_event(max_attempts=max_attempts),
        event_type="LOAN_APPLICATION_GRAPH_SYNC",
        aggregate_type="LoanApplication",
        aggregate_id="APP-OUTBOX-1",
        payload={
            "actor_id": "origination-user",
            "application_id": "APP-OUTBOX-1",
            "application_status": "评估中",
            "created_relations": ["SUBMITS", "TARGETS"],
            "entity_operations": {"borrower": "created", "vehicle": "created"},
            "effect": {
                "application_id": "APP-OUTBOX-1",
                "application_fingerprint": "f" * 64,
                "created_entities": {
                    "natural_person": [{"id_number": "110101199901010019"}],
                    "vehicle": [{"vin": "LVGBM51K0HG999999"}],
                    "dealer": [],
                },
            },
        },
    )


def credit_report_outbox_event(*, max_attempts=3):
    return replace(
        outbox_event(max_attempts=max_attempts),
        event_type="CREDIT_REPORT_GRAPH_SYNC",
        aggregate_type="CreditReport",
        aggregate_id="CR-OUTBOX-1",
        payload={
            "actor_id": "credit-adapter",
            "application_id": "APP-OUTBOX-1",
            "report_id": "CR-OUTBOX-1",
            "provider_report_id": "PROVIDER-CR-1",
            "provider_request_id": "CP-OUTBOX-1",
            "provider_name": "test-provider",
            "provider_mode": "test",
            "synthetic": False,
            "subject_role": "借款人",
            "created_relations": ["HOLDS"],
            "effect": {
                "operation": "created",
                "report_id": "CR-OUTBOX-1",
                "application_id": "APP-OUTBOX-1",
                "previous_row": None,
                "report_fingerprint": "f" * 64,
            },
        },
    )


def anti_fraud_outbox_event(*, max_attempts=3):
    return replace(
        outbox_event(max_attempts=max_attempts),
        event_type="ANTI_FRAUD_REPORT_GRAPH_SYNC",
        aggregate_type="AntiFraudReport",
        aggregate_id="FR-OUTBOX-1",
        payload={
            "actor_id": "fraud-adapter",
            "application_id": "APP-OUTBOX-1",
            "fraud_report_id": "FR-OUTBOX-1",
            "provider_report_id": "PROVIDER-FR-1",
            "provider_request_id": "FP-OUTBOX-1",
            "provider_name": "test-fraud-provider",
            "provider_mode": "test",
            "synthetic": False,
            "association_risk_summary": {
                "path_count": 1,
                "signal_count": 1,
                "max_risk_level": "MEDIUM",
            },
            "created_relations": ["HAS_FRAUD_REPORT"],
            "effect": {
                "operation": "created",
                "fraud_report_id": "FR-OUTBOX-1",
                "application_id": "APP-OUTBOX-1",
                "previous_row": None,
                "report_fingerprint": "f" * 64,
            },
        },
    )


def phone_task_outbox_event(*, max_attempts=3):
    return replace(
        outbox_event(max_attempts=max_attempts),
        event_type="PHONE_VERIFICATION_TASK_DISPATCH",
        aggregate_type="PhoneVerificationRecord",
        aggregate_id="PV-OUTBOX-1",
        payload={
            "actor_id": "phone-adapter",
            "application_id": "APP-OUTBOX-1",
            "verification_id": "PV-OUTBOX-1",
            "verification_round": 2,
            "dispatch_id": "PVD-OUTBOX-1",
            "question_count": 1,
            "template_version": "phone-question-templates-v1.0.0",
            "function_version": "phone-questions-v1.0.0",
            "verified_trigger_signals": ["RISK_CHECK_待人工复核"],
            "created_relations": ["HAS_PHONE_VERIFICATION"],
            "effect": {
                "operation": "created",
                "verification_id": "PV-OUTBOX-1",
                "application_id": "APP-OUTBOX-1",
                "verification_round": 2,
                "dispatch_id": "PVD-OUTBOX-1",
                "retention_policy": "retain_on_dispatch_failure",
            },
        },
    )


def manual_review_outbox_event(*, max_attempts=3):
    return replace(
        outbox_event(max_attempts=max_attempts),
        event_type="MANUAL_REVIEW_GRAPH_SYNC",
        aggregate_type="LoanApplication",
        aggregate_id="APP-OUTBOX-1",
        payload={
            "actor_id": "manual-review-router",
            "application_id": "APP-OUTBOX-1",
            "review_id": "MR-OUTBOX-1",
            "review_reason": "电核存疑",
            "source_execution_id": "SOURCE-OUTBOX-1",
            "matched_rules": ["PHONE_DOUBT"],
            "recommended_decision": "转人工",
            "application_status": "待人工复核",
            "routing_result": "转人工",
            "review_evidence_reference": "EVD-OUTBOX-1",
            "effect": {
                "review_id": "MR-OUTBOX-1",
                "application_id": "APP-OUTBOX-1",
                "previous_application_status": "评估中",
                "previous_decision_result": None,
                "applied_application_status": "待人工复核",
                "applied_decision_result": "转人工",
            },
        },
    )


def decision_recommendation_outbox_event(*, max_attempts=3):
    return replace(
        outbox_event(max_attempts=max_attempts),
        event_type="DECISION_RECOMMENDATION_GRAPH_SYNC",
        aggregate_type="LoanApplication",
        aggregate_id="APP-OUTBOX-1",
        payload={
            "actor_id": "risk-manager-1",
            "application_id": "APP-OUTBOX-1",
            "recommendation_id": "DR-OUTBOX-1",
            "recommendation_version": "rec-v1",
            "recommended_decision": "转人工",
            "decision_authority": "recommendation_only",
            "approver_id": "risk-manager-1",
            "approver_role": "风险经理",
            "evidence_reference": "EVD-DECISION-OUTBOX-1",
            "reason_codes": ["RISK_REVIEW"],
            "effect": {
                "recommendation_id": "DR-OUTBOX-1",
                "application_id": "APP-OUTBOX-1",
                "previous_application_status": "待人工复核",
                "previous_decision_result": None,
                "applied_application_status": "待人工复核",
                "applied_decision_result": "转人工",
            },
        },
    )


class ActionOutboxTests(unittest.TestCase):
    def worker(
        self,
        *,
        event=None,
        action_status="COMMITTING",
        handler=graph_success,
        report=None,
    ):
        proposal = build_personal_credit_baseline()
        executions = MemoryExecutionRepository(status=action_status)
        executions.record = replace(executions.record, ontology_version=proposal.version)
        outbox = MemoryOutboxRepository(event or outbox_event())
        reports = report or FakeReportRepository()
        worker = RiskAssessmentOutboxWorker(
            postgres_service=object(),
            nebula_factory=lambda: None,
            proposal_loader=lambda version: proposal,
            outbox_repository=outbox,
            execution_repository=executions,
            report_repository=reports,
            worker_id="worker-test",
            retry_base_seconds=1,
            retry_max_seconds=4,
            sync_handler=handler,
        )
        return worker, executions, outbox, reports

    def test_successful_delivery_finalizes_action_and_outbox(self):
        worker, executions, outbox, _reports = self.worker()
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(executions.record.status, "SUCCEEDED")
        self.assertEqual(
            executions.record.result_summary["persistence"]["outbox_event_id"],
            "outbox-event-1",
        )
        event_names = [item["event_type"] for item in executions.events]
        self.assertIn("risk_assessment_recorded", event_names)
        self.assertIn("action_succeeded", event_names)

    def test_temporary_failure_stays_committing_and_schedules_retry(self):
        def fail(_proposal):
            raise RuntimeError("graph unavailable")

        worker, executions, _outbox, _reports = self.worker(handler=fail)
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "RETRY")
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(executions.record.status, "COMMITTING")
        self.assertEqual(result.last_error_code, "ACTION_GRAPH_SYNC_FAILED")

    def test_retry_can_succeed_without_recreating_business_report(self):
        calls = {"count": 0}

        def fail_once(_proposal):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary")
            return graph_success(_proposal)

        worker, executions, _outbox, reports = self.worker(handler=fail_once)
        self.assertEqual(worker.process_event("outbox-event-1").status, "RETRY")
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(result.attempt_count, 2)
        self.assertEqual(executions.record.status, "SUCCEEDED")
        self.assertEqual(reports.compensation_count, 0)

    def test_expired_processing_lease_is_reclaimed_idempotently(self):
        event = outbox_event(status="PROCESSING", attempts=1)
        worker, executions, _outbox, _reports = self.worker(event=event)
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(result.attempt_count, 2)
        self.assertEqual(executions.record.status, "SUCCEEDED")

    def test_successful_action_with_unmarked_event_only_marks_outbox(self):
        worker, executions, _outbox, _reports = self.worker(
            action_status="SUCCEEDED",
            handler=lambda _proposal: self.fail("handler should not run"),
        )
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(executions.record.status, "SUCCEEDED")

    def test_terminal_failed_action_cancels_event_without_graph_call(self):
        worker, executions, _outbox, _reports = self.worker(
            action_status="FAILED",
            handler=lambda _proposal: self.fail("handler should not run"),
        )
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "CANCELLED")
        self.assertEqual(executions.record.status, "FAILED")

    def test_retry_exhaustion_compensates_report_and_action(self):
        calls = {"count": 0}

        def fail_then_compensate(_proposal):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("delivery failed")
            return graph_success(_proposal)

        event = outbox_event(max_attempts=1)
        worker, executions, _outbox, reports = self.worker(
            event=event, handler=fail_then_compensate
        )
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "COMPENSATED")
        self.assertEqual(executions.record.status, "COMPENSATED")
        self.assertEqual(reports.compensation_count, 1)

    def test_compensation_failure_enters_alertable_terminal_state(self):
        def fail(_proposal):
            raise RuntimeError("delivery failed")

        event = outbox_event(max_attempts=1)
        report = FakeReportRepository(fail_compensation=True)
        worker, executions, outbox, _reports = self.worker(
            event=event, handler=fail, report=report
        )
        with self.assertRaisesRegex(RuntimeError, "compensation unavailable"):
            worker.process_event("outbox-event-1")
        self.assertEqual(outbox.event.status, "DEAD_LETTER")
        self.assertEqual(executions.record.status, "COMPENSATION_FAILED")
        self.assertEqual(executions.record.error_code, "ACTION_COMPENSATION_FAILED")

    def test_unknown_event_type_is_cancelled(self):
        event = replace(outbox_event(), event_type="UNKNOWN_EVENT")
        worker, executions, _outbox, _reports = self.worker(event=event)
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "CANCELLED")
        self.assertEqual(executions.record.status, "COMMITTING")

    def test_manual_replay_repairs_dead_letter_without_rewriting_terminal_action(self):
        event = outbox_event(status="DEAD_LETTER", attempts=3)
        worker, executions, _outbox, _reports = self.worker(
            event=event, action_status="FAILED"
        )
        result, remediation = worker.replay_dead_letter(
            "outbox-event-1",
            actor_id="ops-1",
            replay_id="replay-1",
            reason_digest="r" * 64,
        )
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(executions.record.status, "FAILED")
        self.assertEqual(remediation["data_quality"], "passed")
        self.assertIn(
            "manual_outbox_replay_succeeded",
            [item["event_type"] for item in executions.events],
        )

    def test_failed_manual_replay_remains_dead_letter(self):
        event = outbox_event(status="DEAD_LETTER", attempts=3)
        worker, executions, outbox, _reports = self.worker(
            event=event,
            action_status="FAILED",
            handler=lambda _proposal: (_ for _ in ()).throw(
                RuntimeError("graph still unavailable")
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "still unavailable"):
            worker.replay_dead_letter(
                "outbox-event-1",
                actor_id="ops-1",
                replay_id="replay-1",
                reason_digest="r" * 64,
            )
        self.assertEqual(outbox.event.status, "DEAD_LETTER")
        self.assertEqual(executions.record.status, "FAILED")

    def test_crash_before_atomic_commit_recovers_orphaned_action(self):
        event = outbox_event(status="CANCELLED")
        worker, executions, outbox, _reports = self.worker(event=event)
        outbox.stale_rows = [{"execution_id": executions.record.execution_id}]
        result = worker.drain()
        self.assertEqual(result["recovered_stale_executions"], 1)
        self.assertEqual(executions.record.status, "FAILED")
        self.assertEqual(
            executions.record.error_code,
            "ACTION_EXECUTION_RECOVERED_AFTER_CRASH",
        )

    def test_schema_and_business_write_encode_durable_atomicity(self):
        outbox_source = (ROOT / "backend/app/action/outbox.py").read_text(
            encoding="utf-8"
        )
        report_source = (
            ROOT / "backend/app/action/risk_assessment_repository.py"
        ).read_text(encoding="utf-8")
        main_source = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
        self.assertIn("FOR UPDATE SKIP LOCKED", outbox_source)
        self.assertNotIn("%s IS NULL OR event_id=%s", outbox_source)
        self.assertIn("UNIQUE (event_type,deduplication_key)", outbox_source)
        self.assertIn("locked_at < now()", outbox_source)
        self.assertIn("stale_execution_without_outbox", outbox_source)
        self.assertIn("persist_and_enqueue", report_source)
        self.assertIn("outbox_repository.enqueue", report_source)
        self.assertIn("run_outbox_worker", main_source)
        self.assertIn("asyncio.to_thread(worker.drain", main_source)

    def test_loan_application_delivery_finalizes_action(self):
        proposal = build_personal_credit_baseline()
        executions = MemoryExecutionRepository(status="COMMITTING")
        executions.record = replace(
            executions.record,
            action_name="create_loan_application",
            actor_role="系统适配器",
            ontology_version=proposal.version,
        )
        outbox = MemoryOutboxRepository(application_outbox_event())
        applications = FakeApplicationRepository()
        worker = LoanApplicationOutboxWorker(
            postgres_service=object(),
            nebula_factory=lambda: None,
            proposal_loader=lambda version: proposal,
            outbox_repository=outbox,
            execution_repository=executions,
            application_repository=applications,
            worker_id="application-worker-test",
            sync_handler=graph_success,
        )
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(executions.record.status, "SUCCEEDED")
        self.assertEqual(
            executions.record.result_summary["decision_boundary"],
            "origination_only_no_credit_decision",
        )
        self.assertIn(
            "application_created",
            [item["event_type"] for item in executions.events],
        )
        self.assertEqual(applications.compensation_count, 0)

    def test_loan_application_retry_exhaustion_compensates(self):
        proposal = build_personal_credit_baseline()
        executions = MemoryExecutionRepository(status="COMMITTING")
        executions.record = replace(
            executions.record,
            action_name="create_loan_application",
            actor_role="系统适配器",
            ontology_version=proposal.version,
        )
        outbox = MemoryOutboxRepository(application_outbox_event(max_attempts=1))
        applications = FakeApplicationRepository()
        calls = {"count": 0}

        def fail_then_compensate(value):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("graph unavailable")
            return graph_success(value)

        worker = LoanApplicationOutboxWorker(
            postgres_service=object(),
            nebula_factory=lambda: None,
            proposal_loader=lambda version: proposal,
            outbox_repository=outbox,
            execution_repository=executions,
            application_repository=applications,
            worker_id="application-worker-test",
            sync_handler=fail_then_compensate,
        )
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "COMPENSATED")
        self.assertEqual(executions.record.status, "COMPENSATED")
        self.assertEqual(applications.compensation_count, 1)
        self.assertEqual(applications.last_effect.application_id, "APP-OUTBOX-1")

    def test_credit_report_delivery_finalizes_holds_relation(self):
        proposal = build_personal_credit_baseline()
        executions = MemoryExecutionRepository(status="COMMITTING")
        executions.record = replace(
            executions.record,
            action_name="request_credit_report",
            actor_role="系统适配器",
            ontology_version=proposal.version,
        )
        outbox = MemoryOutboxRepository(credit_report_outbox_event())
        reports = FakeCreditReportRepository()
        worker = CreditReportOutboxWorker(
            postgres_service=object(),
            nebula_factory=lambda: None,
            proposal_loader=lambda version: proposal,
            outbox_repository=outbox,
            execution_repository=executions,
            report_repository=reports,
            worker_id="credit-worker-test",
            sync_handler=graph_success,
        )
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(executions.record.status, "SUCCEEDED")
        self.assertEqual(executions.record.result_summary["created_relations"], ["HOLDS"])
        self.assertEqual(executions.record.result_summary["provider_mode"], "test")
        self.assertEqual(reports.compensation_count, 0)

    def test_credit_report_retry_exhaustion_compensates(self):
        proposal = build_personal_credit_baseline()
        executions = MemoryExecutionRepository(status="COMMITTING")
        executions.record = replace(
            executions.record,
            action_name="request_credit_report",
            actor_role="系统适配器",
            ontology_version=proposal.version,
        )
        outbox = MemoryOutboxRepository(credit_report_outbox_event(max_attempts=1))
        reports = FakeCreditReportRepository()
        calls = {"count": 0}

        def fail_then_compensate(value):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("graph unavailable")
            return graph_success(value)

        worker = CreditReportOutboxWorker(
            postgres_service=object(),
            nebula_factory=lambda: None,
            proposal_loader=lambda version: proposal,
            outbox_repository=outbox,
            execution_repository=executions,
            report_repository=reports,
            worker_id="credit-worker-test",
            sync_handler=fail_then_compensate,
        )
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "COMPENSATED")
        self.assertEqual(executions.record.status, "COMPENSATED")
        self.assertEqual(reports.compensation_count, 1)
        self.assertEqual(reports.last_effect.report_id, "CR-OUTBOX-1")

    def test_anti_fraud_delivery_finalizes_report_relation(self):
        proposal = build_personal_credit_baseline()
        executions = MemoryExecutionRepository(status="COMMITTING")
        executions.record = replace(
            executions.record,
            action_name="request_fraud_report",
            actor_role="系统适配器",
            ontology_version=proposal.version,
        )
        outbox = MemoryOutboxRepository(anti_fraud_outbox_event())
        reports = FakeAntiFraudReportRepository()
        worker = AntiFraudReportOutboxWorker(
            postgres_service=object(),
            nebula_factory=lambda: None,
            proposal_loader=lambda version: proposal,
            outbox_repository=outbox,
            execution_repository=executions,
            report_repository=reports,
            worker_id="fraud-worker-test",
            sync_handler=graph_success,
        )
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(executions.record.status, "SUCCEEDED")
        self.assertEqual(
            executions.record.result_summary["created_relations"],
            ["HAS_FRAUD_REPORT"],
        )
        self.assertEqual(
            executions.record.result_summary["association_risk_summary"][
                "path_count"
            ],
            1,
        )
        self.assertEqual(reports.compensation_count, 0)

    def test_anti_fraud_retry_exhaustion_compensates(self):
        proposal = build_personal_credit_baseline()
        executions = MemoryExecutionRepository(status="COMMITTING")
        executions.record = replace(
            executions.record,
            action_name="request_fraud_report",
            actor_role="系统适配器",
            ontology_version=proposal.version,
        )
        outbox = MemoryOutboxRepository(anti_fraud_outbox_event(max_attempts=1))
        reports = FakeAntiFraudReportRepository()
        calls = {"count": 0}

        def fail_then_compensate(value):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("graph unavailable")
            return graph_success(value)

        worker = AntiFraudReportOutboxWorker(
            postgres_service=object(),
            nebula_factory=lambda: None,
            proposal_loader=lambda version: proposal,
            outbox_repository=outbox,
            execution_repository=executions,
            report_repository=reports,
            worker_id="fraud-worker-test",
            sync_handler=fail_then_compensate,
        )
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "COMPENSATED")
        self.assertEqual(executions.record.status, "COMPENSATED")
        self.assertEqual(reports.compensation_count, 1)
        self.assertEqual(reports.last_effect.fraud_report_id, "FR-OUTBOX-1")

    def phone_worker(self, *, max_attempts=3, dispatch_error=None):
        proposal = build_personal_credit_baseline()
        executions = MemoryExecutionRepository(status="COMMITTING")
        executions.record = replace(
            executions.record,
            action_name="create_phone_verification_task",
            actor_role="系统适配器",
            ontology_version=proposal.version,
        )
        outbox = MemoryOutboxRepository(
            phone_task_outbox_event(max_attempts=max_attempts)
        )
        tasks = FakePhoneTaskRepository()
        dispatcher = FakePhoneDispatchCoordinator(dispatch_error)
        worker = PhoneVerificationTaskOutboxWorker(
            postgres_service=object(),
            nebula_factory=lambda: None,
            proposal_loader=lambda version: proposal,
            outbox_repository=outbox,
            execution_repository=executions,
            task_repository=tasks,
            dispatch_coordinator=dispatcher,
            worker_id="phone-worker-test",
            retry_base_seconds=1,
            sync_handler=graph_success,
        )
        return worker, executions, outbox, tasks, dispatcher

    def test_phone_task_delivery_finalizes_relation_and_dispatch(self):
        worker, executions, _outbox, tasks, dispatcher = self.phone_worker()
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(executions.record.status, "SUCCEEDED")
        self.assertEqual(
            executions.record.result_summary["created_relations"],
            ["HAS_PHONE_VERIFICATION"],
        )
        self.assertEqual(
            executions.record.result_summary["dispatch_status"], "DISPATCHED"
        )
        self.assertEqual(tasks.dispatched, ["PHONE-EXTERNAL-1"])
        self.assertEqual(dispatcher.calls, 1)

    def test_phone_task_temporary_dispatch_failure_retains_pending_task(self):
        worker, executions, _outbox, tasks, _dispatcher = self.phone_worker(
            dispatch_error=RuntimeError("phone system unavailable")
        )
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "RETRY")
        self.assertEqual(executions.record.status, "COMMITTING")
        self.assertEqual(tasks.failures[-1], (
            "PV-OUTBOX-1", "ACTION_PHONE_DISPATCH_FAILED", False
        ))

    def test_phone_task_dead_letter_retains_record_without_compensation(self):
        worker, executions, _outbox, tasks, _dispatcher = self.phone_worker(
            max_attempts=1,
            dispatch_error=RuntimeError("phone system unavailable"),
        )
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "DEAD_LETTER")
        self.assertEqual(executions.record.status, "FAILED")
        self.assertTrue(executions.record.result_summary["retained_task"])
        self.assertEqual(tasks.failures[-1], (
            "PV-OUTBOX-1", "ACTION_PHONE_DISPATCH_FAILED", True
        ))

    def manual_review_worker(self, *, max_attempts=3, handler=graph_success):
        proposal = build_personal_credit_baseline()
        executions = MemoryExecutionRepository(status="COMMITTING")
        executions.record = replace(
            executions.record,
            action_name="trigger_manual_review",
            actor_role="系统适配器",
            ontology_version=proposal.version,
        )
        outbox = MemoryOutboxRepository(
            manual_review_outbox_event(max_attempts=max_attempts)
        )
        reviews = FakeManualReviewRepository()
        worker = ManualReviewOutboxWorker(
            postgres_service=object(),
            nebula_factory=lambda: None,
            proposal_loader=lambda version: proposal,
            outbox_repository=outbox,
            execution_repository=executions,
            review_repository=reviews,
            worker_id="manual-review-worker-test",
            retry_base_seconds=1,
            sync_handler=handler,
        )
        return worker, executions, outbox, reviews

    def test_manual_review_delivery_finalizes_routing_and_evidence(self):
        worker, executions, _outbox, reviews = self.manual_review_worker()
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(executions.record.status, "SUCCEEDED")
        self.assertEqual(executions.record.result_summary["routing_result"], "转人工")
        self.assertEqual(
            executions.record.result_summary["review_evidence_reference"],
            "EVD-OUTBOX-1",
        )
        self.assertEqual(reviews.compensation_count, 0)

    def test_manual_review_retry_exhaustion_compensates_status_and_reason(self):
        calls = {"count": 0}

        def fail_then_compensate(value):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("graph unavailable")
            return graph_success(value)

        worker, executions, _outbox, reviews = self.manual_review_worker(
            max_attempts=1, handler=fail_then_compensate
        )
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "COMPENSATED")
        self.assertEqual(executions.record.status, "COMPENSATED")
        self.assertEqual(reviews.compensation_count, 1)
        self.assertEqual(reviews.last_effect.review_id, "MR-OUTBOX-1")

    def decision_recommendation_worker(
        self, *, max_attempts=3, handler=graph_success
    ):
        proposal = build_personal_credit_baseline()
        executions = MemoryExecutionRepository(status="COMMITTING")
        executions.record = replace(
            executions.record,
            action_name="record_decision_recommendation",
            actor_id="risk-manager-1",
            actor_role="风险经理",
            ontology_version=proposal.version,
        )
        outbox = MemoryOutboxRepository(
            decision_recommendation_outbox_event(max_attempts=max_attempts)
        )
        recommendations = FakeDecisionRecommendationRepository()
        worker = DecisionRecommendationOutboxWorker(
            postgres_service=object(),
            nebula_factory=lambda: None,
            proposal_loader=lambda version: proposal,
            outbox_repository=outbox,
            execution_repository=executions,
            recommendation_repository=recommendations,
            worker_id="decision-recommendation-worker-test",
            retry_base_seconds=1,
            sync_handler=handler,
        )
        return worker, executions, outbox, recommendations

    def test_decision_recommendation_delivery_preserves_non_final_boundary(self):
        worker, executions, _outbox, recommendations = (
            self.decision_recommendation_worker()
        )
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(executions.record.status, "SUCCEEDED")
        self.assertEqual(
            executions.record.result_summary["decision_authority"],
            "recommendation_only",
        )
        self.assertEqual(
            executions.record.result_summary["decision_boundary"],
            "recommendation_only_not_final_approval",
        )
        self.assertEqual(recommendations.compensation_count, 0)

    def test_decision_recommendation_retry_exhaustion_compensates_exact_write(self):
        calls = {"count": 0}

        def fail_then_compensate(value):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("graph unavailable")
            return graph_success(value)

        worker, executions, _outbox, recommendations = (
            self.decision_recommendation_worker(
                max_attempts=1, handler=fail_then_compensate
            )
        )
        result = worker.process_event("outbox-event-1")
        self.assertEqual(result.status, "COMPENSATED")
        self.assertEqual(executions.record.status, "COMPENSATED")
        self.assertEqual(recommendations.compensation_count, 1)
        self.assertEqual(
            recommendations.last_effect.recommendation_id, "DR-OUTBOX-1"
        )


if __name__ == "__main__":
    unittest.main()
