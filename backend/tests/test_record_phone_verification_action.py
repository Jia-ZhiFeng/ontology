import pathlib
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.action import (
    ActionAuthorizationError,
    ActionIdempotencyConflict,
    OutboxEvent,
    PhoneVerificationAssignment,
    PhoneVerificationTaskNotAssigned,
    PhoneVerificationTaskNotFound,
    PhoneVerificationTerminalConflict,
    RecordPhoneVerificationActionService,
    RecordPhoneVerificationError,
    validate_phone_verification_result_request,
)
from app.ontology.baseline import build_personal_credit_baseline
from tests.test_request_credit_report_action import MemoryExecutionRepository


ROOT = pathlib.Path(__file__).resolve().parents[2]


def valid_request():
    return {
        "verification_id": "PVPHONE0001",
        "callback_id": "CALLBACK-0001",
        "verifier_identity": "数字人电核一号",
        "call_time": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": 180,
        "conclusion": "信息基本一致",
        "doubts": "收入证明待补充",
        "recording_file_id": "REC-PHONE-0001",
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


class FakeResultRepository:
    def __init__(self, *, inspect_error=None):
        now = datetime.now(timezone.utc)
        self.assignment = PhoneVerificationAssignment(
            verification_id="PVPHONE0001",
            application_id="APP20260801000001",
            verification_target="张伟",
            callback_actor_id="phone-result-adapter",
            callback_actor_role="数字人适配器",
            dispatch_status="DISPATCHED",
            task_created_at=now - timedelta(hours=1),
            dispatched_at=now - timedelta(minutes=55),
        )
        self.inspect_error = inspect_error
        self.inspect_count = 0
        self.persist_count = 0

    def ensure_schema(self):
        return None

    def inspect_assignment(self, verification_id, *, actor_id, actor_role):
        self.inspect_count += 1
        if self.inspect_error:
            raise self.inspect_error
        if actor_id != self.assignment.callback_actor_id or actor_role != self.assignment.callback_actor_role:
            raise PhoneVerificationTaskNotAssigned("not assigned")
        return replace(self.assignment, verification_id=verification_id)

    def persist_and_enqueue(
        self, *, assignment, outbox_repository, execution_id, ontology_version,
        callback_id, conclusion, **_values
    ):
        self.persist_count += 1
        event = OutboxEvent(
            event_id="phone-result-outbox-1",
            execution_id=execution_id,
            event_type="PHONE_VERIFICATION_RESULT_GRAPH_SYNC",
            aggregate_type="PhoneVerificationRecord",
            aggregate_id=assignment.verification_id,
            ontology_version=ontology_version,
            deduplication_key=execution_id,
            payload={
                "application_id": assignment.application_id,
                "verification_id": assignment.verification_id,
                "callback_id": callback_id,
                "conclusion": conclusion,
            },
            status="PENDING",
            attempt_count=0,
            max_attempts=5,
        )
        outbox_repository.event = event
        return {"conclusion": conclusion}, event


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
                "verification_id": self.outbox.event.aggregate_id,
                "record_status": "COMPLETED",
                "persistence": {
                    "postgres": "terminal_result_recorded",
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


class RecordPhoneVerificationActionTests(unittest.TestCase):
    def service(self, *, results=None, pending=False):
        proposal = build_personal_credit_baseline()
        executions = MemoryExecutionRepository()
        outbox = FakeOutboxRepository()
        results = results or FakeResultRepository()
        worker = PendingWorker(outbox) if pending else SuccessfulWorker(executions, outbox)
        service = RecordPhoneVerificationActionService(
            postgres_service=object(), nebula_factory=lambda: None,
            proposal=proposal, execution_repository=executions,
            task_repository=object(), result_repository=results,
            outbox_repository=outbox, outbox_worker=worker,
        )
        return service, proposal, executions, results

    @staticmethod
    def execute(service, proposal, request=None, *, role="数字人适配器", actor="phone-result-adapter"):
        return service.execute(
            request or valid_request(), ontology_version=proposal.version,
            actor_id=actor, actor_role=role,
        )

    def test_contract_rejects_unknown_field_naive_time_and_invalid_conclusion(self):
        request = valid_request(); request["request_id"] = "not-declared"
        with self.assertRaisesRegex(Exception, "未声明字段"):
            validate_phone_verification_result_request(request)
        request = valid_request(); request["call_time"] = "2026-08-25T12:00:00"
        with self.assertRaisesRegex(Exception, "包含时区"):
            validate_phone_verification_result_request(request)
        request = valid_request(); request["conclusion"] = "批准贷款"
        with self.assertRaisesRegex(Exception, "受控枚举"):
            validate_phone_verification_result_request(request)

    def test_doubt_and_duration_axioms_are_enforced(self):
        request = valid_request(); request.update(conclusion="存疑", doubts=None)
        with self.assertRaisesRegex(Exception, "必须填写 doubts"):
            validate_phone_verification_result_request(request)
        request = valid_request(); request["duration_seconds"] = 0
        with self.assertRaisesRegex(Exception, "必须大于 0"):
            validate_phone_verification_result_request(request)
        request["conclusion"] = "未接通"
        self.assertEqual(validate_phone_verification_result_request(request).duration_seconds, 0)

    def test_success_and_same_payload_replay_write_once(self):
        service, proposal, _executions, results = self.service()
        request = valid_request()
        first = self.execute(service, proposal, request)
        second = self.execute(service, proposal, request)
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(results.persist_count, 1)

    def test_different_payload_for_terminal_idempotency_key_conflicts(self):
        service, proposal, _executions, results = self.service()
        self.execute(service, proposal)
        changed = valid_request(); changed["conclusion"] = "通过"; changed["doubts"] = None
        with self.assertRaises(ActionIdempotencyConflict):
            self.execute(service, proposal, changed)
        self.assertEqual(results.persist_count, 1)

    def test_role_and_assignment_are_both_enforced(self):
        service, proposal, _executions, results = self.service()
        with self.assertRaises(ActionAuthorizationError):
            self.execute(service, proposal, role="Agent")
        self.assertEqual(results.inspect_count, 0)
        service, proposal, _executions, results = self.service()
        with self.assertRaises(RecordPhoneVerificationError) as raised:
            self.execute(service, proposal, actor="another-adapter")
        self.assertEqual(raised.exception.code, "ACTION_PHONE_TASK_NOT_ASSIGNED")
        self.assertEqual(results.persist_count, 0)

    def test_not_found_and_terminal_conflict_are_controlled(self):
        for error, code in (
            (PhoneVerificationTaskNotFound("missing"), "ACTION_PHONE_TASK_NOT_FOUND"),
            (PhoneVerificationTerminalConflict("terminal"), "ACTION_TERMINAL_STATE_CONFLICT"),
        ):
            service, proposal, executions, _results = self.service(
                results=FakeResultRepository(inspect_error=error)
            )
            with self.assertRaises(RecordPhoneVerificationError) as raised:
                self.execute(service, proposal)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(next(iter(executions.records.values())).status, "FAILED")

    def test_graph_retry_retains_committed_terminal_result(self):
        service, proposal, executions, results = self.service(pending=True)
        response = self.execute(service, proposal)
        self.assertEqual(response["status"], "COMMITTING")
        self.assertEqual(response["outbox"]["status"], "RETRY")
        self.assertEqual(response["result"]["record_status"], "COMPLETED")
        self.assertEqual(results.persist_count, 1)
        self.assertEqual(next(iter(executions.records.values())).status, "COMMITTING")

    def test_repository_uses_row_lock_receipt_and_no_terminal_compensation(self):
        repository = (ROOT / "backend/app/action/phone_verification_result_repository.py").read_text(encoding="utf-8")
        outbox = (ROOT / "backend/app/action/outbox.py").read_text(encoding="utf-8")
        self.assertIn("FOR UPDATE OF task,record", repository)
        self.assertIn("phone_verification_callbacks", repository)
        self.assertIn("PHONE_VERIFICATION_RESULT_GRAPH_SYNC", repository)
        self.assertIn("result_sync_status='PENDING'", repository)
        self.assertIn("retained_terminal_result", outbox)


if __name__ == "__main__":
    unittest.main()
