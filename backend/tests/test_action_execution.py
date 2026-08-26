import pathlib
import unittest
from dataclasses import replace

from app.action import (
    ACTION_STATE_TRANSITIONS,
    ActionAuthorizationError,
    ActionConcurrentUpdateError,
    ActionExecutionNotFound,
    ActionExecutionRecord,
    ActionExecutionService,
    ActionIdempotencyConflict,
    ActionRequestError,
    ActionReservation,
    ActionStateMachine,
    ActionTransitionError,
    UnknownActionError,
    action_idempotency_key_hash,
    action_request_digest,
)


ROOT = pathlib.Path(__file__).resolve().parents[2]


class MemoryActionRepository:
    def __init__(self):
        self.records = {}
        self.by_idempotency = {}
        self.sequence = 0

    def reserve(self, **values):
        key = (values["action_name"], values["idempotency_key_hash"])
        existing_id = self.by_idempotency.get(key)
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
        self.by_idempotency[key] = execution_id
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
        if current.version != expected_version:
            raise ActionConcurrentUpdateError("ACTION_EXECUTION_VERSION_CONFLICT")
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
        return updated


def risk_request(**updates):
    request = {
        "application_id": "APP-1",
        "assessment_version": "risk-v1",
        "data_snapshot_policy": "repeatable-read",
        "request_id": "REQ-1",
    }
    request.update(updates)
    return request


