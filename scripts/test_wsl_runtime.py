"""Run inside Ubuntu: python3 scripts/test_wsl_runtime.py."""
import unittest
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
import wsl_runtime as runtime


class WslRuntimeTests(unittest.TestCase):
    def config(self):
        return {"instance_id": "abcdef012345", "pg_user": "credit", "pg_database": "credit_ontology",
                "pg_password": "a-local-test-password", "pg_port": 5433, "graph_port": 9669}

    def test_instance_id_cannot_escape_project_data_root(self):
        config = self.config() | {"instance_id": "../../other-database"}
        with self.assertRaises(runtime.RuntimeFailure):
            runtime.validate_config(config)

    def test_database_ports_cannot_alias_internal_nebula_ports(self):
        for port in (9559, 9779, 19559, 19779, 19669, 9669):
            with self.assertRaises(runtime.RuntimeFailure):
                runtime.validate_config(self.config() | {"pg_port": port})

    def test_unknown_pid_file_never_receives_signal(self):
        with patch.object(runtime, "process_is_owned", return_value=None), \
             patch.object(runtime, "stop_postgres", return_value=False), patch.object(runtime.os, "kill") as kill:
            self.assertEqual(runtime.stop(self.config())["stopped"], [])
            kill.assert_not_called()

    def test_reused_postgres_pid_never_authorizes_stopping_an_unrelated_process(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            data = base / "postgres"
            data.mkdir()
            (data / "PG_VERSION").write_text("16\n")
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            try:
                (data / "postmaster.pid").write_text(f"{child.pid}\n{data}\n")
                self.assertFalse(runtime.pg_status(base))
                self.assertFalse(runtime.stop_postgres(base))
                self.assertIsNone(child.poll())
            finally:
                child.terminate()
                child.wait(timeout=5)

    def test_pid_replaced_after_opening_descriptor_is_not_signalled(self):
        with patch.object(runtime, "postgres_process_id", side_effect=[123, None]), \
             patch.object(runtime.os, "pidfd_open", return_value=88), \
             patch.object(runtime.signal, "pidfd_send_signal") as send, \
             patch.object(runtime.os, "close") as close:
            self.assertFalse(runtime.stop_postgres(Path("/unused")))
            send.assert_not_called()
            close.assert_called_once_with(88)

    def test_verified_postgres_is_signalled_through_its_descriptor(self):
        with patch.object(runtime, "postgres_process_id", side_effect=[123, 123, None]), \
             patch.object(runtime.os, "pidfd_open", return_value=88), \
             patch.object(runtime.signal, "pidfd_send_signal") as send, \
             patch.object(runtime.os, "close") as close:
            self.assertTrue(runtime.stop_postgres(Path("/unused")))
            send.assert_called_once_with(88, runtime.signal.SIGINT)
            close.assert_called_once_with(88)

    def test_active_listener_is_rejected(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with self.assertRaises(runtime.RuntimeFailure):
                runtime.assert_port_free(server.getsockname()[1])

    def test_time_wait_does_not_prevent_immediate_restart(self):
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            with socket.create_connection(("127.0.0.1", port)) as client:
                connection, _ = server.accept()
                connection.close()
                self.assertEqual(client.recv(1), b"")
        runtime.assert_port_free(port)


if __name__ == "__main__":
    unittest.main()
