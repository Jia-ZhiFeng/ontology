from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from .execution import sha256_digest


class LoanApplicationPersistenceError(RuntimeError):
    """A controlled, fail-closed origination persistence error."""


class LoanApplicationAlreadyExists(LoanApplicationPersistenceError):
    pass


class LoanApplicationMasterDataConflict(LoanApplicationPersistenceError):
    pass


class LoanApplicationAxiomViolation(LoanApplicationPersistenceError):
    pass


@dataclass(frozen=True)
class LoanApplicationEffect:
    application_id: str
    application_fingerprint: str
    created_entities: Mapping[str, tuple[str, ...]]


ENTITY_FIELDS = {
    "natural_person": (
        "id_number",
        "name",
        "phone",
        "gender",
        "address",
        "marital_status",
        "monthly_income",
        "employer",
        "guarantee_rating",
        "guarantee_count",
        "guarantee_total",
    ),
    "vehicle": (
        "vin",
        "brand",
        "model",
        "invoice_price",
        "guide_price",
        "pledge_status",
    ),
    "dealer": ("credit_code", "dealer_name", "location"),
}

APPLICATION_FIELDS = (
    "application_id",
    "submit_date",
    "channel",
    "loan_amount",
    "loan_term",
    "interest_rate",
    "borrower_id",
    "vehicle_vin",
    "dealer_credit_code",
    "guarantor_id",
    "co_borrower_id",
    "application_status",
    "decision_result",
)

ACTIVE_APPLICATION_STATUSES = ("资料待补充", "评估中", "待人工复核")


def _comparable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return value.normalize()
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value


def _application_fingerprint(row: Mapping[str, Any]) -> str:
    return sha256_digest({field: row.get(field) for field in APPLICATION_FIELDS})


