from __future__ import annotations

import hmac
import threading
from typing import Any, Mapping

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from .capability_registry import PublishedReadProjectionAttestation


class ReadProjectionAttestationRepositoryError(RuntimeError):
    """A trusted READ projection publication artifact is unavailable."""

    _PUBLIC_MESSAGES = {
        "AGENT_READ_ATTESTATION_INVALID": "READ 投影发布证明格式无效",
        "AGENT_READ_ATTESTATION_CONFLICT": "同一发布身份的 READ 投影证明不可替换",
        "AGENT_READ_ATTESTATION_MISSING": "当前已发布本体缺少 READ 投影发布证明",
        "AGENT_READ_ATTESTATION_SCOPE_MISMATCH": (
            "READ 投影发布证明与本体发布身份不匹配"
        ),
        "AGENT_READ_ATTESTATION_UNAVAILABLE": "READ 投影发布证明仓储暂不可用",
    }

    def __init__(self, code: str, message: str):
        self.code = code
        self.internal_detail = message
        self.message = self._PUBLIC_MESSAGES.get(
            code,
            "READ 投影发布证明不可用",
        )
        super().__init__(self.message)

    def public(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _unavailable(error: Exception) -> ReadProjectionAttestationRepositoryError:
    return ReadProjectionAttestationRepositoryError(
        "AGENT_READ_ATTESTATION_UNAVAILABLE",
        f"{type(error).__name__}: repository operation failed",
    )


class PostgresReadProjectionAttestationRepository:
    """Immutable, version-and-digest scoped READ publication artifacts."""

    _schema_lock = threading.Lock()
    _schema_ready = False

    def __init__(self, postgres_service):
        if not hasattr(postgres_service, "conn"):
            raise TypeError("postgres_service 必须提供 conn()")
        self.postgres = postgres_service

    def ensure_schema(self) -> None:
        if type(self)._schema_ready:
            return
        with type(self)._schema_lock:
            if type(self)._schema_ready:
                return
            try:
                with self.postgres.conn() as connection, connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout = '10000ms'")
                    cursor.execute("CREATE SCHEMA IF NOT EXISTS mvp_control")
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS mvp_control.agent_read_projection_attestations (
                            ontology_id varchar(64) NOT NULL,
                            ontology_version varchar(64) NOT NULL,
                            ontology_digest char(64) NOT NULL,
                            attestation_id varchar(68) NOT NULL,
                            artifact jsonb NOT NULL,
                            created_at timestamptz NOT NULL DEFAULT now(),
                            PRIMARY KEY (ontology_id, ontology_version, ontology_digest),
                            UNIQUE (attestation_id)
                        )
                    """)
            except ReadProjectionAttestationRepositoryError:
                raise
            except Exception as exc:
                raise _unavailable(exc) from exc
            type(self)._schema_ready = True

    @staticmethod
    def _validated(
        value: PublishedReadProjectionAttestation | Mapping[str, Any],
    ) -> PublishedReadProjectionAttestation:
        try:
            if isinstance(value, PublishedReadProjectionAttestation):
                payload = value.model_dump(mode="python")
            elif isinstance(value, Mapping):
                payload = dict(value)
            else:
                raise TypeError("artifact 类型无效")
            return PublishedReadProjectionAttestation.model_validate(payload)
        except (TypeError, ValidationError) as exc:
            raise ReadProjectionAttestationRepositoryError(
                "AGENT_READ_ATTESTATION_INVALID",
                "READ 投影发布证明格式无效",
            ) from exc

    def save_publication(
        self,
        artifact: PublishedReadProjectionAttestation | Mapping[str, Any],
    ) -> PublishedReadProjectionAttestation:
        """Persist once; the same publication identity cannot be replaced."""

        checked = self._validated(artifact)
        payload = checked.model_dump(mode="json")
        self.ensure_schema()
        try:
            with self.postgres.conn() as connection, connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '10000ms'")
                cursor.execute("""
                    SELECT artifact
                    FROM mvp_control.agent_read_projection_attestations
                    WHERE ontology_id=%s AND ontology_version=%s AND ontology_digest=%s
                    FOR UPDATE
                """, (
                    checked.ontology_id,
                    checked.ontology_version,
                    checked.ontology_digest,
                ))
                existing = cursor.fetchone()
                if existing:
                    stored = self._validated(existing["artifact"])
                    if stored.model_dump(mode="json") != payload:
                        raise ReadProjectionAttestationRepositoryError(
                            "AGENT_READ_ATTESTATION_CONFLICT",
                            "同一发布身份的 READ 投影证明不可替换",
                        )
                    return stored
                cursor.execute("""
                    INSERT INTO mvp_control.agent_read_projection_attestations (
                        ontology_id, ontology_version, ontology_digest,
                        attestation_id, artifact
                    ) VALUES (%s,%s,%s,%s,%s)
                """, (
                    checked.ontology_id,
                    checked.ontology_version,
                    checked.ontology_digest,
                    checked.attestation_id,
                    Jsonb(payload),
                ))
        except ReadProjectionAttestationRepositoryError:
            raise
        except Exception as exc:
            raise _unavailable(exc) from exc
        return checked

    def get_publication(
        self,
        *,
        ontology_id: str,
        ontology_version: str,
        ontology_digest: str,
    ) -> PublishedReadProjectionAttestation:
        self.ensure_schema()
        try:
            with self.postgres.conn() as connection, connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '10000ms'")
                cursor.execute("""
                    SELECT artifact
                    FROM mvp_control.agent_read_projection_attestations
                    WHERE ontology_id=%s AND ontology_version=%s AND ontology_digest=%s
                """, (ontology_id, ontology_version, ontology_digest))
                row = cursor.fetchone()
        except ReadProjectionAttestationRepositoryError:
            raise
        except Exception as exc:
            raise _unavailable(exc) from exc
        try:
            if not row:
                raise ReadProjectionAttestationRepositoryError(
                    "AGENT_READ_ATTESTATION_MISSING",
                    "当前已发布本体缺少 READ 投影发布证明",
                )
            checked = self._validated(row["artifact"])
            expected = (ontology_id, ontology_version, ontology_digest)
            supplied = (
                checked.ontology_id,
                checked.ontology_version,
                checked.ontology_digest,
            )
            if any(
                not hmac.compare_digest(str(left), str(right))
                for left, right in zip(expected, supplied)
            ):
                raise ReadProjectionAttestationRepositoryError(
                    "AGENT_READ_ATTESTATION_SCOPE_MISMATCH",
                    "READ 投影发布证明与本体发布身份不匹配",
                )
            return checked
        except ReadProjectionAttestationRepositoryError:
            raise
        except Exception as exc:
            raise _unavailable(exc) from exc


__all__ = (
    "PostgresReadProjectionAttestationRepository",
    "ReadProjectionAttestationRepositoryError",
)
