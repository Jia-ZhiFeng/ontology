import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("windows_runtime", PROJECT_ROOT / "scripts/windows_runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class WindowsRuntimeTests(unittest.TestCase):
    def local_values(self):
        return {
            "MVP_LOCAL_INSTANCE_ID": "abcdef012345", "MVP_WSL_DISTRO": "Ubuntu-24.04",
            "MVP_AUTH_MODE": "local_session", "MVP_LOCAL_SESSION_TRUST_PROXY": "false",
            "MVP_LOCAL_SESSION_SECRET": "s" * 48, "MVP_API_KEY": "k" * 48,
            "POSTGRES_HOST": "127.0.0.1", "POSTGRES_PORT": "5433", "POSTGRES_USER": "credit",
            "POSTGRES_DATABASE": "credit_ontology", "POSTGRES_PASSWORD": "private-pg-value",
            "NEBULA_HOST": "127.0.0.1", "NEBULA_PORT": "9669", "NEBULA_USER": "root",
            "NEBULA_PASSWORD": "private-graph-value",
        }

    def test_generated_config_preserves_source_and_literal_credentials(self):
        from dotenv import dotenv_values
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = "MVP_API_KEY=" + "k" * 48 + "\nPOSTGRES_PASSWORD='a\\\\b\\\'c${HOME}'\nNEBULA_PASSWORD=nebula\n"
            (root / ".env").write_text(source, encoding="utf-8")
            with patch.multiple(runtime, ROOT=root, LOCAL_ENV=root / ".env.local"), patch.object(runtime, "say"):
                runtime.generate_local_config()
                result = dotenv_values(root / ".env.local", interpolate=False)
                original = dotenv_values(root / ".env", interpolate=False)
                self.assertEqual((root / ".env").read_text(encoding="utf-8"), source)
                self.assertEqual(result["POSTGRES_PASSWORD"], original["POSTGRES_PASSWORD"])
                self.assertEqual(result["MVP_API_KEY"], "k" * 48)
                self.assertEqual(result["POSTGRES_HOST"], "127.0.0.1")
                self.assertEqual(result["MVP_LOCAL_SESSION_TRUST_PROXY"], "false")
                self.assertGreaterEqual(len(result["MVP_LOCAL_SESSION_SECRET"]), 32)
                self.assertNotEqual(result["NEBULA_PASSWORD"], "nebula")
                self.assertLessEqual(len(result["NEBULA_PASSWORD"]), 24)
                saved = (root / ".env.local").read_bytes()
                runtime.generate_local_config()
                self.assertEqual((root / ".env.local").read_bytes(), saved)

    def test_untrusted_local_connection_settings_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env.example").write_text("", encoding="utf-8")
            for change in ({"NEBULA_HOST": "graphd"}, {"MVP_AUTH_MODE": "api_key"}, {"MVP_LOCAL_SESSION_TRUST_PROXY": "true"}, {"NEBULA_PASSWORD": "x" * 25}):
                values = self.local_values() | change
                (root / ".env.local").write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
                with patch.multiple(runtime, ROOT=root, LOCAL_ENV=root / ".env.local"):
                    with self.assertRaises(runtime.RuntimeFailure):
                        runtime.load_local_config()

    def test_windows_wsl_utf16_inventory_is_decoded(self):
        inventory = "\ufeffUbuntu-24.04\r\ndocker-desktop\r\n"
        self.assertEqual(runtime.decode_wsl_output(inventory.encode("utf-16-le")), inventory.lstrip("\ufeff"))

    def test_busy_port_does_not_start_databases_or_kill_processes(self):
        with patch.object(runtime, "read_state", return_value={"services": {}}), \
             patch.object(runtime, "port_free", return_value=False), \
             patch.object(runtime, "call_wsl") as wsl, patch.object(runtime.subprocess, "run") as run:
            with self.assertRaises(runtime.RuntimeFailure):
                runtime.start(self.local_values(), open_browser=False)
            wsl.assert_not_called()
            run.assert_not_called()

    def test_changed_config_cannot_relabel_running_backend(self):
        state = {"services": {"backend": {"pid": 42, "stamp": 1}}, "configuration_digest": "old-config"}
        with patch.object(runtime, "read_state", return_value=state), \
             patch.object(runtime, "service_owned", return_value=True), patch.object(runtime, "call_wsl") as wsl:
            with self.assertRaises(runtime.RuntimeFailure):
                runtime.start(self.local_values(), open_browser=False)
            self.assertEqual(state["configuration_digest"], "old-config")
            wsl.assert_not_called()

    def test_reused_pid_is_not_signaled_or_killed(self):
        state = {"services": {"backend": {"pid": 42, "stamp": 100, "stop_file": "outside-project"}}}
        with patch.object(runtime, "process_stamp", return_value=200), patch.object(runtime.subprocess, "run") as run:
            runtime.stop_server("backend", state)
            self.assertEqual(state["services"], {})
            run.assert_not_called()

    def test_stop_file_must_be_inside_runtime_directory(self):
        state = {"services": {"backend": {"pid": 42, "stamp": 100, "stop_file": str(PROJECT_ROOT / "unexpected.stop")}}}
        with patch.object(runtime, "service_owned", return_value=True), patch.object(runtime.subprocess, "run") as run:
            with self.assertRaises(runtime.RuntimeFailure):
                runtime.stop_server("backend", state)
            run.assert_not_called()

    def test_nonempty_graph_is_not_automatically_republished(self):
        replies = [
            {"active_version": None, "versions": [{"version": "1.1.0", "status": "approved"}]},
            {"vertices": 12, "edges": 4},
        ]
        with patch.object(runtime, "api", side_effect=replies) as api:
            with self.assertRaises(runtime.RuntimeFailure):
                runtime.bootstrap_ontology(self.local_values())
            self.assertEqual(api.call_count, 2)
            self.assertTrue(all("method" not in call.kwargs for call in api.call_args_list))

    def test_existing_active_ontology_is_only_verified(self):
        with patch.object(runtime, "api", side_effect=[{"active_version": "1.2.0"}, {"valid": True}]) as api:
            runtime.bootstrap_ontology(self.local_values())
            self.assertEqual([call.args[0] for call in api.call_args_list], ["/ontology/versions", "/kg/mapping/report"])

    def test_fresh_graph_waits_for_asynchronous_schema_visibility(self):
        graph = Mock()
        graph.snapshot.side_effect = [RuntimeError("Tag schema is not ready"), {"nodes": [], "edges": []}]
        with patch.object(runtime.time, "sleep"):
            runtime.wait_for_graph_schema(graph)
        self.assertEqual(graph.snapshot.call_count, 2)

    def test_permanent_graph_error_is_not_hidden_as_ready(self):
        graph = Mock()
        graph.snapshot.side_effect = RuntimeError("Graph unavailable")
        with self.assertRaisesRegex(RuntimeError, "Graph unavailable"):
            runtime.wait_for_graph_schema(graph, timeout=0)


if __name__ == "__main__":
    unittest.main()
