import pathlib
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from app.action import (
    ActionAuthorizationError,
    ActionExecutionNotFound,
    ActionExecutionRecord,
    ActionIdempotencyConflict,
    ActionReservation,
    ActionStateMachine,
    CreditApplicationSubject,
    CreditConsentMissing,
    CreditProviderRequest,
    CreditProviderResponse,
    CreditProviderResponseInvalid,
    CreditProviderTimeout,
    CreditReportEffect,
    OutboxEvent,
    RequestCreditReportActionService,
    RequestCreditReportError,
    SandboxConsentVerifier,
    SandboxCreditProvider,
    deterministic_provider_request_id,
    validate_credit_report_request,
    validate_provider_response,
)
from app.ontology.baseline import build_personal_credit_baseline


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
        execution_id = f"credit-execution-{self.sequence}"
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
        self.events[execution_id] = [{"event_type": "action_received"}]
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
        self.assert_transition(current, expected_version, target_status)
        updated = replace(
            current,
            status=target_status,
            version=current.version + 1,
            result_summary=result_summary or current.result_summary,
            error_code=error_code,
            error_message=error_message,
        )
        self.records[execution_id] = updated
        self.events[execution_id].append(
            {"event_type": event_type, "details": details or {}}
        )
        return updated

    @staticmethod
    def assert_transition(current, expected_version, target_status):
        if current.version != expected_version:
            raise AssertionError("stale version")
        ActionStateMachine.require_transition(current.status, target_status)

    def record_event(self, execution_id, *, event_type, actor_id, details=None):
        self.get(execution_id)
        self.events[execution_id].append(
            {"event_type": event_type, "details": details or {}}
        )

    def list_audit_events(self, execution_id):
        return list(self.events[execution_id])

    def verify_audit_chain(self, execution_id):
        self.get(execution_id)
        return True


class FakeConsentVerifier:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def verify(self, **values):
        self.calls += 1
        if self.fail:
            raise CreditConsentMissing("missing")
        return {
            "verified": True,
            "verifier": "test",
            "authorization_reference_hash": "a" * 64,
        }