class ActionExecutionTests(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryActionRepository()
        self.service = ActionExecutionService(self.repository)

    def test_request_digest_is_canonical_and_version_bound(self):
        first = action_request_digest(
            "generate_risk_assessment",
            "1.0.0",
            {"b": 2, "a": 1},
        )
        reordered = action_request_digest(
            "generate_risk_assessment",
            "1.0.0",
            {"a": 1, "b": 2},
        )
        changed_version = action_request_digest(
            "generate_risk_assessment",
            "1.0.1",
            {"a": 1, "b": 2},
        )
        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed_version)
        self.assertEqual(len(first), 64)
        self.assertEqual(
            action_request_digest(
                "generate_risk_assessment",
                "1.0.0",
                {"a": 1, "request_id": "REQ-1"},
            ),
            action_request_digest(
                "generate_risk_assessment",
                "1.0.0",
                {"a": 1, "request_id": "REQ-2"},
            ),
        )

    def test_idempotency_hash_uses_only_declared_fields(self):
        base = action_idempotency_key_hash(
            "generate_risk_assessment",
            risk_request(),
        )
        changed_non_key = action_idempotency_key_hash(
            "generate_risk_assessment",
            risk_request(request_id="REQ-2"),
        )
        changed_key = action_idempotency_key_hash(
            "generate_risk_assessment",
            risk_request(assessment_version="risk-v2"),
        )
        self.assertEqual(base, changed_non_key)
        self.assertNotEqual(base, changed_key)
        with self.assertRaises(ActionRequestError):
            action_idempotency_key_hash(
                "generate_risk_assessment",
                {"application_id": "APP-1"},
            )

    def test_state_machine_rejects_skip_and_terminal_restart(self):
        self.assertTrue(ActionStateMachine.can_transition("RECEIVED", "AUTHORIZING"))
        self.assertTrue(ActionStateMachine.can_transition("RUNNING", "WAITING_APPROVAL"))
        with self.assertRaises(ActionTransitionError):
            ActionStateMachine.require_transition("RECEIVED", "COMMITTING")
        with self.assertRaises(ActionTransitionError):
            ActionStateMachine.require_transition("SUCCEEDED", "RUNNING")

    def test_every_nonterminal_state_can_fail_closed(self):
        for state, targets in ACTION_STATE_TRANSITIONS.items():
            if targets:
                expected_failure = (
                    "COMPENSATION_FAILED" if state == "COMPENSATING" else "FAILED"
                )
                self.assertIn(expected_failure, targets, state)

    def test_service_rejects_unknown_action_or_unauthorized_role(self):
        with self.assertRaises(UnknownActionError):
            self.service.start(
                "approve_application",
                {},
                ontology_version="1.0.0",
                actor_id="user-1",
                actor_role="风险经理",
            )
        with self.assertRaises(ActionAuthorizationError):
            self.service.start(
                "generate_risk_assessment",
                risk_request(),
                ontology_version="1.0.0",
                actor_id="agent-1",
                actor_role="Agent",
            )

    def test_same_request_is_idempotent_and_changed_payload_conflicts(self):
        first = self.service.start(
            "generate_risk_assessment",
            risk_request(),
            ontology_version="1.0.0",
            actor_id="risk-1",
            actor_role="风控专员",
        )
        repeated = self.service.start(
            "generate_risk_assessment",
            risk_request(),
            ontology_version="1.0.0",
            actor_id="risk-1",
            actor_role="风控专员",
        )
        self.assertTrue(first.created)
        self.assertFalse(repeated.created)
        self.assertEqual(first.record.execution_id, repeated.record.execution_id)
        repeated_with_new_request_id = self.service.start(
            "generate_risk_assessment",
            risk_request(request_id="REQ-DIFFERENT"),
            ontology_version="1.0.0",
            actor_id="risk-1",
            actor_role="风控专员",
        )
        self.assertFalse(repeated_with_new_request_id.created)
        with self.assertRaises(ActionIdempotencyConflict):
            self.service.start(
                "generate_risk_assessment",
                risk_request(data_snapshot_policy="latest-read"),
                ontology_version="1.0.0",
                actor_id="risk-1",
                actor_role="风控专员",
            )

    def test_optimistic_version_blocks_stale_transition(self):
        reservation = self.service.start(
            "generate_risk_assessment",
            risk_request(),
            ontology_version="1.0.0",
            actor_id="risk-1",
            actor_role="风控专员",
        )
        updated = self.service.advance(
            reservation.record.execution_id,
            "AUTHORIZING",
            expected_version=1,
            event_type="authorization_started",
            actor_id="risk-1",
        )
        self.assertEqual(updated.version, 2)
        with self.assertRaises(ActionConcurrentUpdateError):
            self.service.advance(
                reservation.record.execution_id,
                "VALIDATING",
                expected_version=1,
                event_type="validation_started",
                actor_id="risk-1",
            )

    def test_recommendation_allows_agent_but_not_final_approval_action(self):
        request = {
            "application_id": "APP-1",
            "recommendation_version": "rec-v1",
            "approver_id": "manager-1",
            "approval_assertion": "confirmed",
            "request_id": "REQ-1",
        }
        reservation = self.service.start(
            "record_decision_recommendation",
            request,
            ontology_version="1.0.0",
            actor_id="agent-1",
            actor_role="Agent",
        )
        self.assertTrue(reservation.created)

    def test_postgres_schema_is_append_only_and_stores_no_raw_request(self):
        source = (
            ROOT / "backend/app/action/execution_repository.py"
        ).read_text(encoding="utf-8")
        lowered = source.lower()
        self.assertIn("unique (action_name,idempotency_key_hash)", lowered)
        self.assertIn("before update or delete", lowered)
        self.assertIn("action_audit_events is append-only", lowered)
        self.assertIn("previous_event_digest", lowered)
        self.assertIn("event_digest", lowered)
        self.assertNotIn("request_payload jsonb", lowered)
        self.assertIn("request_digest char(64)", lowered)

    def test_application_startup_initializes_action_control_schema(self):
        source = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
        self.assertIn("execution_repository.ensure_schema()", source)
        self.assertIn("outbox_repository.ensure_schema()", source)
        self.assertIn("run_outbox_worker", source)
        self.assertIn("LoanApplicationOutboxWorker", source)
        self.assertIn("safe_request_validation_error", source)
        self.assertIn("lifespan=lifespan", source)


if __name__ == "__main__":
    unittest.main()
