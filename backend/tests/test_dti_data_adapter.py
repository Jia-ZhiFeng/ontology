import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.action import DTIDataAdapterError, PostgresDTIDataAdapter


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FakeCursor:
    def __init__(self, application, debts):
        self.application = application
        self.debts = debts
        self.statements = []
        self.current = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, parameters=None):
        self.current = " ".join(str(statement).split())
        self.statements.append((self.current, parameters))

    def fetchone(self):
        if "txid_current_snapshot" in self.current:
            return {
                "snapshot_id": "100:100:",
                "snapshot_started_at": datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc),
            }
        if "FROM public.loan_application" in self.current:
            return self.application
        raise AssertionError(f"unexpected fetchone: {self.current}")

    def fetchall(self):
        if "FROM public.external_debt" in self.current:
            return self.debts
        raise AssertionError(f"unexpected fetchall: {self.current}")


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.fake_cursor


class FakePostgres:
    def __init__(self, application, debts):
        self.cursor = FakeCursor(application, debts)

    def conn(self):
        return FakeConnection(self.cursor)


def application(income=Decimal("20000")):
    return {
        "application_id": "APP001",
        "borrower_id": "110101199001011234",
        "monthly_income": income,
    }


class DTIDataAdapterTests(unittest.TestCase):
    def test_loads_only_dti_inputs_in_one_read_only_snapshot(self):
        postgres = FakePostgres(
            application(),
            [
                {"debt_id": "D001", "monthly_payment": Decimal("3200")},
                {"debt_id": "D002", "monthly_payment": Decimal("1200")},
            ],
        )
        snapshot = PostgresDTIDataAdapter(postgres).load(" APP001 ")
        self.assertEqual(snapshot.monthly_income, Decimal("20000"))
        self.assertEqual(snapshot.monthly_debt_payments, (Decimal("3200"), Decimal("1200")))
        self.assertEqual(snapshot.debt_ids, ("D001", "D002"))
        self.assertEqual(snapshot.snapshot_id, "100:100:")
        statements = [statement for statement, _params in postgres.cursor.statements]
        self.assertIn("REPEATABLE READ, READ ONLY", statements[0])
        self.assertNotIn("balance", " ".join(statements).lower())
        self.assertEqual(len([item for item in statements if item.startswith("SET TRANSACTION")]), 1)

    def test_executes_dti_with_snapshot_evidence(self):
        postgres = FakePostgres(
            application(),
            [{"debt_id": "D001", "monthly_payment": Decimal("3200")}],
        )
        envelope = PostgresDTIDataAdapter(postgres).execute(
            "APP001", ontology_version="1.1.0", action_execution_id="ACTION001"
        )
        self.assertEqual(envelope["result"]["dti"], 0.16)
        self.assertEqual(envelope["result"]["debt_item_count"], 1)
        self.assertEqual(envelope["data_snapshot_time"], "2026-08-24T10:00:00+00:00")
        self.assertEqual(envelope["evidence"][-1]["snapshot_mode"], "repeatable_read_read_only")
        self.assertEqual(envelope["evidence"][-1]["debt_ids"], ["D001"])

    def test_no_external_debt_is_valid_zero_dti(self):
        envelope = PostgresDTIDataAdapter(FakePostgres(application(), [])).execute(
            "APP001", ontology_version="1.1.0"
        )
        self.assertEqual(envelope["result"]["dti"], 0.0)
        self.assertEqual(envelope["result"]["debt_item_count"], 0)

    def test_unknown_application_fails_closed(self):
        with self.assertRaisesRegex(DTIDataAdapterError, "贷款申请不存在"):
            PostgresDTIDataAdapter(FakePostgres(None, [])).load("APP404")

    def test_missing_income_fails_closed(self):
        with self.assertRaisesRegex(DTIDataAdapterError, "尚未采集月收入"):
            PostgresDTIDataAdapter(FakePostgres(application(None), [])).load("APP001")

    def test_missing_monthly_payment_never_falls_back_to_balance(self):
        debts = [{"debt_id": "D001", "monthly_payment": None, "balance": Decimal("80000")}]
        with self.assertRaisesRegex(DTIDataAdapterError, "不能用负债余额替代"):
            PostgresDTIDataAdapter(FakePostgres(application(), debts)).load("APP001")

    def test_application_identifier_is_validated_before_database_access(self):
        adapter = PostgresDTIDataAdapter(FakePostgres(application(), []))
        for value in ("", " ", "A" * 33):
            with self.subTest(value=value), self.assertRaises(DTIDataAdapterError):
                adapter.load(value)

    def test_migration_is_idempotent_and_non_destructive(self):
        path = PROJECT_ROOT / "postgres" / "migrations" / "002_add_external_debt_monthly_payment.sql"
        sql = path.read_text(encoding="utf-8")
        upper = sql.upper()
        self.assertIn("ADD COLUMN IF NOT EXISTS MONTHLY_PAYMENT", upper)
        self.assertIn("MONTHLY_PAYMENT IS NULL OR MONTHLY_PAYMENT >= 0", upper)
        self.assertNotIn("DROP TABLE", upper)
        self.assertNotIn("TRUNCATE", upper)
        self.assertNotIn("DELETE FROM", upper)
        self.assertNotIn("SET MONTHLY_PAYMENT = BALANCE", upper)


if __name__ == "__main__":
    unittest.main()
