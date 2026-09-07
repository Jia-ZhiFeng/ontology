import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from app.agent.read_projection_repository import (
    ReadProjectionAttestationRepositoryError,
)
from app.api import routes
from app.ontology.audit import proposal_digest
from app.ontology.baseline import build_personal_credit_baseline


class _Postgres:
    def validation_profile(self):
        return {"profile": "publication-test"}


class _VersionStore:
    def __init__(self, target, current, events):
        self.target = target
        self.current = current
        self.events = events
        self.record_calls = []

    @contextmanager
    def publication_guard(self):
        yield

    def get(self, version):
        if version != self.target["version"]:
            raise AssertionError(f"unexpected version: {version}")
        return self.target

    def active(self):
        return self.current

    def record_publication(self, version, action, details):
        self.events.append("record_publication")
        self.record_calls.append((version, action, details))
        return {**self.target, "version": version, "is_active": True}


class _Nebula:
    def __init__(self, events):
        self.events = events

    def abox_compatibility_report(self, _proposal):
        self.events.append("nebula_compatibility")
        return {"valid": True}

    def write_tbox(self, _proposal, _audit_digest):
        self.events.append("nebula_write_tbox")
        return {"status": "written"}

    def sync_postgres_profile(self, _profile, _proposal):
        self.events.append("nebula_sync")
        return {"status": "synchronized"}

    def mapping_report(self, _proposal):
        self.events.append("nebula_mapping")
        return {"valid": True, "summary": {"mapped": True}}

    def close(self):
        self.events.append("nebula_close")


class _Artifact:
    def __init__(self, events):
        self.events = events
        self.verify_calls = []

    def verify_publication_identity(self, **kwargs):
        self.events.append("attestation_verify")
        self.verify_calls.append(kwargs)


class _AttestationRepository:
    def __init__(self, events, *, artifact=None, get_error=None, save_error=None):
        self.events = events
        self.artifact = artifact
        self.get_error = get_error
        self.save_error = save_error
        self.get_calls = []
        self.save_calls = []

    def save_publication(self, artifact):
        self.events.append("attestation_save")
        self.save_calls.append(artifact)
        if self.save_error is not None:
            raise self.save_error
        return artifact

    def get_publication(self, **kwargs):
        self.events.append("attestation_get")
        self.get_calls.append(kwargs)
        if self.get_error is not None:
            raise self.get_error
        return self.artifact


def _record(proposal, status):
    digest = proposal_digest(proposal)
    return {
        "version": proposal.version,
        "status": status,
        "proposal": proposal.model_dump(mode="json"),
        "proposal_digest": digest,
        "audit_digest": digest,
        "is_active": status == "published",
    }


@contextmanager
def _enter_patches(*patchers):
    with ExitStack() as stack:
        yield tuple(stack.enter_context(item) for item in patchers)


class OntologyPublicationAttestationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.target_proposal = build_personal_credit_baseline()
        self.previous_proposal = self.target_proposal.model_copy(
            update={"version": "0.9.0"}
        )
        self.profile = _Postgres()

    def _common_patches(self, store, nebula_factory):
        audited_digest = store.target["audit_digest"]
        return (
            patch.object(routes, "ensure_version_registry"),
            patch.object(routes, "version_store", store),
            patch.object(routes, "postgres", return_value=self.profile),
            patch.object(routes, "nebula", side_effect=nebula_factory),
            patch.object(
                routes,
                "_action_runtime_compatibility_errors",
                return_value=[],
            ),
            patch.object(
                routes,
                "review_release_candidate",
                return_value={"valid": True, "audited_digest": audited_digest},
            ),
        )

    def test_publish_signs_and_saves_before_nebula_and_version_record(self):
        events = []
        target = _record(self.target_proposal, "approved")
        store = _VersionStore(target, None, events)
        repository = _AttestationRepository(events)
        signed_artifact = object()

        def sign_publication(**_kwargs):
            events.append("attestation_sign")
            return signed_artifact

        def nebula_factory():
            events.append("nebula_factory")
            return _Nebula(events)

        with _enter_patches(
            *self._common_patches(store, nebula_factory),
            patch.object(
                routes,
                "ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY",
                "publication-signing-key-at-least-32-bytes",
            ),
            patch.object(
                routes,
                "_read_projection_attestation_repository",
                return_value=repository,
            ),
            patch.object(
                routes.PublishedReadProjectionAttestation,
                "sign_publication_identity",
                side_effect=sign_publication,
            ),
        ) as entered:
            sign = entered[-1]
            result = routes._switch_published_version(
                self.target_proposal.version, "publish"
            )

        self.assertEqual(result["status"], "published")
        sign.assert_called_once()
        self.assertEqual(repository.save_calls, [signed_artifact])
        self.assertEqual(len(store.record_calls), 1)
        self.assertLess(events.index("attestation_sign"), events.index("attestation_save"))
        self.assertLess(events.index("attestation_save"), events.index("nebula_factory"))
        self.assertLess(events.index("nebula_factory"), events.index("record_publication"))

    def test_publish_attestation_failure_prevents_nebula_and_version_record(self):
        events = []
        target = _record(self.target_proposal, "approved")
        store = _VersionStore(target, None, events)
        failure = ReadProjectionAttestationRepositoryError(
            "AGENT_READ_ATTESTATION_UNAVAILABLE",
            "raw database detail",
        )
        repository = _AttestationRepository(events, save_error=failure)
        signed_artifact = object()

        def nebula_factory():
            events.append("nebula_factory")
            return _Nebula(events)

        with _enter_patches(
            *self._common_patches(store, nebula_factory),
            patch.object(
                routes,
                "ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY",
                "publication-signing-key-at-least-32-bytes",
            ),
            patch.object(
                routes,
                "_read_projection_attestation_repository",
                return_value=repository,
            ),
            patch.object(
                routes.PublishedReadProjectionAttestation,
                "sign_publication_identity",
                return_value=signed_artifact,
            ),
        ) as entered:
            sign = entered[-1]
            with self.assertRaises(ReadProjectionAttestationRepositoryError):
                routes._switch_published_version(
                    self.target_proposal.version, "publish"
                )

        sign.assert_called_once()
        self.assertEqual(repository.save_calls, [signed_artifact])
        self.assertNotIn("nebula_factory", events)
        self.assertEqual(store.record_calls, [])

    def test_ontology_rollback_gets_and_verifies_without_resigning(self):
        events = []
        target = _record(self.target_proposal, "published")
        current = _record(self.previous_proposal, "published")
        store = _VersionStore(target, current, events)
        artifact = _Artifact(events)
        repository = _AttestationRepository(events, artifact=artifact)

        def nebula_factory():
            events.append("nebula_factory")
            return _Nebula(events)

        with _enter_patches(
            *self._common_patches(store, nebula_factory),
            patch.object(routes, "PERSONAL_CREDIT_AGENT_MODE", "ontology_agent"),
            patch.object(
                routes,
                "_ontology_agent_static_configuration_ready",
                return_value=True,
            ),
            patch.object(
                routes,
                "ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY",
                "rollback-verification-key-at-least-32-bytes",
            ),
            patch.object(
                routes,
                "_read_projection_attestation_repository",
                return_value=repository,
            ),
            patch.object(
                routes,
                "_record_agent_read_projection_publication",
            ),
            patch.object(
                routes.PublishedReadProjectionAttestation,
                "sign_publication_identity",
            ),
        ) as entered:
            record_attestation, sign = entered[-2:]
            result = routes._switch_published_version(
                self.target_proposal.version, "rollback"
            )

        self.assertEqual(result["status"], "rolled_back")
        record_attestation.assert_not_called()
        sign.assert_not_called()
        self.assertEqual(len(repository.get_calls), 1)
        self.assertEqual(len(artifact.verify_calls), 1)
        self.assertLess(events.index("attestation_get"), events.index("attestation_verify"))
        self.assertLess(events.index("attestation_verify"), events.index("nebula_factory"))
        self.assertLess(events.index("nebula_factory"), events.index("record_publication"))

    def test_ontology_rollback_missing_history_prevents_nebula_and_record(self):
        events = []
        target = _record(self.target_proposal, "published")
        current = _record(self.previous_proposal, "published")
        store = _VersionStore(target, current, events)
        repository = _AttestationRepository(
            events,
            get_error=ReadProjectionAttestationRepositoryError(
                "AGENT_READ_ATTESTATION_MISSING",
                "missing historical artifact",
            ),
        )

        def nebula_factory():
            events.append("nebula_factory")
            return _Nebula(events)

        with _enter_patches(
            *self._common_patches(store, nebula_factory),
            patch.object(routes, "PERSONAL_CREDIT_AGENT_MODE", "ontology_agent"),
            patch.object(
                routes,
                "_ontology_agent_static_configuration_ready",
                return_value=True,
            ),
            patch.object(
                routes,
                "_read_projection_attestation_repository",
                return_value=repository,
            ),
            patch.object(
                routes,
                "_record_agent_read_projection_publication",
            ),
            patch.object(
                routes.PublishedReadProjectionAttestation,
                "sign_publication_identity",
            ),
        ) as entered:
            record_attestation, sign = entered[-2:]
            with self.assertRaises(ReadProjectionAttestationRepositoryError):
                routes._switch_published_version(
                    self.target_proposal.version, "rollback"
                )

        record_attestation.assert_not_called()
        sign.assert_not_called()
        self.assertEqual(len(repository.get_calls), 1)
        self.assertNotIn("nebula_factory", events)
        self.assertEqual(store.record_calls, [])

    def test_ontology_publish_and_rollback_require_ready_trust_roots(self):
        for action, status in (("publish", "approved"), ("rollback", "published")):
            with self.subTest(action=action):
                events = []
                target = _record(self.target_proposal, status)
                current = (
                    None
                    if action == "publish"
                    else _record(self.previous_proposal, "published")
                )
                store = _VersionStore(target, current, events)

                def nebula_factory():
                    events.append("nebula_factory")
                    return _Nebula(events)

                with _enter_patches(
                    *self._common_patches(store, nebula_factory),
                    patch.object(
                        routes, "PERSONAL_CREDIT_AGENT_MODE", "ontology_agent"
                    ),
                    patch.object(
                        routes,
                        "_ontology_agent_static_configuration_ready",
                        return_value=False,
                    ),
                    patch.object(
                        routes,
                        "_record_agent_read_projection_publication",
                    ),
                    patch.object(
                        routes,
                        "_read_projection_attestation_repository",
                    ),
                ) as entered:
                    record_attestation, repository_factory = entered[-2:]
                    with self.assertRaisesRegex(
                        routes.VersionWorkflowError,
                        "发布证明校验配置未就绪",
                    ):
                        routes._switch_published_version(
                            self.target_proposal.version, action
                        )

                record_attestation.assert_not_called()
                repository_factory.assert_not_called()
                self.assertNotIn("nebula_factory", events)
                self.assertEqual(store.record_calls, [])

    def test_legacy_rollback_does_not_depend_on_read_attestation(self):
        events = []
        target = _record(self.target_proposal, "published")
        current = _record(self.previous_proposal, "published")
        store = _VersionStore(target, current, events)

        def nebula_factory():
            events.append("nebula_factory")
            return _Nebula(events)

        with _enter_patches(
            *self._common_patches(store, nebula_factory),
            patch.object(routes, "PERSONAL_CREDIT_AGENT_MODE", "legacy"),
            patch.object(
                routes,
                "_ontology_agent_static_configuration_ready",
                return_value=True,
            ),
            patch.object(
                routes,
                "_read_projection_attestation_repository",
                side_effect=AssertionError("legacy rollback must not read proof"),
            ),
            patch.object(
                routes,
                "_record_agent_read_projection_publication",
            ),
            patch.object(
                routes.PublishedReadProjectionAttestation,
                "sign_publication_identity",
            ),
        ) as entered:
            repository_factory, record_attestation, sign = entered[-3:]
            result = routes._switch_published_version(
                self.target_proposal.version, "rollback"
            )

        self.assertEqual(result["status"], "rolled_back")
        repository_factory.assert_not_called()
        record_attestation.assert_not_called()
        sign.assert_not_called()
        self.assertEqual(len(store.record_calls), 1)


if __name__ == "__main__":
    unittest.main()
