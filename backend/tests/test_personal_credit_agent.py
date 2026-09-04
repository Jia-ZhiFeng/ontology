import json
import unittest
from types import SimpleNamespace

from app.agent.personal_credit import (
    AgentPlanningError,
    PersonalCreditAgentService,
)
from app.agent.conversation import (
    AgentConversationError,
    AgentConversationStore,
)
from app.ontology.baseline import build_personal_credit_baseline


def complete_snapshot(**overrides):
    values = {
        "application": {"application_id": "APP-AGENT-001", "loan_amount": 180000},
        "borrower": {"monthly_income": 25000},
        "vehicle": {"invoice_price": 220000, "guide_price": 229800},
        "credit_report": {
            "report_id": "CR-001", "outstanding_loan_count": 1,
            "overdue_3y_count": 0, "total_overdue_count": 0,
            "inquiry_count": 2,
        },
        "external_debts": ({
            "debt_id": "DEBT-001", "monthly_payment": 1200,
            "overdue_status": "正常",
        },),
        "anti_fraud_report": {
            "fraud_report_id": "FR-001", "fraud_score": 12,
            "result": "通过", "hit_details": "未命中高风险规则",
        },
        "risk_assessment": {
            "assessment_id": "RA-001", "risk_level": "低", "pd_value": 0.02,
            "risk_factors": [{"factor": "DTI_BASE_CONTEXT"}],
            "check_result": "通过",
        },
        "phone_verification": {
            "verification_id": "PV-001", "conclusion": "通过", "doubts": None,
        },
        "snapshot_id": "100:100:",
        "snapshot_started_at": "2026-08-31T08:00:00+00:00",
        "data_binding_version": "1.1.0",
        "data_binding_digest": "a" * 64,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeAdapter:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot or complete_snapshot()
        self.executed = []
        self.load_calls = []

    def load_postgres_snapshot(self, application_id):
        self.load_calls.append(application_id)
        self.last_application_id = application_id
        return self.snapshot

    def _base(self, functions):
        return {
            "postgres_snapshot_id": self.snapshot.snapshot_id,
            "postgres_snapshot_time": self.snapshot.snapshot_started_at,
            "data_binding_version": self.snapshot.data_binding_version,
            "data_binding_digest": self.snapshot.data_binding_digest,
            "functions": functions,
        }

    def execute_risk_assessment(self, application_id, **kwargs):
        self.executed.append(("risk", application_id, kwargs))
        return self._base({"assess_application_risk": {"result": {
            "risk_level": "低", "pd_value": 0.02, "check_result": "通过",
            "risk_factors": [{
                "factor": "DTI_LOW", "value": 0.12, "contribution": 0.01,
                "evidence_source": "calculate_dti",
            }],
            "model": {"name": "rule-proxy", "version": "1"},
            "calculation_policy": "preview", "decision_boundary": "not_final",
        }}})

    def execute_phone_questions(self, application_id, **kwargs):
        self.executed.append(("questions", application_id, kwargs))
        return self._base({"generate_phone_questions": {"result": {
            "phone_questions": [{
                "question_id": "Q01", "category": "收入负债",
                "question": "请确认当前月收入和月还款金额。",
                "source_factors": ["DTI"], "required": True,
            }],
            "question_count": 1, "template_version": "phone-v1",
        }}})

    def execute_decision_support(self, application_id, **kwargs):
        self.executed.append(("decision", application_id, kwargs))
        return self._base({
            "aggregate_decision_support": {"result": {
                "recommended_decision": "通过",
                "decision_reasons": [{"code": "CHECKS_PASSED", "description": "检查通过"}],
                "decision_boundary": "not_final", "rule_version": "decision-v1",
            }},
            "build_evidence_explanation": {"result": {
                "evidence_chain": [{"source_type": "LoanApplication"}],
                "lineage": {"missing_sources": []},
            }},
        })


class FakeLLM:
    def __init__(self, response):
        self.responses = response if isinstance(response, list) else [response]
        self.calls = []

    async def chat_json(self, system, user, **options):
        self.calls.append((system, json.loads(user), options))
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        response = self.responses[index]
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, str):
            return response
        return json.dumps(response, ensure_ascii=False)


class PersonalCreditAgentTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self, snapshot=None, conversation_store=None):
        adapter = FakeAdapter(snapshot)
        return PersonalCreditAgentService(
            adapter,
            ontology_version="1.1.0",
            conversation_store=conversation_store,
        ), adapter

    def test_readiness_exposes_only_tools_with_complete_evidence(self):
        service, _adapter = self.make_service()
        readiness = service.inspect_case_readiness("APP-AGENT-001")
        self.assertEqual(readiness["status"], "READY")
        self.assertEqual(set(readiness["available_tools"]), {
            "inspect_case_readiness", "preview_risk_assessment",
            "generate_verification_questions", "generate_decision_support",
        })
        self.assertFalse(readiness["blocked_tools"])
        self.assertEqual(readiness["data_binding_version"], "1.1.0")

    async def test_decision_is_blocked_without_phone_evidence(self):
        service, adapter = self.make_service(complete_snapshot(phone_verification=None))
        result = await service.run(
            "APP-AGENT-001", "请给我最终信审决策建议"
        )
        self.assertFalse(result["executed"])
        self.assertEqual(result["plan"]["selected_tool"], "generate_decision_support")
        self.assertEqual(result["plan"]["missing_evidence"], ["phone_verification"])
        self.assertEqual(adapter.executed, [])

    async def test_deterministic_goal_executes_only_read_only_risk_preview(self):
        service, adapter = self.make_service()
        result = await service.run("APP-AGENT-001", "只生成风险评估报告")
        self.assertTrue(result["executed"])
        self.assertEqual(result["plan"]["planner_mode"], "deterministic")
        self.assertEqual(result["plan"]["selected_tool"], "preview_risk_assessment")
        self.assertEqual(len(result["plan"]["steps"]), 1)
        self.assertEqual(
            result["plan"]["steps"][0]["tool_name"],
            result["plan"]["selected_tool"],
        )
        self.assertEqual(result["result"]["risk_level"], "低")
        self.assertNotIn("value", result["result"]["risk_factors"][0])
        self.assertIn("违约概率为 2.00%", result["answer"])
        self.assertIn("DTI_LOW（贡献 0.01）", result["answer"])
        self.assertEqual(adapter.executed[0][0], "risk")
        self.assertIsNone(adapter.executed[0][2]["action_execution_id"])

    async def test_noise_and_out_of_domain_inputs_require_clarification_without_actions(self):
        invalid_inputs = (
            ("", "INVALID_QUERY"),
            ("   ", "INVALID_QUERY"),
            ("1", "INVALID_QUERY"),
            ("007", "INVALID_QUERY"),
            ("3.14159", "INVALID_QUERY"),
            ("!!!", "INVALID_QUERY"),
            ("。？！……", "INVALID_QUERY"),
        )
        for instruction, expected_reason_code in invalid_inputs:
            with self.subTest(instruction=instruction):
                service, adapter = self.make_service()
                llm = FakeLLM({
                    "goal": "猜测一个动作",
                    "selected_tool": "inspect_case_readiness",
                    "reason": "不应被调用",
                })

                result = await service.run(
                    "APP-AGENT-001",
                    instruction,
                    llm_client=llm,
                    max_steps=4,
                )

                self.assertEqual(result["plan"]["status"], "NEEDS_CLARIFICATION")
                self.assertEqual(result["plan"]["overall_status"], "NEEDS_CLARIFICATION")
                self.assertEqual(result["response_type"], "NO_ACTION")
                self.assertEqual(result["reason_code"], expected_reason_code)
                self.assertEqual(result["plan"]["response_type"], "NO_ACTION")
                self.assertEqual(result["plan"]["reason_code"], expected_reason_code)
                self.assertTrue(result["plan"]["clarification_required"])
                self.assertIsNone(result["plan"]["selected_tool"])
                self.assertEqual(result["plan"]["steps"], [])
                self.assertFalse(result["executed"])
                self.assertIsNone(result["result"])
                self.assertTrue(result["answer"].strip())
                self.assertEqual(result["final_answer"], result["answer"])
                self.assertEqual(result["executions"], [])
                self.assertEqual(result["proposed_actions"], [])
                self.assertEqual(result["execution_summary"], {
                    "planned": 0,
                    "executed": 0,
                    "blocked": 0,
                    "failed": 0,
                    "status": "NEEDS_CLARIFICATION",
                })
                self.assertEqual(adapter.load_calls, [])
                self.assertEqual(adapter.executed, [])
                self.assertEqual(llm.calls, [])

    async def test_greetings_are_chat_turns_and_never_load_or_execute_case_data(self):
        for instruction in ("你好！", "hello"):
            with self.subTest(instruction=instruction):
                service, adapter = self.make_service()
                llm = FakeLLM({"response_type": "ACTION_PLAN", "steps": []})

                result = await service.run(
                    "APP-AGENT-001", instruction, llm_client=llm
                )

                self.assertEqual(result["response_type"], "CHAT")
                self.assertEqual(result["reason_code"], "GREETING")
                self.assertEqual(result["plan"]["status"], "CHAT")
                self.assertEqual(result["executions"], [])
                self.assertEqual(result["execution_summary"]["status"], "CHAT")
                self.assertTrue(result["answer"].strip())
                self.assertEqual(result["final_answer"], result["answer"])
                self.assertEqual(adapter.load_calls, [])
                self.assertEqual(adapter.executed, [])
                self.assertEqual(llm.calls, [])

    async def test_unrecognized_text_without_llm_is_out_of_scope_and_runs_nothing(self):
        for instruction in ("测试", "今天天气怎么样", "hello world"):
            with self.subTest(instruction=instruction):
                service, adapter = self.make_service()

                result = await service.run(
                    "APP-AGENT-001", instruction, max_steps=4
                )

                self.assertEqual(result["response_type"], "NO_ACTION")
                self.assertEqual(result["reason_code"], "OUT_OF_SCOPE")
                self.assertEqual(result["plan"]["steps"], [])
                self.assertEqual(result["executions"], [])
                self.assertEqual(adapter.load_calls, [])
                self.assertEqual(adapter.executed, [])

    async def test_valid_short_credit_requests_still_execute_the_intended_tool(self):
        cases = (
            ("查风险", "preview_risk_assessment", "risk"),
            ("电核", "generate_verification_questions", "questions"),
        )
        for instruction, expected_tool, adapter_call in cases:
            with self.subTest(instruction=instruction):
                service, adapter = self.make_service()

                result = await service.run("APP-AGENT-001", instruction)

                self.assertEqual(result["plan"]["selected_tool"], expected_tool)
                self.assertEqual(result["plan"]["status"], "READY")
                self.assertEqual(result["response_type"], "ACTION_RUN")
                self.assertIsNone(result["reason_code"])
                self.assertTrue(result["executed"])
                self.assertEqual(adapter.executed[0][0], adapter_call)

    async def test_greeting_with_credit_intent_is_not_swallowed_by_greeting_guard(self):
        service, adapter = self.make_service()

        result = await service.run("APP-AGENT-001", "你好，查风险")

        self.assertEqual(result["response_type"], "ACTION_RUN")
        self.assertEqual(result["plan"]["selected_tool"], "preview_risk_assessment")
        self.assertTrue(result["executed"])
        self.assertEqual(adapter.executed[0][0], "risk")

    async def test_llm_can_understand_natural_credit_paraphrase_not_in_static_keywords(self):
        service, adapter = self.make_service()
        llm = FakeLLM({
            "status": "READY",
            "goal": "评估当前申请风险",
            "steps": [{
                "tool_name": "preview_risk_assessment",
                "reason": "用户在询问这笔申请是否可靠。",
            }],
        })

        result = await service.run(
            "APP-AGENT-001",
            "这笔单子靠谱吗",
            llm_client=llm,
            max_steps=2,
        )

        self.assertEqual(result["response_type"], "ACTION_RUN")
        self.assertEqual(result["plan"]["planner_mode"], "llm")
        self.assertEqual(result["plan"]["selected_tool"], "preview_risk_assessment")
        self.assertTrue(result["executed"])
        self.assertEqual(adapter.executed[0][0], "risk")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(
            result["synthesis"]["mode"], "deterministic_after_model_plan"
        )
        self.assertEqual(result["synthesis"]["status"], "COMPLETED")
        self.assertIn("风险等级为低", result["answer"])
        self.assertEqual(llm.calls[0][2], {
            "temperature": 0.1,
            "max_tokens": 700,
        })

    async def test_llm_can_explicitly_request_clarification_with_zero_steps(self):
        service, adapter = self.make_service()
        llm = FakeLLM({
            "status": "NEEDS_CLARIFICATION",
            "goal": "了解贷款申请",
            "steps": [],
            "clarification_question": "请说明要查风险、材料还是决策建议。",
        })

        result = await service.run(
            "APP-AGENT-001",
            "帮我了解这个贷款申请",
            llm_client=llm,
            max_steps=3,
        )

        self.assertEqual(result["plan"]["planner_mode"], "llm")
        self.assertEqual(result["plan"]["status"], "NEEDS_CLARIFICATION")
        self.assertEqual(result["response_type"], "NO_ACTION")
        self.assertEqual(result["reason_code"], "NEEDS_CLARIFICATION")
        self.assertEqual(result["plan"]["steps"], [])
        self.assertEqual(result["executions"], [])
        self.assertEqual(result["answer"], "请说明要查风险、材料还是决策建议。")
        self.assertEqual(adapter.executed, [])
        self.assertEqual(len(llm.calls), 1)

    async def test_llm_selects_registered_question_tool_then_backend_executes(self):
        service, adapter = self.make_service()
        llm = FakeLLM({
            "goal": "生成本轮电核问题",
            "selected_tool": "generate_verification_questions",
            "reason": "已有风险和反欺诈证据。",
        })
        result = await service.run(
            "APP-AGENT-001",
            "请按现有情况准备一份与申请人沟通的清单",
            llm_client=llm,
        )
        self.assertEqual(result["plan"]["planner_mode"], "llm")
        self.assertEqual(result["plan"]["selected_tool"], "generate_verification_questions")
        self.assertEqual(adapter.executed[0][0], "questions")
        self.assertIn("tools", llm.calls[0][1])
        self.assertIn("history", llm.calls[0][1])
        self.assertNotIn("available_tools", llm.calls[0][1])
        self.assertIn("未创建电核任务", result["answer"])
        self.assertEqual(len(llm.calls), 1)

    async def test_llm_cannot_select_write_action_or_unknown_tool(self):
        service, adapter = self.make_service()
        llm = FakeLLM({
            "goal": "直接审批",
            "selected_tool": "record_decision_recommendation",
            "reason": "尝试写入",
        })
        with self.assertRaisesRegex(AgentPlanningError, "未注册或非只读"):
            await service.run(
                "APP-AGENT-001", "直接通过", llm_client=llm
            )
        self.assertEqual(adapter.load_calls, [])
        self.assertEqual(adapter.executed, [])

    async def test_plan_only_never_executes_tool(self):
        service, adapter = self.make_service()
        result = await service.run(
            "APP-AGENT-001", "生成决策建议", execute=False
        )
        self.assertFalse(result["executed"])
        self.assertTrue(result["plan"]["executable"])
        self.assertEqual(adapter.executed, [])

    async def test_default_max_steps_preserves_legacy_single_tool_behavior(self):
        service, adapter = self.make_service()

        result = await service.run(
            "APP-AGENT-001",
            "评估风险，生成电核问题并给出决策建议",
        )

        self.assertEqual(result["plan"]["max_steps"], 1)
        self.assertEqual(result["plan"]["selected_tool"], "generate_decision_support")
        self.assertEqual(len(result["plan"]["steps"]), 1)
        self.assertEqual(result["plan"]["steps"][0]["tool_name"], "generate_decision_support")
        self.assertEqual(adapter.executed[0][0], "decision")
        # Existing clients can continue to consume the original top-level fields.
        self.assertTrue(result["executed"])
        self.assertEqual(result["result"]["recommended_decision"], "通过")
        self.assertIn("信审决策建议", result["answer"])

    async def test_max_steps_must_be_between_one_and_four(self):
        service, adapter = self.make_service()

        for invalid in (0, 5):
            with self.subTest(max_steps=invalid):
                with self.assertRaisesRegex(ValueError, "1.*4"):
                    await service.plan(
                        "APP-AGENT-001", "检查案件", max_steps=invalid
                    )
                with self.assertRaisesRegex(ValueError, "1.*4"):
                    await service.run(
                        "APP-AGENT-001", "检查案件",
                        max_steps=invalid,
                    )
        self.assertEqual(adapter.executed, [])

    async def test_deterministic_multi_intent_plan_is_ordered_and_deduplicated(self):
        service, _adapter = self.make_service()

        plan = await service.plan(
            "APP-AGENT-001",
            "先检查案件就绪和材料，再评估风险和反欺诈风险，"
            "生成电核问题，最后给出决策建议",
            max_steps=4,
        )

        self.assertEqual(plan["planner_mode"], "deterministic")
        self.assertEqual(
            [step["tool_name"] for step in plan["steps"]],
            [
                "inspect_case_readiness",
                "preview_risk_assessment",
                "generate_verification_questions",
                "generate_decision_support",
            ],
        )
        self.assertEqual(
            [step["step_id"] for step in plan["steps"]],
            ["step-1", "step-2", "step-3", "step-4"],
        )
        self.assertEqual([step["position"] for step in plan["steps"]], [1, 2, 3, 4])
        self.assertEqual(len({step["tool_name"] for step in plan["steps"]}), 4)
        self.assertEqual(plan["selected_tool"], "inspect_case_readiness")
        self.assertEqual(plan["overall_status"], "READY")
        self.assertEqual(
            plan["execution_semantics"], "same_snapshot_independent"
        )
        plan_reasons = "".join(step["reason"] for step in plan["steps"])
        for false_dependency_signal in ("再把", "最后", "上一步", "流入下一步"):
            self.assertNotIn(false_dependency_signal, plan_reasons)

    async def test_llm_multi_step_plan_is_ordered_deduplicated_and_bounded(self):
        service, _adapter = self.make_service()
        llm = FakeLLM({
            "goal": "形成风险预览并准备电核",
            "steps": [
                {"tool_name": "preview_risk_assessment", "reason": "先评估风险"},
                {"tool_name": "preview_risk_assessment", "reason": "重复步骤"},
                {"tool_name": "generate_verification_questions", "reason": "再生成问题"},
            ],
        })

        plan = await service.plan(
            "APP-AGENT-001",
            "请按你的判断安排所需能力",
            llm_client=llm,
            max_steps=3,
        )

        self.assertEqual(
            [step["tool_name"] for step in plan["steps"]],
            ["preview_risk_assessment", "generate_verification_questions"],
        )
        self.assertEqual(llm.calls[0][1]["max_steps"], 3)
        self.assertEqual(
            llm.calls[0][1]["execution_semantics"],
            "same_snapshot_independent",
        )
        self.assertIn("独立只读视角", llm.calls[0][0])
        self.assertEqual(
            plan["execution_semantics"], "same_snapshot_independent"
        )

    async def test_multi_step_run_returns_trace_summary_and_proposed_actions(self):
        service, adapter = self.make_service()

        result = await service.run(
            "APP-AGENT-001",
            "评估风险，生成电核问题，并给出决策建议",
            max_steps=3,
        )

        self.assertEqual(
            [item["tool_name"] for item in result["executions"]],
            [
                "preview_risk_assessment",
                "generate_verification_questions",
                "generate_decision_support",
            ],
        )
        self.assertTrue(all(item["executed"] for item in result["executions"]))
        self.assertTrue(all(item["status"] == "EXECUTED" for item in result["executions"]))
        self.assertEqual([item[0] for item in adapter.executed], ["risk", "questions", "decision"])
        self.assertEqual(len(adapter.load_calls), 1)
        self.assertTrue(all(
            item[2]["snapshot"] is adapter.snapshot
            for item in adapter.executed
        ))
        self.assertEqual(result["execution_summary"]["planned"], 3)
        self.assertEqual(result["execution_summary"]["executed"], 3)
        self.assertEqual(result["execution_summary"]["blocked"], 0)
        self.assertEqual(result["execution_summary"]["failed"], 0)
        self.assertEqual(result["execution_summary"]["status"], "COMPLETED")
        self.assertEqual(
            result["proposed_actions"],
            [
                {
                    "action_name": "generate_risk_assessment",
                    "source_step_id": "step-1",
                    "requires_confirmation": True,
                    "auto_executable": False,
                },
                {
                    "action_name": "create_phone_verification_task",
                    "source_step_id": "step-2",
                    "requires_confirmation": True,
                    "auto_executable": False,
                },
                {
                    "action_name": "record_decision_recommendation",
                    "source_step_id": "step-3",
                    "requires_confirmation": True,
                    "auto_executable": False,
                },
            ],
        )
        # The old result continues to mirror the first planned tool.
        self.assertEqual(result["result"]["risk_level"], "低")

    async def test_plan_only_multi_step_run_executes_nothing(self):
        service, adapter = self.make_service()

        result = await service.run(
            "APP-AGENT-001",
            "评估风险，生成电核问题，并给出决策建议",
            execute=False,
            max_steps=3,
        )

        self.assertFalse(result["executed"])
        self.assertIsNone(result["result"])
        self.assertEqual(adapter.executed, [])
        self.assertEqual(result["execution_summary"]["executed"], 0)
        self.assertEqual(result["execution_summary"]["failed"], 0)
        self.assertTrue(all(not item["executed"] for item in result["executions"]))
        self.assertTrue(all(item["status"] == "PLANNED" for item in result["executions"]))

    async def test_llm_unknown_or_write_step_rejects_entire_plan(self):
        invalid_tools = (
            "record_decision_recommendation",
            "unknown_credit_tool",
        )
        for invalid_tool in invalid_tools:
            with self.subTest(tool=invalid_tool):
                service, adapter = self.make_service()
                llm = FakeLLM({
                    "goal": "尝试混合执行",
                    "steps": [
                        {
                            "tool_name": "preview_risk_assessment",
                            "reason": "合法只读步骤",
                        },
                        {"tool_name": invalid_tool, "reason": "非法步骤"},
                    ],
                })

                with self.assertRaisesRegex(
                    AgentPlanningError, "未注册或非只读"
                ):
                    await service.run(
                        "APP-AGENT-001",
                        "请按你的判断混合处理",
                        llm_client=llm,
                        max_steps=2,
                    )

                self.assertEqual(adapter.load_calls, [])
                self.assertEqual(adapter.executed, [])

    async def test_llm_step_arguments_sql_and_cypher_are_rejected_before_reads(self):
        injected_fields = {
            "arguments": {"application_id": "APP-OTHER"},
            "params": {"force": True},
            "input": {"query": "override"},
            "application_id": "APP-OTHER",
            "sql": "SELECT * FROM loan_applications",
            "cypher": "MATCH (n) RETURN n",
        }
        for field_name, injected_value in injected_fields.items():
            with self.subTest(field=field_name):
                service, adapter = self.make_service()
                llm = FakeLLM({
                    "response_type": "ACTION_PLAN",
                    "goal": "检查风险",
                    "steps": [{
                        "tool_name": "preview_risk_assessment",
                        "reason": "尝试注入参数",
                        field_name: injected_value,
                    }],
                })
                with self.assertRaisesRegex(
                    AgentPlanningError, "不得提供工具参数"
                ):
                    await service.run(
                        "APP-AGENT-001",
                        "请按你的判断处理当前上下文",
                        llm_client=llm,
                    )
                self.assertEqual(adapter.load_calls, [])
                self.assertEqual(adapter.executed, [])

    async def test_all_ontology_action_names_are_rejected_as_read_tools(self):
        for action in build_personal_credit_baseline().action_types:
            with self.subTest(action=action.name):
                service, adapter = self.make_service()
                llm = FakeLLM({
                    "response_type": "ACTION_PLAN",
                    "goal": "尝试调用业务 Action",
                    "steps": [{
                        "tool_name": action.name,
                        "reason": "伪装成只读工具",
                    }],
                })
                with self.assertRaisesRegex(
                    AgentPlanningError, "未注册或非只读"
                ):
                    await service.run(
                        "APP-AGENT-001",
                        "请按你的判断处理当前上下文",
                        llm_client=llm,
                    )
                self.assertEqual(adapter.load_calls, [])
                self.assertEqual(adapter.executed, [])

    async def test_deterministic_conversation_continues_risk_to_questions_to_decision(self):
        store = AgentConversationStore(max_turns=8, ttl_seconds=60)
        service, adapter = self.make_service(conversation_store=store)

        first = await service.run("APP-AGENT-001", "查风险")
        second = await service.run(
            "APP-AGENT-001", "继续", conversation_id=first["conversation_id"]
        )
        third = await service.run(
            "APP-AGENT-001", "下一步", conversation_id=first["conversation_id"]
        )
        fourth = await service.run(
            "APP-AGENT-001", "继续", conversation_id=first["conversation_id"]
        )

        self.assertEqual(
            [first["turn_index"], second["turn_index"], third["turn_index"], fourth["turn_index"]],
            [1, 2, 3, 4],
        )
        self.assertEqual(second["plan"]["selected_tool"], "generate_verification_questions")
        self.assertEqual(third["plan"]["selected_tool"], "generate_decision_support")
        self.assertEqual(fourth["response_type"], "CHAT")
        self.assertEqual(fourth["reason_code"], "CHAIN_COMPLETE")
        self.assertEqual([item[0] for item in adapter.executed], ["risk", "questions", "decision"])
        self.assertEqual(len(fourth["context_window"]), 4)

    async def test_conversation_is_bound_to_application_and_ontology_scope(self):
        store = AgentConversationStore(ttl_seconds=60)
        service, _adapter = self.make_service(conversation_store=store)
        first = await service.run("APP-AGENT-001", "你好")

        with self.assertRaisesRegex(AgentConversationError, "案件或本体版本"):
            await service.run(
                "APP-AGENT-002",
                "你好",
                conversation_id=first["conversation_id"],
            )

        other_adapter = FakeAdapter()
        other_version_service = PersonalCreditAgentService(
            other_adapter,
            ontology_version="1.2.0",
            conversation_store=store,
        )
        with self.assertRaisesRegex(AgentConversationError, "案件或本体版本"):
            await other_version_service.run(
                "APP-AGENT-001",
                "你好",
                conversation_id=first["conversation_id"],
            )
        self.assertEqual(other_adapter.load_calls, [])
        self.assertEqual(other_adapter.executed, [])

    async def test_llm_chat_uses_history_but_reads_no_case_data_for_chat_turn(self):
        store = AgentConversationStore(ttl_seconds=60)
        service, adapter = self.make_service(conversation_store=store)
        llm = FakeLLM([
            {
                "response_type": "ACTION_RESULT",
                "answer": "风险等级为低，当前主要风险因素贡献有限。",
                "decision_boundary": "NOT_FINAL_APPROVAL",
            },
            {
                "response_type": "CHAT",
                "answer": "上一轮结果较低是因为已识别风险因素的贡献有限。",
                "steps": [],
            },
        ])

        first = await service.run(
            "APP-AGENT-001", "查风险", llm_client=llm
        )
        loads_after_action = len(adapter.load_calls)
        second = await service.run(
            "APP-AGENT-001",
            "为什么是这个结果？",
            llm_client=llm,
            conversation_id=first["conversation_id"],
        )

        self.assertEqual(first["synthesis"]["status"], "COMPLETED")
        self.assertEqual(second["response_type"], "CHAT")
        self.assertEqual(len(adapter.load_calls), loads_after_action)
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual([item[0] for item in adapter.executed], ["risk"])
        self.assertEqual(second["model_calls"], 1)
        self.assertEqual(llm.calls[1][2], {
            "temperature": 0.3,
            "max_tokens": 700,
        })
        history = llm.calls[1][1]["history"]
        self.assertEqual(len(history), 1)
        self.assertIn("风险等级为低", history[0]["assistant"])
        serialized = json.dumps(history, ensure_ascii=False).lower()
        self.assertNotIn("app-agent-001", serialized)
        self.assertNotIn("snapshot", serialized)
        self.assertNotIn("lineage", serialized)

    async def test_llm_synthesis_receives_only_allowlisted_result_projection(self):
        service, _adapter = self.make_service()
        llm = FakeLLM({
            "response_type": "ACTION_RESULT",
            "answer": "风险等级为低，评估结论为通过，结论仅供信审人员参考。",
            "decision_boundary": "NOT_FINAL_APPROVAL",
        })

        result = await service.run(
            "APP-AGENT-001", "申请编号 APP-AGENT-001，手机号 13800138000，查风险",
            llm_client=llm,
        )

        self.assertEqual(result["synthesis"]["status"], "COMPLETED")
        self.assertIn("不代表最终审批", result["answer"])
        self.assertEqual(len(llm.calls), 1)
        synthesis_payload = llm.calls[0][1]
        self.assertIn("[已脱敏]", synthesis_payload["instruction"])
        serialized = json.dumps(synthesis_payload, ensure_ascii=False).lower()
        self.assertNotIn("app-agent-001", serialized)
        self.assertNotIn("postgres_snapshot_id", serialized)
        self.assertNotIn("data_lineage", serialized)
        self.assertNotIn("application_id", serialized)

    async def test_invalid_llm_synthesis_falls_back_without_losing_action_result(self):
        service, adapter = self.make_service()
        llm = FakeLLM({
            "response_type": "ACTION_RESULT",
            "answer": "终审通过，可以放款。",
            "decision_boundary": "NOT_FINAL_APPROVAL",
        })

        result = await service.run("APP-AGENT-001", "查风险", llm_client=llm)

        self.assertEqual(result["synthesis"]["status"], "FALLBACK")
        self.assertEqual(result["synthesis"]["reason_code"], "MODEL_SYNTHESIS_FAILED")
        self.assertTrue(result["executed"])
        self.assertEqual(result["result"]["risk_level"], "低")
        self.assertEqual(adapter.executed[0][0], "risk")
        self.assertNotIn("终审通过", result["answer"])

    async def test_process_only_synthesis_is_rejected_with_auditable_fallback(self):
        service, adapter = self.make_service()
        llm = FakeLLM({
            "response_type": "ACTION_RESULT",
            "answer": "正在分析案件，接下来会整理证据并给出结论。",
            "decision_boundary": "NOT_FINAL_APPROVAL",
        })

        result = await service.run(
            "APP-AGENT-001", "查风险", llm_client=llm
        )

        self.assertEqual(len(llm.calls), 1)
        self.assertTrue(result["executed"])
        self.assertEqual(result["result"]["risk_level"], "低")
        self.assertIn("风险等级为低", result["answer"])
        self.assertEqual(result["final_answer"], result["answer"])
        self.assertTrue(result["degraded"])
        self.assertEqual(result["synthesis"]["status"], "FALLBACK")
        self.assertEqual(
            result["synthesis"]["reason_code"],
            "MODEL_SYNTHESIS_PROCESS_ONLY",
        )
        self.assertEqual(result["synthesis"]["quality_gate"], "REJECTED")
        self.assertEqual(
            [item["stage"] for item in result["trace"]],
            ["planner", "readiness", "action", "synthesis", "fallback"],
        )
        allowed_trace_fields = {
            "stage", "status", "duration_ms", "source", "step_id", "tool_name",
        }
        self.assertTrue(all(
            set(item).issubset(allowed_trace_fields)
            and isinstance(item["duration_ms"], int)
            for item in result["trace"]
        ))
        self.assertNotIn(
            "app-agent-001",
            json.dumps(result["trace"], ensure_ascii=False).lower(),
        )
        self.assertEqual(len(adapter.load_calls), 1)

    async def test_partial_success_sets_executed_and_preserves_first_success_result(self):
        service, adapter = self.make_service()
        original_execute = service.execute_tool

        def partially_failing_execute(application_id, tool_name, **kwargs):
            if tool_name == "preview_risk_assessment":
                raise AgentPlanningError("风险预览暂不可用")
            return original_execute(application_id, tool_name, **kwargs)

        service.execute_tool = partially_failing_execute
        result = await service.run(
            "APP-AGENT-001",
            "评估风险并生成电核问题",
            max_steps=2,
        )

        self.assertTrue(result["executed"])
        self.assertEqual(result["execution_summary"]["status"], "PARTIAL")
        self.assertEqual(result["execution_summary"]["executed"], 1)
        self.assertEqual(result["execution_summary"]["failed"], 1)
        self.assertEqual(result["result"]["question_count"], 1)
        self.assertIn("电核问题", result["answer"])
        self.assertIn("执行失败", result["answer"])
        self.assertEqual(result["final_answer"], result["answer"])
        self.assertTrue(result["degraded"])
        self.assertEqual([item[0] for item in adapter.executed], ["questions"])
        self.assertEqual(len(adapter.load_calls), 1)

    async def test_unexpected_later_action_error_keeps_partial_result_and_hides_detail(self):
        service, adapter = self.make_service()
        original_execute = service.execute_tool

        def unexpectedly_failing_execute(application_id, tool_name, **kwargs):
            if tool_name == "generate_verification_questions":
                raise RuntimeError("db password=secret internal")
            return original_execute(application_id, tool_name, **kwargs)

        service.execute_tool = unexpectedly_failing_execute
        result = await service.run(
            "APP-AGENT-001",
            "评估风险并生成电核问题",
            max_steps=2,
        )

        self.assertTrue(result["executed"])
        self.assertEqual(result["execution_summary"]["status"], "PARTIAL")
        self.assertEqual(result["execution_summary"]["executed"], 1)
        self.assertEqual(result["execution_summary"]["failed"], 1)
        self.assertEqual(result["result"]["risk_level"], "低")
        self.assertEqual(
            result["executions"][1]["error_code"],
            "ACTION_EXECUTION_FAILED",
        )
        self.assertEqual(result["final_answer"], result["answer"])
        self.assertEqual(result["trace"][-2]["status"], "FAILED")
        serialized = json.dumps(result, ensure_ascii=False).lower()
        self.assertNotIn("password", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("internal", serialized)
        self.assertEqual([item[0] for item in adapter.executed], ["risk"])

    async def test_active_llm_continue_uses_history_and_never_repeats_previous_action(self):
        store = AgentConversationStore(max_turns=8, ttl_seconds=60)
        service, adapter = self.make_service(conversation_store=store)
        llm = FakeLLM([
            {
                "response_type": "ACTION_RESULT",
                "answer": "风险等级为低，评估结论为通过。",
                "decision_boundary": "NOT_FINAL_APPROVAL",
            },
            {
                "response_type": "ACTION_RESULT",
                "answer": "电核问题如下：请确认当前月收入和月还款金额。",
                "decision_boundary": "NOT_FINAL_APPROVAL",
            },
        ])

        first = await service.run(
            "APP-AGENT-001", "查风险", llm_client=llm
        )
        second = await service.run(
            "APP-AGENT-001",
            "继续",
            llm_client=llm,
            conversation_id=first["conversation_id"],
        )

        self.assertEqual(second["plan"]["planner_mode"], "deterministic_follow_up")
        self.assertEqual(
            second["plan"]["selected_tool"], "generate_verification_questions"
        )
        self.assertEqual([item[0] for item in adapter.executed], ["risk", "questions"])
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(first["model_calls"], 1)
        self.assertEqual(second["model_calls"], 1)
        self.assertTrue(all(
            call[2] == {"temperature": 0.4, "max_tokens": 1200}
            for call in llm.calls
        ))

    async def test_model_planner_duplicate_action_is_not_executed_without_repeat_request(self):
        store = AgentConversationStore(max_turns=8, ttl_seconds=60)
        service, adapter = self.make_service(conversation_store=store)
        llm = FakeLLM([
            {
                "response_type": "ACTION_RESULT",
                "answer": "风险等级为低，评估结论为通过。",
                "decision_boundary": "NOT_FINAL_APPROVAL",
            },
            {
                "response_type": "ACTION_PLAN",
                "goal": "再次判断申请可靠性",
                "steps": [{
                    "tool_name": "preview_risk_assessment",
                    "reason": "模型误选了上一轮工具",
                }],
            },
        ])

        first = await service.run(
            "APP-AGENT-001", "查风险", llm_client=llm
        )
        second = await service.run(
            "APP-AGENT-001",
            "那这笔单子靠谱吗",
            llm_client=llm,
            conversation_id=first["conversation_id"],
        )

        self.assertEqual(second["response_type"], "CHAT")
        self.assertEqual(second["reason_code"], "DUPLICATE_ACTION_AVOIDED")
        self.assertFalse(second["executed"])
        self.assertEqual([item[0] for item in adapter.executed], ["risk"])
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(second["model_calls"], 1)

    async def test_invalid_json_or_provider_failure_degrades_to_final_answer(self):
        for model_response in ("not-json", TimeoutError("provider timeout")):
            with self.subTest(response=type(model_response).__name__):
                service, adapter = self.make_service()
                llm = FakeLLM(model_response)

                result = await service.run(
                    "APP-AGENT-001",
                    "请按你的判断处理这笔单子",
                    llm_client=llm,
                )

                self.assertEqual(result["response_type"], "NO_ACTION")
                self.assertEqual(
                    result["reason_code"], "MODEL_PLANNER_FALLBACK"
                )
                self.assertTrue(result["answer"].strip())
                self.assertEqual(result["final_answer"], result["answer"])
                self.assertTrue(result["degraded"])
                self.assertEqual(result["synthesis"]["status"], "FALLBACK")
                self.assertEqual(
                    result["synthesis"]["reason_code"],
                    "MODEL_PLANNER_FAILED",
                )
                self.assertTrue(result["synthesis"]["model_called"])
                self.assertEqual(result["model_calls"], 1)
                self.assertEqual(
                    [item["stage"] for item in result["trace"]],
                    ["planner", "fallback"],
                )
                self.assertEqual(adapter.load_calls, [])
                self.assertEqual(adapter.executed, [])

    async def test_history_explanation_failure_uses_previous_final_answer(self):
        store = AgentConversationStore(max_turns=8, ttl_seconds=60)
        service, adapter = self.make_service(conversation_store=store)
        first = await service.run("APP-AGENT-001", "查风险")
        loads_after_first = len(adapter.load_calls)
        llm = FakeLLM("not-json")

        second = await service.run(
            "APP-AGENT-001",
            "为什么是这个结果？",
            llm_client=llm,
            conversation_id=first["conversation_id"],
        )

        self.assertEqual(second["response_type"], "CHAT")
        self.assertEqual(second["reason_code"], "HISTORY_MODEL_FALLBACK")
        self.assertIn("上一轮可用结论是", second["answer"])
        self.assertIn("风险评估结论", second["answer"])
        self.assertEqual(second["final_answer"], second["answer"])
        self.assertTrue(second["degraded"])
        self.assertEqual(second["synthesis"]["status"], "FALLBACK")
        self.assertEqual(
            second["synthesis"]["reason_code"], "HISTORY_MODEL_FAILED"
        )
        self.assertEqual(
            [item["stage"] for item in second["trace"]],
            ["planner", "fallback"],
        )
        self.assertEqual(len(adapter.load_calls), loads_after_first)
        self.assertEqual(len(llm.calls), 1)

    async def test_process_phrases_without_actual_action_values_are_rejected(self):
        process_answers = (
            "正在分析风险等级，稍后给出结果。",
            "已完成分析，主要依据稍后提供。",
        )
        for process_answer in process_answers:
            with self.subTest(answer=process_answer):
                service, _adapter = self.make_service()
                llm = FakeLLM({
                    "response_type": "ACTION_RESULT",
                    "answer": process_answer,
                    "decision_boundary": "NOT_FINAL_APPROVAL",
                })

                result = await service.run(
                    "APP-AGENT-001", "查风险", llm_client=llm
                )

                self.assertEqual(result["synthesis"]["status"], "FALLBACK")
                self.assertEqual(
                    result["synthesis"]["reason_code"],
                    "MODEL_SYNTHESIS_PROCESS_ONLY",
                )
                self.assertIn("风险等级为低", result["final_answer"])
                self.assertNotEqual(result["final_answer"], process_answer)

    async def test_readiness_answer_lists_available_capabilities_and_missing_evidence(self):
        service, _adapter = self.make_service()

        result = await service.run("APP-AGENT-001", "检查材料是否齐全")

        self.assertIn("案件就绪度结论", result["answer"])
        self.assertIn("可直接运行", result["answer"])
        self.assertIn("生成风险评估预览", result["answer"])
        self.assertIn("当前缺少：无", result["answer"])

    async def test_negated_approval_phrases_are_allowed_in_llm_chat(self):
        for answer in (
            "不能认为审批已通过，当前只能作为辅助判断。",
            "当前未同意放款，仍需有权人员完成审批。",
        ):
            with self.subTest(answer=answer):
                service, adapter = self.make_service()
                llm = FakeLLM({
                    "response_type": "CHAT",
                    "answer": answer,
                    "steps": [],
                })

                result = await service.run(
                    "APP-AGENT-001", "这笔单子靠谱吗", llm_client=llm
                )

                self.assertEqual(result["response_type"], "CHAT")
                self.assertEqual(result["answer"], answer)
                self.assertFalse(result["degraded"])
                self.assertEqual(adapter.load_calls, [])

    async def test_llm_chat_cannot_claim_write_or_final_approval(self):
        unsafe_answers = (
            "审批已通过，已写入业务记录。",
            "同意放款。",
        )
        for unsafe_answer in unsafe_answers:
            with self.subTest(answer=unsafe_answer):
                service, adapter = self.make_service()
                llm = FakeLLM({
                    "response_type": "CHAT",
                    "answer": unsafe_answer,
                    "steps": [],
                })

                with self.assertRaisesRegex(AgentPlanningError, "审批边界"):
                    await service.run(
                        "APP-AGENT-001", "这笔申请怎么样", llm_client=llm
                    )
                self.assertEqual(adapter.load_calls, [])
                self.assertEqual(adapter.executed, [])


if __name__ == "__main__":
    unittest.main()
