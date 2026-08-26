import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from app.action import (
    FunctionContractError,
    FunctionDataAdapterError,
    PersonalCreditFunctionDataAdapter,
)


class FakeCursor:
    def __init__(self, dataset):
        self.dataset = dataset
        self.current = ""
        self.statements = []

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
                "snapshot_id": "200:200:",
                "snapshot_started_at": datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
            }
        if "FROM public.loan_application AS application" in self.current:
            return self.dataset.get("base")
        if "FROM public.credit_report" in self.current:
            return self.dataset.get("credit_report")
        if "FROM public.anti_fraud_report" in self.current:
            return self.dataset.get("anti_fraud_report")
        if "FROM public.risk_assessment_report" in self.current:
            return self.dataset.get("risk_assessment")
        if "FROM public.phone_verification_record" in self.current:
            return self.dataset.get("phone_verification")
        raise AssertionError(f"unexpected fetchone: {self.current}")

    def fetchall(self):
        if "FROM public.external_debt" in self.current:
            return self.dataset.get("external_debts", [])
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
    def __init__(self, dataset):
        self.cursor = FakeCursor(dataset)
        self.connection_count = 0

    def conn(self):
        self.connection_count += 1
        return FakeConnection(self.cursor)


def node(node_id, object_type, **fields):
    return {
        "id": node_id,
        "properties": {"props": json.dumps({
            **fields, "_ontology": {"object_type": object_type}
        }, ensure_ascii=False)},
    }


class FakeNebula:
    def __init__(self, *, truncated=False):
        self.closed = False
        self.truncated = truncated
        self.borrower = "abox:NaturalPerson:110101198803120011"
        self.other = "abox:NaturalPerson:110101198511090033"
        self.other_application = "abox:LoanApplication:APP20260805000003"

    def resolve_node_id(self, node_id):
        if node_id != "110101198803120011":
            raise AssertionError(node_id)
        return self.borrower

    def query_graph(self, query, hops, limit, offset, mode):
        self.query_args = (query, hops, limit, offset, mode)
        return {
            "nodes": [
                node(self.borrower, "NaturalPerson"),
                node(self.other, "NaturalPerson"),
                node(self.other_application, "LoanApplication", application_status="资料待补充"),
            ],
            "edges": [
                {"source": self.borrower, "target": self.other,
                 "properties": {"kind": "SHARES_CONTACT"}},
                {"source": self.other, "target": self.other_application,
                 "properties": {"kind": "SUBMITS"}},
            ],
            "page": {"edge_returned": 2, "result_truncated": self.truncated},
        }

    def close(self):
        self.closed = True


def dataset(**overrides):
    value = {
        "base": {
            "application_id": "APP20260801000001",
            "loan_amount": Decimal("200000"),
            "application_status": "评估中",
            "decision_result": "转人工",
            "borrower_id": "110101198803120011",
            "vehicle_vin": "LHGCM82633A000001",
            "monthly_income": Decimal("25000"),
            "invoice_price": Decimal("268000"),
            "guide_price": Decimal("321800"),
        },
        "credit_report": {
            "report_id": "CR20260001", "outstanding_loan_count": 2,
            "overdue_3y_count": 1, "total_overdue_count": 2, "inquiry_count": 5,
        },
        "external_debts": [{
            "debt_id": "DEBT20260001", "monthly_payment": Decimal("3200"),
            "overdue_status": "正常",
        }],
        "anti_fraud_report": {
            "fraud_report_id": "FR20260001", "fraud_score": Decimal("18.5"),
            "result": "通过", "hit_details": "未命中高风险规则",
        },
        "risk_assessment": {
            "assessment_id": "RA20260001", "risk_level": "中",
            "pd_value": Decimal("0.0315"), "risk_factors": "查询偏高",
            "check_result": "待人工复核",
        },
        "phone_verification": {
            "verification_id": "PV20260001", "conclusion": "信息基本一致",
            "doubts": "收入需补充银行流水",
        },
    }
    value.update(overrides)
    return value


