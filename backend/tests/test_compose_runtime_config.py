import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_TEXT = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def service_block(name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\s*\n(.*?)(?=^  [a-zA-Z0-9_-]+:\s*\n|^volumes:\s*\n|\Z)",
        COMPOSE_TEXT,
    )
    if not match:
        raise AssertionError(f"Compose service not found: {name}")
    return match.group(1)


class ComposeRuntimeConfigTests(unittest.TestCase):
    def test_nebula_services_expose_real_status_healthchecks(self):
        expected = {
            "metad0": ("19559", "--ws_http_port=19559"),
            "storaged0": ("19779", "--ws_http_port=19779"),
            "graphd": ("19669", "--ws_http_port=19669"),
        }
        for service, (port, flag) in expected.items():
            with self.subTest(service=service):
                block = service_block(service)
                self.assertIn(flag, block)
                self.assertIn(f"http://{service}:{port}/status", block)
                self.assertIn("healthcheck:", block)

    def test_nebula_init_is_polled_idempotent_and_fail_closed(self):
        block = service_block("nebula-init")
        self.assertNotIn("sleep 15", block)
        self.assertNotIn("|| true", block)
        self.assertIn("SHOW HOSTS", block)
        self.assertIn("ADD HOSTS", block)
        self.assertIn("grep -q 'storaged0'", block)
        self.assertIn("grep -q 'ONLINE'", block)
        self.assertIn("exit 1", block)
        self.assertIn("max_attempts=60", block)

    def test_service_dependencies_wait_for_health_or_success(self):
        self.assertIn("metad0: {condition: service_healthy}", service_block("storaged0"))
        graphd = service_block("graphd")
        self.assertIn("metad0: {condition: service_healthy}", graphd)
        self.assertIn("storaged0: {condition: service_healthy}", graphd)
        init = service_block("nebula-init")
        self.assertIn("graphd: {condition: service_healthy}", init)
        self.assertIn("storaged0: {condition: service_healthy}", init)
        backend = service_block("backend")
        self.assertIn("postgres: {condition: service_healthy}", backend)
        self.assertIn("graphd: {condition: service_healthy}", backend)
        self.assertIn("nebula-init: {condition: service_completed_successfully}", backend)
        self.assertIn("backend: {condition: service_healthy}", service_block("frontend"))

    def test_application_healthchecks_verify_functional_readiness(self):
        backend = service_block("backend")
        self.assertIn("/api/health", backend)
        self.assertIn("data.get('status')=='ok'", backend)
        frontend = service_block("frontend")
        self.assertIn("wget", frontend)
        self.assertIn("http://localhost/", frontend)

    def test_long_running_services_have_restart_policy(self):
        for service in ("postgres", "metad0", "storaged0", "graphd", "backend", "frontend"):
            with self.subTest(service=service):
                self.assertIn("restart: unless-stopped", service_block(service))
        self.assertIn('restart: "no"', service_block("nebula-init"))

    def test_published_ports_are_loopback_only(self):
        expected={
            "postgres":"127.0.0.1:5432:5432",
            "graphd":"127.0.0.1:9669:9669",
            "backend":"127.0.0.1:8000:8000",
            "frontend":"127.0.0.1:8080:80",
        }
        for service,mapping in expected.items():
            with self.subTest(service=service):
                self.assertIn(mapping,service_block(service))

    def test_local_session_mode_is_explicit_and_anonymous_uat_stays_off(self):
        backend=service_block("backend")
        self.assertIn("MVP_AUTH_MODE: ${MVP_AUTH_MODE:-local_session}",backend)
        self.assertIn(
            "MVP_LOCAL_SESSION_TRUST_PROXY: ${MVP_LOCAL_SESSION_TRUST_PROXY:-true}",
            backend,
        )
        self.assertIn(
            "WORKBENCH_UNAUTHENTICATED: ${WORKBENCH_UNAUTHENTICATED:-false}",
            backend,
        )
        self.assertIn(
            "PERSONAL_CREDIT_AGENT_MODE: ${PERSONAL_CREDIT_AGENT_MODE:-legacy}",
            backend,
        )


if __name__ == "__main__":
    unittest.main()
