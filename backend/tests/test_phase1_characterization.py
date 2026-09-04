import hashlib
import json
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from app.action.action_design_catalog import (
    ACTION_RUNTIME_DESIGNS,
    ACTION_STATE_TRANSITIONS,
    TERMINAL_ACTION_STATES,
)
from app.action.reusable_actions import (
    PERSONAL_CREDIT_ACTION_REUSE_PROFILES,
)
from app.action.workflow import (
    OPTIONAL_WORKFLOW_ACTIONS,
    WORKFLOW_ACTIONS,
    WORKFLOW_DEPENDENCIES,
    WORKFLOW_STATUSES,
    WORKFLOW_STEP_STATUSES,
    WORKFLOW_VERSION,
)
from app.agent.conversation import AgentConversationStore
from app.agent.personal_credit import (
    READ_ONLY_AGENT_TOOLS,
    PersonalCreditAgentService,
)
from app.api import routes
from app.ontology.baseline import build_personal_credit_baseline


ROOT = Path(__file__).resolve().parents[2]
GOLDEN = json.loads(
    (ROOT / "backend/tests/golden/personal_credit_phase1_contract.json")
    .read_text(encoding="utf-8")
)


def normalized_schema(value):
    if isinstance(value, dict):
        return {
            key: normalized_schema(item)
            for key, item in sorted(value.items())
            if key not in {"title"}
        }
    if isinstance(value, list):
        return [normalized_schema(item) for item in value]
    return value


def contract_digest(value) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ReadinessSnapshotAdapter:
    def __init__(self):
        self.load_calls = []
        self.executed = []
        self.snapshot = SimpleNamespace(
            application={"application_id": "APP-GOLDEN-001"},
            borrower={"monthly_income": 20000},
            vehicle={"invoice_price": 200000},
            credit_report={
                "report_id": "CR-GOLDEN",
                "outstanding_loan_count": 0,
                "overdue_3y_count": 0,
                "total_overdue_count": 0,
                "inquiry_count": 1,
            },
            external_debts=(),
            anti_fraud_report={
                "fraud_report_id": "FR-GOLDEN",
                "fraud_score": 0,
                "result": "通过",
                "hit_details": "无",
            },
            risk_assessment={
                "assessment_id": "RA-GOLDEN",
                "risk_level": "低",
                "risk_factors": [],
                "check_result": "通过",
            },
            phone_verification={
                "verification_id": "PV-GOLDEN",
                "conclusion": "通过",
            },
            snapshot_id="golden-snapshot",
            snapshot_started_at="2026-09-03T00:00:00+00:00",
            data_binding_version="1.1.0",
            data_binding_digest="a" * 64,
        )

    def load_postgres_snapshot(self, application_id):
        self.load_calls.append(application_id)
        return self.snapshot

    def execute_risk_assessment(self, application_id, **_kwargs):
        self.executed.append(("risk", application_id))
        return {
            "postgres_snapshot_id": self.snapshot.snapshot_id,
            "postgres_snapshot_time": self.snapshot.snapshot_started_at,
            "data_binding_version": self.snapshot.data_binding_version,
            "data_binding_digest": self.snapshot.data_binding_digest,
            "functions": {
                "assess_application_risk": {
                    "result": {
                        "risk_level": "低",
                        "pd_value": 0.02,
                        "check_result": "通过",
                        "risk_factors": [{
                            "factor": "DTI_LOW",
                            "contribution": 0.01,
                            "evidence_source": "calculate_dti",
                        }],
                        "model": {"name": "rule-proxy", "version": "1"},
                        "calculation_policy": "preview",
                        "decision_boundary": "not_final",
                    }
                }
            },
        }


