import pathlib
import unittest
from dataclasses import replace
from datetime import datetime

from app.action import (
    ActionAuthorizationError,
    ActionExecutionNotFound,
    ActionExecutionRecord,
    ActionIdempotencyConflict,
    ActionReservation,
    ActionStateMachine,
    CreateLoanApplicationActionService,
    CreateLoanApplicationError,
    LoanApplicationAlreadyExists,
    LoanApplicationEffect,
    OutboxEvent,
    validate_loan_application_request,
)
from app.ontology.baseline import build_personal_credit_baseline
from app.core.security import mask_payload
from app.action.loan_application_repository import _comparable


ROOT = pathlib.Path(__file__).resolve().parents[2]


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
        execution_id = f"application-execution-{self.sequence}"
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
        if current.version != expected_version:
            raise AssertionError("stale version")
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

    def record_event(self, execution_id, *, event_type, actor_id, details=None):
        self.get(execution_id)
        self.events[execution_id].append(event_type)

    def verify_audit_chain(self, execution_id):
        return bool(self.events.get(execution_id))


class FakeOutboxRepository:
    def __init__(self):
        self.events = {}

    def ensure_schema(self):
        return None

    def get(self, event_id):
        return self.events[event_id]


class FakeApplicationRepository:
    def __init__(self, *, existing=False):
        self.existing = existing
        self.persist_count = 0
        self.compensate_count = 0

    def persist_and_enqueue(self, **values):
        self.persist_count += 1
        if self.existing:
            raise LoanApplicationAlreadyExists("already exists")
        application = values["application"]
        execution_id = values["execution_id"]
        effect = LoanApplicationEffect(
            application_id=application["application_id"],
            application_fingerprint="f" * 64,
            created_entities={
                "natural_person": (values["borrower"]["id_number"],),
                "vehicle": (values["vehicle"]["vin"],),
                "dealer": (),
            },
        )
        event = OutboxEvent(
            event_id="application-outbox-1",
            execution_id=execution_id,
            event_type="LOAN_APPLICATION_GRAPH_SYNC",
            aggregate_type="LoanApplication",
            aggregate_id=application["application_id"],
            ontology_version=values["ontology_version"],
            deduplication_key=execution_id,
            payload={
                "actor_id": values["actor_id"],
                "application_id": application["application_id"],
                "application_status": application["application_status"],
                "created_relations": ["SUBMITS", "TARGETS"],
                "entity_operations": {"borrower": "created", "vehicle": "created"},
                "effect": {
                    "application_id": effect.application_id,
                    "application_fingerprint": effect.application_fingerprint,
                    "created_entities": {
                        "natural_person": [
                            {"id_number": values["borrower"]["id_number"]}
                        ],
                        "vehicle": [{"vin": values["vehicle"]["vin"]}],
                        "dealer": [],
                    },
                },
            },
            status="PENDING",
            attempt_count=0,
            max_attempts=values["max_attempts"],
        )
        values["outbox_repository"].events[event.event_id] = event
        return {
            **application,
            "borrower_id": values["borrower"]["id_number"],
            "vehicle_vin": values["vehicle"]["vin"],
        }, effect, event

    def compensate(self, effect):
        self.compensate_count += 1


class FakeApplicationWorker:
    def __init__(self, executions, outbox, applications, *, fail=False):
        self.executions = executions
        self.outbox = outbox
        self.applications = applications
        self.fail = fail

    def process_event(self, event_id):
        event = replace(
            self.outbox.get(event_id), status="PROCESSING", attempt_count=1
        )
        self.outbox.events[event_id] = event
        current = self.executions.get(event.execution_id)
        if self.fail:
            current = self.executions.transition(
                current.execution_id,
                "COMPENSATING",
                expected_version=current.version,
                event_type="compensation_started",
                actor_id="application-worker",
            )
            self.applications.compensate(None)
            self.executions.transition(
                current.execution_id,
                "COMPENSATED",
                expected_version=current.version,
                event_type="action_compensated",
                actor_id="application-worker",
                result_summary={
                    "compensated": True,
                    "original_error_code": "ACTION_GRAPH_SYNC_FAILED",
                },
            )
            event = replace(event, status="COMPENSATED")
            self.outbox.events[event_id] = event
            return event
        self.executions.record_event(
            event.execution_id,
            event_type="application_created",
            actor_id="application-worker",
        )
        current = self.executions.get(event.execution_id)
        self.executions.transition(
            current.execution_id,
            "SUCCEEDED",
            expected_version=current.version,
            event_type="action_succeeded",
            actor_id="application-worker",
            result_summary={
                "application_id": event.payload["application_id"],
                "application_status": event.payload["application_status"],
                "created_relations": event.payload["created_relations"],
                "entity_operations": event.payload["entity_operations"],
                "persistence": {"postgres": "created", "nebulagraph": "synchronized"},
                "decision_boundary": "origination_only_no_credit_decision",
            },
        )
        event = replace(event, status="SUCCEEDED")
        self.outbox.events[event_id] = event
        return event


