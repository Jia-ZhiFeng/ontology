import pathlib
import unittest
from dataclasses import replace
from decimal import Decimal

from app.action import (
    ActionAuthorizationError,
    OutboxEvent,
    PhoneTaskDispatchRequest,
    PhoneVerificationContext,
    PhoneVerificationPrerequisiteFailed,
    PhoneVerificationTaskEffect,
    PhoneVerificationTriggerNotMet,
    CreatePhoneVerificationTaskActionService,
    CreatePhoneVerificationTaskError,
    SandboxPhoneTaskDispatcher,
    deterministic_phone_dispatch_id,
    validate_phone_dispatch_response,
    validate_phone_verification_task_request,
)
from app.ontology.baseline import build_personal_credit_baseline
from tests.test_request_credit_report_action import MemoryExecutionRepository


ROOT = pathlib.Path(__file__).resolve().parents[2]


class FakeFunctionAdapter:
    def __init__(self, *, valid=True):
        self.valid = valid
        self.calls = 0

    def execute_phone_questions(self, application_id, **_values):
        self.calls += 1
        timestamp = "2026-08-25T08:00:00+00:00"
        questions = [{
            "question_id": "Q01",
            "category": "征信查询",
            "question": "请说明近期征信查询对应的融资申请及结果。",
            "source_factors": ["INQUIRY_HIGH"],
            "required": True,
        }]
        return {
            "application_id": application_id,
            "status": "success",
            "read_only": True,
            "postgres_snapshot_id": "300:300:",
            "postgres_snapshot_time": timestamp,
            "cross_store_atomic": True,
            "execution_order": ["generate_phone_questions"],
            "functions": {
                "generate_phone_questions": {
                    "status": "success",
                    "result": {
                        "phone_questions": questions if self.valid else [],
                        "question_count": 1,
                        "template_version": "phone-question-templates-v1.0.0",
                    },
                    "evidence": [{"source_factor": "INQUIRY_HIGH"}],
                    "complete": True,
                    "implementation_version": "phone-questions-v1.0.0",
                    "duration_ms": 1,
                    "data_snapshot_time": timestamp,
                }
            },
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


class FakeTaskRepository:
    def __init__(self, *, prerequisite_error=None):
        self.prerequisite_error = prerequisite_error
        self.persist_count = 0
        self.context = PhoneVerificationContext(
            application_id="APP20260801000001",
            application_status="评估中",
            borrower_name="张伟",
            risk_assessment_id="RA20260001",
            risk_level="中",
            risk_factors="查询偏高",
            risk_check_result="待人工复核",
            fraud_report_id="FR20260001",
            fraud_score=Decimal("18.5"),
            fraud_result="通过",
            fraud_hit_details="未命中高风险规则",
            verified_trigger_signals=("RISK_LEVEL_中", "RISK_CHECK_待人工复核"),
        )

    def ensure_schema(self):
        return None

    def inspect_target(self, application_id):
        if self.prerequisite_error:
            raise self.prerequisite_error
        return replace(self.context, application_id=application_id)

    def persist_and_enqueue(
        self, *, outbox_repository, execution_id, ontology_version,
        verification_round, context, **_values
    ):
        self.persist_count += 1
        effect = PhoneVerificationTaskEffect(
            operation="created",
            verification_id="PVPHONE0001",
            application_id=context.application_id,
            verification_round=verification_round,
            dispatch_id=deterministic_phone_dispatch_id(
                context.application_id, verification_round
            ),
        )
        event = OutboxEvent(
            event_id="phone-outbox-1",
            execution_id=execution_id,
            event_type="PHONE_VERIFICATION_TASK_DISPATCH",
            aggregate_type="PhoneVerificationRecord",
            aggregate_id=effect.verification_id,
            ontology_version=ontology_version,
            deduplication_key=execution_id,
            payload={"effect": {"operation": "created"}},
            status="PENDING",
            attempt_count=0,
            max_attempts=5,
        )
        outbox_repository.event = event
        return (
            {"verification_id": effect.verification_id},
            {"dispatch_status": "PENDING"},
            effect,
            event,
        )


class SuccessfulOutboxWorker:
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
                "verification_id": self.outbox.event.aggregate_id,
                "dispatch_status": "DISPATCHED",
                "question_count": 1,
                "created_relations": ["HAS_PHONE_VERIFICATION"],
                "persistence": {
                    "postgres": "created",
                    "nebulagraph": "synchronized",
                },
            },
        )
        self.outbox.event = replace(self.outbox.event, status="SUCCEEDED")
        return self.outbox.event


class PendingOutboxWorker:
    def __init__(self, outbox):
        self.outbox = outbox

    def process_event(self, event_id):
        self.outbox.event = replace(
            self.outbox.event,
            status="RETRY",
            attempt_count=1,
            last_error_code="ACTION_DISPATCH_PENDING",
            last_error_message="dispatcher disabled",
        )
        return self.outbox.event


def valid_request():
    return {
        "application_id": "APP20260801000001",
        "verification_round": 2,
        "trigger_reason": "风险评估待人工复核",
        "request_id": "REQ-PHONE-1",
    }


