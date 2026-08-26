from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping


class RiskAssessmentPersistenceError(RuntimeError):
    """Risk assessment business write failed closed."""


class RiskAssessmentAlreadyExists(RiskAssessmentPersistenceError):
    pass


class RiskAssessmentApplicationNotFound(RiskAssessmentPersistenceError):
    pass


@dataclass(frozen=True)
class RiskAssessmentEffect:
    operation: str
    assessment_id: str
    application_id: str
    previous_row: Mapping[str, Any] | None


REPORT_OUTPUT_FIELDS = (
    "risk_level",
    "assessment_time",
    "assessment_channel",
    "pd_value",
    "threshold",
    "risk_factors",
    "check_item",
    "check_result",
    "checker",
)

ASSESSMENT_STARTED_FIELDS = tuple(
    field for field in REPORT_OUTPUT_FIELDS if field != "threshold"
)


def deterministic_assessment_id(application_id: str, assessment_version: str) -> str:
    digest = hashlib.sha256(
        f"{application_id}|{assessment_version}".encode("utf-8")
    ).hexdigest()[:24].upper()
    return f"RA{digest}"


def _assessment_started(row: Mapping[str, Any]) -> bool:
    # threshold may be preconfigured before an assessment starts.
    return any(row.get(field) is not None for field in ASSESSMENT_STARTED_FIELDS)