class TestApplicationService(CreateLoanApplicationActionService):
    def __init__(self, *, existing=False, graph_failure=False):
        self.fake_executions = MemoryExecutionRepository()
        self.fake_outbox = FakeOutboxRepository()
        self.fake_applications = FakeApplicationRepository(existing=existing)
        self.fake_worker = FakeApplicationWorker(
            self.fake_executions,
            self.fake_outbox,
            self.fake_applications,
            fail=graph_failure,
        )
        proposal = build_personal_credit_baseline()
        super().__init__(
            postgres_service=object(),
            nebula_factory=lambda: None,
            proposal=proposal,
            execution_repository=self.fake_executions,
            application_repository=self.fake_applications,
            outbox_repository=self.fake_outbox,
            outbox_worker=self.fake_worker,
            outbox_max_attempts=1,
        )


def request(**updates):
    value = {
        "application_id": "APP20260824000999",
        "application": {
            "submit_date": "2026-08-24T10:30:00+08:00",
            "channel": "线上H5",
            "loan_amount": 100000,
            "loan_term": 36,
            "interest_rate": 0.045,
            "application_status": "评估中",
        },
        "borrower": {
            "id_number": "110101199901010019",
            "name": "测试借款人",
            "phone": "13900000009",
            "address": "测试地址",
            "monthly_income": 20000,
        },
        "vehicle": {
            "vin": "LVGBM51K0HG999999",
            "brand": "测试品牌",
            "model": "测试车型",
            "invoice_price": 160000,
            "guide_price": 180000,
            "pledge_status": "未抵押",
        },
        "request_id": "REQ-CREATE-999",
    }
    value.update(updates)
    return value


