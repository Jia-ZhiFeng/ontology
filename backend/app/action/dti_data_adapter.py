from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .function_registry import FunctionExecutionContext, FunctionRegistry
from .ratio_functions import build_personal_credit_ratio_registry


class DTIDataAdapterError(RuntimeError):
    """DTI 输入无法从受控 PostgreSQL 快照构造。"""


@dataclass(frozen=True)
class DTIInputSnapshot:
    application_id: str
    monthly_income: Decimal | int | float | str | None
    monthly_debt_payments: tuple[Decimal | int | float | str, ...]
    debt_ids: tuple[str, ...]
    snapshot_id: str
    snapshot_started_at: str

    def function_inputs(self) -> dict[str, Any]:
        return {
            "monthly_income": self.monthly_income,
            "monthly_debt_payments": list(self.monthly_debt_payments),
        }


def _snapshot_time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        raise DTIDataAdapterError("PostgreSQL 未返回快照时间")
    text = str(value).strip()
    if not text:
        raise DTIDataAdapterError("PostgreSQL 返回的快照时间无效")
    return text


class PostgresDTIDataAdapter:
    """从单个 REPEATABLE READ / READ ONLY 快照读取 DTI 必需字段。"""

    def __init__(self, postgres_service):
        if not hasattr(postgres_service, "conn"):
            raise TypeError("postgres_service 必须提供 conn()")
        self.postgres = postgres_service

    @staticmethod
    def _application_id(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise DTIDataAdapterError("application_id 不能为空")
        identifier = value.strip()
        if len(identifier) > 32:
            raise DTIDataAdapterError("application_id 长度不能超过 32")
        return identifier

    def load(self, application_id: str) -> DTIInputSnapshot:
        identifier = self._application_id(application_id)
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            cursor.execute(
                """
                SELECT txid_current_snapshot()::text AS snapshot_id,
                       transaction_timestamp() AS snapshot_started_at
                """
            )
            snapshot = cursor.fetchone()
            if not snapshot:
                raise DTIDataAdapterError("PostgreSQL 未返回一致性快照信息")

            cursor.execute(
                """
                SELECT application.application_id,
                       application.borrower_id,
                       person.monthly_income
                FROM public.loan_application AS application
                LEFT JOIN public.natural_person AS person
                  ON person.id_number = application.borrower_id
                WHERE application.application_id = %s
                """,
                (identifier,),
            )
            application = cursor.fetchone()
            if not application:
                raise DTIDataAdapterError(f"贷款申请不存在：{identifier}")
            borrower_id = application.get("borrower_id")
            if not borrower_id:
                raise DTIDataAdapterError(f"贷款申请缺少借款人关联：{identifier}")
            if application.get("monthly_income") is None:
                raise DTIDataAdapterError(f"贷款申请的借款人尚未采集月收入：{identifier}")

            cursor.execute(
                """
                SELECT debt_id, monthly_payment
                FROM public.external_debt
                WHERE borrower_id = %s
                ORDER BY debt_id
                """,
                (borrower_id,),
            )
            debts = cursor.fetchall()

        missing = sorted(
            str(row.get("debt_id"))
            for row in debts
            if row.get("monthly_payment") is None
        )
        if missing:
            raise DTIDataAdapterError(
                "外部负债缺少月还款额，不能用负债余额替代：" + ",".join(missing)
            )

        return DTIInputSnapshot(
            application_id=identifier,
            monthly_income=application["monthly_income"],
            monthly_debt_payments=tuple(row["monthly_payment"] for row in debts),
            debt_ids=tuple(str(row["debt_id"]) for row in debts),
            snapshot_id=str(snapshot["snapshot_id"]),
            snapshot_started_at=_snapshot_time(snapshot["snapshot_started_at"]),
        )

    def execute(
        self,
        application_id: str,
        *,
        ontology_version: str,
        action_execution_id: str | None = None,
        actor: str | None = None,
        registry: FunctionRegistry | None = None,
    ) -> dict[str, Any]:
        snapshot = self.load(application_id)
        active_registry = registry or build_personal_credit_ratio_registry()
        envelope = active_registry.execute(
            "calculate_dti",
            snapshot.function_inputs(),
            FunctionExecutionContext(
                ontology_version=ontology_version,
                action_execution_id=action_execution_id,
                actor=actor,
                data_snapshot_time=snapshot.snapshot_started_at,
                metadata={
                    "snapshot_id": snapshot.snapshot_id,
                    "snapshot_mode": "repeatable_read_read_only",
                },
            ),
        )
        envelope["evidence"].append(
            {
                "source": "PostgreSQL consistent snapshot",
                "application_id": snapshot.application_id,
                "debt_ids": list(snapshot.debt_ids),
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_mode": "repeatable_read_read_only",
            }
        )
        return envelope
