import pathlib
import unittest
from dataclasses import replace
from types import SimpleNamespace

from app.action import (
    OutboxEvent,
    OutboxReplayError,
    OutboxReplayRecord,
    OutboxReplayService,
)


ROOT = pathlib.Path(__file__).resolve().parents[2]
EVENT_ID = "11111111-1111-4111-8111-111111111111"


def replay_record(status="REQUESTED"):
    return OutboxReplayRecord(
        replay_id="22222222-2222-4222-8222-222222222222",
        event_id=EVENT_ID,
        execution_id="execution-1",
        event_type="RISK_ASSESSMENT_GRAPH_SYNC",
        request_id="REPLAY-REQ-1",
        requested_by="ops-1",
        requested_role="系统运维",
        reason="NebulaGraph 已恢复，申请人工重放图同步",
        reason_digest="r" * 64,
        status=status,
        result_summary=(
            {"event_status": "SUCCEEDED"} if status == "SUCCEEDED" else None
        ),
    )


def dead_letter_event():
    return OutboxEvent(
        event_id=EVENT_ID,
        execution_id="execution-1",
        event_type="RISK_ASSESSMENT_GRAPH_SYNC",
        aggregate_type="RiskAssessmentReport",
        aggregate_id="RA-1",
        ontology_version="1.1.0",
        deduplication_key="execution-1",
        payload={},
        status="DEAD_LETTER",
        attempt_count=5,
        max_attempts=5,
    )


class FakeReplayRepository:
    def __init__(self, *, created=True, existing_status="SUCCEEDED"):
        self.created = created
        self.record = replay_record(
            "REQUESTED" if created else existing_status
        )
        self.event = dead_letter_event()
        self.reserve_count = 0

    def ensure_schema(self):
        return None

    def reserve(self, **_values):
        self.reserve_count += 1
        return self.record, self.created, self.event

    def mark_processing(self, replay_id):
        self.record = replace(self.record, status="PROCESSING")
        return self.record

    def mark_succeeded(self, replay_id, result_summary):
        self.record = replace(
            self.record, status="SUCCEEDED", result_summary=result_summary
        )
        return self.record

    def mark_failed(self, replay_id, *, error_code, error_message):
        self.record = replace(
            self.record,
            status="FAILED",
            error_code=error_code,
            error_message=error_message,
        )
        return self.record


class FakeExecutions:
    def __init__(self):
        self.events = []
        self.record = SimpleNamespace(status="FAILED")

    def record_event(self, execution_id, *, event_type, actor_id, details=None):
        self.events.append(
            {"event_type": event_type, "actor_id": actor_id, "details": details}
        )

    def get(self, execution_id):
        return self.record


class SuccessfulReplayWorker:
    def replay_dead_letter(self, event_id, **_values):
        return replace(dead_letter_event(), status="SUCCEEDED"), {
            "mapping_valid": True
        }


class FailedReplayWorker:
    def replay_dead_letter(self, event_id, **_values):
        raise RuntimeError("graph unavailable")


class OutboxReplayTests(unittest.TestCase):
    def service(self, *, repository=None, worker=None):
        repository = repository or FakeReplayRepository()
        executions = FakeExecutions()
        workers = (
            {"RISK_ASSESSMENT_GRAPH_SYNC": worker}
            if worker is not None
            else {}
        )
        return (
            OutboxReplayService(
                repository=repository,
                execution_repository=executions,
                workers=workers,
            ),
            repository,
            executions,
        )

    @staticmethod
    def execute(service):
        return service.execute(
            event_id=EVENT_ID,
            request_id="REPLAY-REQ-1",
            reason="NebulaGraph 已恢复，申请人工重放图同步",
            actor_id="ops-1",
            actor_role="系统运维",
        )

    def test_success_records_request_and_preserves_original_history(self):
        service, repository, executions = self.service(
            worker=SuccessfulReplayWorker()
        )
        result = self.execute(service)
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertTrue(result["original_action_history_preserved"])
        self.assertEqual(repository.record.status, "SUCCEEDED")
        self.assertEqual(
            executions.events[0]["event_type"], "manual_outbox_replay_requested"
        )

    def test_same_request_returns_idempotent_history_without_worker(self):
        repository = FakeReplayRepository(created=False)
        service, repository, executions = self.service(repository=repository)
        result = self.execute(service)
        self.assertTrue(result["idempotent_replay"])
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(executions.events, [])

    def test_failed_handler_is_audited_and_does_not_claim_success(self):
        service, repository, executions = self.service(worker=FailedReplayWorker())
        result = self.execute(service)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(
            result["error_code"], "ACTION_OUTBOX_MANUAL_REPLAY_FAILED"
        )
        self.assertEqual(repository.record.status, "FAILED")
        self.assertEqual(
            executions.events[-1]["event_type"], "manual_outbox_replay_failed"
        )

    def test_missing_handler_fails_closed(self):
        service, repository, _executions = self.service()
        result = self.execute(service)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error_code"], "ACTION_OUTBOX_HANDLER_NOT_FOUND")
        self.assertEqual(repository.record.status, "FAILED")

    def test_request_validation_rejects_invalid_id_and_short_reason(self):
        service, repository, _executions = self.service()
        with self.assertRaises(OutboxReplayError) as raised:
            service.execute(
                event_id="not-a-uuid",
                request_id="REQ",
                reason="足够长的重放原因说明",
                actor_id="ops-1",
                actor_role="系统运维",
            )
        self.assertEqual(raised.exception.code, "ACTION_OUTBOX_EVENT_INVALID")
        with self.assertRaises(OutboxReplayError) as raised:
            service.execute(
                event_id=EVENT_ID,
                request_id="REQ",
                reason="太短",
                actor_id="ops-1",
                actor_role="系统运维",
            )
        self.assertEqual(
            raised.exception.code, "ACTION_OUTBOX_REPLAY_REQUEST_INVALID"
        )
        self.assertEqual(repository.reserve_count, 0)

    def test_repository_has_lock_idempotency_alerts_and_reason_digest(self):
        source = (ROOT / "backend/app/action/outbox_replay.py").read_text(
            encoding="utf-8"
        )
        outbox = (ROOT / "backend/app/action/outbox.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("outbox_replay_requests", source)
        self.assertIn("UNIQUE (event_id,request_id)", source)
        self.assertIn("FOR UPDATE OF event", source)
        self.assertIn("reason_digest", source)
        self.assertIn("event.status='DEAD_LETTER'", source)
        self.assertIn("claim_dead_letter", outbox)
        self.assertIn("restore_dead_letter", outbox)


if __name__ == "__main__":
    unittest.main()