def response_for(request):
    return CreditProviderResponse(
        provider_request_id=request.provider_request_id,
        report_id="CRTEST0001",
        subject_id_number=request.subject_id_number,
        query_time=datetime.now(timezone.utc),
        application_channel=request.application_channel,
        outstanding_loan_count=1,
        overdue_3y_count=0,
        total_overdue_count=0,
        inquiry_count=2,
        reporting_agency="央行征信",
        provider_name="test-provider",
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
        return response_for(request), False


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
    def __init__(self, *, fail_load=False, fail_writes=0):
        self.fail_load = fail_load
        self.fail_writes = fail_writes
        self.persist_count = 0
        self.context = CreditApplicationSubject(
            application_id="APP20260801000001",
            subject_id_number="110101198803120011",
            subject_role="借款人",
            application_channel="线上H5",
        )

    def load_application_subject(self, application_id, subject_id_number):
        if self.fail_load:
            from app.action import CreditReportSubjectNotFound

            raise CreditReportSubjectNotFound("out of scope")
        return replace(
            self.context,
            application_id=application_id,
            subject_id_number=subject_id_number,
        )

    def persist_and_enqueue(self, *, response, outbox_repository, execution_id, **values):
        self.persist_count += 1
        if self.persist_count <= self.fail_writes:
            raise RuntimeError("temporary local write failure")
        effect = CreditReportEffect(
            operation="created",
            report_id=response.report_id,
            application_id=values["context"].application_id,
            previous_row=None,
            report_fingerprint="f" * 64,
        )
        event = OutboxEvent(
            event_id="credit-outbox-1",
            execution_id=execution_id,
            event_type="CREDIT_REPORT_GRAPH_SYNC",
            aggregate_type="CreditReport",
            aggregate_id=response.report_id,
            ontology_version=values["ontology_version"],
            deduplication_key=execution_id,
            payload={"effect": {"operation": "created"}},
            status="PENDING",
            attempt_count=0,
            max_attempts=5,
        )
        outbox_repository.event = event
        return {"report_id": response.report_id}, effect, event


class SuccessfulOutboxWorker:
    def __init__(self, executions, outbox):
        self.executions = executions
        self.outbox = outbox

    def process_event(self, event_id):
        current = self.executions.get(self.outbox.event.execution_id)
        result = {
            "report_id": self.outbox.event.aggregate_id,
            "provider_request_id": "provider-request",
            "persistence": {"postgres": "created", "nebulagraph": "synchronized"},
        }
        self.executions.transition(
            current.execution_id,
            "SUCCEEDED",
            expected_version=current.version,
            event_type="action_succeeded",
            actor_id="worker",
            result_summary=result,
        )
        self.outbox.event = replace(self.outbox.event, status="SUCCEEDED")
        return self.outbox.event


def valid_request():
    return {
        "application_id": "APP20260801000001",
        "subject_id_number": "110101198803120011",
        "authorization_reference": "AUTH-TEST-1",
        "query_purpose": "贷款审批",
        "request_id": "REQ-CREDIT-1",
    }


class RequestCreditReportActionTests(unittest.TestCase):
    def service(self, *, consent=None, provider=None, reports=None):
        proposal = build_personal_credit_baseline()
        executions = MemoryExecutionRepository()
        outbox = FakeOutboxRepository()
        reports = reports or FakeReportRepository()
        provider = provider or FakeProviderCoordinator()
        service = RequestCreditReportActionService(
            postgres_service=object(),
            nebula_factory=lambda: None,
            proposal=proposal,
            execution_repository=executions,
            consent_verifier=consent or FakeConsentVerifier(),
            provider_coordinator=provider,
            report_repository=reports,
            outbox_repository=outbox,
            outbox_worker=SuccessfulOutboxWorker(executions, outbox),
        )
        return service, proposal, executions, provider, reports

    def test_request_contract_rejects_unknown_purpose_and_fields(self):
        values = valid_request()
        values["query_purpose"] = "营销"
        with self.assertRaisesRegex(Exception, "贷款审批"):
            validate_credit_report_request(values)
        values = valid_request()
        values["raw_report"] = "must-not-enter"
        with self.assertRaisesRegex(Exception, "未声明字段"):
            validate_credit_report_request(values)

    def test_success_and_replay_query_provider_once(self):
        service, proposal, _executions, provider, reports = self.service()
        first = service.execute(
            valid_request(),
            ontology_version=proposal.version,
            actor_id="credit-adapter",
            actor_role="系统适配器",
        )
        second = service.execute(
            valid_request(),
            ontology_version=proposal.version,
            actor_id="credit-adapter",
            actor_role="系统适配器",
        )
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(provider.calls, 1)
        self.assertEqual(reports.persist_count, 1)

    def test_local_write_retry_reuses_cached_provider_response(self):
        reports = FakeReportRepository(fail_writes=1)
        service, proposal, _executions, provider, reports = self.service(reports=reports)
        result = service.execute(
            valid_request(),
            ontology_version=proposal.version,
            actor_id="credit-adapter",
            actor_role="系统适配器",
        )
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(reports.persist_count, 2)
        self.assertEqual(provider.calls, 1)

    def test_role_denied_before_consent_provider_or_write(self):
        consent = FakeConsentVerifier()
        service, proposal, _executions, provider, reports = self.service(consent=consent)
        with self.assertRaises(ActionAuthorizationError):
            service.execute(
                valid_request(),
                ontology_version=proposal.version,
                actor_id="agent",
                actor_role="Agent",
            )
        self.assertEqual(consent.calls, 0)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(reports.persist_count, 0)

    def test_subject_scope_and_consent_fail_closed(self):
        reports = FakeReportRepository(fail_load=True)
        service, proposal, _executions, provider, _reports = self.service(reports=reports)
        with self.assertRaises(RequestCreditReportError) as raised:
            service.execute(
                valid_request(),
                ontology_version=proposal.version,
                actor_id="credit-adapter",
                actor_role="系统适配器",
            )
        self.assertEqual(raised.exception.code, "ACTION_SUBJECT_NOT_FOUND")
        self.assertEqual(provider.calls, 0)

        consent = FakeConsentVerifier(fail=True)
        service, proposal, _executions, provider, _reports = self.service(consent=consent)
        with self.assertRaises(RequestCreditReportError) as raised:
            service.execute(
                valid_request(),
                ontology_version=proposal.version,
                actor_id="credit-adapter",
                actor_role="系统适配器",
            )
        self.assertEqual(raised.exception.code, "ACTION_CREDIT_CONSENT_MISSING")
        self.assertEqual(provider.calls, 0)

    def test_timeout_fails_execution_without_business_write(self):
        provider = FakeProviderCoordinator(CreditProviderTimeout("timeout"))
        service, proposal, executions, _provider, reports = self.service(provider=provider)
        with self.assertRaises(RequestCreditReportError) as raised:
            service.execute(
                valid_request(),
                ontology_version=proposal.version,
                actor_id="credit-adapter",
                actor_role="系统适配器",
            )
        self.assertEqual(raised.exception.code, "ACTION_EXTERNAL_TIMEOUT")
        self.assertEqual(reports.persist_count, 0)
        self.assertEqual(next(iter(executions.records.values())).status, "FAILED")

    def test_provider_response_must_match_request(self):
        request = CreditProviderRequest(
            provider_request_id="CPTEST",
            application_id="APP1",
            subject_id_number="110101198803120011",
            query_purpose="贷款审批",
            application_channel="线上H5",
        )
        response = replace(response_for(request), subject_id_number="110101199007210022")
        with self.assertRaises(CreditProviderResponseInvalid):
            validate_provider_response(
                response,
                request,
                expected_provider_name="test-provider",
                expected_provider_mode="test",
            )

    def test_sandbox_is_explicit_deterministic_and_synthetic(self):
        verifier = SandboxConsentVerifier()
        verified = verifier.verify(
            application_id="APP1",
            subject_id_number="110101198803120011",
            authorization_reference="SANDBOX-CONSENT:APP1",
            query_purpose="贷款审批",
        )
        self.assertTrue(verified["verified"])
        provider_request_id = deterministic_provider_request_id(
            "APP1", "110101198803120011"
        )
        request = CreditProviderRequest(
            provider_request_id=provider_request_id,
            application_id="APP1",
            subject_id_number="110101198803120011",
            query_purpose="贷款审批",
            application_channel="线上H5",
        )
        response = SandboxCreditProvider().query(request, timeout_seconds=30)
        self.assertTrue(response.synthetic)
        self.assertTrue(response.report_id.startswith("SBXCR"))
        self.assertNotIn(request.subject_id_number, str(response.cache_payload()))

    def test_repository_uses_atomic_outbox_and_compensation_guard(self):
        source = (
            ROOT / "backend/app/action/credit_report_repository.py"
        ).read_text(encoding="utf-8")
        provider_source = (ROOT / "backend/app/action/credit_provider.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("FOR UPDATE", source)
        self.assertIn("outbox_repository.enqueue", source)
        self.assertIn("report_fingerprint", source)
        self.assertIn("credit_provider_receipts", provider_source)
        self.assertIn("provider_request_id", provider_source)
        self.assertNotIn('"subject_id_number": self.subject_id_number', provider_source)


if __name__ == "__main__":
    unittest.main()
