import unittest

from app.agent.capability_registry import PublishedReadProjectionAttestation
from app.agent.read_projection_repository import (
    PostgresReadProjectionAttestationRepository,
    ReadProjectionAttestationRepositoryError,
)
from app.api import routes
from app.ontology.audit import proposal_digest
from app.ontology.baseline import build_personal_credit_baseline


class _Cursor:
    def __init__(self, postgres):
        self.postgres = postgres
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, parameters=None):
        normalized = " ".join(str(statement).split()).lower()
        self.postgres.statements.append((normalized, parameters))
        if normalized.startswith("select artifact"):
            self._result = self.postgres.row
        elif normalized.startswith("insert into"):
            wrapped = parameters[-1]
            artifact = getattr(wrapped, "obj", wrapped)
            self.postgres.row = {"artifact": artifact}

    def fetchone(self):
        return self._result


class _Connection:
    def __init__(self, postgres):
        self.postgres = postgres

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return _Cursor(self.postgres)


class _Postgres:
    def __init__(self):
        self.row = None
        self.statements = []

    def conn(self):
        return _Connection(self)


class _FailingPostgres:
    def conn(self):
        raise RuntimeError(
            "syntax error near SELECT secret_value FROM internal_table"
        )


class ReadProjectionAttestationRepositoryTests(unittest.TestCase):
    def setUp(self):
        PostgresReadProjectionAttestationRepository._schema_ready = False
        self.postgres = _Postgres()
        self.repository = PostgresReadProjectionAttestationRepository(
            self.postgres
        )
        proposal = build_personal_credit_baseline()
        self.artifact = PublishedReadProjectionAttestation.sign_publication_identity(
            ontology_id="personal-credit",
            ontology_version=proposal.version,
            ontology_digest=proposal_digest(proposal),
            catalog=routes._personal_credit_action_capability_catalog(proposal),
            signing_key=b"publication-attestation-signing-key-32-bytes",
        )

    def test_save_and_load_are_version_and_digest_scoped(self):
        saved = self.repository.save_publication(self.artifact)
        loaded = self.repository.get_publication(
            ontology_id=self.artifact.ontology_id,
            ontology_version=self.artifact.ontology_version,
            ontology_digest=self.artifact.ontology_digest,
        )

        self.assertEqual(saved, self.artifact)
        self.assertEqual(loaded, self.artifact)
        self.assertEqual(
            self.repository.save_publication(self.artifact), self.artifact
        )

    def test_same_publication_identity_cannot_be_replaced(self):
        self.repository.save_publication(self.artifact)
        replacement = self.artifact.model_copy(update={
            "signature": "rpas_" + "f" * 64,
        })

        with self.assertRaises(ReadProjectionAttestationRepositoryError) as raised:
            self.repository.save_publication(replacement)

        self.assertEqual(
            raised.exception.code,"AGENT_READ_ATTESTATION_CONFLICT"
        )

    def test_missing_or_malformed_artifact_fails_closed(self):
        with self.assertRaises(ReadProjectionAttestationRepositoryError) as missing:
            self.repository.get_publication(
                ontology_id=self.artifact.ontology_id,
                ontology_version=self.artifact.ontology_version,
                ontology_digest=self.artifact.ontology_digest,
            )
        self.assertEqual(missing.exception.code,"AGENT_READ_ATTESTATION_MISSING")

        self.postgres.row={"artifact":{"attestation_id":"invalid"}}
        with self.assertRaises(ReadProjectionAttestationRepositoryError) as invalid:
            self.repository.get_publication(
                ontology_id=self.artifact.ontology_id,
                ontology_version=self.artifact.ontology_version,
                ontology_digest=self.artifact.ontology_digest,
            )
        self.assertEqual(invalid.exception.code,"AGENT_READ_ATTESTATION_INVALID")

    def test_loaded_artifact_must_match_requested_publication_scope(self):
        self.repository.save_publication(self.artifact)

        with self.assertRaises(ReadProjectionAttestationRepositoryError) as raised:
            self.repository.get_publication(
                ontology_id=self.artifact.ontology_id,
                ontology_version=self.artifact.ontology_version + "-forged",
                ontology_digest=self.artifact.ontology_digest,
            )

        self.assertEqual(
            raised.exception.code,
            "AGENT_READ_ATTESTATION_SCOPE_MISMATCH",
        )

    def test_unknown_database_failures_are_fixed_unavailable_errors(self):
        for operation in ("ensure", "save", "get"):
            with self.subTest(operation=operation):
                PostgresReadProjectionAttestationRepository._schema_ready = False
                repository = PostgresReadProjectionAttestationRepository(
                    _FailingPostgres()
                )
                with self.assertRaises(
                    ReadProjectionAttestationRepositoryError
                ) as raised:
                    if operation == "ensure":
                        repository.ensure_schema()
                    elif operation == "save":
                        repository.save_publication(self.artifact)
                    else:
                        repository.get_publication(
                            ontology_id=self.artifact.ontology_id,
                            ontology_version=self.artifact.ontology_version,
                            ontology_digest=self.artifact.ontology_digest,
                        )

                self.assertEqual(
                    raised.exception.code,
                    "AGENT_READ_ATTESTATION_UNAVAILABLE",
                )
                self.assertEqual(
                    raised.exception.message,
                    "READ 投影发布证明仓储暂不可用",
                )
                self.assertNotIn("SELECT", str(raised.exception))
                self.assertNotIn("secret_value", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
