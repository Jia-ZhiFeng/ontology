import hashlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ValidationError

from app.action.capabilities import (
    ActionCapabilityCatalog,
    CapabilityAccess,
    OntologyCapabilityReference,
)
from app.agent.capability_registry import (
    AgentAuthenticationSource,
    AgentCapabilityAllowlist,
    AgentCapabilityDecision,
    AgentCapabilityRegistry,
    AgentCapabilityRegistryError,
    AgentDynamicPolicy,
    AgentPrincipalContext,
    PublishedReadProjectionAttestation,
    ReadProjectionContractAttestation,
)
from app.agent.ontology_context import OntologyContextProvider
from app.api import routes
from app.ontology.audit import proposal_digest
from app.ontology.baseline import build_personal_credit_baseline
from app.ontology.data_binding import OntologyDataBinding


ROOT = Path(__file__).resolve().parents[2]
READ_ATTESTATION_KEY = b"published-read-projection-test-key-32-bytes"
UNTRUSTED_ATTESTATION_KEY = b"untrusted-read-projection-key-32-bytes!!"
GOLDEN_PATH = (
    ROOT / "backend/tests/golden/personal_credit_agent_manifest_v1.json"
)


def _canonical_sha256(value):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _manifest_golden_projection(manifest):
    capabilities = {}
    for item in manifest.capabilities:
        contract = item.model_dump(mode="json")
        contract.pop("capability_id", None)
        capabilities[item.name] = {
            "operation": item.operation.value,
            "risk": item.risk,
            "proposal_only": item.proposal_only,
            "model_input_fields": list(item.model_input_schema["properties"]),
            "model_contract_sha256": _canonical_sha256(contract),
        }
    return {
        "manifest_version": manifest.manifest_version,
        "manifest_id": manifest.manifest_id,
        "context": manifest.context,
        "catalog_digest": manifest.catalog_digest,
        "read_projection_attestation_id_sha256": _canonical_sha256(
            manifest.read_projection_attestation_id
        ),
        "allowlist_revision": manifest.allowlist_revision,
        "policy_revision": manifest.policy_revision,
        "principal_scope_digest": manifest.principal_scope_digest,
        "capability_count": len(manifest.capabilities),
        "order": [item.name for item in manifest.capabilities],
        "manifest_sha256": _canonical_sha256(manifest),
        "capabilities": capabilities,
        "direct_write_execution_enabled": (
            manifest.direct_write_execution_enabled
        ),
        "final_approval": manifest.final_approval,
    }


