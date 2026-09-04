import os
import subprocess
import sys
import unittest
from pathlib import Path

from app.agent.runtime import (
    AgentRuntimeUnavailableError,
    build_personal_credit_agent_runtime,
    personal_credit_agent_runtime_status,
    require_personal_credit_agent_runtime,
)
from app.core.agent_runtime import (
    AgentModeConfigurationError,
    PersonalCreditAgentMode,
    parse_personal_credit_agent_mode,
)


class PersonalCreditAgentModeTests(unittest.TestCase):
    def test_only_legacy_and_ontology_agent_are_valid_modes(self):
        self.assertIs(
            parse_personal_credit_agent_mode(" LEGACY "),
            PersonalCreditAgentMode.LEGACY,
        )
        self.assertIs(
            parse_personal_credit_agent_mode("ontology_agent"),
            PersonalCreditAgentMode.ONTOLOGY_AGENT,
        )
        self.assertIs(
            parse_personal_credit_agent_mode(PersonalCreditAgentMode.LEGACY),
            PersonalCreditAgentMode.LEGACY,
        )
        for invalid in (None, "", "rules", "ontology", "auto"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(AgentModeConfigurationError):
                    parse_personal_credit_agent_mode(invalid)

    def test_legacy_mode_builds_only_the_legacy_runtime(self):
        calls = []

        def legacy_factory():
            calls.append("legacy")
            return object()

        runtime = build_personal_credit_agent_runtime(
            mode="legacy",
            legacy_factory=legacy_factory,
        )
        self.assertIsNotNone(runtime)
        self.assertEqual(calls, ["legacy"])

    def test_unimplemented_ontology_mode_fails_without_legacy_fallback(self):
        calls = []
        with self.assertRaises(AgentRuntimeUnavailableError) as raised:
            build_personal_credit_agent_runtime(
                mode="ontology_agent",
                legacy_factory=lambda: calls.append("legacy"),
            )
        self.assertEqual(raised.exception.code, "ONTOLOGY_AGENT_NOT_READY")
        self.assertEqual(calls, [])

        with self.assertRaises(AgentRuntimeUnavailableError):
            require_personal_credit_agent_runtime("ontology_agent")

    def test_runtime_status_exposes_safe_rollback_metadata(self):
        legacy = personal_credit_agent_runtime_status("legacy").public()
        ontology = personal_credit_agent_runtime_status(
            "ontology_agent"
        ).public()
        self.assertEqual(legacy, {
            "mode": "legacy",
            "available": True,
            "rollback_mode": "legacy",
            "ontology_agent_status": "not_selected",
        })
        self.assertEqual(ontology, {
            "mode": "ontology_agent",
            "available": False,
            "rollback_mode": "legacy",
            "ontology_agent_status": "pending_runtime",
        })

    def test_invalid_environment_mode_fails_application_import(self):
        project_root = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(project_root / "backend")
        environment["PERSONAL_CREDIT_AGENT_MODE"] = "unsafe-auto-mode"
        completed = subprocess.run(
            [sys.executable, "-c", "import app.core.config"],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("PERSONAL_CREDIT_AGENT_MODE", completed.stderr)


if __name__ == "__main__":
    unittest.main()
