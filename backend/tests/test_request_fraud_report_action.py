import pathlib
import unittest
from dataclasses import replace

from app.action import (
    ActionAuthorizationError,
    AntiFraudApplicationContext,
    AntiFraudApplicationIncomplete,
    AntiFraudReportEffect,
    FraudProviderRequest,
    FraudProviderResponse,
    FraudProviderResponseInvalid,
    FraudProviderTimeout,
    OutboxEvent,
    RequestFraudReportActionService,
    RequestFraudReportError,
    SandboxFraudProvider,
    deterministic_fraud_provider_request_id,
    validate_fraud_provider_response,
    validate_fraud_report_request,
)
from app.ontology.baseline import build_personal_credit_baseline
from tests.test_request_credit_report_action import MemoryExecutionRepository


ROOT = pathlib.Path(__file__).resolve().parents[2]


class FakeFunctionAdapter:
    def __init__(self, *, complete=True):
        self.complete = complete
        self.calls = 0

    def execute_association_risk(self, application_id, **_values):
        self.calls += 1
        timestamp = "2026-08-24T08:00:00+00:00"
        return {
            "application_id": application_id,
            "status": "success",
            "read_only": True,
            "postgres_snapshot_id": "100:100:",
            "postgres_snapshot_time": timestamp,
            "graph_snapshot_time": "2026-08-24T08:00:01+00:00",
            "cross_store_atomic": False,
            "execution_order": ["detect_association_risk"],
            "functions": {
                "detect_association_risk": {
                    "status": "success",
                    "result": {
                        "association_paths": [],
                        "association_risk_signals": [],
                        "query_coverage": {
                            "candidate_count": 0,
                            "evaluated_count": 0,
                            "truncated": not self.complete,
                        },
                        "rule_version": "personal-credit-rules-1.0",
                    },
                    "evidence": [{"source": "NebulaGraph"}],
                    "complete": self.complete,
                    "implementation_version": "association-risk-rules-1.0",
                    "duration_ms": 1,
                    "data_snapshot_time": timestamp,
                }
            },
        }


def provider_response(request):
    return FraudProviderResponse(
        provider_request_id=request.provider_request_id,
        application_id=request.application_id,
        fraud_report_id="FRTEST0001",
        fraud_score=12.5,
        result="通过",
        hit_details="未命中高风险规则",
        provider_name="test-fraud-provider",
        provider_mode="test",
        synthetic=False,
    )


class FakeProviderCoordinator:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = 0

    def query(self, request, **_values):
        self.calls += 1
        if self.fail:
            raise self.fail
        return provider_response(request), False


class FakeOutboxRepository:
    def __init__(self):
        self.event = None

    def ensure_schema(self):
        pass

    def get(self, event_id):
        if not self.event or self.event.event_id != event_id:
            raise KeyError(event_id)
        return self.event


class FakeReportRepository:
    def __init__(self, *, incomplete=False, fail_writes=0):
        self.incomplete = incomplete
        self.fail_writes = fail_writes
        self.persist_count = 0
        self.context = AntiFraudApplicationContext(
            application_id="APP20260805000003",
            borrower_id_number="110101198511090033",
            borrower_phone="13800000001",
            borrower_address="北京市海淀区",
            vehicle_vin="LVGBM51K0HG000003",
            application_channel=None,
            application_status="资料待补充",
        )

    def inspect_target(self, application_id):
        if self.incomplete:
            raise AntiFraudApplicationIncomplete("missing TARGETS")
        return replace(self.context, application_id=application_id)

    def persist_and_enqueue(self, *, response, outbox_repository, execution_id, **values):
        self.persist_count += 1
        if self.persist_count <= self.fail_writes:
            raise RuntimeError("temporary local write failure")
        effect = AntiFraudReportEffect(
            operation="materialized_placeholder",
            fraud_report_id="FR20260003",
            application_id=values["context"].application_id,
            previous_row={"fraud_score": None, "result": None, "hit_details": None},
            report_fingerprint="f" * 64,
        )
        event = OutboxEvent(
            event_id="fraud-outbox-1",
            execution_id=execution_id,
            event_type="ANTI_FRAUD_REPORT_GRAPH_SYNC",
            aggregate_type="AntiFraudReport",
            aggregate_id=effect.fraud_report_id,
            ontology_version=values["ontology_version"],
            deduplication_key=execution_id,
            payload={"effect": {"operation": effect.operation}},
            status="PENDING",
            attempt_count=0,
            max_attempts=5,
        )
        outbox_repository.event = event
        return {"fraud_report_id": effect.fraud_report_id}, effect, event


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
                "fraud_report_id": self.outbox.event.aggregate_id,
                "provider_request_id": "provider-request",
                "association_risk_summary": {"path_count": 0},
                "persistence": {
                    "postgres": "materialized_placeholder",
                    "nebulagraph": "synchronized",
                },
            },
        )
        self.outbox.event = replace(self.outbox.event, status="SUCCEEDED")
        return self.outbox.event


def valid_request():
    return {
        "application_id": "APP20260805000003",
        "request_id": "REQ-FRAUD-1",
    }