class PhaseOneStaticContractTests(unittest.TestCase):
    def test_normalized_schema_and_security_digests_match_golden(self):
        proposal = build_personal_credit_baseline()
        observed = {
            "agent_run_request_schema": contract_digest(normalized_schema(
                routes.PersonalCreditAgentRunRequest.model_json_schema()
            )),
            "action_request_schemas": contract_digest({
                name: normalized_schema(model.model_json_schema())
                for name, (model, _executor)
                in routes._PERSONAL_CREDIT_ACTION_ADAPTERS.items()
            }),
            "action_tbox_and_security": contract_digest({
                "action_types": [
                    item.model_dump(mode="json")
                    for item in proposal.action_types
                ],
                "dynamic_security": [
                    item.model_dump(mode="json")
                    for item in proposal.dynamic_security
                ],
            }),
            "action_runtime_design": contract_digest({
                "designs": {
                    name: asdict(design)
                    for name, design in ACTION_RUNTIME_DESIGNS.items()
                },
                "state_transitions": ACTION_STATE_TRANSITIONS,
                "terminal_states": sorted(TERMINAL_ACTION_STATES),
            }),
            "action_registry_bindings": contract_digest({
                "catalog": routes._ACTION_ADAPTER_REGISTRY.describe(
                    "personal-credit", ontology_version="1.1.0"
                ),
                "executors": {
                    name: {
                        "request_model": model.__name__,
                        "executor": executor.__name__,
                    }
                    for name, (model, executor)
                    in routes._PERSONAL_CREDIT_ACTION_ADAPTERS.items()
                },
            }),
        }
        self.assertEqual(observed, GOLDEN["contract_digests"])

    def test_agent_request_and_static_read_tool_contract_match_golden(self):
        expected = GOLDEN["agent"]
        fields = routes.PersonalCreditAgentRunRequest.model_fields
        self.assertEqual(list(fields), expected["run_request_fields"])
        self.assertEqual(
            [name for name, field in fields.items() if field.is_required()],
            expected["run_request_required"],
        )
        self.assertEqual(
            {
                name: field.default
                for name, field in fields.items()
                if not field.is_required()
            },
            expected["run_request_defaults"],
        )
        self.assertEqual(
            routes.PersonalCreditAgentRunRequest.model_config.get("extra"),
            "forbid",
        )
        self.assertEqual(
            list(READ_ONLY_AGENT_TOOLS), expected["read_only_tools"]
        )

    def test_all_eight_action_runtime_contracts_match_golden(self):
        expected = GOLDEN["actions"]
        proposal = build_personal_credit_baseline()
        names = expected["names"]
        self.assertEqual([item.name for item in proposal.action_types], names)
        self.assertEqual(list(ACTION_RUNTIME_DESIGNS), names)
        self.assertEqual(list(PERSONAL_CREDIT_ACTION_REUSE_PROFILES), names)
        self.assertEqual(list(routes._PERSONAL_CREDIT_ACTION_ADAPTERS), names)
        self.assertEqual(
            {
                name: list(model.model_fields)
                for name, (model, _executor)
                in routes._PERSONAL_CREDIT_ACTION_ADAPTERS.items()
            },
            expected["request_fields"],
        )
        self.assertEqual(
            {
                name: list(design.idempotency_fields)
                for name, design in ACTION_RUNTIME_DESIGNS.items()
            },
            expected["idempotency_fields"],
        )
        self.assertEqual(
            {
                name: list(design.response_fields)
                for name, design in ACTION_RUNTIME_DESIGNS.items()
            },
            expected["response_fields"],
        )
        policies = {
            policy.action: policy for policy in proposal.dynamic_security
        }
        self.assertEqual(
            {
                name: policies[name].approval_required
                for name in names
            },
            expected["approval_required"],
        )
        self.assertEqual(
            len({
                profile.archetype
                for profile in PERSONAL_CREDIT_ACTION_REUSE_PROFILES.values()
            }),
            expected["archetype_count"],
        )
        self.assertNotIn("approve_application", names)
        action_types = {item.name: item for item in proposal.action_types}
        required_kinds = set(expected["required_step_kinds"])
        for name, design in ACTION_RUNTIME_DESIGNS.items():
            with self.subTest(action=name):
                kinds = {step.kind for step in design.steps}
                self.assertTrue(required_kinds <= kinds)
                self.assertTrue(design.consistency_boundary)
                self.assertTrue(design.compensation)
                self.assertIn("action_failed", design.audit_events)
                self.assertTrue(action_types[name].audit_required)
                self.assertEqual(
                    action_types[name].execution_mode, "declaration_only"
                )
                self.assertTrue(policies[name].audit_required)

    def test_fixed_workflow_contract_matches_golden_without_becoming_agent_policy(self):
        expected = GOLDEN["workflow"]
        self.assertEqual(WORKFLOW_VERSION, expected["version"])
        self.assertEqual(list(WORKFLOW_STATUSES), expected["statuses"])
        self.assertEqual(
            list(WORKFLOW_STEP_STATUSES), expected["step_statuses"]
        )
        self.assertEqual(list(WORKFLOW_ACTIONS), expected["actions"])
        self.assertEqual(
            {name: list(values) for name, values in WORKFLOW_DEPENDENCIES.items()},
            expected["dependencies"],
        )
        self.assertEqual(
            sorted(OPTIONAL_WORKFLOW_ACTIONS), expected["optional_actions"]
        )


class PhaseOneAgentResponseContractTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_service(adapter):
        return PersonalCreditAgentService(
            adapter,
            ontology_version="1.1.0",
            conversation_store=AgentConversationStore(
                max_conversations=4,
                max_turns=8,
                scope_secret=b"phase-one-golden-scope-secret-001",
            ),
        )

    async def test_normalized_legacy_action_run_matches_golden(self):
        adapter = ReadinessSnapshotAdapter()
        service = self.make_service(adapter)

        result = await service.run(
            "APP-GOLDEN-001",
            "检查材料是否齐全",
            execute=True,
            max_steps=1,
        )
        expected = GOLDEN["agent"]["action_run_response"]

        self.assertEqual(sorted(result), expected["top_level_fields"])
        self.assertEqual(sorted(result["plan"]), expected["plan_fields"])
        self.assertEqual(
            sorted(result["executions"][0]), expected["execution_fields"]
        )
        self.assertEqual(
            sorted(result["execution_summary"]),
            expected["execution_summary_fields"],
        )
        self.assertEqual(
            sorted(result["synthesis"]), expected["synthesis_fields"]
        )
        self.assertEqual(
            [item["stage"] for item in result["trace"]],
            expected["trace_stages"],
        )
        self.assertEqual(result["response_type"], "ACTION_RUN")
        self.assertEqual(result["answer"], result["final_answer"])
        self.assertEqual(result["turn_index"], 1)
        self.assertEqual(len(result["context_window"]), 1)
        self.assertEqual(adapter.load_calls, ["APP-GOLDEN-001"])
        self.assertEqual(result["proposed_actions"], [])

        chat = await service.run("APP-GOLDEN-001", "你好")
        no_action = await service.run("APP-GOLDEN-001", "今天天气怎么样")
        self.assertEqual(
            [chat["response_type"], no_action["response_type"], result["response_type"]],
            GOLDEN["agent"]["response_types"],
        )
        self.assertEqual(adapter.load_calls, ["APP-GOLDEN-001"])

    async def test_legacy_risk_semantics_match_normalized_golden(self):
        adapter = ReadinessSnapshotAdapter()
        result = await self.make_service(adapter).run(
            "APP-GOLDEN-001", "查风险", execute=True, max_steps=1
        )

        projection = {
            "response_type": result["response_type"],
            "executed": result["executed"],
            "plan": {
                key: result["plan"][key]
                for key in (
                    "planner_mode", "selected_tool", "status",
                    "overall_status", "read_only", "requires_confirmation",
                    "final_approval", "suggested_write_action",
                )
            },
            "execution_summary": result["execution_summary"],
            "executions": [
                {
                    key: execution[key]
                    for key in ("tool_name", "status", "executed")
                }
                for execution in result["executions"]
            ],
            "proposed_actions": result["proposed_actions"],
            "synthesis": result["synthesis"],
            "trace": [
                {
                    key: item[key]
                    for key in ("stage", "status", "source", "tool_name")
                    if key in item
                }
                for item in result["trace"]
            ],
        }
        projection["plan"]["step_count"] = len(result["plan"]["steps"])

        self.assertEqual(
            projection, GOLDEN["agent"]["legacy_risk_projection"]
        )
        self.assertEqual(result["result"], result["executions"][0]["result"])
        self.assertEqual(result["answer"], result["final_answer"])
        self.assertTrue(result["answer"].strip())
        self.assertEqual(adapter.executed, [("risk", "APP-GOLDEN-001")])


if __name__ == "__main__":
    unittest.main()
