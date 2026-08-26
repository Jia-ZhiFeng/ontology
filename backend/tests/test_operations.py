import pathlib
import unittest

from app.action.operations import (
    DisabledAlertNotifier,
    ExternalCircuitBreaker,
    ExternalCircuitOpenError,
    OperationalAlertService,
    OperationsError,
    WebhookAlertNotifier,
)


ROOT = pathlib.Path(__file__).resolve().parents[2]


class FakeCircuitRepository:
    def __init__(self):
        self.state = "CLOSED"
        self.failures = 0
        self.resets = 0

    def before_call(self, dependency_name, **_values):
        return {
            "allowed": self.state == "CLOSED",
            "record": {
                "dependency_name": dependency_name,
                "state": self.state,
                "next_probe_at": None,
            },
        }

    def record_failure(self, dependency_name, *, failure_threshold, **_values):
        self.failures += 1
        opened = self.failures >= failure_threshold and self.state != "OPEN"
        if opened:
            self.state = "OPEN"
        return {
            "opened": opened,
            "record": {
                "dependency_name": dependency_name,
                "state": self.state,
                "failure_count": self.failures,
                "next_probe_at": None,
            },
        }

    def record_success(self, dependency_name):
        del dependency_name
        closed = self.state != "CLOSED" or self.failures > 0
        self.state = "CLOSED"
        self.failures = 0
        return {"closed": closed, "record": {"state": "CLOSED"}}


class FakeAlertRepository:
    def __init__(self):
        self.resolved = []
        self.notifications = []

    def ensure_schema(self):
        return None

    def resolve_by_key(self, key, **values):
        self.resolved.append((key, values))

    def record_notification(self, alert_id, **values):
        self.notifications.append((alert_id, values))

    def raise_alert(self, **values):
        return {
            "alert_id": "alert-1",
            "category": values["category"],
            "severity": values["severity"],
            "title": values["title"],
            "occurrence_count": 1,
            "escalation_level": 0,
            "should_notify": True,
            "created": True,
        }

    def due_for_escalation(self, **_values):
        return []


class FakeAlertService:
    def __init__(self):
        self.repository = FakeAlertRepository()
        self.raised = []

    def raise_alert(self, **values):
        self.raised.append(values)
        return values


class FakeNotifier:
    channel_name = "fake-webhook"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def notify(self, alert):
        self.calls.append(dict(alert))
        if self.fail:
            raise RuntimeError("notification failed")


class OperationsTests(unittest.TestCase):
    def test_circuit_opens_after_threshold_and_fails_fast(self):
        repository = FakeCircuitRepository()
        alerts = FakeAlertService()
        circuit = ExternalCircuitBreaker(
            "credit-provider", repository, alerts, failure_threshold=2
        )

        circuit.before_call()
        circuit.record_failure("ACTION_EXTERNAL_TIMEOUT", "timeout one")
        self.assertEqual(alerts.raised, [])
        circuit.record_failure("ACTION_EXTERNAL_TIMEOUT", "timeout two")

        self.assertEqual(repository.state, "OPEN")
        self.assertEqual(len(alerts.raised), 1)
        self.assertEqual(alerts.raised[0]["category"], "CIRCUIT_OPEN")
        with self.assertRaises(ExternalCircuitOpenError) as raised:
            circuit.before_call()
        self.assertEqual(raised.exception.code, "ACTION_EXTERNAL_CIRCUIT_OPEN")

    def test_success_closes_circuit_and_resolves_alert(self):
        repository = FakeCircuitRepository()
        repository.state = "OPEN"
        repository.failures = 3
        alerts = FakeAlertService()
        circuit = ExternalCircuitBreaker("fraud-provider", repository, alerts)

        circuit.record_success()

        self.assertEqual(repository.state, "CLOSED")
        self.assertEqual(
            alerts.repository.resolved[0][0], "circuit:fraud-provider"
        )

    def test_notification_success_and_failure_are_audited(self):
        for fail, expected in ((False, True), (True, False)):
            repository = FakeAlertRepository()
            notifier = FakeNotifier(fail=fail)
            service = OperationalAlertService(repository, notifier)
            service.raise_alert(
                alert_key="test:key",
                source_type="TEST",
                source_id="source",
                category="TEST_ALERT",
                severity="WARNING",
                title="test",
                message="test message",
                metadata={},
                actor_id="test",
            )
            self.assertEqual(repository.notifications[0][1]["success"], expected)

    def test_disabled_notifier_never_attempts_outbound_delivery(self):
        repository = FakeAlertRepository()
        service = OperationalAlertService(repository, DisabledAlertNotifier())
        service.raise_alert(
            alert_key="test:key",
            source_type="TEST",
            source_id="source",
            category="TEST_ALERT",
            severity="WARNING",
            title="test",
            message="test message",
            metadata={},
            actor_id="test",
        )
        self.assertEqual(repository.notifications, [])

    def test_webhook_requires_https_except_explicit_local_debug(self):
        with self.assertRaises(OperationsError):
            WebhookAlertNotifier("http://example.com/hook")
        with self.assertRaises(OperationsError):
            WebhookAlertNotifier("https://user:pass@example.com/hook")
        notifier = WebhookAlertNotifier(
            "http://127.0.0.1:9999/hook", allow_insecure_local=True
        )
        self.assertEqual(notifier.channel_name, "webhook")

    def test_persistent_schema_and_runtime_integration_are_explicit(self):
        source = (ROOT / "backend/app/action/operations.py").read_text(
            encoding="utf-8"
        )
        routes = (ROOT / "backend/app/api/routes.py").read_text(encoding="utf-8")
        main = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
        self.assertIn("mvp_control.operational_alerts", source)
        self.assertIn("mvp_control.external_circuit_breakers", source)
        self.assertIn("FOR UPDATE SKIP LOCKED", source)
        self.assertIn("HALF_OPEN", source)
        self.assertIn("/operations/alerts", routes)
        self.assertIn("run_operations_monitor", main)


if __name__ == "__main__":
    unittest.main()
