from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .credit_provider import CreditProviderResponse


class CreditReportPersistenceError(RuntimeError):
    pass


class CreditReportSubjectNotFound(CreditReportPersistenceError):
    pass


class CreditReportAlreadyExists(CreditReportPersistenceError):
    pass


@dataclass(frozen=True)
class CreditApplicationSubject:
    application_id: str
    subject_id_number: str
    subject_role: str
    application_channel: str


@dataclass(frozen=True)
class CreditReportEffect:
    operation: str
    report_id: str
    application_id: str
    previous_row: Mapping[str, Any] | None
    report_fingerprint: str


REPORT_VALUE_FIELDS = (
    "query_time",
    "application_channel",
    "outstanding_loan_count",
    "overdue_3y_count",
    "total_overdue_count",
    "inquiry_count",
    "reporting_agency",
)


def _report_started(row: Mapping[str, Any]) -> bool:
    return any(row.get(field) is not None for field in REPORT_VALUE_FIELDS)


def _fingerprint(row: Mapping[str, Any]) -> str:
    values = []
    for field in ("report_id", "subject_type", "subject_id_number", *REPORT_VALUE_FIELDS):
        value = row.get(field)
        if isinstance(value, datetime):
            value = value.isoformat()
        values.append((field, value))
    return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()