class PostgresRiskAssessmentRepository:
    """Create a report or materialize one pre-created all-NULL placeholder."""

    def __init__(self, postgres_service):
        self.postgres = postgres_service

    def inspect_target(self, application_id: str) -> dict[str, Any] | None:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT application_id FROM public.loan_application WHERE application_id=%s",
                (application_id,),
            )
            if not cursor.fetchone():
                raise RiskAssessmentApplicationNotFound(
                    f"贷款申请不存在：{application_id}"
                )
            cursor.execute(
                """
                SELECT * FROM public.risk_assessment_report
                WHERE application_id=%s ORDER BY assessment_id
                """,
                (application_id,),
            )
            rows = cursor.fetchall()
        if len(rows) > 1:
            raise RiskAssessmentPersistenceError(
                "当前申请已有多份风险评估报告，违反 AX-APP-007"
            )
        if rows and _assessment_started(rows[0]):
            raise RiskAssessmentAlreadyExists(
                "当前申请已有正式风险评估报告；本 Action 不覆盖历史结论"
            )
        return dict(rows[0]) if rows else None

    def persist(
        self,
        *,
        application_id: str,
        assessment_version: str,
        report: Mapping[str, Any],
    ) -> tuple[dict[str, Any], RiskAssessmentEffect]:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            return self._persist_with_cursor(
                cursor,
                application_id=application_id,
                assessment_version=assessment_version,
                report=report,
            )

    def persist_and_enqueue(
        self,
        *,
        application_id: str,
        assessment_version: str,
        report: Mapping[str, Any],
        outbox_repository,
        execution_id: str,
        ontology_version: str,
        outbox_payload: Mapping[str, Any],
        max_attempts: int,
    ):
        """Persist the report and graph-sync intent in one PostgreSQL transaction."""
        outbox_repository.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            stored, effect = self._persist_with_cursor(
                cursor,
                application_id=application_id,
                assessment_version=assessment_version,
                report=report,
            )
            payload = {
                **dict(outbox_payload),
                "assessment_version": assessment_version,
                "report": {
                    "assessment_id": stored["assessment_id"],
                    "application_id": stored["application_id"],
                    "risk_level": stored["risk_level"],
                    "pd_value": stored["pd_value"],
                    "check_result": stored["check_result"],
                },
                "effect": {
                    "operation": effect.operation,
                    "assessment_id": effect.assessment_id,
                    "application_id": effect.application_id,
                    "previous_row": effect.previous_row,
                },
            }
            event = outbox_repository.enqueue(
                cursor,
                execution_id=execution_id,
                event_type="RISK_ASSESSMENT_GRAPH_SYNC",
                aggregate_type="RiskAssessmentReport",
                aggregate_id=effect.assessment_id,
                ontology_version=ontology_version,
                deduplication_key=execution_id,
                payload=payload,
                max_attempts=max_attempts,
            )
            return stored, effect, event

    @staticmethod
    def _persist_with_cursor(
        cursor,
        *,
        application_id: str,
        assessment_version: str,
        report: Mapping[str, Any],
    ) -> tuple[dict[str, Any], RiskAssessmentEffect]:
        # Serialize different assessment versions for the same application.
        cursor.execute(
            """
            SELECT application_id FROM public.loan_application
            WHERE application_id=%s FOR UPDATE
            """,
            (application_id,),
        )
        if not cursor.fetchone():
            raise RiskAssessmentApplicationNotFound(
                f"贷款申请不存在：{application_id}"
            )
        cursor.execute(
            """
            SELECT * FROM public.risk_assessment_report
            WHERE application_id=%s ORDER BY assessment_id FOR UPDATE
            """,
            (application_id,),
        )
        rows = cursor.fetchall()
        if len(rows) > 1:
            raise RiskAssessmentPersistenceError(
                "当前申请已有多份风险评估报告，违反 AX-APP-007"
            )
        if rows and _assessment_started(rows[0]):
            raise RiskAssessmentAlreadyExists(
                "当前申请已有正式风险评估报告；本 Action 不覆盖历史结论"
            )

        values = (
            report["risk_level"],
            report["assessment_channel"],
            report["pd_value"],
            report["threshold"],
            report["risk_factors"],
            report["check_item"],
            report["check_result"],
            report.get("checker"),
        )
        if rows:
            previous = dict(rows[0])
            assessment_id = str(previous["assessment_id"])
            cursor.execute(
                """
                UPDATE public.risk_assessment_report
                SET risk_level=%s,assessment_time=now(),assessment_channel=%s,
                    pd_value=%s,threshold=%s,risk_factors=%s,check_item=%s,
                    check_result=%s,checker=%s
                WHERE assessment_id=%s AND application_id=%s
                RETURNING *
                """,
                (*values, assessment_id, application_id),
            )
            operation = "materialized_placeholder"
        else:
            previous = None
            assessment_id = deterministic_assessment_id(
                application_id, assessment_version
            )
            cursor.execute(
                """
                INSERT INTO public.risk_assessment_report (
                    assessment_id,application_id,risk_level,assessment_time,
                    assessment_channel,pd_value,threshold,risk_factors,
                    check_item,check_result,checker
                ) VALUES (%s,%s,%s,now(),%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (assessment_id, application_id, *values),
            )
            operation = "created"
        stored = cursor.fetchone()
        if not stored:
            raise RiskAssessmentPersistenceError("风险评估报告写入后无法回读")
        effect = RiskAssessmentEffect(
            operation=operation,
            assessment_id=assessment_id,
            application_id=application_id,
            previous_row=previous,
        )
        return dict(stored), effect

    def compensate(self, effect: RiskAssessmentEffect) -> None:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM public.risk_assessment_report
                WHERE assessment_id=%s AND application_id=%s FOR UPDATE
                """,
                (effect.assessment_id, effect.application_id),
            )
            current = cursor.fetchone()
            if not current:
                if effect.operation == "created":
                    return
                raise RiskAssessmentPersistenceError("待补偿的预创建报告不存在")
            if effect.operation == "created":
                cursor.execute(
                    """
                    DELETE FROM public.risk_assessment_report
                    WHERE assessment_id=%s AND application_id=%s
                    """,
                    (effect.assessment_id, effect.application_id),
                )
                return
            previous = dict(effect.previous_row or {})
            cursor.execute(
                """
                UPDATE public.risk_assessment_report
                SET risk_level=%s,assessment_time=%s,assessment_channel=%s,
                    pd_value=%s,threshold=%s,risk_factors=%s,check_item=%s,
                    check_result=%s,checker=%s
                WHERE assessment_id=%s AND application_id=%s
                """,
                tuple(previous.get(field) for field in REPORT_OUTPUT_FIELDS)
                + (effect.assessment_id, effect.application_id),
            )
