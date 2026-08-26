from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from .fraud_provider import FraudProviderResponse


class AntiFraudPersistenceError(RuntimeError):
    pass


class AntiFraudApplicationIncomplete(AntiFraudPersistenceError):
    pass


class AntiFraudReportAlreadyExists(AntiFraudPersistenceError):
    pass


@dataclass(frozen=True)
class AntiFraudApplicationContext:
    application_id: str
    borrower_id_number: str
    borrower_phone: str | None
    borrower_address: str | None
    vehicle_vin: str
    application_channel: str | None
    application_status: str


@dataclass(frozen=True)
class AntiFraudReportEffect:
    operation: str
    fraud_report_id: str
    application_id: str
    previous_row: Mapping[str, Any] | None
    report_fingerprint: str


REPORT_VALUE_FIELDS = ("fraud_score", "result", "hit_details")


def _report_started(row: Mapping[str, Any]) -> bool:
    return any(row.get(field) is not None for field in REPORT_VALUE_FIELDS)


def _fingerprint(row: Mapping[str, Any]) -> str:
    values = []
    for field in ("fraud_report_id", "application_id", *REPORT_VALUE_FIELDS):
        value = row.get(field)
        if isinstance(value, Decimal):
            value = format(value, "f")
        values.append((field, value))
    return hashlib.sha256(repr(values).encode("utf-8")).hexdigest()


