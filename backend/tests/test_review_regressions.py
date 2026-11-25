"""Behavior regressions from the native-runtime and application code review."""
import asyncio
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from fastapi import HTTPException

from app.action import outbox
from app.action.workflow import PostgresPersonalCreditWorkflowRepository, WorkflowError, _safe_mapping
from app.api import routes
from app.core.security import mask_graph_snapshot, mask_payload, mask_schema_payload
from app.kg.nebula import NebulaGraphService
from app.ontology.baseline import build_personal_credit_baseline


class WorkflowDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_duplicate_does_not_fail_the_original_dispatch(self):
        repository = Mock()
        repository.get.return_value = {
            "workflow": {"ontology_version": "1.1.0", "application_id": "APP-1"},
            "steps": [{"action_name": "request_credit_report", "status": "WAITING",
                       "dispatch_request_id": "original", "execution_id": "original-execution"}],
        }
        repository.begin_step.side_effect = WorkflowError("WORKFLOW_STEP_NOT_READY", "正在执行")
        model = Mock()
        model.model_dump.return_value = {"application_id": "APP-1"}
        with patch.object(routes, "_workflow_repository", return_value=repository), \
             patch.object(routes, "_require_registered_action_version"), \
             patch.object(routes, "_workflow_action_payload", return_value=model), \
             patch.object(routes._ACTION_ADAPTER_REGISTRY, "execute_model", new_callable=AsyncMock) as dispatch:
            with self.assertRaises(HTTPException):
                await routes.execute_personal_credit_workflow_step(
                    "workflow-1", "request_credit_report",
                    routes.ExecutePersonalCreditWorkflowStepRequest(payload={}),
                )
        repository.record_dispatch_failure.assert_not_called()
        repository.attach_action_result.assert_not_called()
        dispatch.assert_not_awaited()


class WorkflowRepositoryOwnershipTests(unittest.TestCase):
    def repository(self, step):
        postgres = MagicMock()
        cursor = postgres.conn.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = step
        repository = PostgresPersonalCreditWorkflowRepository(postgres)
        repository.ensure_schema = Mock()
        repository._workflow = Mock(return_value={"workflow_id": "wf"})
        repository._detail = Mock(return_value={"steps": [step]})
        return repository, cursor

    def test_stale_dispatch_cannot_attach_success_or_failure(self):
        for method in ("attach_action_result", "record_dispatch_failure"):
            with self.subTest(method=method):
                repository, cursor = self.repository({"status": "WAITING", "dispatch_request_id": "new"})
                kwargs = dict(dispatch_request_id="old", execution_id="execution", error_code="FAIL",
                              error_message="failed", actor_id="tester")
                if method == "attach_action_result":
                    kwargs.update(action_status="SUCCEEDED", result_summary={})
                with self.assertRaises(WorkflowError) as caught:
                    getattr(repository, method)("wf", "request_credit_report", **kwargs)
                self.assertEqual(caught.exception.code, "WORKFLOW_STEP_CONFLICT")
                self.assertFalse(any(str(call.args[0]).lstrip().startswith(("UPDATE", "INSERT", "DELETE"))
                                     for call in cursor.execute.call_args_list))

    def test_late_http_result_preserves_already_reconciled_execution(self):
        step = {"status": "COMPLETED", "dispatch_request_id": "owned", "execution_id": "execution"}
        repository, cursor = self.repository(step)
        result = repository.attach_action_result(
            "wf", "request_credit_report", dispatch_request_id="owned", execution_id="execution",
            action_status="COMMITTING", result_summary={}, error_code=None, error_message=None, actor_id="tester",
        )
        self.assertEqual(result["steps"][0]["status"], "COMPLETED")
        self.assertFalse(any(str(call.args[0]).lstrip().startswith(("UPDATE", "INSERT", "DELETE"))
                             for call in cursor.execute.call_args_list))


