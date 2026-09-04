import unittest

from pydantic import BaseModel, ConfigDict

from app.action import (
    ACTION_RUNTIME_DESIGNS,
    ActionExecutionService,
    ActionRuntimeDesign,
    ActionStepDesign,
    PERSONAL_CREDIT_ACTION_REUSE_PROFILES,
    ReusableActionAdapterRegistry,
    ReusableActionAlreadyRegistered,
    ReusableActionNotFound,
    ReusableActionRequestInvalid,
    build_action_reuse_audit,
    validate_action_reuse_profiles,
)
from app.ontology.models import ActionType, DynamicSecurity, OntologyProposal
from tests.test_action_execution import MemoryActionRepository


class GenericReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    reason: str


class ReusableActionCatalogTests(unittest.TestCase):
    def test_all_eight_actions_have_an_explicit_reuse_profile(self):
        self.assertEqual(validate_action_reuse_profiles(), [])
        self.assertEqual(
            set(PERSONAL_CREDIT_ACTION_REUSE_PROFILES),
            set(ACTION_RUNTIME_DESIGNS),
        )
        audit = build_action_reuse_audit()
        self.assertEqual(audit["status"], "passed")
        self.assertEqual(audit["action_count"], 8)
        self.assertEqual(audit["archetype_count"], 7)
        self.assertTrue(
            all(item["portable_steps"] for item in audit["actions"])
        )
        self.assertTrue(
            all(item["adapter_steps"] for item in audit["actions"])
        )

    def test_credit_and_fraud_actions_share_external_report_archetype(self):
        profiles = PERSONAL_CREDIT_ACTION_REUSE_PROFILES
        self.assertEqual(
            profiles["request_credit_report"].archetype,
            "request_external_report",
        )
        self.assertEqual(
            profiles["request_fraud_report"].archetype,
            "request_external_report",
        )

    def test_execution_service_accepts_non_credit_ontology_design(self):
        action_name = "route_case_review"
        proposal = OntologyProposal(
            version="supply-chain-1.0",
            action_types=[
                ActionType(
                    name=action_name,
                    idempotency_key="case_id+reason",
                )
            ],
            dynamic_security=[
                DynamicSecurity(
                    action=action_name,
                    allowed_roles=["业务员"],
                    audit_required=True,
                )
            ],
        )
        design = ActionRuntimeDesign(
            action_name=action_name,
            trigger="供应链异常进入人工复核",
            request_fields=("case_id", "reason", "request_id"),
            response_fields=("execution_id", "status"),
            idempotency_fields=("case_id", "reason"),
            steps=(
                ActionStepDesign(name="authorize", kind="authorize"),
                ActionStepDesign(
                    name="reserve",
                    kind="idempotency",
                    depends_on=("authorize",),
                ),
                ActionStepDesign(
                    name="audit",
                    kind="audit",
                    depends_on=("reserve",),
                ),
            ),
            completion_rule="人工复核任务已登记",
            consistency_boundary="单事务提交",
            compensation=("失败时回滚",),
            audit_events=(
                "action_received",
                "authorization_checked",
                "idempotency_checked",
                "action_succeeded",
                "action_failed",
            ),
            error_codes=("ACTION_WRITE_FAILED",),
        )
        service = ActionExecutionService(
            MemoryActionRepository(),
            proposal=proposal,
            designs={action_name: design},
        )
        reservation = service.start(
            action_name,
            {"case_id": "PO-1", "reason": "金额异常", "request_id": "REQ-1"},
            ontology_version=proposal.version,
            actor_id="operator-1",
            actor_role="业务员",
        )
        self.assertTrue(reservation.created)
        self.assertEqual(reservation.record.action_name, action_name)
        self.assertEqual(reservation.record.ontology_version, "supply-chain-1.0")


class ReusableActionAdapterRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_action_can_bind_different_ontology_adapters(self):
        registry = ReusableActionAdapterRegistry()
        profile = PERSONAL_CREDIT_ACTION_REUSE_PROFILES["trigger_manual_review"]

        async def credit_executor(request):
            return {"ontology": "credit", "case_id": request.case_id}

        async def supply_executor(request):
            return {"ontology": "supply", "case_id": request.case_id}

        registry.register(
            ontology_id="credit",
            action_name="trigger_manual_review",
            request_model=GenericReviewRequest,
            executor=credit_executor,
            profile=profile,
        )
        registry.register(
            ontology_id="supply-chain",
            action_name="trigger_manual_review",
            request_model=GenericReviewRequest,
            executor=supply_executor,
            profile=profile,
        )

        credit = await registry.execute(
            "credit",
            "trigger_manual_review",
            {"case_id": "C-1", "reason": "高风险"},
        )
        supply = await registry.execute(
            "supply-chain",
            "trigger_manual_review",
            {"case_id": "PO-1", "reason": "价格异常"},
        )
        self.assertEqual(credit["ontology"], "credit")
        self.assertEqual(supply["ontology"], "supply")

    async def test_registry_rejects_duplicates_unknowns_and_invalid_payload(self):
        registry = ReusableActionAdapterRegistry()
        profile = PERSONAL_CREDIT_ACTION_REUSE_PROFILES["trigger_manual_review"]

        async def executor(request):
            return {"case_id": request.case_id}

        values = {
            "ontology_id": "credit",
            "action_name": "trigger_manual_review",
            "request_model": GenericReviewRequest,
            "executor": executor,
            "profile": profile,
        }
        registry.register(**values)
        with self.assertRaises(ReusableActionAlreadyRegistered):
            registry.register(**values)
        with self.assertRaises(ReusableActionNotFound):
            registry.resolve("unknown", "trigger_manual_review")
        with self.assertRaises(ReusableActionRequestInvalid):
            await registry.execute(
                "credit", "trigger_manual_review", {"case_id": "C-1"}
            )

    async def test_registry_resolves_exact_version_before_wildcard(self):
        registry = ReusableActionAdapterRegistry()
        profile = PERSONAL_CREDIT_ACTION_REUSE_PROFILES["trigger_manual_review"]

        async def wildcard_executor(request):
            return {"adapter": "wildcard", "case_id": request.case_id}

        async def version_two_executor(request):
            return {"adapter": "v2", "case_id": request.case_id}

        registry.register(
            ontology_id="credit",
            ontology_version="*",
            action_name="trigger_manual_review",
            request_model=GenericReviewRequest,
            executor=wildcard_executor,
            profile=profile,
        )
        registry.register(
            ontology_id="credit",
            ontology_version="2.0",
            action_name="trigger_manual_review",
            request_model=GenericReviewRequest,
            executor=version_two_executor,
            profile=profile,
        )

        version_one = await registry.execute(
            "credit",
            "trigger_manual_review",
            {"case_id": "C-1", "reason": "规则命中"},
            ontology_version="1.0",
        )
        version_two = await registry.execute(
            "credit",
            "trigger_manual_review",
            {"case_id": "C-2", "reason": "规则命中"},
            ontology_version="2.0",
        )
        self.assertEqual(version_one["adapter"], "wildcard")
        self.assertEqual(version_two["adapter"], "v2")


if __name__ == "__main__":
    unittest.main()