class FunctionDataAdapterTests(unittest.TestCase):
    def make_adapter(self, values=None, *, truncated=False):
        postgres = FakePostgres(values or dataset())
        graph = FakeNebula(truncated=truncated)
        return PersonalCreditFunctionDataAdapter(postgres, lambda: graph), postgres, graph

    def test_postgres_context_uses_one_minimal_read_only_snapshot(self):
        adapter, postgres, _graph = self.make_adapter()
        snapshot = adapter.load_postgres_snapshot(" APP20260801000001 ")
        self.assertEqual(snapshot.snapshot_id, "200:200:")
        self.assertEqual(snapshot.borrower["monthly_income"], Decimal("25000"))
        self.assertEqual(snapshot.external_debts[0]["monthly_payment"], Decimal("3200"))
        self.assertEqual(postgres.connection_count, 1)
        statements = [statement for statement, _params in postgres.cursor.statements]
        self.assertIn("REPEATABLE READ, READ ONLY", statements[0])
        self.assertFalse(any("SELECT *" in statement.upper() for statement in statements))

    def test_execute_all_runs_every_function_without_business_write(self):
        adapter, postgres, graph = self.make_adapter()
        result = adapter.execute_all(
            "APP20260801000001", ontology_version="1.1.0",
            action_execution_id="ACTION001", actor="unit-test",
        )
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["cross_store_atomic"])
        self.assertEqual(len(result["functions"]), 9)
        self.assertEqual(set(result["execution_order"]), set(result["functions"]))
        self.assertEqual(result["functions"]["calculate_dti"]["result"]["dti"], 0.128)
        self.assertTrue(result["functions"]["detect_association_risk"]["result"]["association_paths"])
        self.assertIn(
            result["functions"]["aggregate_decision_support"]["result"]["recommended_decision"],
            {"转人工", "拒绝", "补充材料", "通过"},
        )
        self.assertEqual(postgres.connection_count, 1)
        self.assertTrue(graph.closed)
        sql = " ".join(statement for statement, _params in postgres.cursor.statements).upper()
        for forbidden in (" INSERT ", " UPDATE ", " DELETE ", " TRUNCATE "):
            self.assertNotIn(forbidden, " " + sql + " ")

    def test_risk_action_adapter_executes_only_the_five_declared_functions(self):
        adapter, postgres, graph = self.make_adapter()
        result = adapter.execute_risk_assessment(
            "APP20260801000001",
            ontology_version="1.1.0",
            action_execution_id="ACTION002",
            actor="unit-test",
        )
        self.assertEqual(tuple(result["functions"]), (
            "calculate_dti", "calculate_ltv", "evaluate_credit_risk",
            "evaluate_fraud_risk", "assess_application_risk",
        ))
        self.assertEqual(postgres.connection_count, 1)
        self.assertFalse(graph.closed)
        self.assertTrue(result["cross_store_atomic"])
        self.assertIsNone(result["graph_snapshot_time"])

    def test_fraud_action_adapter_executes_only_association_risk(self):
        adapter, postgres, graph = self.make_adapter()
        result = adapter.execute_association_risk(
            "APP20260801000001",
            ontology_version="1.1.0",
            action_execution_id="ACTION003",
            actor="unit-test",
        )
        self.assertEqual(result["execution_order"], ["detect_association_risk"])
        self.assertEqual(tuple(result["functions"]), ("detect_association_risk",))
        self.assertTrue(
            result["functions"]["detect_association_risk"]["result"][
                "association_paths"
            ]
        )
        self.assertEqual(postgres.connection_count, 1)
        self.assertTrue(graph.closed)
        self.assertFalse(result["cross_store_atomic"])

    def test_phone_task_adapter_executes_only_question_generation(self):
        adapter, postgres, graph = self.make_adapter()
        result = adapter.execute_phone_questions(
            "APP20260801000001",
            ontology_version="1.1.0",
            action_execution_id="ACTION004",
            actor="unit-test",
        )
        self.assertEqual(result["execution_order"], ["generate_phone_questions"])
        self.assertEqual(tuple(result["functions"]), ("generate_phone_questions",))
        question_result = result["functions"]["generate_phone_questions"]["result"]
        self.assertEqual(question_result["question_count"], 1)
        self.assertEqual(question_result["phone_questions"][0]["category"], "征信查询")
        self.assertEqual(postgres.connection_count, 1)
        self.assertFalse(graph.closed)
        self.assertTrue(result["cross_store_atomic"])

    def test_manual_review_adapter_executes_only_decision_and_evidence_functions(self):
        adapter, postgres, graph = self.make_adapter()
        result = adapter.execute_decision_support(
            "APP20260801000001",
            ontology_version="1.1.0",
            action_execution_id="ACTION005",
            actor="unit-test",
        )
        self.assertEqual(tuple(result["functions"]), (
            "aggregate_decision_support", "build_evidence_explanation",
        ))
        self.assertEqual(
            result["functions"]["aggregate_decision_support"]["result"][
                "recommended_decision"
            ],
            "转人工",
        )
        lineage = result["functions"]["build_evidence_explanation"]["result"]["lineage"]
        self.assertEqual(lineage["action_execution_id"], "ACTION005")
        self.assertFalse(lineage["missing_sources"])
        self.assertEqual(postgres.connection_count, 1)
        self.assertFalse(graph.closed)

    def test_graph_query_is_closed_and_uses_typed_borrower_seed(self):
        adapter, _postgres, graph = self.make_adapter()
        result = adapter.load_association_graph("110101198803120011")
        self.assertTrue(graph.closed)
        self.assertEqual(graph.query_args[0], graph.borrower)
        self.assertEqual(graph.query_args[1], 2)
        self.assertEqual(result["query_coverage"]["candidate_count"], 2)

    def test_truncated_graph_fails_closed_before_combined_assessment(self):
        adapter, _postgres, _graph = self.make_adapter(truncated=True)
        with self.assertRaisesRegex(FunctionContractError, "数据不完整"):
            adapter.execute_all("APP20260801000001", ontology_version="1.1.0")

    def test_missing_credit_report_fails_closed(self):
        adapter, _postgres, _graph = self.make_adapter(dataset(credit_report=None))
        with self.assertRaises(FunctionContractError):
            adapter.execute_all("APP20260801000001", ontology_version="1.1.0")

    def test_missing_monthly_payment_never_uses_balance(self):
        debts = [{
            "debt_id": "DEBT20260001", "monthly_payment": None,
            "overdue_status": "正常", "balance": Decimal("80000"),
        }]
        adapter, _postgres, _graph = self.make_adapter(dataset(external_debts=debts))
        with self.assertRaises(FunctionContractError):
            adapter.execute_all("APP20260801000001", ontology_version="1.1.0")

    def test_unknown_or_structurally_incomplete_application_is_rejected(self):
        adapter, _postgres, _graph = self.make_adapter(dataset(base=None))
        with self.assertRaisesRegex(FunctionDataAdapterError, "贷款申请不存在"):
            adapter.load_postgres_snapshot("APP404")
        broken = dataset(); broken["base"] = {**broken["base"], "vehicle_vin": None}
        adapter, _postgres, _graph = self.make_adapter(broken)
        with self.assertRaisesRegex(FunctionDataAdapterError, "缺少车辆关联"):
            adapter.load_postgres_snapshot("APP20260801000001")

    def test_application_identifier_is_validated_before_read(self):
        adapter, postgres, _graph = self.make_adapter()
        for value in ("", " ", "A" * 33):
            with self.subTest(value=value), self.assertRaises(FunctionDataAdapterError):
                adapter.load_postgres_snapshot(value)
        self.assertEqual(postgres.connection_count, 0)


if __name__ == "__main__":
    unittest.main()