class PostgresCreditReportRepository:
    """Persist a CreditReport; HOLDS is derived from subject_id_number in the same row."""

    def __init__(self, postgres_service):
        self.postgres = postgres_service

    @staticmethod
    def _subject_context(cursor, application_id: str, subject_id_number: str):
        cursor.execute(
            """
            SELECT application_id,channel,borrower_id,guarantor_id,co_borrower_id
            FROM public.loan_application WHERE application_id=%s
            """,
            (application_id,),
        )
        application = cursor.fetchone()
        if not application:
            raise CreditReportSubjectNotFound(f"贷款申请不存在：{application_id}")
        roles = {
            "借款人": application.get("borrower_id"),
            "担保人": application.get("guarantor_id"),
            "共借人": application.get("co_borrower_id"),
        }
        role = next(
            (name for name, identifier in roles.items() if identifier == subject_id_number),
            None,
        )
        if role is None:
            raise CreditReportSubjectNotFound("查询主体不属于当前贷款申请")
        if not application.get("channel"):
            raise CreditReportPersistenceError("贷款申请渠道缺失，不能发起征信查询")
        cursor.execute(
            "SELECT id_number FROM public.natural_person WHERE id_number=%s",
            (subject_id_number,),
        )
        if not cursor.fetchone():
            raise CreditReportSubjectNotFound("查询主体自然人记录不存在")
        return CreditApplicationSubject(
            application_id=application_id,
            subject_id_number=subject_id_number,
            subject_role=role,
            application_channel=str(application["channel"]),
        )

    def load_application_subject(
        self, application_id: str, subject_id_number: str
    ) -> CreditApplicationSubject:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            return self._subject_context(cursor, application_id, subject_id_number)

    def persist_and_enqueue(
        self,
        *,
        context: CreditApplicationSubject,
        response: CreditProviderResponse,
        outbox_repository,
        execution_id: str,
        ontology_version: str,
        actor_id: str,
        request_id: str,
        provider_response_cached: bool,
        max_attempts: int,
    ):
        outbox_repository.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT application_id FROM public.loan_application
                WHERE application_id=%s FOR UPDATE
                """,
                (context.application_id,),
            )
            if not cursor.fetchone():
                raise CreditReportSubjectNotFound("提交时贷款申请已不存在")
            current_context = self._subject_context(
                cursor, context.application_id, context.subject_id_number
            )
            if current_context.application_channel != response.application_channel:
                raise CreditReportPersistenceError("提交时申请渠道已变化，拒绝写入报告")

            cursor.execute(
                """
                SELECT * FROM public.credit_report
                WHERE subject_id_number=%s ORDER BY report_id FOR UPDATE
                """,
                (context.subject_id_number,),
            )
            reports = cursor.fetchall()
            for report in reports:
                if report["report_id"] == response.report_id:
                    raise CreditReportAlreadyExists("供应商报告编号已存在，拒绝重复登记")
            placeholders = [report for report in reports if not _report_started(report)]
            if len(placeholders) > 1:
                raise CreditReportPersistenceError("当前主体存在多份空征信占位记录")

            values = tuple(getattr(response, field) for field in REPORT_VALUE_FIELDS)
            if placeholders:
                previous = {
                    "subject_type": placeholders[0].get("subject_type"),
                    **{
                        field: placeholders[0].get(field) for field in REPORT_VALUE_FIELDS
                    },
                }
                report_id = str(placeholders[0]["report_id"])
                cursor.execute(
                    """
                    UPDATE public.credit_report
                    SET subject_type='个人',query_time=%s,application_channel=%s,
                        outstanding_loan_count=%s,overdue_3y_count=%s,
                        total_overdue_count=%s,inquiry_count=%s,reporting_agency=%s
                    WHERE report_id=%s AND subject_id_number=%s
                    RETURNING *
                    """,
                    (*values, report_id, context.subject_id_number),
                )
                operation = "materialized_placeholder"
            else:
                previous = None
                report_id = response.report_id
                cursor.execute(
                    """
                    INSERT INTO public.credit_report (
                        report_id,subject_type,subject_id_number,query_time,
                        application_channel,outstanding_loan_count,overdue_3y_count,
                        total_overdue_count,inquiry_count,reporting_agency
                    ) VALUES (%s,'个人',%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING *
                    """,
                    (report_id, context.subject_id_number, *values),
                )
                operation = "created"
            stored = cursor.fetchone()
            if not stored or not _report_started(stored):
                raise CreditReportPersistenceError("征信报告写入后回读校验失败")
            effect = CreditReportEffect(
                operation=operation,
                report_id=report_id,
                application_id=context.application_id,
                previous_row=previous,
                report_fingerprint=_fingerprint(stored),
            )
            event = outbox_repository.enqueue(
                cursor,
                execution_id=execution_id,
                event_type="CREDIT_REPORT_GRAPH_SYNC",
                aggregate_type="CreditReport",
                aggregate_id=report_id,
                ontology_version=ontology_version,
                deduplication_key=execution_id,
                payload={
                    "actor_id": actor_id,
                    "request_id": request_id,
                    "application_id": context.application_id,
                    "report_id": report_id,
                    "provider_report_id": response.report_id,
                    "provider_request_id": response.provider_request_id,
                    "provider_name": response.provider_name,
                    "provider_mode": response.provider_mode,
                    "synthetic": response.synthetic,
                    "subject_role": context.subject_role,
                    "provider_response_cached": provider_response_cached,
                    "created_relations": ["HOLDS"],
                    "effect": {
                        "operation": effect.operation,
                        "report_id": effect.report_id,
                        "application_id": effect.application_id,
                        "previous_row": effect.previous_row,
                        "report_fingerprint": effect.report_fingerprint,
                    },
                },
                max_attempts=max_attempts,
            )
            return dict(stored), effect, event

    def compensate(self, effect: CreditReportEffect) -> None:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM public.credit_report WHERE report_id=%s FOR UPDATE",
                (effect.report_id,),
            )
            current = cursor.fetchone()
            if not current:
                if effect.operation == "created":
                    return
                raise CreditReportPersistenceError("待补偿的征信占位报告不存在")
            if _fingerprint(current) != effect.report_fingerprint:
                raise CreditReportPersistenceError("征信报告已被后续业务修改，拒绝自动补偿")
            if effect.operation == "created":
                cursor.execute(
                    "DELETE FROM public.credit_report WHERE report_id=%s",
                    (effect.report_id,),
                )
                return
            previous = dict(effect.previous_row or {})
            cursor.execute(
                """
                UPDATE public.credit_report
                SET subject_type=%s,query_time=%s,application_channel=%s,
                    outstanding_loan_count=%s,overdue_3y_count=%s,
                    total_overdue_count=%s,inquiry_count=%s,reporting_agency=%s
                WHERE report_id=%s
                """,
                (
                    previous.get("subject_type"),
                    *(previous.get(field) for field in REPORT_VALUE_FIELDS),
                    effect.report_id,
                ),
            )


def effect_from_payload(payload: Mapping[str, Any]) -> CreditReportEffect:
    raw = payload.get("effect")
    if not isinstance(raw, Mapping):
        raise CreditReportPersistenceError("outbox 缺少征信报告补偿效果")
    return CreditReportEffect(
        operation=str(raw.get("operation") or ""),
        report_id=str(raw.get("report_id") or ""),
        application_id=str(raw.get("application_id") or ""),
        previous_row=(
            raw.get("previous_row")
            if isinstance(raw.get("previous_row"), Mapping)
            else None
        ),
        report_fingerprint=str(raw.get("report_fingerprint") or ""),
    )