class AgentCapabilityRegistryTests(unittest.TestCase):
    def setUp(self):
        self.proposal = build_personal_credit_baseline()
        self.ontology_digest = proposal_digest(self.proposal)
        self.record = {
            "ontology_id": "personal-credit",
            "version": self.proposal.version,
            "status": "published",
            "is_active": True,
            "published_at": datetime(2026, 9, 4, tzinfo=timezone.utc),
            "proposal": self.proposal,
            "proposal_digest": self.ontology_digest,
            "audit_digest": self.ontology_digest,
        }
        self.provider = OntologyContextProvider(
            lambda _version: self.record,
            lambda: self.record,
            secret=b"agent-capability-registry-tests-32-bytes!!",
        )
        self.snapshot = {
            "application": {"application_id": "APP-REGISTRY-001"},
            "snapshot_id": "SNAPSHOT-REGISTRY-001",
            "data_binding_version": self.proposal.version,
            "data_binding_digest": OntologyDataBinding(
                self.proposal
            ).binding_digest,
        }
        self.context = self.provider.resolve_context(
            "APP-REGISTRY-001", self.snapshot
        )
        self.catalog = routes._personal_credit_action_capability_catalog(
            self.proposal
        )
        self.read_names = tuple(
            item.name
            for item in self.catalog.capabilities
            if item.access is CapabilityAccess.READ
        )
        self.write_names = tuple(
            item.name
            for item in self.catalog.capabilities
            if item.access is CapabilityAccess.WRITE
        )
        self.all_names = tuple(item.name for item in self.catalog.capabilities)
        self.attestation = PublishedReadProjectionAttestation.sign_for_publication(
            self.context,
            self.catalog,
            signing_key=READ_ATTESTATION_KEY,
        )
        self.allowlist = AgentCapabilityAllowlist(
            revision="personal-credit-agent-allowlist-v1",
            read_names=self.read_names,
            write_proposal_names=self.write_names,
        )
        self.policy = AgentDynamicPolicy(
            revision="personal-credit-agent-policy-v1",
            allowed_names=self.all_names,
            denied_names=(),
            write_proposal_authentication_sources=(
                AgentAuthenticationSource.LOCAL_SESSION,
                AgentAuthenticationSource.API_CLIENT,
                AgentAuthenticationSource.IAM,
            ),
        )
        self.principal = AgentPrincipalContext(
            principal_id="local-test-user",
            authentication_source=AgentAuthenticationSource.LOCAL_SESSION,
            authenticated=True,
            application_id="APP-REGISTRY-001",
            snapshot_id="SNAPSHOT-REGISTRY-001",
        )

    def registry(self, **overrides):
        arguments = {
            "catalog_builder": lambda: self.catalog,
            "context_provider": self.provider,
            "read_projection_attestation": self.attestation,
            "read_projection_verification_key": READ_ATTESTATION_KEY,
            "allowlist": self.allowlist,
            "dynamic_policy": self.policy,
        }
        arguments.update(overrides)
        return AgentCapabilityRegistry(**arguments)

    @staticmethod
    def replace_capability(catalog, name, **updates):
        capabilities = tuple(
            (
                item.model_copy(update=updates)
                if item.name == name
                else item.model_copy(deep=True)
            )
            for item in catalog.capabilities
        )
        candidate = catalog.model_copy(update={"capabilities": capabilities})
        return ActionCapabilityCatalog.model_validate(
            candidate.model_dump(mode="python")
        )

    def test_builds_strict_model_manifest_for_eight_writes_and_four_reads(self):
        calls = []

        def builder():
            calls.append("fresh")
            return self.catalog

        registry = self.registry(catalog_builder=builder)
        first = registry.build_manifest(
            context=self.context, principal=self.principal
        )
        second = registry.build_manifest(
            context=self.context, principal=self.principal
        )

        self.assertEqual(calls, ["fresh", "fresh"])
        self.assertEqual(first, second)
        self.assertEqual(len(first.capabilities), 12)
        self.assertFalse(first.direct_write_execution_enabled)
        self.assertFalse(first.final_approval)
        self.assertNotIn("application_id", first.context)
        self.assertNotIn("snapshot_id", first.context)

        schemas = {
            item.name: item.model_input_schema
            for item in first.capabilities
        }
        nonempty = {
            name: tuple(schema["properties"])
            for name, schema in schemas.items()
            if schema["properties"]
        }
        self.assertEqual(nonempty, {
            "create_phone_verification_task": ("trigger_reason",),
            "trigger_manual_review": ("review_reason",),
        })
        for name, schema in schemas.items():
            self.assertFalse(schema["additionalProperties"])
            self.assertNotIn("$defs", schema)
            self.assertNotIn("$ref", schema)
            self.assertNotIn("title", schema)
            self.assertNotIn("default", schema)
            self.assertNotIn("pattern", schema)
            if name in self.read_names:
                self.assertEqual(schema["properties"], {})

        payload = json.dumps(first.model_payload(), ensure_ascii=False)
        self.assertNotIn("principal_scope_digest", first.model_payload())
        for forbidden in (
            "runtime_binding",
            "request_model",
            "executor",
            "security_policy",
            "allowed_roles",
            "denied_roles",
            "source_table",
            "source_column",
            "APP-REGISTRY-001",
            "SNAPSHOT-REGISTRY-001",
            "local-test-user",
        ):
            self.assertNotIn(forbidden, payload)
        self.assertNotIn("sql", payload.lower())
        self.assertNotIn("cypher", payload.lower())

    def test_manifest_matches_phase_three_golden(self):
        manifest = self.registry().build_manifest(
            context=self.context, principal=self.principal
        )
        expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(_manifest_golden_projection(manifest), expected)

    def test_intersection_applies_allowlist_policy_and_deny_precedence(self):
        allowlist = AgentCapabilityAllowlist(
            revision="limited-allowlist-v2",
            read_names=("inspect_case_readiness", "preview_risk_assessment"),
            write_proposal_names=(
                "generate_risk_assessment",
                "trigger_manual_review",
            ),
        )
        policy = AgentDynamicPolicy(
            revision="limited-policy-v3",
            allowed_names=(
                "inspect_case_readiness",
                "preview_risk_assessment",
                "generate_risk_assessment",
                "trigger_manual_review",
            ),
            denied_names=("preview_risk_assessment", "trigger_manual_review"),
            write_proposal_authentication_sources=(
                AgentAuthenticationSource.LOCAL_SESSION,
            ),
        )
        manifest = self.registry(
            allowlist=allowlist, dynamic_policy=policy
        ).build_manifest(context=self.context, principal=self.principal)
        self.assertEqual(
            tuple(item.name for item in manifest.capabilities),
            ("generate_risk_assessment", "inspect_case_readiness"),
        )

        unknown_policy = policy.model_copy(
            update={"allowed_names": policy.allowed_names + ("unknown",)}
        )
        with self.assertRaises(AgentCapabilityRegistryError) as raised:
            self.registry(dynamic_policy=unknown_policy).build_manifest(
                context=self.context, principal=self.principal
            )
        self.assertEqual(raised.exception.code, "AGENT_CAPABILITY_POLICY_REJECTED")

        wildcard_allowlist = self.allowlist.model_copy(
            update={"read_names": ("*",)}
        )
        with self.assertRaises(AgentCapabilityRegistryError) as raised:
            self.registry(allowlist=wildcard_allowlist).build_manifest(
                context=self.context, principal=self.principal
            )
        self.assertEqual(raised.exception.code, "AGENT_CAPABILITY_POLICY_REJECTED")

    def test_projected_reads_require_exact_full_contract_attestation(self):
        changed_catalog = self.replace_capability(
            self.catalog,
            "preview_risk_assessment",
            description="受控只读风险预览合同发生版本变化。",
        )
        with self.assertRaises(AgentCapabilityRegistryError) as raised:
            self.registry(catalog_builder=lambda: changed_catalog).build_manifest(
                context=self.context, principal=self.principal
            )
        self.assertEqual(
            raised.exception.code,
            "AGENT_CAPABILITY_ATTESTATION_REJECTED",
        )

        payload = self.attestation.model_dump(mode="python")
        payload["contracts"] = payload["contracts"][:-1]
        with self.assertRaises(ValidationError):
            PublishedReadProjectionAttestation.model_validate(payload)

        payload = self.attestation.model_dump(mode="python")
        payload["projection_version"] = "unpublished-projection-v2"
        with self.assertRaises(ValidationError):
            PublishedReadProjectionAttestation.model_validate(payload)

        changed_contracts = list(self.attestation.contracts)
        changed_contracts[0] = ReadProjectionContractAttestation(
            name=changed_contracts[0].name,
            contract_digest="0" * 64,
        )
        identity = PublishedReadProjectionAttestation._identity_payload(
            ontology_id=self.attestation.ontology_id,
            ontology_version=self.attestation.ontology_version,
            ontology_digest=self.attestation.ontology_digest,
            projection_version=self.attestation.projection_version,
            contracts=changed_contracts,
        )
        self_consistent_but_false = PublishedReadProjectionAttestation(
            attestation_id="rpa_" + _canonical_sha256(identity),
            ontology_id=self.attestation.ontology_id,
            ontology_version=self.attestation.ontology_version,
            ontology_digest=self.attestation.ontology_digest,
            projection_version=self.attestation.projection_version,
            contracts=tuple(changed_contracts),
            signature=self.attestation.signature,
        )
        with self.assertRaises(AgentCapabilityRegistryError) as raised:
            self.registry(
                read_projection_attestation=self_consistent_but_false
            ).build_manifest(context=self.context, principal=self.principal)
        self.assertEqual(
            raised.exception.code,
            "AGENT_CAPABILITY_ATTESTATION_REJECTED",
        )

    def test_attestation_cannot_bless_unknown_ontology_reference(self):
        read = self.catalog.by_name()["preview_risk_assessment"]
        references = list(read.ontology_references)
        references[0] = OntologyCapabilityReference(
            ontology_id=self.context.ontology_id,
            ontology_version=self.context.ontology_version,
            declaration_kind="ObjectType",
            declaration_name="UnpublishedObject",
        )
        changed_catalog = self.replace_capability(
            self.catalog,
            read.name,
            ontology_references=tuple(references),
        )
        changed_attestation = PublishedReadProjectionAttestation.sign_for_publication(
            self.context,
            changed_catalog,
            signing_key=READ_ATTESTATION_KEY,
        )
        with self.assertRaises(AgentCapabilityRegistryError) as raised:
            self.registry(
                catalog_builder=lambda: changed_catalog,
                read_projection_attestation=changed_attestation,
            ).build_manifest(context=self.context, principal=self.principal)
        self.assertEqual(
            raised.exception.code, "AGENT_CAPABILITY_CONTEXT_REJECTED"
        )

    def test_self_consistent_read_reissue_without_trust_root_is_rejected(self):
        changed_catalog = self.replace_capability(
            self.catalog,
            "preview_risk_assessment",
            description="安全但未经受控发布的新说明。",
        )
        untrusted_attestation = PublishedReadProjectionAttestation.sign_for_publication(
            self.context,
            changed_catalog,
            signing_key=UNTRUSTED_ATTESTATION_KEY,
        )
        with self.assertRaises(AgentCapabilityRegistryError) as raised:
            self.registry(
                catalog_builder=lambda: changed_catalog,
                read_projection_attestation=untrusted_attestation,
            ).build_manifest(context=self.context, principal=self.principal)
        self.assertEqual(
            raised.exception.code,
            "AGENT_CAPABILITY_ATTESTATION_REJECTED",
        )

    def test_nested_catalog_mutation_is_revalidated_fail_closed(self):
        mutated = self.catalog.model_copy(deep=True)
        mutated.capabilities[0].input_schema["additionalProperties"] = True
        with self.assertRaises(AgentCapabilityRegistryError) as raised:
            self.registry(catalog_builder=lambda: mutated).build_manifest(
                context=self.context, principal=self.principal
            )
        self.assertEqual(
            raised.exception.code, "AGENT_CAPABILITY_CATALOG_REJECTED"
        )
        public = raised.exception.public()
        self.assertEqual(set(public), {"code", "message"})
        self.assertNotIn("additionalProperties", public["message"])

    def test_nested_manifest_mutation_is_revalidated_before_model_or_arguments(self):
        registry = self.registry()
        manifest = registry.build_manifest(
            context=self.context, principal=self.principal
        )
        target = manifest.by_name()["trigger_manual_review"]
        # Mutate the original nested dictionary: frozen Pydantic models do not
        # make nested JSON Schema containers immutable.
        original = next(
            item
            for item in manifest.capabilities
            if item.name == target.name
        )
        original.model_input_schema["properties"]["injected"] = {
            "type": "string",
            "minLength": 1,
            "maxLength": 500,
        }
        original.model_input_schema["required"].append("injected")

        with self.assertRaises(AgentCapabilityRegistryError) as raised:
            manifest.model_payload()
        self.assertEqual(
            raised.exception.code, "AGENT_CAPABILITY_MANIFEST_STALE"
        )

        with self.assertRaises(AgentCapabilityRegistryError) as raised:
            registry.validate_model_arguments(
                manifest,
                capability_name=target.name,
                arguments={
                    "review_reason": "需要人工复核",
                    "injected": "attempt",
                },
            )
        self.assertEqual(
            raised.exception.code, "AGENT_CAPABILITY_MANIFEST_STALE"
        )

    def test_authorization_rebuilds_catalog_and_rejects_binding_drift(self):
        current = [self.catalog]
        calls = []

        def builder():
            calls.append(len(calls) + 1)
            return current[0]

        registry = self.registry(catalog_builder=builder)
        manifest = registry.build_manifest(
            context=self.context, principal=self.principal
        )
        write = self.catalog.by_name()["request_credit_report"]
        changed_binding = write.runtime_binding.model_copy(
            update={"implementation_version": "adapter-runtime-v2"}
        )
        current[0] = self.replace_capability(
            self.catalog,
            write.name,
            runtime_binding=changed_binding,
        )

        with self.assertRaises(AgentCapabilityRegistryError) as raised:
            registry.authorize_decision(
                context=self.context,
                principal=self.principal,
                manifest=manifest,
                decision=AgentCapabilityDecision.PROPOSE_WRITE_ACTION,
                capability_name=write.name,
                arguments={},
            )
        self.assertEqual(
            raised.exception.code, "AGENT_CAPABILITY_MANIFEST_STALE"
        )
        self.assertEqual(calls, [1, 2])

    def test_validate_arguments_allows_only_two_rebuilt_reason_fields(self):
        registry = self.registry()
        manifest = registry.build_manifest(
            context=self.context, principal=self.principal
        )
        authorized = registry.authorize_decision(
            context=self.context,
            principal=self.principal,
            manifest=manifest,
            decision=AgentCapabilityDecision.PROPOSE_WRITE_ACTION,
            capability_name="trigger_manual_review",
            arguments={"review_reason": "风险信号需要人工复核"},
        )
        self.assertEqual(
            authorized.model_arguments,
            {"review_reason": "风险信号需要人工复核"},
        )
        self.assertTrue(authorized.proposal_only)
        self.assertFalse(authorized.direct_execution_allowed)
        self.assertFalse(authorized.final_approval)

        invalid_arguments = (
            {},
            {"application_id": "APP-OTHER"},
            {"review_reason": "正常", "request_id": "injected"},
            {"review_reason": {"nested": "not allowed"}},
            {"review_reason": "SELECT value FROM secret_table"},
            {"review_reason": "联系号码 13800138000 需确认"},
            {"review_reason": 1},
            {"review_reason": "x" * 501},
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(
                AgentCapabilityRegistryError
            ) as raised:
                registry.validate_model_arguments(
                    manifest,
                    capability_name="trigger_manual_review",
                    arguments=arguments,
                )
            self.assertEqual(
                raised.exception.code, "AGENT_CAPABILITY_ARGUMENTS_REJECTED"
            )
            self.assertNotIn("secret_table", raised.exception.public()["message"])

        self.assertEqual(
            registry.validate_model_arguments(
                manifest,
                capability_name="inspect_case_readiness",
                arguments={},
            ),
            {},
        )

    def test_direct_write_execution_and_wrong_decision_are_always_denied(self):
        registry = self.registry()
        manifest = registry.build_manifest(
            context=self.context, principal=self.principal
        )
        with self.assertRaises(AgentCapabilityRegistryError) as raised:
            registry.authorize_decision(
                context=self.context,
                principal=self.principal,
                manifest=manifest,
                decision=AgentCapabilityDecision.EXECUTE_WRITE_ACTION,
                capability_name="trigger_manual_review",
                arguments={"review_reason": "需要人工复核"},
            )
        self.assertEqual(raised.exception.code, "AGENT_WRITE_EXECUTION_DISABLED")

        wrong_pairs = (
            (
                AgentCapabilityDecision.PROPOSE_WRITE_ACTION,
                "inspect_case_readiness",
            ),
            (
                AgentCapabilityDecision.CALL_READ_ACTIONS,
                "request_credit_report",
            ),
        )
        for decision, capability_name in wrong_pairs:
            with self.subTest(decision=decision), self.assertRaises(
                AgentCapabilityRegistryError
            ) as raised:
                registry.authorize_decision(
                    context=self.context,
                    principal=self.principal,
                    manifest=manifest,
                    decision=decision,
                    capability_name=capability_name,
                    arguments={},
                )
            self.assertEqual(
                raised.exception.code, "AGENT_CAPABILITY_DECISION_REJECTED"
            )

    def test_local_api_write_policy_is_independent_from_mvp_roles(self):
        no_local_write = self.policy.model_copy(update={
            "write_proposal_authentication_sources": (
                AgentAuthenticationSource.API_CLIENT,
                AgentAuthenticationSource.IAM,
            )
        })
        local_manifest = self.registry(
            dynamic_policy=no_local_write
        ).build_manifest(context=self.context, principal=self.principal)
        self.assertEqual(
            tuple(item.name for item in local_manifest.capabilities),
            self.read_names,
        )

        with self.assertRaises(ValidationError):
            AgentPrincipalContext(
                principal_id="forged-local-role",
                authentication_source=AgentAuthenticationSource.LOCAL_SESSION,
                authenticated=True,
                application_id="APP-REGISTRY-001",
                snapshot_id="SNAPSHOT-REGISTRY-001",
                iam_roles=("信审员",),
            )

        api_principal = self.principal.model_copy(update={
            "principal_id": "api-client-one",
            "authentication_source": AgentAuthenticationSource.API_CLIENT,
        })
        api_manifest = self.registry().build_manifest(
            context=self.context, principal=api_principal
        )
        self.assertEqual(len(api_manifest.capabilities), 12)

    def test_iam_dynamic_security_uses_deny_precedence(self):
        iam_principal = AgentPrincipalContext(
            principal_id="iam-subject-one",
            authentication_source=AgentAuthenticationSource.IAM,
            authenticated=True,
            application_id="APP-REGISTRY-001",
            snapshot_id="SNAPSHOT-REGISTRY-001",
            iam_roles=("信审员", "Agent"),
        )
        manifest = self.registry().build_manifest(
            context=self.context, principal=iam_principal
        )
        names = {item.name for item in manifest.capabilities}
        self.assertTrue(set(self.read_names).issubset(names))
        # 信审员 is allowed, but Agent is explicitly denied for this Action.
        self.assertNotIn("request_credit_report", names)
        # This Action explicitly allows Agent and does not deny it.
        self.assertIn("record_decision_recommendation", names)

    def test_catalog_cannot_relax_published_dynamic_security(self):
        write = self.catalog.by_name()["request_credit_report"]
        relaxed_security = write.security_policy.model_copy(update={
            "allowed_roles": ("UNTRUSTED",),
            "denied_roles": (),
        })
        changed_catalog = self.replace_capability(
            self.catalog,
            write.name,
            security_policy=relaxed_security,
        )
        with self.assertRaises(AgentCapabilityRegistryError) as raised:
            self.registry(catalog_builder=lambda: changed_catalog).build_manifest(
                context=self.context,
                principal=self.principal,
            )
        self.assertEqual(
            raised.exception.code,
            "AGENT_CAPABILITY_CATALOG_REJECTED",
        )

    def test_empty_published_iam_allowlist_authorizes_nobody(self):
        proposal = self.proposal.model_copy(deep=True)
        target = next(
            item
            for item in proposal.dynamic_security
            if item.action == "request_credit_report"
        )
        target.allowed_roles = []
        digest = proposal_digest(proposal)
        record = {
            **self.record,
            "proposal": proposal,
            "proposal_digest": digest,
            "audit_digest": digest,
        }
        provider = OntologyContextProvider(
            lambda _version: record,
            lambda: record,
            secret=b"empty-iam-allowlist-context-secret-32-bytes",
        )
        snapshot = {
            **self.snapshot,
            "data_binding_digest": OntologyDataBinding(proposal).binding_digest,
        }
        context = provider.resolve_context("APP-REGISTRY-001", snapshot)
        catalog = routes._personal_credit_action_capability_catalog(proposal)
        attestation = PublishedReadProjectionAttestation.sign_for_publication(
            context,
            catalog,
            signing_key=READ_ATTESTATION_KEY,
        )
        iam_principal = AgentPrincipalContext(
            principal_id="iam-empty-allowlist-test",
            authentication_source=AgentAuthenticationSource.IAM,
            authenticated=True,
            application_id="APP-REGISTRY-001",
            snapshot_id="SNAPSHOT-REGISTRY-001",
            iam_roles=("信审员",),
        )
        registry = AgentCapabilityRegistry(
            catalog_builder=lambda: catalog,
            context_provider=provider,
            read_projection_attestation=attestation,
            read_projection_verification_key=READ_ATTESTATION_KEY,
            allowlist=self.allowlist,
            dynamic_policy=self.policy,
        )
        manifest = registry.build_manifest(
            context=context,
            principal=iam_principal,
        )
        self.assertNotIn(
            "request_credit_report",
            {item.name for item in manifest.capabilities},
        )

    def test_context_and_principal_scope_cannot_cross_case(self):
        other_snapshot = dict(self.snapshot)
        other_snapshot["application"] = {"application_id": "APP-REGISTRY-002"}
        other_snapshot["snapshot_id"] = "SNAPSHOT-REGISTRY-002"
        other_context = self.provider.resolve_context(
            "APP-REGISTRY-002", other_snapshot
        )
        with self.assertRaises(AgentCapabilityRegistryError) as raised:
            self.registry().build_manifest(
                context=other_context,
                principal=self.principal,
            )
        self.assertEqual(
            raised.exception.code, "AGENT_CAPABILITY_PRINCIPAL_REJECTED"
        )

        unauthenticated = self.principal.model_copy(
            update={"authenticated": False}
        )
        with self.assertRaises(AgentCapabilityRegistryError) as raised:
            self.registry().build_manifest(
                context=self.context, principal=unauthenticated
            )
        self.assertEqual(
            raised.exception.code, "AGENT_CAPABILITY_PRINCIPAL_REJECTED"
        )


if __name__ == "__main__":
    unittest.main()