class ProjectionTests(unittest.TestCase):
    def worker(self, postgres, graph):
        return outbox.RiskAssessmentOutboxWorker(
            postgres_service=postgres, nebula_factory=lambda: graph, proposal_loader=Mock(),
            outbox_repository=Mock(), execution_repository=Mock(), report_repository=Mock(),
        )

    def test_snapshot_read_and_active_version_are_inside_the_shared_lock(self):
        old = build_personal_credit_baseline()
        active = old.model_copy(update={"version": "1.1.1"})
        lock = threading.Lock()
        held = threading.local()
        first_in_graph, release_first, second_waiting = (threading.Event() for _ in range(3))
        source, snapshots, projected, observed_versions = ["A"], [], [], []

        @contextmanager
        def guard():
            if threading.current_thread().name.startswith("projection-second"):
                second_waiting.set()
            with lock:
                held.value = True
                try:
                    yield
                finally:
                    held.value = False

        def active_record():
            self.assertTrue(getattr(held, "value", False), "活动版本必须在锁内读取")
            return {"proposal": active.model_dump(mode="json")}

        def snapshot():
            self.assertTrue(getattr(held, "value", False), "快照必须在锁内读取")
            value = list(source)
            snapshots.append(value)
            return {"rows": value}

        def synchronize(profile, proposal):
            self.assertTrue(getattr(held, "value", False))
            observed_versions.append(proposal.version)
            if len(snapshots) == 1:
                first_in_graph.set()
                if not release_first.wait(5):
                    raise AssertionError("test did not release first sync")
            projected[:] = profile["rows"]
            return {"nodes": len(projected)}

        def mapping(proposal):
            self.assertTrue(getattr(held, "value", False), "映射校验完成前不得释放锁")
            self.assertEqual(proposal.version, "1.1.1")
            return {"valid": True}

        versions, postgres, graph = Mock(), Mock(), Mock()
        versions.publication_guard.side_effect = guard
        versions.active.side_effect = active_record
        postgres.validation_profile.side_effect = snapshot
        graph.sync_postgres_profile.side_effect = synchronize
        graph.mapping_report.side_effect = mapping
        worker = self.worker(postgres, graph)
        with patch.object(outbox, "OntologyVersionRepository", return_value=versions), \
             patch.object(outbox, "validate_catalog_data_quality", return_value={"valid": True}), \
             patch.object(outbox, "execute_axioms", return_value={"valid": True}), \
             ThreadPoolExecutor(max_workers=1, thread_name_prefix="projection-first") as first_pool, \
             ThreadPoolExecutor(max_workers=1, thread_name_prefix="projection-second") as second_pool:
            first = first_pool.submit(worker._sync_and_verify, old)
            try:
                self.assertTrue(first_in_graph.wait(5))
                source.append("B")
                second = second_pool.submit(worker._sync_and_verify, old)
                self.assertTrue(second_waiting.wait(5))
                self.assertEqual(snapshots, [["A"]], "等待写锁时不能提前取得旧快照")
            finally:
                release_first.set()
            self.assertEqual(first.result(timeout=5)["projection_ontology_version"], "1.1.1")
            self.assertEqual(second.result(timeout=5)["projection_ontology_version"], "1.1.1")
        self.assertEqual(projected, ["A", "B"])
        self.assertEqual(observed_versions, ["1.1.1", "1.1.1"])
        self.assertNotEqual(old.version, active.version, "历史业务版本不应被改写")
        self.assertEqual(graph.close.call_count, 2)

    def test_no_active_projection_fails_before_graph_or_snapshot_access(self):
        versions, postgres, graph = MagicMock(), Mock(), Mock()
        versions.active.return_value = None
        with patch.object(outbox, "OntologyVersionRepository", return_value=versions):
            with self.assertRaises(outbox.OutboxHandlerError) as caught:
                self.worker(postgres, graph)._sync_and_verify(build_personal_credit_baseline())
        self.assertEqual(caught.exception.code, "ACTION_ACTIVE_PROJECTION_UNAVAILABLE")
        postgres.validation_profile.assert_not_called()
        graph.sync_postgres_profile.assert_not_called()
        versions.publication_guard.return_value.__exit__.assert_called_once()


class AsyncEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_does_not_block_other_requests_during_slow_graph_io(self):
        entered, release = threading.Event(), threading.Event()
        graph = Mock()
        def slow_ping():
            entered.set()
            return release.wait(3)
        graph.ping.side_effect = slow_ping
        postgres = Mock()
        postgres.ping.return_value = True
        async def other_request():
            while not entered.is_set():
                await asyncio.sleep(0)
            request = Mock()
            request.state.mvp_auth_expires_at = None
            response = await routes.check_mvp_authentication(request)
            release.set()
            return response
        with patch.object(routes, "postgres", return_value=postgres), patch.object(routes, "nebula", return_value=graph):
            health, _ = await asyncio.gather(routes.health(), other_request())
        self.assertEqual(health["status"], "ok", "慢请求必须允许事件循环继续处理其他请求")

    async def test_threaded_action_preserves_pinned_ontology_context(self):
        from app.core.concurrency import threaded_endpoint
        seen = []
        main_thread = threading.get_ident()
        @threaded_endpoint
        def action():
            seen.append(threading.get_ident())
            return routes._ACTION_ONTOLOGY_VERSION_OVERRIDE.get()
        token = routes._ACTION_ONTOLOGY_VERSION_OVERRIDE.set("historical-version")
        try:
            self.assertEqual(await action(), "historical-version")
        finally:
            routes._ACTION_ONTOLOGY_VERSION_OVERRIDE.reset(token)
        self.assertNotEqual(seen, [main_thread])

    async def test_health_reports_graph_initialization_failure_as_degraded(self):
        postgres = Mock()
        postgres.ping.return_value = True
        with patch.object(routes, "postgres", return_value=postgres), \
             patch.object(routes, "nebula", side_effect=TimeoutError("unavailable")):
            health = await routes.health()
        self.assertEqual(health, {"status": "degraded", "postgresql": True, "nebulagraph": False})

    async def test_manual_sync_holds_publication_lock_before_reading_snapshot(self):
        order = []
        @contextmanager
        def guard():
            order.append("lock")
            try:
                yield
            finally:
                order.append("unlock")
        postgres, graph, versions = Mock(), Mock(), Mock()
        versions.publication_guard.side_effect = guard
        postgres.validation_profile.side_effect = lambda: order.append("snapshot") or {}
        graph.sync_postgres_profile.side_effect = lambda *args: order.append("write") or {}
        graph.mapping_report.side_effect = lambda *args: order.append("verify") or {"valid": True}
        with patch.object(routes, "version_store", versions), patch.object(routes, "postgres", return_value=postgres), \
             patch.object(routes, "nebula", return_value=graph), \
             patch.object(routes, "active_version_proposal", return_value=build_personal_credit_baseline()), \
             patch.object(routes, "validate_catalog_data_quality", return_value={"valid": True}), \
             patch.object(routes, "execute_axioms", return_value={"valid": True}):
            await routes.sync(routes.ProfileRequest())
        self.assertEqual(order, ["lock", "snapshot", "write", "verify", "unlock"])


class NebulaTimeoutTests(unittest.TestCase):
    def test_pool_uses_a_finite_request_timeout_and_closes_failed_initialization(self):
        with patch("app.kg.nebula.ConnectionPool") as pool_factory:
            pool = pool_factory.return_value
            pool.init.return_value = False
            with self.assertRaises(RuntimeError):
                NebulaGraphService("127.0.0.1", 9669, "test", "test", "test")
            config = pool.init.call_args.args[1]
            self.assertGreater(config.timeout, 0)
            self.assertLessEqual(config.timeout, 60000)
            pool.close.assert_called_once()


class NameRedactionTests(unittest.TestCase):
    def test_personal_names_of_every_script_are_redacted_in_nested_and_audit_payloads(self):
        for name in ("Alice Smith", "José García", "Ольга", "张三"):
            with self.subTest(name=name):
                source = {"borrower": {"name": name}, "props": json.dumps({"name": name}, ensure_ascii=False)}
                self.assertNotIn(name, json.dumps(mask_payload(source), ensure_ascii=False))
                self.assertNotIn(name, json.dumps(_safe_mapping(source), ensure_ascii=False))

    def test_explicit_schema_names_survive_without_exempting_business_evidence(self):
        source = {"proposal": {"name": "NaturalPerson"}, "results": [{"name": "CompleteIdentity",
                  "violations": [{"name": "Alice Smith"}]}], "audit_result": {"input_summary": {"name": "Bob Smith"}}}
        result = mask_schema_payload(source)
        self.assertEqual(result["proposal"]["name"], "NaturalPerson")
        self.assertEqual(result["results"][0]["name"], "CompleteIdentity")
        self.assertNotIn("Alice Smith", json.dumps(result))
        self.assertNotIn("Bob Smith", json.dumps(result))

    def test_graph_schema_names_survive_but_entity_names_are_masked(self):
        source = {"nodes": [
            {"id": "class", "tags": ["ontology"], "properties": {"name": "NaturalPerson"}},
            {"id": "person", "tags": ["entity"], "properties": {"props": json.dumps({"name": "Alice Smith"})}},
        ], "edges": [{"source": "person", "target": "class"}]}
        result = mask_graph_snapshot(source)
        self.assertEqual(result["nodes"][0]["properties"]["name"], "NaturalPerson")
        self.assertNotIn("Alice Smith", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