class PostgresAntiFraudReportRepository:
    """Persist AntiFraudReport; HAS_FRAUD_REPORT derives from application_id."""

    def __init__(self, postgres_service):
        self.postgres = postgres_service

    @staticmethod
    def _load_context(cursor, application_id: str, *, lock: bool = False):
        if lock:
            cursor.execute(
                """
                SELECT application_id FROM public.loan_application
                WHERE application_id=%s FOR UPDATE
                """,
                (application_id,),
            )
            if not cursor.fetchone():
                raise AntiFraudApplicationIncomplete(
                    f"贷款申请不存在：{application_id}"
                )
        cursor.execute(
            """
            SELECT application.application_id,application.channel,
                   application.application_status,application.borrower_id,
                   application.vehicle_vin,person.phone,person.address,
                   person.id_number AS person_id,vehicle.vin AS existing_vin
            FROM public.loan_application AS application
            LEFT JOIN public.natural_person AS person
              ON person.id_number=application.borrower_id
            LEFT JOIN public.vehicle AS vehicle
              ON vehicle.vin=application.vehicle_vin
            WHERE application.application_id=%s
            """,
            (application_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise AntiFraudApplicationIncomplete(f"贷款申请不存在：{application_id}")
        missing = []
        if not row.get("borrower_id") or not row.get("person_id"):
            missing.append("SUBMITS")
        if not row.get("vehicle_vin") or not row.get("existing_vin"):
            missing.append("TARGETS")
        if not row.get("application_status"):
            missing.append("application_status")
        if missing:
            raise AntiFraudApplicationIncomplete(
                f"申请核心上下文不完整：{','.join(missing)}"
            )
        return AntiFraudApplicationContext(
            application_id=str(row["application_id"]),
            borrower_id_number=str(row["borrower_id"]),
            borrower_phone=(str(row["phone"]) if row.get("phone") else None),
            borrower_address=(str(row["address"]) if row.get("address") else None),
            vehicle_vin=str(row["vehicle_vin"]),
            application_channel=(
                str(row["channel"]) if row.get("channel") else None
            ),
            application_status=str(row["application_status"]),
        )

    def inspect_target(self, application_id: str) -> AntiFraudApplicationContext:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            context = self._load_context(cursor, application_id)
            cursor.execute(
                """
                SELECT * FROM public.anti_fraud_report
                WHERE application_id=%s ORDER BY fraud_report_id
                """,
                (application_id,),
            )
            reports = cursor.fetchall()
        if len(reports) > 1:
            raise AntiFraudPersistenceError(
                "当前申请已有多份反欺诈报告，违反 HAS_FRAUD_REPORT 基数"
            )
        if reports and _report_started(reports[0]):
            raise AntiFraudReportAlreadyExists(
                "当前申请已有正式反欺诈报告；本 Action 不覆盖历史结论"
            )
        return context

    def persist_and_enqueue(
        self,
        *,
        context: AntiFraudApplicationContext,
        response: FraudProviderResponse,
        association_risk_summary: Mapping[str, Any],
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
            current = self._load_context(cursor, context.application_id, lock=True)
            if current != context:
                raise AntiFraudApplicationIncomplete(
                    "提交时申请核心上下文已变化，拒绝写入反欺诈报告"
                )
            cursor.execute(
                """
                SELECT * FROM public.anti_fraud_report
                WHERE application_id=%s ORDER BY fraud_report_id FOR UPDATE
                """,
                (context.application_id,),
            )
            reports = cursor.fetchall()
            if len(reports) > 1:
                raise AntiFraudPersistenceError(
                    "当前申请已有多份反欺诈报告，违反 HAS_FRAUD_REPORT 基数"
                )
            if reports and _report_started(reports[0]):
                raise AntiFraudReportAlreadyExists(
                    "当前申请已有正式反欺诈报告；拒绝覆盖"
                )
            for report in reports:
                if report["fraud_report_id"] == response.fraud_report_id:
                    raise AntiFraudReportAlreadyExists("反欺诈供应商报告编号已存在")

            values = (
                response.fraud_score,
                response.result,
                response.hit_details,
            )
            if reports:
                previous = {
                    field: reports[0].get(field) for field in REPORT_VALUE_FIELDS
                }
                fraud_report_id = str(reports[0]["fraud_report_id"])
                cursor.execute(
                    """
                    UPDATE public.anti_fraud_report
                    SET fraud_score=%s,result=%s,hit_details=%s
                    WHERE fraud_report_id=%s AND application_id=%s
                    RETURNING *
                    """,
                    (*values, fraud_report_id, context.application_id),
                )
                operation = "materialized_placeholder"
            else:
                previous = None
                fraud_report_id = response.fraud_report_id
                cursor.execute(
                    """
                    INSERT INTO public.anti_fraud_report (
                        fraud_report_id,application_id,fraud_score,result,hit_details
                    ) VALUES (%s,%s,%s,%s,%s) RETURNING *
                    """,
                    (fraud_report_id, context.application_id, *values),
                )
                operation = "created"
            stored = cursor.fetchone()
            if not stored or not _report_started(stored):
                raise AntiFraudPersistenceError("反欺诈报告写入后回读校验失败")
            effect = AntiFraudReportEffect(
                operation=operation,
                fraud_report_id=fraud_report_id,
                application_id=context.application_id,
                previous_row=previous,
                report_fingerprint=_fingerprint(stored),
            )
            event = outbox_repository.enqueue(
                cursor,
                execution_id=execution_id,
                event_type="ANTI_FRAUD_REPORT_GRAPH_SYNC",
                aggregate_type="AntiFraudReport",
                aggregate_id=fraud_report_id,
                ontology_version=ontology_version,
                deduplication_key=execution_id,
                payload={
                    "actor_id": actor_id,
                    "request_id": request_id,
                    "application_id": context.application_id,
                    "fraud_report_id": fraud_report_id,
                    "provider_report_id": response.fraud_report_id,
                    "provider_request_id": response.provider_request_id,
                    "provider_name": response.provider_name,
                    "provider_mode": response.provider_mode,
                    "synthetic": response.synthetic,
                    "provider_response_cached": provider_response_cached,
                    "association_risk_summary": dict(association_risk_summary),
                    "created_relations": ["HAS_FRAUD_REPORT"],
                    "effect": {
                        "operation": effect.operation,
                        "fraud_report_id": effect.fraud_report_id,
                        "application_id": effect.application_id,
                        "previous_row": effect.previous_row,
                        "report_fingerprint": effect.report_fingerprint,
                    },
                },
                max_attempts=max_attempts,
            )
            return dict(stored), effect, event

    def compensate(self, effect: AntiFraudReportEffect) -> None:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM public.anti_fraud_report
                WHERE fraud_report_id=%s AND application_id=%s FOR UPDATE
                """,
                (effect.fraud_report_id, effect.application_id),
            )
            current = cursor.fetchone()
            if not current:
                if effect.operation == "created":
                    return
                raise AntiFraudPersistenceError("待补偿的反欺诈占位报告不存在")
            if _fingerprint(current) != effect.report_fingerprint:
                raise AntiFraudPersistenceError(
                    "反欺诈报告已被后续业务修改，拒绝自动补偿"
                )
            if effect.operation == "created":
                cursor.execute(
                    "DELETE FROM public.anti_fraud_report WHERE fraud_report_id=%s",
                    (effect.fraud_report_id,),
                )
                return
            previous = dict(effect.previous_row or {})
            cursor.execute(
                """
                UPDATE public.anti_fraud_report
                SET fraud_score=%s,result=%s,hit_details=%s
                WHERE fraud_report_id=%s AND application_id=%s
                """,
                tuple(previous.get(field) for field in REPORT_VALUE_FIELDS)
                + (effect.fraud_report_id, effect.application_id),
            )


def effect_from_payload(payload: Mapping[str, Any]) -> AntiFraudReportEffect:
    raw = payload.get("effect")
    if not isinstance(raw, Mapping):
        raise AntiFraudPersistenceError("outbox 缺少反欺诈报告补偿效果")
    return AntiFraudReportEffect(
        operation=str(raw.get("operation") or ""),
        fraud_report_id=str(raw.get("fraud_report_id") or ""),
        application_id=str(raw.get("application_id") or ""),
        previous_row=(
            raw.get("previous_row")
            if isinstance(raw.get("previous_row"), Mapping)
            else None
        ),
        report_fingerprint=str(raw.get("report_fingerprint") or ""),
    )