class CreateLoanApplicationActionTests(unittest.TestCase):
    def execute(self, service, values=None, role="系统适配器"):
        return service.execute(
            values or request(),
            ontology_version=service.proposal.version,
            actor_id="origination-test",
            actor_role=role,
        )

    def test_valid_request_is_normalized_and_has_no_decision(self):
        validated = validate_loan_application_request(request())
        self.assertEqual(validated.application["application_status"], "评估中")
        self.assertNotIn("decision_result", validated.application)
        self.assertEqual(str(validated.application["interest_rate"]), "0.045")

    def test_offline_channel_requires_dealer(self):
        values = request()
        values["application"] = {**values["application"], "channel": "经销商APP"}
        with self.assertRaisesRegex(Exception, "必须提供 dealer"):
            validate_loan_application_request(values)

    def test_roles_are_pairwise_distinct(self):
        values = request(
            co_borrower={
                "id_number": "110101199901010019",
                "name": "重复角色",
            }
        )
        with self.assertRaisesRegex(Exception, "必须互不相同"):
            validate_loan_application_request(values)

    def test_final_state_cannot_be_injected_at_origination(self):
        values = request()
        values["application"] = {
            **values["application"],
            "application_status": "已完成",
        }
        with self.assertRaisesRegex(Exception, "新申请状态"):
            validate_loan_application_request(values)

    def test_success_persists_once_syncs_graph_and_audits(self):
        service = TestApplicationService()
        response = self.execute(service)
        self.assertEqual(response["status"], "SUCCEEDED")
        self.assertEqual(response["result"]["created_relations"], ["SUBMITS", "TARGETS"])
        self.assertEqual(
            response["result"]["decision_boundary"],
            "origination_only_no_credit_decision",
        )
        self.assertEqual(service.fake_applications.persist_count, 1)
        events = service.fake_executions.events[response["execution_id"]]
        for required in (
            "authorization_checked",
            "idempotency_checked",
            "application_axioms_validated",
            "graph_sync_enqueued",
            "application_created",
            "action_succeeded",
        ):
            self.assertIn(required, events)

    def test_same_request_replays_without_second_write(self):
        service = TestApplicationService()
        first = self.execute(service)
        second = self.execute(service)
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["execution_id"], second["execution_id"])
        self.assertEqual(service.fake_applications.persist_count, 1)

    def test_same_application_changed_payload_conflicts(self):
        service = TestApplicationService()
        self.execute(service)
        changed = request()
        changed["application"] = {**changed["application"], "loan_amount": 120000}
        with self.assertRaises(ActionIdempotencyConflict):
            self.execute(service, changed)

    def test_legacy_existing_application_fails_without_overwrite(self):
        service = TestApplicationService(existing=True)
        with self.assertRaises(CreateLoanApplicationError) as context:
            self.execute(service)
        self.assertEqual(context.exception.code, "ACTION_APPLICATION_ALREADY_EXISTS")
        self.assertEqual(context.exception.status, "FAILED")
        self.assertEqual(service.fake_applications.persist_count, 1)

    def test_graph_failure_compensates_created_application(self):
        service = TestApplicationService(graph_failure=True)
        with self.assertRaises(CreateLoanApplicationError) as context:
            self.execute(service)
        self.assertEqual(context.exception.code, "ACTION_GRAPH_SYNC_FAILED")
        self.assertEqual(context.exception.status, "COMPENSATED")
        self.assertEqual(service.fake_applications.compensate_count, 1)

    def test_unauthorized_role_never_writes(self):
        service = TestApplicationService()
        with self.assertRaises(ActionAuthorizationError):
            self.execute(service, role="风控专员")
        self.assertEqual(service.fake_applications.persist_count, 0)

    def test_repository_is_atomic_locked_and_never_updates_master_data(self):
        source = (
            ROOT / "backend/app/action/loan_application_repository.py"
        ).read_text(encoding="utf-8")
        self.assertIn("pg_advisory_xact_lock", source)
        self.assertIn("persist_and_enqueue", source)
        self.assertIn("outbox_repository.enqueue", source)
        self.assertIn("FOR UPDATE", source)
        self.assertNotIn("UPDATE public.natural_person", source)
        self.assertNotIn("UPDATE public.vehicle", source)
        self.assertNotIn("UPDATE public.dealer", source)
        self.assertEqual(
            _comparable(datetime.fromisoformat("2026-08-24T16:00:00+08:00")),
            _comparable(datetime.fromisoformat("2026-08-24T08:00:00+00:00")),
        )

    def test_outbox_effect_identifiers_are_keyed_for_recursive_masking(self):
        payload = {
            "effect": {
                "created_entities": {
                    "natural_person": [{"id_number": "110101199901010019"}],
                    "vehicle": [{"vin": "LVGBM51K0HG999999"}],
                    "dealer": [{"credit_code": "DEALER-SENSITIVE-1"}],
                }
            }
        }
        masked = mask_payload(payload)
        self.assertNotEqual(
            masked["effect"]["created_entities"]["vehicle"][0]["vin"],
            "LVGBM51K0HG999999",
        )
        self.assertNotEqual(
            masked["effect"]["created_entities"]["dealer"][0]["credit_code"],
            "DEALER-SENSITIVE-1",
        )

    def test_api_identity_is_server_controlled(self):
        source = (ROOT / "backend/app/api/routes.py").read_text(encoding="utf-8")
        self.assertIn("actor_id=MVP_CREATE_APPLICATION_ACTOR_ID", source)
        self.assertIn("actor_role=MVP_CREATE_APPLICATION_ACTOR_ROLE", source)
        self.assertNotIn("actor_role:str", str(request()))


if __name__ == "__main__":
    unittest.main()