class CreatePhoneVerificationTaskActionTests(unittest.TestCase):
    def service(self, *, tasks=None, functions=None, pending=False):
        proposal = build_personal_credit_baseline()
        executions = MemoryExecutionRepository()
        outbox = FakeOutboxRepository()
        tasks = tasks or FakeTaskRepository()
        functions = functions or FakeFunctionAdapter()
        worker = (
            PendingOutboxWorker(outbox)
            if pending
            else SuccessfulOutboxWorker(executions, outbox)
        )
        service = CreatePhoneVerificationTaskActionService(
            postgres_service=object(),
            nebula_factory=lambda: None,
            proposal=proposal,
            execution_repository=executions,
            dispatch_coordinator=object(),
            task_repository=tasks,
            function_adapter=functions,
            outbox_repository=outbox,
            outbox_worker=worker,
        )
        return service, proposal, executions, tasks, functions

    def execute(self, service, proposal, *, role="系统适配器"):
        return service.execute(
            valid_request(),
            ontology_version=proposal.version,
            actor_id="phone-adapter",
            actor_role=role,
        )

    def test_request_contract_rejects_unknown_fields_and_invalid_round(self):
        values = valid_request()
        values["conclusion"] = "通过"
        with self.assertRaisesRegex(Exception, "未声明字段"):
            validate_phone_verification_task_request(values)
        values = valid_request()
        values["verification_round"] = 0
        with self.assertRaisesRegex(Exception, "1 到 99"):
            validate_phone_verification_task_request(values)

    def test_success_and_replay_generate_and_persist_once(self):
        service, proposal, _executions, tasks, functions = self.service()
        first = self.execute(service, proposal)
        second = self.execute(service, proposal)
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(tasks.persist_count, 1)
        self.assertEqual(functions.calls, 1)

    def test_role_denied_before_prerequisite_or_function(self):
        service, proposal, _executions, tasks, functions = self.service()
        with self.assertRaises(ActionAuthorizationError):
            self.execute(service, proposal, role="Agent")
        self.assertEqual(tasks.persist_count, 0)
        self.assertEqual(functions.calls, 0)

    def test_missing_reports_fail_before_question_generation(self):
        tasks = FakeTaskRepository(
            prerequisite_error=PhoneVerificationPrerequisiteFailed("missing reports")
        )
        service, proposal, executions, tasks, functions = self.service(tasks=tasks)
        with self.assertRaises(CreatePhoneVerificationTaskError) as raised:
            self.execute(service, proposal)
        self.assertEqual(raised.exception.code, "ACTION_PHONE_PREREQUISITE_FAILED")
        self.assertEqual(functions.calls, 0)
        self.assertEqual(next(iter(executions.records.values())).status, "FAILED")

    def test_trigger_not_met_blocks_question_generation(self):
        tasks = FakeTaskRepository(
            prerequisite_error=PhoneVerificationTriggerNotMet("no trigger")
        )
        service, proposal, _executions, tasks, functions = self.service(tasks=tasks)
        with self.assertRaises(CreatePhoneVerificationTaskError) as raised:
            self.execute(service, proposal)
        self.assertEqual(raised.exception.code, "ACTION_PHONE_TRIGGER_NOT_MET")
        self.assertEqual(functions.calls, 0)

    def test_invalid_question_set_never_creates_task(self):
        functions = FakeFunctionAdapter(valid=False)
        service, proposal, executions, tasks, _functions = self.service(
            functions=functions
        )
        with self.assertRaises(CreatePhoneVerificationTaskError) as raised:
            self.execute(service, proposal)
        self.assertEqual(raised.exception.code, "ACTION_QUESTION_SET_INVALID")
        self.assertEqual(tasks.persist_count, 0)
        self.assertEqual(next(iter(executions.records.values())).status, "FAILED")

    def test_disabled_dispatch_returns_committing_and_retains_task(self):
        service, proposal, executions, tasks, _functions = self.service(pending=True)
        result = self.execute(service, proposal)
        self.assertEqual(result["status"], "COMMITTING")
        self.assertEqual(result["outbox"]["status"], "RETRY")
        self.assertEqual(result["result"]["dispatch_status"], "PENDING")
        self.assertEqual(tasks.persist_count, 1)
        self.assertEqual(next(iter(executions.records.values())).status, "COMMITTING")

    def test_sandbox_dispatch_is_deterministic_and_synthetic(self):
        dispatch_id = deterministic_phone_dispatch_id("APP1", 1)
        request = PhoneTaskDispatchRequest(
            dispatch_id=dispatch_id,
            verification_id="PVTEST001",
            application_id="APP1",
            verification_round=1,
            verification_target="测试用户",
            trigger_reason="风险触发",
            questions=({
                "question_id": "Q01", "category": "还款能力",
                "question": "请确认收入。", "source_factors": ["DTI_HIGH"],
                "required": True,
            },),
        )
        first = SandboxPhoneTaskDispatcher().dispatch(request, timeout_seconds=10)
        second = SandboxPhoneTaskDispatcher().dispatch(request, timeout_seconds=10)
        self.assertEqual(first.external_task_id, second.external_task_id)
        self.assertTrue(first.synthetic)
        validate_phone_dispatch_response(
            first,
            request,
            expected_provider_name="local-phone-sandbox",
            expected_provider_mode="sandbox",
        )

    def test_repository_and_receipt_keep_dispatch_idempotent_and_retain_dead_letter(self):
        repository = (
            ROOT / "backend/app/action/phone_verification_repository.py"
        ).read_text(encoding="utf-8")
        dispatch = (ROOT / "backend/app/action/phone_dispatch.py").read_text(
            encoding="utf-8"
        )
        outbox = (ROOT / "backend/app/action/outbox.py").read_text(encoding="utf-8")
        self.assertIn("UNIQUE (application_id,verification_round)", repository)
        self.assertIn("PHONE_VERIFICATION_TASK_DISPATCH", repository)
        self.assertIn("phone_dispatch_receipts", dispatch)
        self.assertIn("request_context_hash", dispatch)
        self.assertIn("retain_on_dispatch_failure", repository)
        self.assertIn("_retain_dead_letter", outbox)


if __name__ == "__main__":
    unittest.main()