class RequestFraudReportActionTests(unittest.TestCase):
    def service(self, *, provider=None, reports=None, functions=None):
        proposal = build_personal_credit_baseline()
        executions = MemoryExecutionRepository()
        outbox = FakeOutboxRepository()
        reports = reports or FakeReportRepository()
        provider = provider or FakeProviderCoordinator()
        functions = functions or FakeFunctionAdapter()
        service = RequestFraudReportActionService(
            postgres_service=object(),
            nebula_factory=lambda: None,
            proposal=proposal,
            execution_repository=executions,
            provider_coordinator=provider,
            report_repository=reports,
            function_adapter=functions,
            outbox_repository=outbox,
            outbox_worker=SuccessfulOutboxWorker(executions, outbox),
        )
        return service, proposal, executions, provider, reports, functions

    def execute(self, service, proposal, *, role="系统适配器"):
        return service.execute(
            valid_request(),
            ontology_version=proposal.version,
            actor_id="fraud-adapter",
            actor_role=role,
        )

    def test_request_contract_rejects_unknown_fields(self):
        values = valid_request()
        values["fraud_score"] = 10
        with self.assertRaisesRegex(Exception, "未声明字段"):
            validate_fraud_report_request(values)

    def test_success_and_replay_call_function_and_provider_once(self):
        service, proposal, _executions, provider, reports, functions = self.service()
        first = self.execute(service, proposal)
        second = self.execute(service, proposal)
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(functions.calls, 1)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(reports.persist_count, 1)

    def test_local_write_retry_does_not_repeat_external_query(self):
        reports = FakeReportRepository(fail_writes=1)
        service, proposal, _executions, provider, reports, _functions = self.service(
            reports=reports
        )
        self.assertEqual(self.execute(service, proposal)["status"], "SUCCEEDED")
        self.assertEqual(reports.persist_count, 2)
        self.assertEqual(provider.calls, 1)

    def test_role_and_incomplete_application_fail_before_function_or_provider(self):
        service, proposal, _executions, provider, reports, functions = self.service()
        with self.assertRaises(ActionAuthorizationError):
            self.execute(service, proposal, role="Agent")
        self.assertEqual(functions.calls, 0)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(reports.persist_count, 0)

        reports = FakeReportRepository(incomplete=True)
        service, proposal, _executions, provider, _reports, functions = self.service(
            reports=reports
        )
        with self.assertRaises(RequestFraudReportError) as raised:
            self.execute(service, proposal)
        self.assertEqual(raised.exception.code, "ACTION_APPLICATION_INCOMPLETE")
        self.assertEqual(functions.calls, 0)
        self.assertEqual(provider.calls, 0)

    def test_incomplete_association_query_blocks_provider_and_report(self):
        functions = FakeFunctionAdapter(complete=False)
        service, proposal, executions, provider, reports, _functions = self.service(
            functions=functions
        )
        with self.assertRaises(RequestFraudReportError) as raised:
            self.execute(service, proposal)
        self.assertEqual(raised.exception.code, "ACTION_FUNCTION_OUTPUT_INVALID")
        self.assertEqual(provider.calls, 0)
        self.assertEqual(reports.persist_count, 0)
        self.assertEqual(next(iter(executions.records.values())).status, "FAILED")

    def test_provider_timeout_blocks_report_write(self):
        provider = FakeProviderCoordinator(FraudProviderTimeout("timeout"))
        service, proposal, executions, _provider, reports, _functions = self.service(
            provider=provider
        )
        with self.assertRaises(RequestFraudReportError) as raised:
            self.execute(service, proposal)
        self.assertEqual(raised.exception.code, "ACTION_EXTERNAL_TIMEOUT")
        self.assertEqual(reports.persist_count, 0)
        self.assertEqual(next(iter(executions.records.values())).status, "FAILED")

    def test_provider_response_must_match_application(self):
        request = FraudProviderRequest(
            provider_request_id="FPTEST",
            application_id="APP1",
            borrower_id_number="110101198803120011",
            borrower_phone=None,
            borrower_address=None,
            vehicle_vin="LHGCM82633A000001",
            application_channel="线上H5",
        )
        response = replace(provider_response(request), application_id="APP2")
        with self.assertRaises(FraudProviderResponseInvalid):
            validate_fraud_provider_response(
                response,
                request,
                expected_provider_name="test-fraud-provider",
                expected_provider_mode="test",
            )

    def test_sandbox_is_deterministic_and_synthetic(self):
        provider_request_id = deterministic_fraud_provider_request_id("APP1")
        request = FraudProviderRequest(
            provider_request_id=provider_request_id,
            application_id="APP1",
            borrower_id_number="110101198803120011",
            borrower_phone="13800000001",
            borrower_address="北京市朝阳区",
            vehicle_vin="LHGCM82633A000001",
            application_channel="线上H5",
        )
        first = SandboxFraudProvider().query(request, timeout_seconds=30)
        second = SandboxFraudProvider().query(request, timeout_seconds=30)
        self.assertEqual(first, second)
        self.assertTrue(first.synthetic)
        self.assertTrue(first.fraud_report_id.startswith("SBXFR"))
        self.assertNotIn(request.borrower_id_number, str(first.cache_payload()))

    def test_repository_and_receipt_encode_atomicity_and_no_raw_context(self):
        repository = (
            ROOT / "backend/app/action/anti_fraud_report_repository.py"
        ).read_text(encoding="utf-8")
        provider = (ROOT / "backend/app/action/fraud_provider.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("FOR UPDATE", repository)
        self.assertIn("outbox_repository.enqueue", repository)
        self.assertIn("report_fingerprint", repository)
        self.assertIn("fraud_provider_receipts", provider)
        self.assertIn("request_context_hash", provider)
        self.assertNotIn('"borrower_id_number": self.borrower_id_number', provider)


if __name__ == "__main__":
    unittest.main()
