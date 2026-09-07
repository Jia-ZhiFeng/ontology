import asyncio
import json
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.action.capabilities import CapabilityAccess
from app.agent.capability_registry import (
    AgentAuthenticationSource,
    AgentCapabilityAllowlist,
    AgentCapabilityRegistry,
    AgentDynamicPolicy,
    AgentPrincipalContext,
    PublishedReadProjectionAttestation,
)
from app.agent.conversation import AgentConversationError, AgentConversationStore
from app.agent.ontology_context import OntologyContextProvider
from app.agent.ontology_runtime import (
    OntologyAgentBudgetLimits,
    OntologyAgentRuntime,
)
from app.agent.personal_credit import PersonalCreditAgentService
from app.api import routes
from app.ontology.audit import proposal_digest
from app.ontology.baseline import build_personal_credit_baseline
from app.ontology.data_binding import OntologyDataBinding


READ_ATTESTATION_KEY = b"ontology-runtime-read-attestation-key-32-bytes"
PHASE1_AGENT_CONTRACT = json.loads(
    (
        Path(__file__).resolve().parent
        / "golden/personal_credit_phase1_contract.json"
    ).read_text(encoding="utf-8")
)["agent"]["action_run_response"]


class _FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def chat_json(self, system, user, **options):
        self.calls.append({
            "system": system,
            "user": json.loads(user),
            "options": options,
        })
        if not self.responses:
            raise RuntimeError("no fake model response")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _ReadExecutor:
    def __init__(self, *, advance_clock=None):
        self.calls = []
        self.advance_clock = advance_clock

    async def __call__(self, *, authorization, application_id, snapshot):
        self.calls.append({
            "capability_name": authorization.capability_name,
            "application_id": application_id,
            "snapshot": snapshot,
            "authorization": authorization,
        })
        if self.advance_clock is not None:
            self.advance_clock()
        if authorization.capability_name == "inspect_case_readiness":
            return {
                "status": "READY",
                "available_evidence": ["identity", "income"],
                "missing_evidence": [],
                "available_tools": ["preview_risk_assessment"],
                "blocked_tools": {},
                "application_id": "must-not-leak",
            }
        if authorization.capability_name == "preview_risk_assessment":
            return {
                "risk_level": "LOW",
                "pd_value": 0.023,
                "check_result": "PASS",
                "risk_factors": [
                    {"factor": "income_stability", "contribution": -0.18}
                ],
                "decision_boundary": "NOT_FINAL_APPROVAL",
                "snapshot_id": "must-not-leak",
            }
        if authorization.capability_name == "generate_verification_questions":
            return {
                "question_count": 1,
                "phone_questions": [{
                    "category": "employment",
                    "question": "请确认当前任职时间。",
                    "required": True,
                }],
            }
        if authorization.capability_name == "generate_decision_support":
            return {
                "recommended_decision": "MANUAL_REVIEW",
                "decision_reasons": [{
                    "code": "RISK_REVIEW",
                    "description": "建议人工复核收入稳定性。",
                }],
                "decision_boundary": "NOT_FINAL_APPROVAL",
            }
        raise RuntimeError("unexpected read capability")


class OntologyAgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.proposal = build_personal_credit_baseline()
        ontology_digest = proposal_digest(self.proposal)
        self.record = {
            "ontology_id": "personal-credit",
            "version": self.proposal.version,
            "status": "published",
            "is_active": True,
            "published_at": datetime(2026, 9, 4, tzinfo=timezone.utc),
            "proposal": self.proposal,
            "proposal_digest": ontology_digest,
            "audit_digest": ontology_digest,
        }
        self.active_loads = 0

        def active_loader():
            self.active_loads += 1
            return self.record

        self.provider = OntologyContextProvider(
            lambda _version: self.record,
            active_loader,
            secret=b"ontology-runtime-context-secret-32-bytes!!",
        )
        self.publication = self.provider.resolve_published_ontology()
        self.snapshot = {
            "application": {"application_id": "APP-RUNTIME-001"},
            "snapshot_id": "SNAPSHOT-RUNTIME-001",
            "data_binding_version": self.proposal.version,
            "data_binding_digest": OntologyDataBinding(
                self.proposal
            ).binding_digest,
        }
        context = self.provider.bind_snapshot(
            self.publication, "APP-RUNTIME-001", self.snapshot
        )
        self.catalog = routes._personal_credit_action_capability_catalog(
            self.proposal
        )
        read_names = tuple(
            item.name for item in self.catalog.capabilities
            if item.access is CapabilityAccess.READ
        )
        write_names = tuple(
            item.name for item in self.catalog.capabilities
            if item.access is CapabilityAccess.WRITE
        )
        self.attestation = PublishedReadProjectionAttestation.sign_for_publication(
            context,
            self.catalog,
            signing_key=READ_ATTESTATION_KEY,
        )
        self.registry = AgentCapabilityRegistry(
            catalog_builder=lambda: self.catalog,
            context_provider=self.provider,
            read_projection_attestation=self.attestation,
            read_projection_verification_key=READ_ATTESTATION_KEY,
            allowlist=AgentCapabilityAllowlist(
                revision="ontology-runtime-allowlist-v1",
                read_names=read_names,
                write_proposal_names=write_names,
            ),
            dynamic_policy=AgentDynamicPolicy(
                revision="ontology-runtime-policy-v1",
                allowed_names=read_names + write_names,
                denied_names=(),
                write_proposal_authentication_sources=(
                    AgentAuthenticationSource.LOCAL_SESSION,
                ),
            ),
        )
        self.store = AgentConversationStore(
            scope_secret=b"ontology-runtime-conversation-scope-key"
        )
        self.snapshot_calls = []

    def _principal(self, context, *, principal_id="local-runtime-user"):
        return AgentPrincipalContext(
            principal_id=principal_id,
            authentication_source=AgentAuthenticationSource.LOCAL_SESSION,
            authenticated=True,
            application_id=context.application_id,
            snapshot_id=context.snapshot_id,
        )

    def _runtime(
        self,
        llm,
        executor=None,
        *,
        clock=None,
        limits=None,
        publication=True,
        principal_id="local-runtime-user",
    ):
        executor = executor or _ReadExecutor()

        async def snapshot_loader(application_id):
            self.snapshot_calls.append(application_id)
            return self.snapshot

        return OntologyAgentRuntime(
            context_provider=self.provider,
            capability_registry=self.registry,
            principal=lambda context: self._principal(
                context, principal_id=principal_id
            ),
            snapshot_loader=snapshot_loader,
            read_executor=executor,
            published_ontology=self.publication if publication else None,
            llm_client=llm,
            conversation_store=self.store,
            budget_limits=limits,
            clock=clock,
        ), executor

    async def test_lookup_read_observation_replan_final_and_safe_response(self):
        llm = _FakeLLM(
            {
                "decision": "LOOKUP_ONTOLOGY",
                "capability_names": ["preview_risk_assessment"],
            },
            {
                "decision": "CALL_READ_ACTIONS",
                "calls": [{
                    "capability_name": "preview_risk_assessment",
                    "arguments": {},
                }],
            },
            {
                "decision": "FINAL",
                "answer": "风险预览显示当前为低风险。",
                "observation_ids": ["obs-1", "obs-2"],
                "boundary": "NOT_FINAL_APPROVAL",
            },
        )
        runtime, executor = self._runtime(llm)

        result = await runtime.run(
            "APP-RUNTIME-001", "请结合本体能力判断当前风险", max_steps=4
        )

        self.assertEqual(result["model_calls"], 3)
        self.assertTrue(result["executed"])
        self.assertEqual(len(executor.calls), 1)
        self.assertIs(executor.calls[0]["snapshot"], self.snapshot)
        self.assertEqual(
            [item["kind"] for item in result["observations"]],
            ["ONTOLOGY_LOOKUP", "READ_ACTION"],
        )
        stages = [item["stage"] for item in result["trace"]]
        self.assertEqual(
            stages,
            ["plan", "ontology_lookup", "replan", "action", "observation",
             "replan", "answer"],
        )
        self.assertIn("不代表最终审批", result["final_answer"])
        self.assertIn("风险评估预览", result["final_answer"])
        self.assertNotIn("风险预览显示当前为低风险", result["final_answer"])
        self.assertEqual(
            result["synthesis"]["mode"], "deterministic_observation"
        )
        self.assertEqual(result["trace"][-1]["source"], "backend_observation")
        for key in (
            "response_type", "reason_code", "plan", "executed", "answer",
            "final_answer", "result", "executions", "execution_summary",
            "proposed_actions", "synthesis", "model_calls", "trace",
            "conversation_id", "turn_index", "context_window",
        ):
            self.assertIn(key, result)
        self.assertTrue(
            set(PHASE1_AGENT_CONTRACT["top_level_fields"]).issubset(result)
        )
        self.assertTrue(
            set(PHASE1_AGENT_CONTRACT["plan_fields"]).issubset(result["plan"])
        )
        self.assertTrue(
            set(PHASE1_AGENT_CONTRACT["execution_fields"]).issubset(
                result["executions"][0]
            )
        )
        self.assertTrue(
            set(PHASE1_AGENT_CONTRACT["execution_summary_fields"]).issubset(
                result["execution_summary"]
            )
        )
        self.assertTrue(
            set(PHASE1_AGENT_CONTRACT["synthesis_fields"]).issubset(
                result["synthesis"]
            )
        )

        public_json = json.dumps(result, ensure_ascii=False)
        for secret in (
            "SNAPSHOT-RUNTIME-001", "local-runtime-user",
            "must-not-leak", "思维过程",
        ):
            self.assertNotIn(secret, public_json)
        model_payloads=json.dumps(llm.calls,ensure_ascii=False)
        self.assertNotIn("APP-RUNTIME-001",model_payloads)
        self.assertNotIn(
            "APP-RUNTIME-001",
            json.dumps({
                "trace":result["trace"],
                "observations":result["observations"],
            },ensure_ascii=False),
        )

    async def test_natural_expression_uses_manifest_without_legacy_keyword_router(self):
        instruction = "这单接下来最该关注哪一项？"
        self.assertEqual(
            PersonalCreditAgentService._deterministic_tools(instruction, 1),
            [],
        )
        llm = _FakeLLM(
            {
                "decision": "CALL_READ_ACTIONS",
                "calls": [{
                    "capability_name": "preview_risk_assessment",
                    "arguments": {},
                }],
            },
            {
                "decision": "FINAL",
                "answer": "这项关注点的风险预览为低风险。",
                "observation_ids": ["obs-1"],
                "boundary": "NOT_FINAL_APPROVAL",
            },
        )
        runtime, executor = self._runtime(llm)

        with (
            patch.object(
                PersonalCreditAgentService,
                "_deterministic_tools",
                side_effect=AssertionError("legacy keyword router must not run"),
            ) as legacy_router,
            patch.object(
                PersonalCreditAgentService,
                "_deterministic_follow_up_tools",
                side_effect=AssertionError("legacy follow-up router must not run"),
            ) as legacy_follow_up,
        ):
            result = await runtime.run(
                "APP-RUNTIME-001", instruction, max_steps=1
            )

        legacy_router.assert_not_called()
        legacy_follow_up.assert_not_called()
        self.assertEqual(
            [item["capability_name"] for item in executor.calls],
            ["preview_risk_assessment"],
        )
        manifest_names = {
            item["name"]
            for item in llm.calls[0]["user"]["capability_manifest"]["capabilities"]
        }
        self.assertIn("preview_risk_assessment", manifest_names)
        self.assertEqual(
            result["executions"][0]["tool_name"],
            "preview_risk_assessment",
        )

    async def test_batch_is_fully_authorized_before_any_read_executes(self):
        llm = _FakeLLM({
            "decision": "CALL_READ_ACTIONS",
            "calls": [
                {"capability_name": "preview_risk_assessment", "arguments": {}},
                {"capability_name": "trigger_manual_review", "arguments": {}},
            ],
        })
        runtime, executor = self._runtime(llm)

        result = await runtime.run(
            "APP-RUNTIME-001", "评估风险并直接人工复核", max_steps=2
        )

        self.assertEqual(executor.calls, [])
        self.assertEqual(result["executions"], [])
        self.assertEqual(result["reason_code"], "READ_BATCH_AUTHORIZATION_REJECTED")
        self.assertTrue(result["degraded"])

    async def test_read_observations_survive_model_failure(self):
        llm = _FakeLLM(
            {
                "decision": "CALL_READ_ACTIONS",
                "calls": [
                    {"capability_name": "preview_risk_assessment", "arguments": {}},
                    {"capability_name": "generate_verification_questions", "arguments": {}},
                ],
            },
            RuntimeError("raw provider error must not leak"),
        )
        runtime, executor = self._runtime(llm)

        result = await runtime.run(
            "APP-RUNTIME-001", "评估风险并准备电核问题", max_steps=2
        )

        self.assertEqual(len(executor.calls), 2)
        self.assertTrue(all(call["snapshot"] is self.snapshot for call in executor.calls))
        self.assertEqual(result["execution_summary"]["executed"], 2)
        self.assertEqual(len(result["observations"]), 2)
        self.assertIn("风险评估预览", result["final_answer"])
        self.assertIn("已生成1个电核问题", result["final_answer"])
        self.assertNotIn("raw provider error", json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["reason_code"], "ONTOLOGY_AGENT_MODEL_FAILED")

    async def test_non_finite_read_values_are_rejected_before_model_replan(self):
        class NonFiniteRiskExecutor(_ReadExecutor):
            async def __call__(self, *, authorization, application_id, snapshot):
                result = await super().__call__(
                    authorization=authorization,
                    application_id=application_id,
                    snapshot=snapshot,
                )
                result["pd_value"] = float("nan")
                return result

        llm = _FakeLLM({
            "decision": "CALL_READ_ACTIONS",
            "calls": [{
                "capability_name": "preview_risk_assessment",
                "arguments": {},
            }],
        })
        runtime, executor = self._runtime(llm, NonFiniteRiskExecutor())

        result = await runtime.run(
            "APP-RUNTIME-001", "读取风险预览", max_steps=1
        )

        self.assertEqual(len(executor.calls), 1)
        self.assertGreaterEqual(len(llm.calls), 1)
        self.assertEqual(result["executions"][0]["status"], "FAILED")
        self.assertNotIn("NaN", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("NaN", json.dumps(llm.calls, ensure_ascii=False))

    async def test_execute_false_plans_and_performs_zero_reads(self):
        llm = _FakeLLM({
            "decision": "CALL_READ_ACTIONS",
            "calls": [{
                "capability_name": "preview_risk_assessment",
                "arguments": {},
            }],
        })
        runtime, executor = self._runtime(llm)

        result = await runtime.run(
            "APP-RUNTIME-001", "规划风险预览", execute=False, max_steps=1
        )

        self.assertEqual(executor.calls, [])
        self.assertFalse(result["executed"])
        self.assertEqual(result["response_type"], "ACTION_PLAN")
        self.assertEqual(result["executions"][0]["status"], "PLANNED")
        self.assertEqual(result["reason_code"], "EXECUTION_DISABLED_BY_REQUEST")

    async def test_write_decision_only_creates_in_memory_proposal(self):
        llm = _FakeLLM(
            {
                "decision": "PROPOSE_WRITE_ACTION",
                "capability_name": "trigger_manual_review",
                "arguments": {"review_reason": "风险信号需要人工复核"},
            },
            {
                "decision": "FINAL",
                "answer": "已准备待确认的人工复核提议，尚未执行。",
                "observation_ids": ["obs-1"],
                "boundary": "NOT_FINAL_APPROVAL",
            },
        )
        runtime, executor = self._runtime(llm)

        result = await runtime.run(
            "APP-RUNTIME-001", "建议转人工复核", max_steps=1
        )

        self.assertEqual(executor.calls, [])
        self.assertEqual(result["response_type"], "ACTION_PLAN")
        self.assertEqual(result["execution_summary"]["status"], "PROPOSED")
        self.assertEqual(result["executions"][0]["status"], "PROPOSED")
        self.assertEqual(len(result["proposed_actions"]), 1)
        proposal = result["proposed_actions"][0]
        self.assertTrue(proposal["proposal_only"])
        self.assertTrue(proposal["requires_confirmation"])
        self.assertFalse(proposal["auto_executable"])
        self.assertFalse(proposal["final_approval"])

    async def test_unknown_injected_or_query_arguments_execute_nothing(self):
        cases = (
            ("unknown_read", {}),
            ("preview_risk_assessment", {"application_id": "injected"}),
            ("preview_risk_assessment", {"sql": "select secret"}),
            ("preview_risk_assessment", {"cypher": "MATCH (n) RETURN n"}),
            ("preview_risk_assessment", {"nested": {"x": 1}}),
        )
        for capability_name, arguments in cases:
            with self.subTest(capability_name=capability_name, arguments=arguments):
                llm = _FakeLLM({
                    "decision": "CALL_READ_ACTIONS",
                    "calls": [{
                        "capability_name": capability_name,
                        "arguments": arguments,
                    }],
                })
                runtime, executor = self._runtime(llm)
                result = await runtime.run(
                    "APP-RUNTIME-001", "读取案件", max_steps=1
                )
                self.assertEqual(executor.calls, [])
                self.assertEqual(result["executions"], [])
                self.assertEqual(
                    result["reason_code"], "READ_BATCH_AUTHORIZATION_REJECTED"
                )

    async def test_write_reason_query_language_creates_no_proposal_or_execution(self):
        unsafe_reasons = (
            "SELECT customer FROM secret_table",
            "MATCH (n) RETURN n",
        )
        for reason in unsafe_reasons:
            with self.subTest(reason=reason):
                llm = _FakeLLM({
                    "decision": "PROPOSE_WRITE_ACTION",
                    "capability_name": "trigger_manual_review",
                    "arguments": {"review_reason": reason},
                })
                runtime, executor = self._runtime(llm)

                result = await runtime.run(
                    "APP-RUNTIME-001", "准备人工复核提议", max_steps=1
                )

                self.assertEqual(executor.calls, [])
                self.assertEqual(result["executions"], [])
                self.assertEqual(result["proposed_actions"], [])
                self.assertFalse(result["executed"])
                self.assertEqual(
                    result["reason_code"], "WRITE_PROPOSAL_REJECTED"
                )

    async def test_duplicate_signature_is_not_replayed(self):
        repeated = {
            "decision": "CALL_READ_ACTIONS",
            "calls": [{
                "capability_name": "preview_risk_assessment",
                "arguments": {},
            }],
        }
        llm = _FakeLLM(repeated, repeated)
        runtime, executor = self._runtime(llm)

        result = await runtime.run(
            "APP-RUNTIME-001", "读取一次风险预览", max_steps=2
        )

        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(result["execution_summary"]["executed"], 1)
        self.assertEqual(result["reason_code"], "READ_BATCH_AUTHORIZATION_REJECTED")

    async def test_lookup_budget_is_one(self):
        lookup = {
            "decision": "LOOKUP_ONTOLOGY",
            "capability_names": ["preview_risk_assessment"],
        }
        llm = _FakeLLM(lookup, lookup)
        runtime, executor = self._runtime(llm)

        result = await runtime.run(
            "APP-RUNTIME-001", "查询本体两次", max_steps=1
        )

        self.assertEqual(executor.calls, [])
        self.assertEqual(result["budget"]["used"]["ontology_lookups"], 1)
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["reason_code"], "ONTOLOGY_LOOKUP_REJECTED")

    async def test_soft_deadline_stops_replanning_but_preserves_observation(self):
        now = [0.0]
        clock = lambda: now[0]
        executor = _ReadExecutor(advance_clock=lambda: now.__setitem__(0, 31.0))
        llm = _FakeLLM({
            "decision": "CALL_READ_ACTIONS",
            "calls": [{
                "capability_name": "preview_risk_assessment",
                "arguments": {},
            }],
        })
        runtime, executor = self._runtime(llm, executor, clock=clock)

        result = await runtime.run(
            "APP-RUNTIME-001", "风险预览后继续判断", max_steps=1
        )

        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["reason_code"], "AGENT_BUDGET_FALLBACK")
        self.assertIn("风险评估预览", result["final_answer"])

    async def test_published_identity_is_pinned_before_snapshot_and_not_reloaded(self):
        events = []

        def active_loader():
            events.append("publication")
            return self.record

        provider = OntologyContextProvider(
            lambda _version: self.record,
            active_loader,
            secret=b"ontology-runtime-context-secret-32-bytes!!",
        )
        original_provider = self.provider
        self.provider = provider
        context = provider.bind_snapshot(
            provider.resolve_published_ontology(),
            "APP-RUNTIME-001",
            self.snapshot,
        )
        self.attestation = PublishedReadProjectionAttestation.sign_for_publication(
            context, self.catalog, signing_key=READ_ATTESTATION_KEY
        )
        self.registry = AgentCapabilityRegistry(
            catalog_builder=lambda: self.catalog,
            context_provider=provider,
            read_projection_attestation=self.attestation,
            read_projection_verification_key=READ_ATTESTATION_KEY,
            allowlist=self.registry._allowlist,
            dynamic_policy=self.registry._dynamic_policy,
        )
        events.clear()

        async def snapshot_loader(_application_id):
            events.append("snapshot")
            return self.snapshot

        runtime = OntologyAgentRuntime(
            context_provider=provider,
            capability_registry=self.registry,
            principal=lambda ctx: self._principal(ctx),
            snapshot_loader=snapshot_loader,
            read_executor=_ReadExecutor(),
            llm_client=_FakeLLM({
                "decision": "FINAL",
                "answer": "本轮无需调用能力。",
                "observation_ids": [],
                "boundary": "NOT_FINAL_APPROVAL",
            }),
            conversation_store=self.store,
        )
        try:
            await runtime.run("APP-RUNTIME-001", "仅回答", max_steps=1)
        finally:
            self.provider = original_provider

        self.assertEqual(events, ["publication", "snapshot"])

    async def test_conversation_scope_separates_principals_but_allows_new_snapshot(self):
        llm = _FakeLLM({
            "decision": "FINAL",
            "answer": "第一轮完成。",
            "observation_ids": [],
            "boundary": "NOT_FINAL_APPROVAL",
        })
        runtime, _executor = self._runtime(llm, principal_id="principal-a")
        first = await runtime.run("APP-RUNTIME-001", "第一轮", max_steps=1)
        first_context_id = llm.calls[0]["user"]["capability_manifest"][
            "context"
        ]["context_id"]

        other_llm = _FakeLLM({
            "decision": "FINAL",
            "answer": "不应执行。",
            "observation_ids": [],
            "boundary": "NOT_FINAL_APPROVAL",
        })
        other_runtime, other_executor = self._runtime(
            other_llm, principal_id="principal-b"
        )
        with self.assertRaises(AgentConversationError):
            await other_runtime.run(
                "APP-RUNTIME-001",
                "另一主体",
                max_steps=1,
                conversation_id=first["conversation_id"],
            )
        self.assertEqual(other_llm.calls, [])
        self.assertEqual(other_executor.calls, [])

        self.snapshot = dict(self.snapshot)
        self.snapshot["snapshot_id"] = "SNAPSHOT-RUNTIME-002"
        same_llm = _FakeLLM({
            "decision": "FINAL",
            "answer": "第二轮完成。",
            "observation_ids": [],
            "boundary": "NOT_FINAL_APPROVAL",
        })
        same_runtime, _ = self._runtime(same_llm, principal_id="principal-a")
        second = await same_runtime.run(
            "APP-RUNTIME-001",
            "第二轮",
            max_steps=1,
            conversation_id=first["conversation_id"],
        )
        self.assertEqual(second["turn_index"], 2)
        second_context_id = same_llm.calls[0]["user"]["capability_manifest"][
            "context"
        ]["context_id"]
        self.assertNotEqual(first_context_id, second_context_id)
        self.assertEqual(
            self.snapshot_calls,
            ["APP-RUNTIME-001", "APP-RUNTIME-001", "APP-RUNTIME-001"],
        )

    async def test_conversation_cannot_cross_application(self):
        snapshot_calls = []

        async def snapshot_loader(application_id):
            snapshot_calls.append(application_id)
            return {
                **self.snapshot,
                "application": {"application_id": application_id},
                "snapshot_id": f"SNAPSHOT-{application_id}",
            }

        first_llm = _FakeLLM({
            "decision": "FINAL",
            "answer": "第一案件回合完成。",
            "observation_ids": [],
            "boundary": "NOT_FINAL_APPROVAL",
        })
        executor = _ReadExecutor()
        runtime = OntologyAgentRuntime(
            context_provider=self.provider,
            capability_registry=self.registry,
            principal=lambda context: self._principal(
                context, principal_id="principal-a"
            ),
            snapshot_loader=snapshot_loader,
            read_executor=executor,
            published_ontology=self.publication,
            llm_client=first_llm,
            conversation_store=self.store,
        )
        first = await runtime.run("APP-RUNTIME-001", "第一案件", max_steps=1)
        other_llm = _FakeLLM({
            "decision": "FINAL",
            "answer": "不应执行。",
            "observation_ids": [],
            "boundary": "NOT_FINAL_APPROVAL",
        })

        with self.assertRaises(AgentConversationError):
            await runtime.run(
                "APP-RUNTIME-002",
                "第二案件",
                max_steps=1,
                conversation_id=first["conversation_id"],
                llm_client=other_llm,
            )

        self.assertEqual(snapshot_calls, ["APP-RUNTIME-001", "APP-RUNTIME-002"])
        self.assertEqual(other_llm.calls, [])
        self.assertEqual(executor.calls, [])

    async def test_conversation_cannot_cross_ontology_version_or_digest(self):
        first_llm = _FakeLLM({
            "decision": "FINAL",
            "answer": "第一版本回合完成。",
            "observation_ids": [],
            "boundary": "NOT_FINAL_APPROVAL",
        })
        first_runtime, _ = self._runtime(first_llm)
        first = await first_runtime.run(
            "APP-RUNTIME-001", "第一版本", max_steps=1
        )

        version_two = self.proposal.model_copy(deep=True)
        version_two.version = "1.2.0"
        version_two_digest = proposal_digest(version_two)
        version_two_record = {
            "ontology_id": "personal-credit",
            "version": version_two.version,
            "status": "published",
            "is_active": True,
            "published_at": datetime(2026, 9, 5, tzinfo=timezone.utc),
            "proposal": version_two,
            "proposal_digest": version_two_digest,
            "audit_digest": version_two_digest,
        }
        version_two_provider = OntologyContextProvider(
            lambda version: (
                version_two_record if version == version_two.version else None
            ),
            lambda: version_two_record,
            secret=b"ontology-runtime-context-secret-32-bytes!!",
        )
        version_two_publication = (
            version_two_provider.resolve_published_ontology(version_two.version)
        )
        version_two_snapshot = {
            "application": {"application_id": "APP-RUNTIME-001"},
            "snapshot_id": "SNAPSHOT-RUNTIME-V2",
            "data_binding_version": version_two.version,
            "data_binding_digest": OntologyDataBinding(
                version_two
            ).binding_digest,
        }
        version_two_context = version_two_provider.bind_snapshot(
            version_two_publication,
            "APP-RUNTIME-001",
            version_two_snapshot,
        )
        version_two_catalog = routes._personal_credit_action_capability_catalog(
            version_two
        )
        version_two_attestation = (
            PublishedReadProjectionAttestation.sign_for_publication(
                version_two_context,
                version_two_catalog,
                signing_key=READ_ATTESTATION_KEY,
            )
        )
        version_two_registry = AgentCapabilityRegistry(
            catalog_builder=lambda: version_two_catalog,
            context_provider=version_two_provider,
            read_projection_attestation=version_two_attestation,
            read_projection_verification_key=READ_ATTESTATION_KEY,
            allowlist=self.registry._allowlist,
            dynamic_policy=self.registry._dynamic_policy,
        )
        version_two_llm = _FakeLLM({
            "decision": "FINAL",
            "answer": "不应执行。",
            "observation_ids": [],
            "boundary": "NOT_FINAL_APPROVAL",
        })
        version_two_executor = _ReadExecutor()
        version_two_runtime = OntologyAgentRuntime(
            context_provider=version_two_provider,
            capability_registry=version_two_registry,
            principal=lambda context: self._principal(context),
            snapshot_loader=lambda _application_id: version_two_snapshot,
            read_executor=version_two_executor,
            published_ontology=version_two_publication,
            requested_ontology_version=version_two.version,
            llm_client=version_two_llm,
            conversation_store=self.store,
        )

        with self.assertRaises(AgentConversationError):
            await version_two_runtime.run(
                "APP-RUNTIME-001",
                "第二版本",
                max_steps=1,
                conversation_id=first["conversation_id"],
            )

        self.assertEqual(version_two_llm.calls, [])
        self.assertEqual(version_two_executor.calls, [])

    async def test_no_model_returns_safe_legacy_compatible_fallback(self):
        runtime, executor = self._runtime(None)

        result = await runtime.run(
            "APP-RUNTIME-001", "请处理案件", max_steps=1
        )

        self.assertEqual(executor.calls, [])
        self.assertEqual(result["response_type"], "NO_ACTION")
        self.assertEqual(result["reason_code"], "ONTOLOGY_AGENT_MODEL_UNAVAILABLE")
        self.assertEqual(result["model_calls"], 0)
        self.assertTrue(result["degraded"])

    async def test_model_budget_stops_at_three_and_preserves_three_reads(self):
        llm = _FakeLLM(
            {
                "decision": "CALL_READ_ACTIONS",
                "calls": [{"capability_name": "inspect_case_readiness", "arguments": {}}],
            },
            {
                "decision": "CALL_READ_ACTIONS",
                "calls": [{"capability_name": "preview_risk_assessment", "arguments": {}}],
            },
            {
                "decision": "CALL_READ_ACTIONS",
                "calls": [{"capability_name": "generate_verification_questions", "arguments": {}}],
            },
            AssertionError("a fourth model call must never happen"),
        )
        runtime, executor = self._runtime(llm)

        result = await runtime.run(
            "APP-RUNTIME-001", "按本体逐步读取三类证据", max_steps=4
        )

        self.assertEqual(result["model_calls"], 3)
        self.assertEqual(len(llm.calls), 3)
        self.assertEqual(len(executor.calls), 3)
        self.assertEqual(result["execution_summary"]["executed"], 3)
        self.assertEqual(result["reason_code"], "AGENT_BUDGET_FALLBACK")

    async def test_action_budget_rejects_next_batch_before_partial_execution(self):
        llm = _FakeLLM(
            {
                "decision": "CALL_READ_ACTIONS",
                "calls": [
                    {"capability_name": "inspect_case_readiness", "arguments": {}},
                    {"capability_name": "preview_risk_assessment", "arguments": {}},
                ],
            },
            {
                "decision": "CALL_READ_ACTIONS",
                "calls": [
                    {"capability_name": "generate_verification_questions", "arguments": {}},
                    {"capability_name": "generate_decision_support", "arguments": {}},
                    {"capability_name": "inspect_case_readiness", "arguments": {}},
                ],
            },
        )
        runtime, executor = self._runtime(llm)

        result = await runtime.run(
            "APP-RUNTIME-001", "读取多类证据", max_steps=4
        )

        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(result["budget"]["used"]["action_calls"], 2)
        self.assertEqual(result["reason_code"], "ACTION_BUDGET_REJECTED")

    async def test_four_read_actions_fit_one_batch_and_share_snapshot(self):
        names = [
            "inspect_case_readiness",
            "preview_risk_assessment",
            "generate_verification_questions",
            "generate_decision_support",
        ]
        llm = _FakeLLM(
            {
                "decision": "CALL_READ_ACTIONS",
                "calls": [
                    {"capability_name": name, "arguments": {}} for name in names
                ],
            },
            {
                "decision": "FINAL",
                "answer": (
                    "案件已就绪，风险为低风险；已生成电核问题，"
                    "只读决策建议为人工复核。"
                ),
                "observation_ids": ["obs-1", "obs-2", "obs-3", "obs-4"],
                "boundary": "NOT_FINAL_APPROVAL",
            },
        )
        runtime, executor = self._runtime(llm)

        result = await runtime.run(
            "APP-RUNTIME-001", "综合检查当前案件", max_steps=4
        )

        self.assertEqual(len(executor.calls), 4)
        self.assertTrue(all(call["snapshot"] is self.snapshot for call in executor.calls))
        self.assertEqual(result["budget"]["used"]["action_calls"], 4)

    async def test_ask_user_returns_clarification_without_actions(self):
        llm = _FakeLLM({
            "decision": "ASK_USER",
            "question": "请说明你希望检查材料完整性还是风险水平。",
        })
        runtime, executor = self._runtime(llm)

        result = await runtime.run(
            "APP-RUNTIME-001", "帮我看看", max_steps=1
        )

        self.assertEqual(executor.calls, [])
        self.assertEqual(result["reason_code"], "NEEDS_CLARIFICATION")
        self.assertIn("材料完整性还是风险水平", result["final_answer"])
        self.assertFalse(result["degraded"])

    async def test_all_eight_write_capabilities_remain_proposal_only(self):
        write_names = [
            item.name for item in self.catalog.capabilities
            if item.access is CapabilityAccess.WRITE
        ]
        self.assertEqual(len(write_names), 8)
        for name in write_names:
            with self.subTest(name=name):
                arguments = (
                    {"trigger_reason": "需要进一步电核"}
                    if name == "create_phone_verification_task"
                    else {"review_reason": "需要人工复核"}
                    if name == "trigger_manual_review"
                    else {}
                )
                llm = _FakeLLM(
                    {
                        "decision": "PROPOSE_WRITE_ACTION",
                        "capability_name": name,
                        "arguments": arguments,
                    },
                    {
                        "decision": "FINAL",
                        "answer": "已生成待确认提议，尚未执行。",
                        "observation_ids": ["obs-1"],
                        "boundary": "NOT_FINAL_APPROVAL",
                    },
                )
                runtime, executor = self._runtime(llm)
                result = await runtime.run(
                    "APP-RUNTIME-001", "生成待确认操作建议", max_steps=1
                )
                self.assertEqual(executor.calls, [])
                self.assertEqual(len(result["proposed_actions"]), 1)
                self.assertFalse(result["executed"])
                self.assertTrue(result["proposed_actions"][0]["proposal_only"])

    async def test_hard_deadline_cancels_awaited_read_and_starts_no_follow_up(self):
        class SlowRead:
            def __init__(self):
                self.calls = 0

            async def __call__(self, **_kwargs):
                self.calls += 1
                await asyncio.sleep(10)

        slow = SlowRead()
        llm = _FakeLLM({
            "decision": "CALL_READ_ACTIONS",
            "calls": [{"capability_name": "preview_risk_assessment", "arguments": {}}],
        })
        limits = OntologyAgentBudgetLimits(
            soft_timeout_seconds=0.5,
            hard_timeout_seconds=0.5,
        )
        runtime, _executor = self._runtime(llm, slow, limits=limits)
        started = time.monotonic()

        result = await runtime.run(
            "APP-RUNTIME-001", "执行受限风险预览", max_steps=1
        )

        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(slow.calls, 1)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(result["execution_summary"]["failed"], 1)

    async def test_final_cannot_invent_case_outcome_without_observation(self):
        llm = _FakeLLM({
            "decision": "FINAL",
            "answer": "该案件风险等级为低风险，材料齐全。",
            "observation_ids": [],
            "boundary": "NOT_FINAL_APPROVAL",
        })
        runtime, executor = self._runtime(llm)

        result = await runtime.run(
            "APP-RUNTIME-001", "判断案件结果", max_steps=1
        )

        self.assertEqual(executor.calls, [])
        self.assertEqual(result["reason_code"], "MODEL_FINAL_REJECTED")
        self.assertNotIn("低风险", result["final_answer"])

    async def test_final_rejects_approval_variants_and_observation_conflicts(self):
        for unsafe_answer in (
            "审批通过，可以进入后续流程。",
            "终审通过。",
            "APPROVED",
            "当前为高风险。",
            "虽然观察为低风险，但实际应视为高风险。",
        ):
            with self.subTest(answer=unsafe_answer):
                llm = _FakeLLM(
                    {
                        "decision": "CALL_READ_ACTIONS",
                        "calls": [{
                            "capability_name": "preview_risk_assessment",
                            "arguments": {},
                        }],
                    },
                    {
                        "decision": "FINAL",
                        "answer": unsafe_answer,
                        "observation_ids": ["obs-1"],
                        "boundary": "NOT_FINAL_APPROVAL",
                    },
                )
                runtime, executor = self._runtime(llm)
                result = await runtime.run(
                    "APP-RUNTIME-001", "判断风险", max_steps=1
                )
                self.assertEqual(len(executor.calls), 1)
                self.assertEqual(result["reason_code"], "MODEL_FINAL_REJECTED")
                self.assertIn("LOW", result["final_answer"])
                self.assertNotIn(unsafe_answer, result["final_answer"])

    async def test_final_rejects_approval_variants_without_observation(self):
        for unsafe_answer in (
            "审批通过，可以进入后续流程。",
            "终审通过。",
            "正式批准该申请。",
            "本申请予以批准。",
            "审批结果：通过。",
            "核准该申请。",
            "该案件可以进入放款流程。",
            "APPROVED",
            "APPROVE",
            "approval granted",
        ):
            with self.subTest(answer=unsafe_answer):
                llm = _FakeLLM({
                    "decision": "FINAL",
                    "answer": unsafe_answer,
                    "observation_ids": [],
                    "boundary": "NOT_FINAL_APPROVAL",
                })
                runtime, executor = self._runtime(llm)

                result = await runtime.run(
                    "APP-RUNTIME-001", "判断案件结果", max_steps=1
                )

                self.assertEqual(executor.calls, [])
                self.assertEqual(result["reason_code"], "MODEL_FINAL_REJECTED")
                self.assertNotIn(unsafe_answer, result["final_answer"])

    async def test_write_proposal_final_cannot_claim_execution_after_disclaimer(self):
        unsafe_answer = "待确认 Proposal 尚未执行，但其实已经执行完成。"
        llm = _FakeLLM(
            {
                "decision": "PROPOSE_WRITE_ACTION",
                "capability_name": "trigger_manual_review",
                "arguments": {"review_reason": "风险信号需要人工复核"},
            },
            {
                "decision": "FINAL",
                "answer": unsafe_answer,
                "observation_ids": ["obs-1"],
                "boundary": "NOT_FINAL_APPROVAL",
            },
        )
        runtime, executor = self._runtime(llm)

        result = await runtime.run(
            "APP-RUNTIME-001", "建议转人工复核", max_steps=1
        )

        self.assertEqual(executor.calls, [])
        self.assertEqual(result["reason_code"], "MODEL_FINAL_REJECTED")
        self.assertIn("尚未执行", result["final_answer"])
        self.assertNotIn("其实已经执行完成", result["final_answer"])

    async def test_credentials_are_redacted_and_model_cannot_request_them(self):
        llm = _FakeLLM({
            "decision": "ASK_USER",
            "question": "请提供您的密码和 API Key。",
        })
        runtime, executor = self._runtime(llm)

        result = await runtime.run(
            "APP-RUNTIME-001",
            "password=abc123 密码：dont-send-this",
            max_steps=1,
        )

        self.assertEqual(executor.calls, [])
        sent=json.dumps(llm.calls,ensure_ascii=False)
        self.assertNotIn("abc123",sent)
        self.assertNotIn("dont-send-this",sent)
        self.assertEqual(result["reason_code"],"MODEL_CLARIFICATION_REJECTED")
        self.assertNotIn("请提供您的密码",result["final_answer"])


if __name__ == "__main__":
    unittest.main()