class PostgresLoanApplicationRepository:
    """Persist an application and all declared role endpoints in one transaction."""

    def __init__(self, postgres_service):
        self.postgres = postgres_service

    @staticmethod
    def _lock_business_key(cursor, namespace: str, key: str) -> None:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"create_loan_application:{namespace}:{key}",),
        )

    @staticmethod
    def _insert_or_reuse_entity(
        cursor,
        *,
        table: str,
        key_field: str,
        values: Mapping[str, Any],
    ) -> str:
        fields = ENTITY_FIELDS[table]
        key = str(values[key_field])
        cursor.execute(
            f"SELECT * FROM public.{table} WHERE {key_field}=%s FOR UPDATE",
            (key,),
        )
        existing = cursor.fetchone()
        if existing:
            conflicts = [
                field
                for field in fields
                if values.get(field) is not None
                and _comparable(existing.get(field)) != _comparable(values.get(field))
            ]
            if conflicts:
                raise LoanApplicationMasterDataConflict(
                    f"既有 {table} 主数据与进件不一致：{','.join(conflicts)}"
                )
            return "reused"

        placeholders = ",".join(["%s"] * len(fields))
        cursor.execute(
            f"INSERT INTO public.{table} ({','.join(fields)}) VALUES ({placeholders})",
            tuple(values.get(field) for field in fields),
        )
        return "created"

    @staticmethod
    def _validate_frequency(cursor, borrower_id: str, submit_date: datetime) -> None:
        cursor.execute(
            """
            SELECT application_id,submit_date FROM public.loan_application
            WHERE borrower_id=%s ORDER BY submit_date,application_id FOR UPDATE
            """,
            (borrower_id,),
        )
        entries = [
            (row["submit_date"], row["application_id"])
            for row in cursor.fetchall()
            if row.get("submit_date") is not None
        ]
        entries.append((submit_date, "<new>"))
        entries.sort(key=lambda item: (item[0], item[1]))
        start = 0
        for end, (current_time, _identifier) in enumerate(entries):
            while current_time - entries[start][0] > timedelta(days=7):
                start += 1
            if end - start + 1 > 3:
                raise LoanApplicationAxiomViolation(
                    "同一借款人滚动7日内申请不得超过3笔（AX-APP-005）"
                )

    @staticmethod
    def _validate_active_vehicle(cursor, vehicle_vin: str) -> None:
        cursor.execute(
            """
            SELECT application_id FROM public.loan_application
            WHERE vehicle_vin=%s AND application_status=ANY(%s)
            ORDER BY application_id FOR UPDATE
            """,
            (vehicle_vin, list(ACTIVE_APPLICATION_STATUSES)),
        )
        if cursor.fetchone():
            raise LoanApplicationAxiomViolation(
                "同一VIN不能同时关联多笔未完成申请（AX-APP-006）"
            )

    def persist_and_enqueue(
        self,
        *,
        application: Mapping[str, Any],
        borrower: Mapping[str, Any],
        vehicle: Mapping[str, Any],
        dealer: Mapping[str, Any] | None,
        guarantor: Mapping[str, Any] | None,
        co_borrower: Mapping[str, Any] | None,
        outbox_repository,
        execution_id: str,
        ontology_version: str,
        actor_id: str,
        request_id: str,
        max_attempts: int,
    ):
        outbox_repository.ensure_schema()
        application_id = str(application["application_id"])
        borrower_id = str(borrower["id_number"])
        vehicle_vin = str(vehicle["vin"])
        created_entities: dict[str, list[str]] = {
            "natural_person": [],
            "vehicle": [],
            "dealer": [],
        }
        operations: dict[str, str] = {}

        with self.postgres.conn() as connection, connection.cursor() as cursor:
            for namespace, key in sorted(
                {
                    ("application", application_id),
                    ("borrower", borrower_id),
                    ("vehicle", vehicle_vin),
                }
            ):
                self._lock_business_key(cursor, namespace, key)
            cursor.execute(
                "SELECT application_id FROM public.loan_application WHERE application_id=%s",
                (application_id,),
            )
            if cursor.fetchone():
                raise LoanApplicationAlreadyExists(
                    f"贷款申请已存在：{application_id}"
                )

            person_inputs = [("borrower", borrower)]
            if guarantor:
                person_inputs.append(("guarantor", guarantor))
            if co_borrower:
                person_inputs.append(("co_borrower", co_borrower))
            for role, person in sorted(
                person_inputs, key=lambda item: str(item[1]["id_number"])
            ):
                person_id = str(person["id_number"])
                self._lock_business_key(cursor, "person", person_id)
                operation = self._insert_or_reuse_entity(
                    cursor,
                    table="natural_person",
                    key_field="id_number",
                    values=person,
                )
                operations[role] = operation
                if operation == "created":
                    created_entities["natural_person"].append(person_id)

            operation = self._insert_or_reuse_entity(
                cursor,
                table="vehicle",
                key_field="vin",
                values=vehicle,
            )
            operations["vehicle"] = operation
            if operation == "created":
                created_entities["vehicle"].append(vehicle_vin)

            if dealer:
                dealer_id = str(dealer["credit_code"])
                self._lock_business_key(cursor, "dealer", dealer_id)
                operation = self._insert_or_reuse_entity(
                    cursor,
                    table="dealer",
                    key_field="credit_code",
                    values=dealer,
                )
                operations["dealer"] = operation
                if operation == "created":
                    created_entities["dealer"].append(dealer_id)

            self._validate_frequency(cursor, borrower_id, application["submit_date"])
            self._validate_active_vehicle(cursor, vehicle_vin)

            row = {
                **dict(application),
                "borrower_id": borrower_id,
                "vehicle_vin": vehicle_vin,
                "dealer_credit_code": dealer.get("credit_code") if dealer else None,
                "guarantor_id": guarantor.get("id_number") if guarantor else None,
                "co_borrower_id": co_borrower.get("id_number") if co_borrower else None,
                "decision_result": None,
            }
            cursor.execute(
                f"""
                INSERT INTO public.loan_application ({','.join(APPLICATION_FIELDS)})
                VALUES ({','.join(['%s'] * len(APPLICATION_FIELDS))})
                RETURNING *
                """,
                tuple(row.get(field) for field in APPLICATION_FIELDS),
            )
            stored = dict(cursor.fetchone())
            if any(
                _comparable(stored.get(field)) != _comparable(row.get(field))
                for field in APPLICATION_FIELDS
            ):
                raise LoanApplicationPersistenceError("贷款申请写入后回读校验失败")

            effect = LoanApplicationEffect(
                application_id=application_id,
                application_fingerprint=_application_fingerprint(stored),
                created_entities={
                    table: tuple(sorted(identifiers))
                    for table, identifiers in created_entities.items()
                },
            )
            relations = ["SUBMITS", "TARGETS"]
            if dealer:
                relations.append("SOLD_BY")
            if guarantor:
                relations.append("GUARANTEES")
            if co_borrower:
                relations.append("CO_BORROWS")
            event = outbox_repository.enqueue(
                cursor,
                execution_id=execution_id,
                event_type="LOAN_APPLICATION_GRAPH_SYNC",
                aggregate_type="LoanApplication",
                aggregate_id=application_id,
                ontology_version=ontology_version,
                deduplication_key=execution_id,
                payload={
                    "actor_id": actor_id,
                    "request_id": request_id,
                    "application_id": application_id,
                    "application_status": stored["application_status"],
                    "created_relations": relations,
                    "entity_operations": operations,
                    "effect": {
                        "application_id": effect.application_id,
                        "application_fingerprint": effect.application_fingerprint,
                        "created_entities": {
                            table: [
                                {
                                    {
                                        "natural_person": "id_number",
                                        "vehicle": "vin",
                                        "dealer": "credit_code",
                                    }[table]: identifier
                                }
                                for identifier in identifiers
                            ]
                            for table, identifiers in effect.created_entities.items()
                        },
                    },
                },
                max_attempts=max_attempts,
            )
            return stored, effect, event

    def compensate(self, effect: LoanApplicationEffect) -> None:
        """Remove only the untouched application and master rows created by this Action."""
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            self._lock_business_key(cursor, "application", effect.application_id)
            cursor.execute(
                "SELECT * FROM public.loan_application WHERE application_id=%s FOR UPDATE",
                (effect.application_id,),
            )
            current = cursor.fetchone()
            if current:
                if _application_fingerprint(current) != effect.application_fingerprint:
                    raise LoanApplicationPersistenceError(
                        "申请已被后续业务修改，拒绝自动补偿删除"
                    )
                for table in (
                    "risk_assessment_report",
                    "anti_fraud_report",
                    "phone_verification_record",
                ):
                    cursor.execute(
                        f"SELECT 1 FROM public.{table} WHERE application_id=%s LIMIT 1",
                        (effect.application_id,),
                    )
                    if cursor.fetchone():
                        raise LoanApplicationPersistenceError(
                            "申请已被后续报告引用，拒绝自动补偿删除"
                        )
                cursor.execute(
                    "DELETE FROM public.loan_application WHERE application_id=%s",
                    (effect.application_id,),
                )

            for identifier in effect.created_entities.get("dealer", ()):
                cursor.execute(
                    """
                    DELETE FROM public.dealer d WHERE d.credit_code=%s
                    AND NOT EXISTS (
                        SELECT 1 FROM public.loan_application a
                        WHERE a.dealer_credit_code=d.credit_code
                    )
                    """,
                    (identifier,),
                )
            for identifier in effect.created_entities.get("vehicle", ()):
                cursor.execute(
                    """
                    DELETE FROM public.vehicle v WHERE v.vin=%s
                    AND NOT EXISTS (
                        SELECT 1 FROM public.loan_application a WHERE a.vehicle_vin=v.vin
                    )
                    """,
                    (identifier,),
                )
            for identifier in effect.created_entities.get("natural_person", ()):
                cursor.execute(
                    """
                    DELETE FROM public.natural_person p WHERE p.id_number=%s
                    AND NOT EXISTS (
                        SELECT 1 FROM public.loan_application a
                        WHERE p.id_number IN (a.borrower_id,a.guarantor_id,a.co_borrower_id)
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM public.credit_report c
                        WHERE c.subject_id_number=p.id_number
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM public.external_debt d
                        WHERE d.borrower_id=p.id_number
                    )
                    """,
                    (identifier,),
                )


def effect_from_payload(payload: Mapping[str, Any]) -> LoanApplicationEffect:
    raw = payload.get("effect")
    if not isinstance(raw, Mapping):
        raise LoanApplicationPersistenceError("outbox 缺少贷款申请补偿效果")
    created = raw.get("created_entities")
    if not isinstance(created, Mapping):
        raise LoanApplicationPersistenceError("outbox 创建实体清单无效")
    key_fields = {
        "natural_person": "id_number",
        "vehicle": "vin",
        "dealer": "credit_code",
    }
    normalized: dict[str, tuple[str, ...]] = {}
    for table, identifiers in created.items():
        if not isinstance(identifiers, (list, tuple)):
            continue
        key_field = key_fields.get(str(table))
        values = []
        for item in identifiers:
            if isinstance(item, Mapping) and key_field:
                value = item.get(key_field)
            else:  # Backward-compatible with events written before the keyed payload.
                value = item
            if value:
                values.append(str(value))
        normalized[str(table)] = tuple(values)
    return LoanApplicationEffect(
        application_id=str(raw.get("application_id") or ""),
        application_fingerprint=str(raw.get("application_fingerprint") or ""),
        created_entities=normalized,
    )
