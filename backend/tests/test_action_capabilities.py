import hashlib
import json
import unittest
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from app.action.business_functions import build_personal_credit_readonly_registry
from app.action.capabilities import (
    JSON_SCHEMA_DRAFT,
    PERSONAL_CREDIT_READ_CAPABILITY_NAMES,
    ActionCapability,
    ActionCapabilityContractError,
    CapabilityAccess,
    CapabilityOrigin,
    CapabilityParameterPolicy,
    CapabilityParameterSource,
    CapabilityRisk,
    ConfirmationMode,
    OntologyDeclarationStatus,
    build_action_capability_catalog,
)
from app.action.reusable_actions import (
    ONTOLOGY_PERSONAL_CREDIT,
    PERSONAL_CREDIT_ACTION_REUSE_PROFILES,
    ReusableActionAdapterRegistry,
)
from app.agent.personal_credit import READ_ONLY_AGENT_TOOLS, PersonalCreditAgentService
from app.api import routes
from app.ontology.audit import proposal_digest
from app.ontology.baseline import build_personal_credit_baseline


ROOT = Path(__file__).resolve().parents[2]
GOLDEN = json.loads(
    (
        ROOT
        / "backend/tests/golden/personal_credit_action_capabilities_v1.json"
    ).read_text(encoding="utf-8")
)


class WrongActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surprise: str


class CreditRequestWithUnknownField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    subject_id_number: str
    authorization_reference: str
    query_purpose: str = "贷款审批"
    request_id: str
    unexpected_model_parameter: str


class CreditRequestWithRenamedField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    subject_id_number: str
    authorization_reference: str
    query_reason: str = "贷款审批"
    request_id: str


def _validated_copy(model, **updates):
    payload = model.model_dump(mode="python")
    payload.update(updates)
    return type(model).model_validate(payload)


def _canonical_sha256(value):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _catalog_golden_projection(catalog):
    capabilities = {}
    for capability in catalog.capabilities:
        confirmation = capability.confirmation_policy
        capabilities[capability.name] = {
            "access": capability.access.value,
            "origin": capability.origin.value,
            "declaration_status": capability.declaration_status.value,
            "risk": capability.risk.value,
            "input_schema_sha256": _canonical_sha256(capability.input_schema),
            "output_schema_sha256": _canonical_sha256(capability.output_schema),
            "parameter_sources": {
                item.field: item.source.value
                for item in capability.parameter_policies
            },
            "runtime_context_sources": {
                item.field: item.source.value
                for item in capability.runtime_context_policies
            },
            "evidence": {
                requirement.key: [
                    f"{reference.declaration_kind}:{reference.declaration_name}"
                    for reference in requirement.ontology_references
                ]
                for requirement in capability.required_evidence
            },
            "precondition_bindings": [
                {
                    "precondition_sha256": _canonical_sha256(
                        binding.precondition
                    ),
                    "evidence_keys": list(binding.evidence_keys),
                    "runtime_guard_ids": list(binding.runtime_guard_ids),
                }
                for binding in capability.precondition_bindings
            ],
            "idempotency_fields": list(capability.idempotency_fields),
            "suggested_write_action": capability.suggested_write_action,
            "confirmation": {
                "mode": confirmation.mode.value,
                "required": confirmation.required,
                "proposal_only": confirmation.proposal_only,
                "agent_execution_enabled": confirmation.agent_execution_enabled,
                "revalidate_on_execute": confirmation.revalidate_on_execute,
                "ontology_approval_required": (
                    confirmation.ontology_approval_required
                ),
                "approval_condition_sha256": (
                    _canonical_sha256(confirmation.approval_condition)
                    if confirmation.approval_condition
                    else None
                ),
            },
            "runtime_kind": capability.runtime_binding.kind.value,
        }
    return {
        "contract_version": catalog.contract_version,
        "catalog_sha256": _canonical_sha256(catalog),
        "ontology_id": catalog.ontology_id,
        "ontology_version": catalog.ontology_version,
        "ontology_digest": catalog.ontology_digest,
        "counts": {
            "ontology_actions": catalog.ontology_action_count,
            "legacy_reads": catalog.legacy_read_capability_count,
            "total": catalog.capability_count,
        },
        "order": [item.name for item in catalog.capabilities],
        "capabilities": capabilities,
    }


class ActionCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.proposal = build_personal_credit_baseline()

    def build_catalog(self, **overrides):
        arguments = {
            "ontology_id": ONTOLOGY_PERSONAL_CREDIT,
            "proposal": self.proposal,
            "adapter_registry": routes._ACTION_ADAPTER_REGISTRY,
            "expected_write_bindings": routes._PERSONAL_CREDIT_ACTION_ADAPTERS,
            "read_tools": READ_ONLY_AGENT_TOOLS,
            "read_request_model": routes.PersonalCreditAgentToolRequest,
            "read_executor": PersonalCreditAgentService.execute_tool,
            "read_function_registry": build_personal_credit_readonly_registry(),
        }
        arguments.update(overrides)
        return build_action_capability_catalog(**arguments)

    @staticmethod
    def registry_with_override(
        action_name, *, request_model=None, executor=None
    ):
        registry = ReusableActionAdapterRegistry()
        for name, (registered_model, registered_executor) in (
            routes._PERSONAL_CREDIT_ACTION_ADAPTERS.items()
        ):
            registry.register(
                ontology_id=ONTOLOGY_PERSONAL_CREDIT,
                ontology_version="*",
                action_name=name,
                request_model=(
                    request_model
                    if name == action_name and request_model is not None
                    else registered_model
                ),
                executor=(
                    executor
                    if name == action_name and executor is not None
                    else registered_executor
                ),
                profile=PERSONAL_CREDIT_ACTION_REUSE_PROFILES[name],
            )
        return registry

    def test_catalog_standardizes_eight_writes_and_four_reads(self):
        catalog = routes._personal_credit_action_capability_catalog(self.proposal)
        expected_write_names = tuple(
            action.name for action in self.proposal.action_types
        )
        expected_names = expected_write_names + PERSONAL_CREDIT_READ_CAPABILITY_NAMES

        self.assertEqual(catalog.ontology_action_count, 8)
        self.assertEqual(catalog.legacy_read_capability_count, 4)
        self.assertEqual(catalog.capability_count, 12)
        self.assertEqual(
            tuple(capability.name for capability in catalog.capabilities),
            expected_names,
        )
        self.assertEqual(
            len({capability.capability_id for capability in catalog.capabilities}),
            12,
        )

        by_name = catalog.by_name()
        action_types = {action.name: action for action in self.proposal.action_types}
        security = {policy.action: policy for policy in self.proposal.dynamic_security}
        for name in expected_write_names:
            capability = by_name[name]
            action = action_types[name]
            self.assertIs(capability.access, CapabilityAccess.WRITE)
            self.assertIs(capability.origin, CapabilityOrigin.ONTOLOGY_ACTION)
            self.assertIs(
                capability.declaration_status,
                OntologyDeclarationStatus.DECLARED,
            )
            self.assertEqual(capability.label, action.label)
            self.assertEqual(capability.description, action.description)
            self.assertEqual(capability.effects, tuple(action.effects))
            self.assertTrue(capability.side_effects)
            self.assertGreaterEqual(len(capability.idempotency_fields), 1)
            self.assertIs(
                capability.confirmation_policy.mode,
                ConfirmationMode.PROPOSAL_ONLY,
            )
            self.assertFalse(
                capability.confirmation_policy.agent_execution_enabled
            )
            self.assertTrue(capability.confirmation_policy.revalidate_on_execute)
            self.assertEqual(
                capability.confirmation_policy.ontology_approval_required,
                security[name].approval_required,
            )
            self.assertIsNotNone(capability.execution_safety)
            self.assertEqual(
                {item.precondition for item in capability.precondition_bindings},
                set(capability.preconditions),
            )
            self.assertTrue(all(
                binding.evidence_keys and binding.runtime_guard_ids
                for binding in capability.precondition_bindings
            ))
            self.assertFalse(capability.final_approval)

        expected_read_risks = {
            "inspect_case_readiness": CapabilityRisk.LOW,
            "preview_risk_assessment": CapabilityRisk.MEDIUM,
            "generate_verification_questions": CapabilityRisk.MEDIUM,
            "generate_decision_support": CapabilityRisk.HIGH,
        }
        for name in PERSONAL_CREDIT_READ_CAPABILITY_NAMES:
            capability = by_name[name]
            self.assertIs(capability.access, CapabilityAccess.READ)
            self.assertIs(capability.origin, CapabilityOrigin.LEGACY_READ_TOOL)
            self.assertIs(
                capability.declaration_status,
                OntologyDeclarationStatus.PROJECTED,
            )
            self.assertFalse(capability.side_effects)
            self.assertEqual(capability.effects, ())
            self.assertEqual(capability.idempotency_fields, ())
            self.assertIs(capability.risk, expected_read_risks[name])
            self.assertIs(
                capability.confirmation_policy.mode, ConfirmationMode.NONE
            )
            self.assertTrue(
                capability.confirmation_policy.agent_execution_enabled
            )
            self.assertIsNone(capability.execution_safety)
            self.assertEqual(capability.precondition_bindings, ())
            self.assertFalse(capability.final_approval)

        recommendation = by_name["record_decision_recommendation"]
        self.assertIs(recommendation.risk, CapabilityRisk.CRITICAL)
        self.assertTrue(recommendation.decision_support_only)
        self.assertTrue(
            recommendation.confirmation_policy.ontology_approval_required
        )

    def test_catalog_canonical_projection_matches_phase_two_golden(self):
        first = self.build_catalog()
        second = self.build_catalog()

        first_projection = _catalog_golden_projection(first)
        self.assertEqual(first_projection, _catalog_golden_projection(second))
        self.assertEqual(first_projection, GOLDEN)

    def test_model_level_read_and_write_invariants_are_fail_closed(self):
        catalog = self.build_catalog()
        read = catalog.by_name()["preview_risk_assessment"]
        write = catalog.by_name()["request_credit_report"]

        invalid_reads = (
            {"side_effects": True},
            {"effects": ("write hidden state",)},
            {"origin": CapabilityOrigin.ONTOLOGY_ACTION},
            {"declaration_status": OntologyDeclarationStatus.DECLARED},
            {"execution_safety": write.execution_safety},
            {"precondition_bindings": write.precondition_bindings},
        )
        for updates in invalid_reads:
            with self.subTest(read_updates=updates), self.assertRaises(
                ValidationError
            ):
                _validated_copy(read, **updates)

        invalid_writes = (
            {"side_effects": False},
            {"effects": ()},
            {"idempotency_fields": ()},
            {"risk": CapabilityRisk.LOW},
            {"origin": CapabilityOrigin.LEGACY_READ_TOOL},
            {"declaration_status": OntologyDeclarationStatus.PROJECTED},
            {"final_approval": True},
        )
        for updates in invalid_writes:
            with self.subTest(write_updates=updates), self.assertRaises(
                ValidationError
            ):
                _validated_copy(write, **updates)

        confirmation = write.confirmation_policy.model_dump(mode="python")
        confirmation["agent_execution_enabled"] = True
        with self.assertRaises(ValidationError):
            type(write.confirmation_policy).model_validate(confirmation)

        with self.assertRaises(ValidationError):
            CapabilityParameterPolicy(
                field="application_id",
                source=CapabilityParameterSource.SERVER_CONTEXT,
                model_writable=True,
            )
        with self.assertRaises(ValidationError):
            CapabilityParameterPolicy(
                field="goal",
                source=CapabilityParameterSource.MODEL_PROPOSAL,
                model_writable=False,
            )

    def test_builder_requires_exactly_four_legacy_read_capabilities(self):
        missing = dict(READ_ONLY_AGENT_TOOLS)
        missing.pop("generate_decision_support")
        extra = dict(READ_ONLY_AGENT_TOOLS)
        extra["unpublished_read"] = READ_ONLY_AGENT_TOOLS[
            "inspect_case_readiness"
        ]

        for read_tools in (missing, extra):
            with self.subTest(read_names=tuple(read_tools)), self.assertRaises(
                ActionCapabilityContractError
            ) as raised:
                self.build_catalog(read_tools=read_tools)
            self.assertIn("legacy READ 必须精确覆盖4项", str(raised.exception))

    def test_builder_rejects_wrong_request_model_and_executor_bindings(self):
        action_name = "request_credit_report"
        wrong_model_registry = self.registry_with_override(
            action_name, request_model=WrongActionRequest
        )
        with self.assertRaises(ActionCapabilityContractError) as raised:
            self.build_catalog(adapter_registry=wrong_model_registry)
        self.assertIn("请求模型绑定错误", str(raised.exception))
        self.assertIn("请求 Schema 与运行合同不一致", str(raised.exception))
        self.assertIn("显式参数来源白名单必须精确覆盖", str(raised.exception))

        def wrong_executor(_request):
            return {"unexpected": True}

        wrong_executor_registry = self.registry_with_override(
            action_name, executor=wrong_executor
        )
        with self.assertRaises(ActionCapabilityContractError) as raised:
            self.build_catalog(adapter_registry=wrong_executor_registry)
        self.assertIn("executor 绑定错误", str(raised.exception))

        with self.assertRaises(ActionCapabilityContractError) as raised:
            self.build_catalog(read_executor=object())
        self.assertIn("legacy READ 执行入口不可调用", str(raised.exception))

    def test_builder_rejects_unbound_runtime_function(self):
        registry = build_personal_credit_readonly_registry()
        self.assertTrue(registry.unbind("calculate_dti"))

        with self.assertRaises(ActionCapabilityContractError) as raised:
            self.build_catalog(read_function_registry=registry)

        message = str(raised.exception)
        self.assertIn("Function 尚未绑定", message)
        self.assertIn("calculate_dti", message)

    def test_builder_rejects_new_precondition_and_function_returns_drift(self):
        precondition_drift = self.proposal.model_copy(deep=True)
        credit_action = next(
            item
            for item in precondition_drift.action_types
            if item.name == "request_credit_report"
        )
        credit_action.preconditions.append("新增但尚未绑定的发布前条件")
        with self.assertRaises(ActionCapabilityContractError) as raised:
            self.build_catalog(proposal=precondition_drift)
        self.assertIn(
            "前置条件 runtime binding 未精确覆盖",
            str(raised.exception),
        )

        function_drift = self.proposal.model_copy(deep=True)
        dti = next(
            item
            for item in function_drift.functions
            if item.name == "calculate_dti"
        )
        dti.returns.append("unexpected_runtime_return")
        with self.assertRaises(ActionCapabilityContractError) as raised:
            self.build_catalog(proposal=function_drift)
        self.assertIn("Function 运行定义漂移", str(raised.exception))
        self.assertIn("calculate_dti", str(raised.exception))

    def test_unknown_or_renamed_request_fields_have_no_parameter_source_fallback(self):
        for request_model in (
            CreditRequestWithUnknownField,
            CreditRequestWithRenamedField,
        ):
            registry = self.registry_with_override(
                "request_credit_report", request_model=request_model
            )
            with self.subTest(model=request_model.__name__), self.assertRaises(
                ActionCapabilityContractError
            ) as raised:
                self.build_catalog(adapter_registry=registry)
            message = str(raised.exception)
            self.assertIn("请求 Schema 与运行合同不一致", message)
            self.assertIn("参数来源合同错误", message)
            self.assertIn("显式参数来源白名单必须精确覆盖", message)

    def test_capability_identity_is_bound_to_version_and_proposal_digest(self):
        first = self.build_catalog()
        self.assertEqual(first.ontology_digest, proposal_digest(self.proposal))
        self.assertEqual(len(first.ontology_digest), 64)

        version_two = self.proposal.model_copy(deep=True)
        version_two.version = "1.2.0"
        second = self.build_catalog(proposal=version_two)
        self.assertEqual(second.ontology_version, "1.2.0")
        self.assertNotEqual(first.ontology_digest, second.ontology_digest)
        self.assertTrue(all(
            capability.ontology_version == "1.2.0"
            and capability.ontology_digest == second.ontology_digest
            and ":1.2.0:" in capability.capability_id
            and second.ontology_digest in capability.capability_id
            for capability in second.capabilities
        ))
        self.assertTrue(all(
            reference.ontology_version == "1.2.0"
            for capability in second.capabilities
            for reference in capability.ontology_references
        ))

        changed_content = self.proposal.model_copy(deep=True)
        changed_content.action_types[0].description += "（同版本合同修订）"
        third = self.build_catalog(proposal=changed_content)
        self.assertEqual(third.ontology_version, first.ontology_version)
        self.assertNotEqual(third.ontology_digest, first.ontology_digest)
        self.assertNotEqual(
            {item.capability_id for item in third.capabilities},
            {item.capability_id for item in first.capabilities},
        )

    def test_parameter_sources_protect_server_sensitive_and_confirmation_data(self):
        catalog = self.build_catalog()
        forbidden_query_fields = {"sql", "cypher", "query", "query_text", "raw_query"}
        model_proposal_fields = set()

        for capability in catalog.capabilities:
            properties = set(capability.input_schema["properties"])
            policies = {item.field: item for item in capability.parameter_policies}
            self.assertEqual(set(policies), properties)
            self.assertFalse({item.lower() for item in properties} & forbidden_query_fields)
            for policy in (*capability.parameter_policies, *capability.runtime_context_policies):
                if policy.source is CapabilityParameterSource.MODEL_PROPOSAL:
                    model_proposal_fields.add((capability.name, policy.field))
                    self.assertTrue(policy.model_writable)
                else:
                    self.assertFalse(policy.model_writable)

        credit = {
            item.field: item
            for item in catalog.by_name()["request_credit_report"].parameter_policies
        }
        self.assertIs(
            credit["application_id"].source,
            CapabilityParameterSource.SERVER_CONTEXT,
        )
        self.assertIs(
            credit["request_id"].source,
            CapabilityParameterSource.SERVER_CONTEXT,
        )
        for field in ("subject_id_number", "authorization_reference"):
            self.assertIs(
                credit[field].source, CapabilityParameterSource.TRUSTED_USER
            )
            self.assertTrue(credit[field].sensitive)
            self.assertFalse(credit[field].model_writable)
        self.assertIs(
            credit["query_purpose"].source,
            CapabilityParameterSource.SERVER_CONTEXT,
        )

        recommendation = catalog.by_name()["record_decision_recommendation"]
        recommendation_inputs = {
            item.field: item for item in recommendation.parameter_policies
        }
        for field in ("approver_id", "approval_assertion"):
            self.assertIs(
                recommendation_inputs[field].source,
                CapabilityParameterSource.TRUSTED_CONFIRMATION,
            )
            self.assertFalse(recommendation_inputs[field].model_writable)
        confirmation_context = {
            item.field: item for item in recommendation.runtime_context_policies
        }
        for field in ("confirmation_actor_id", "confirmation_actor_role"):
            self.assertIs(
                confirmation_context[field].source,
                CapabilityParameterSource.TRUSTED_CONFIRMATION,
            )
            self.assertTrue(confirmation_context[field].sensitive)
            self.assertFalse(confirmation_context[field].model_writable)
        protected_server_fields = {
            "assessment_version": "generate_risk_assessment",
            "data_snapshot_policy": "generate_risk_assessment",
            "verification_round": "create_phone_verification_task",
            "verification_id": "record_phone_verification",
            "source_execution_id": "trigger_manual_review",
            "recommendation_version": "record_decision_recommendation",
        }
        by_name = catalog.by_name()
        for field, capability_name in protected_server_fields.items():
            policies = {
                item.field: item
                for item in by_name[capability_name].parameter_policies
            }
            self.assertIs(
                policies[field].source,
                CapabilityParameterSource.SERVER_CONTEXT,
            )
            self.assertFalse(policies[field].model_writable)
        self.assertEqual(
            model_proposal_fields,
            {
                ("create_phone_verification_task", "trigger_reason"),
                ("trigger_manual_review", "review_reason"),
            },
        )

    def test_write_output_schemas_use_typed_runtime_envelopes(self):
        catalog = self.build_catalog()
        envelope_fields = {
            "execution_id",
            "action_name",
            "status",
            "ontology_version",
            "idempotent_replay",
            "result",
            "error_code",
            "error_message",
            "audit_reference",
            "audit_chain_valid",
            "outbox",
        }
        action_result_markers = {
            "create_loan_application": "created_relations",
            "request_credit_report": "report_id",
            "request_fraud_report": "fraud_report_id",
            "generate_risk_assessment": "assessment_id",
            "create_phone_verification_task": "verification_id",
            "record_phone_verification": "record_status",
            "trigger_manual_review": "review_evidence_reference",
            "record_decision_recommendation": "decision_authority",
        }

        writes = [
            item for item in catalog.capabilities
            if item.access is CapabilityAccess.WRITE
        ]
        self.assertEqual(len(writes), 8)
        for capability in writes:
            schema = capability.output_schema
            self.assertIs(capability.input_schema["additionalProperties"], False)
            self.assertEqual(schema["$schema"], JSON_SCHEMA_DRAFT)
            self.assertEqual(schema["type"], "object")
            self.assertIs(schema["additionalProperties"], False)
            self.assertEqual(set(schema["properties"]), envelope_fields)
            self.assertTrue({
                "execution_id",
                "action_name",
                "status",
                "ontology_version",
                "idempotent_replay",
                "result",
                "error_code",
                "error_message",
                "audit_reference",
            }.issubset(schema["required"]))
            self.assertEqual(
                schema["properties"]["action_name"]["const"], capability.name
            )
            result_schema = schema["properties"]["result"]
            self.assertEqual(result_schema["type"], "object")
            self.assertIs(result_schema["additionalProperties"], False)
            self.assertIn(
                action_result_markers[capability.name],
                result_schema["properties"],
            )
            self.assertTrue(all(
                field_schema
                for field_schema in schema["properties"].values()
            ))
            self.assertTrue(all(
                field_schema
                for field_schema in result_schema["properties"].values()
            ))
            self.assertTrue(capability.runtime_design_response_fields)

        reads = [
            item for item in catalog.capabilities
            if item.access is CapabilityAccess.READ
        ]
        self.assertEqual(len(reads), 4)
        for capability in reads:
            schema = capability.output_schema
            self.assertIs(capability.input_schema["additionalProperties"], False)
            self.assertEqual(schema["$schema"], JSON_SCHEMA_DRAFT)
            self.assertEqual(schema["type"], "object")
            self.assertIs(schema["additionalProperties"], False)
            self.assertTrue(schema["properties"])
            for field_schema in schema["properties"].values():
                self.assertTrue(field_schema)
                self.assertTrue(
                    {"type", "anyOf", "oneOf", "$ref", "const"}
                    & set(field_schema)
                )

    def test_safe_summary_is_allowlisted_and_does_not_leak_internal_contracts(self):
        catalog = self.build_catalog()
        summary = catalog.safe_summary()
        self.assertEqual(
            set(summary),
            {
                "contract_version",
                "ontology_id",
                "ontology_version",
                "ontology_digest",
                "ontology_action_count",
                "legacy_read_capability_count",
                "capability_count",
                "read_count",
                "write_count",
                "agent_write_execution_enabled",
            },
        )
        self.assertEqual(summary["read_count"], 4)
        self.assertEqual(summary["write_count"], 8)
        self.assertEqual(summary["capability_count"], 12)
        self.assertFalse(summary["agent_write_execution_enabled"])

        serialized = json.dumps(summary, ensure_ascii=False).lower()
        for forbidden in (
            "input_schema",
            "output_schema",
            "runtime_binding",
            "request_model",
            "executor",
            "parameter_policies",
            "security_policy",
            "source_table",
            "source_column",
            "subject_id_number",
            "authorization_reference",
            "approver_id",
            "api_key",
            "sql",
            "cypher",
        ):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
