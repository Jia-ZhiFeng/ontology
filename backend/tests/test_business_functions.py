import json
import unittest

from app.action import (
    FunctionAlreadyBoundError,
    FunctionContractError,
    FunctionExecutionContext,
    bind_personal_credit_business_functions,
    build_personal_credit_function_registry,
    build_personal_credit_readonly_registry,
)


def context():
    return FunctionExecutionContext(
        ontology_version="1.1.0",
        action_execution_id="FUNCTION-TEST",
        actor="unit-test",
        data_snapshot_time="2026-08-24T12:00:00+08:00",
    )


def credit_report(**overrides):
    value = {
        "report_id": "CR001",
        "outstanding_loan_count": 0,
        "overdue_3y_count": 0,
        "total_overdue_count": 0,
        "inquiry_count": 0,
    }
    value.update(overrides)
    return value


def fraud_report(**overrides):
    value = {
        "fraud_report_id": "FR001",
        "fraud_score": 10,
        "result": "通过",
        "hit_details": "未命中高风险规则",
    }
    value.update(overrides)
    return value


def envelope(result, *, complete=True):
    return {"status": "success", "complete": complete, "result": result}


def graph_node(node_id, object_type, **data):
    payload = {
        **data,
        "_ontology": {"object_type": object_type},
    }
    return {
        "id": node_id,
        "properties": {"props": json.dumps(payload, ensure_ascii=False)},
    }


class BusinessFunctionTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_personal_credit_readonly_registry()

    def execute(self, name, inputs):
        return self.registry.execute(name, inputs, context())

    def test_readonly_registry_explicitly_binds_all_nine_functions(self):
        self.assertEqual(len(self.registry.bound_names()), 9)
        self.assertEqual(set(self.registry.names()), set(self.registry.bound_names()))
        for name in self.registry.names():
            self.assertIsNotNone(self.registry.describe(name)["implementation_version"])

    def test_business_binding_rejects_collision_without_replace(self):
        registry = build_personal_credit_function_registry()
        bind_personal_credit_business_functions(registry)
        with self.assertRaises(FunctionAlreadyBoundError):
            bind_personal_credit_business_functions(registry)
        bind_personal_credit_business_functions(registry, replace=True)
        self.assertEqual(len(registry.bound_names()), 7)

    def test_credit_risk_returns_ordered_explainable_signals(self):
        result = self.execute("evaluate_credit_risk", {
            "credit_report": credit_report(
                outstanding_loan_count=5,
                overdue_3y_count=3,
                total_overdue_count=5,
                inquiry_count=6,
            ),
            "external_debts": [{"debt_id": "D001", "overdue_status": "逾期90天以上"}],
        })["result"]
        self.assertEqual(result["max_risk_level"], "HIGH")
        self.assertGreaterEqual(result["signal_count"], 5)
        self.assertTrue(any(item["hard_stop"] for item in result["credit_risk_signals"]))
        self.assertTrue(all(item["evidence"] for item in result["credit_risk_signals"]))

    def test_credit_risk_clean_report_has_no_positive_signal(self):
        result = self.execute("evaluate_credit_risk", {
            "credit_report": credit_report(), "external_debts": []
        })["result"]
        self.assertEqual(result["credit_risk_signals"], [])
        self.assertEqual(result["max_risk_level"], "LOW")

    def test_credit_risk_fails_on_missing_count_or_unknown_debt_status(self):
        broken = credit_report(); broken["inquiry_count"] = None
        with self.assertRaises(FunctionContractError):
            self.execute("evaluate_credit_risk", {"credit_report": broken, "external_debts": []})
        with self.assertRaises(FunctionContractError):
            self.execute("evaluate_credit_risk", {
                "credit_report": credit_report(),
                "external_debts": [{"debt_id": "D001", "overdue_status": "未知"}],
            })

    def test_fraud_risk_hard_reject_and_score_are_explainable(self):
        result = self.execute("evaluate_fraud_risk", {
            "anti_fraud_report": fraud_report(fraud_score=91, result="拒绝", hit_details="设备黑名单"),
            "association_paths": [],
            "query_coverage": {"candidate_count": 0, "evaluated_count": 0, "truncated": False},
        })["result"]
        self.assertEqual(result["max_risk_level"], "HIGH")
        self.assertTrue(any(item["hard_stop"] for item in result["fraud_risk_signals"]))

    def test_fraud_risk_uses_association_and_propagates_truncation(self):
        response = self.execute("evaluate_fraud_risk", {
            "anti_fraud_report": fraud_report(),
            "association_paths": [{"nodes": ["P1", "P2"], "target_status": "评估中"}],
            "query_coverage": {"candidate_count": 5, "evaluated_count": 1, "truncated": True},
        })
        self.assertFalse(response["complete"])
        self.assertEqual(response["confidence"], 0.7)
        self.assertTrue(response["warnings"])
        self.assertEqual(response["result"]["max_risk_level"], "MEDIUM")

    def test_fraud_risk_rejects_invalid_score_or_result(self):
        for report in (fraud_report(fraud_score=101), fraud_report(result="未知")):
            with self.subTest(report=report), self.assertRaises(FunctionContractError):
                self.execute("evaluate_fraud_risk", {
                    "anti_fraud_report": report,
                    "association_paths": [], "query_coverage": {},
                })

    def test_assessment_combines_four_preceding_results(self):
        response = self.execute("assess_application_risk", {
            "application_id": "APP001",
            "dti_result": envelope({"dti": 0.47}),
            "ltv_result": envelope({"ltv": 0.88}),
            "credit_risk_result": envelope({"credit_risk_signals": [
                {"signal_code": "OVERDUE_HISTORY", "risk_level": "MEDIUM", "hard_stop": False}
            ]}),
            "fraud_risk_result": envelope({"fraud_risk_signals": []}),
        })
        result = response["result"]
        self.assertGreaterEqual(result["pd_value"], 0.17)
        self.assertIn(result["risk_level"], {"中", "高"})
        self.assertEqual(result["check_result"], "待人工复核")
        self.assertFalse(result["model"]["calibrated_pd"])
        self.assertTrue(response["warnings"])

    def test_assessment_hard_stop_is_reject_risk_not_final_approval(self):
        result = self.execute("assess_application_risk", {
            "application_id": "APP001",
            "dti_result": envelope({"dti": 0.1}),
            "ltv_result": envelope({"ltv": 0.5}),
            "credit_risk_result": envelope({"credit_risk_signals": []}),
            "fraud_risk_result": envelope({"fraud_risk_signals": [
                {"signal_code": "FRAUD_REPORT_REJECT", "risk_level": "HIGH", "hard_stop": True}
            ]}),
        })["result"]
        self.assertEqual(result["risk_level"], "拒绝")
        self.assertEqual(result["check_result"], "不通过")
        self.assertIn("not_final_approval", result["decision_boundary"])

    def test_assessment_refuses_incomplete_preceding_function(self):
        with self.assertRaises(FunctionContractError):
            self.execute("assess_application_risk", {
                "application_id": "APP001",
                "dti_result": envelope({"dti": 0.1}),
                "ltv_result": envelope({"ltv": 0.5}),
                "credit_risk_result": envelope({"credit_risk_signals": []}),
                "fraud_risk_result": envelope({"fraud_risk_signals": []}, complete=False),
            })

    def test_phone_questions_are_deduplicated_and_traceable(self):
        result = self.execute("generate_phone_questions", {
            "risk_factors": [
                {"factor": "DTI_HIGH"}, {"factor": "DTI_VERY_HIGH"},
                {"factor": "CREDIT_INQUIRY_HIGH"},
            ],
            "fraud_hit_details": "设备关联多个申请人",
            "association_risk_signals": [{"signal_code": "SHARED_CONTACT_PATH"}],
        })["result"]
        self.assertEqual(result["question_count"], 4)
        self.assertEqual([item["question_id"] for item in result["phone_questions"]], ["Q01", "Q02", "Q03", "Q04"])

    def test_phone_questions_have_safe_fallback(self):
        result = self.execute("generate_phone_questions", {
            "risk_factors": [], "fraud_hit_details": "未命中", "association_risk_signals": []
        })["result"]
        self.assertEqual(result["question_count"], 1)

    def test_association_detects_shared_contact_paths_and_masks_people(self):
        p1 = "abox:NaturalPerson:110101199001011234"
        p2 = "abox:NaturalPerson:110101199001011235"
        app = "abox:LoanApplication:APP009"
        result = self.execute("detect_association_risk", {
            "borrower_node_id": p1,
            "graph_nodes": [
                graph_node(p1, "NaturalPerson"), graph_node(p2, "NaturalPerson"),
                graph_node(app, "LoanApplication", application_status="评估中"),
            ],
            "graph_edges": [
                {"source": p1, "target": p2, "properties": {"kind": "SHARES_CONTACT"}},
                {"source": p2, "target": app, "properties": {"kind": "SUBMITS"}},
            ],
            "hops": 2,
            "query_coverage": {"candidate_count": 2, "evaluated_count": 2, "truncated": False},
        })["result"]
        self.assertEqual(len(result["association_paths"]), 2)
        self.assertTrue(all("110101" not in node for path in result["association_paths"] for node in path["nodes"]))
        self.assertTrue(result["association_risk_signals"])

    def test_association_ignores_non_risk_relations(self):
        p1 = "abox:NaturalPerson:P1"; app = "abox:LoanApplication:APP1"
        result = self.execute("detect_association_risk", {
            "borrower_node_id": p1,
            "graph_nodes": [graph_node(p1, "NaturalPerson"), graph_node(app, "LoanApplication")],
            "graph_edges": [{"source": p1, "target": app, "properties": {"kind": "SUBMITS"}}],
            "hops": 2, "query_coverage": {},
        })["result"]
        self.assertEqual(result["association_paths"], [])
        self.assertEqual(result["association_risk_signals"], [])

    def test_association_truncation_is_explicit(self):
        p1 = "abox:NaturalPerson:P1"
        response = self.execute("detect_association_risk", {
            "borrower_node_id": p1,
            "graph_nodes": [graph_node(p1, "NaturalPerson")],
            "graph_edges": [], "hops": 2,
            "query_coverage": {"truncated": True},
        })
        self.assertFalse(response["complete"])
        self.assertTrue(response["result"]["query_coverage"]["truncated"])

    def test_decision_support_passes_only_clean_complete_combination(self):
        response = self.execute("aggregate_decision_support", {
            "application": {"application_id": "APP001"},
            "risk_assessment": {"assessment_id": "RA1", "risk_level": "低", "pd_value": 0.02},
            "anti_fraud_report": {"fraud_report_id": "FR1", "result": "通过"},
            "phone_verification": {"verification_id": "PV1", "conclusion": "通过"},
        })
        self.assertEqual(response["result"]["recommended_decision"], "通过")
        self.assertIn("not_final_approval", response["result"]["decision_boundary"])
        self.assertTrue(response["warnings"])

    def test_decision_support_escalates_or_requests_material(self):
        base = {
            "application": {"application_id": "APP001"},
            "risk_assessment": {"assessment_id": "RA1", "risk_level": "低", "pd_value": 0.02},
            "anti_fraud_report": {"fraud_report_id": "FR1", "result": "通过"},
        }
        material = self.execute("aggregate_decision_support", {**base, "phone_verification": None})["result"]
        review = self.execute("aggregate_decision_support", {
            **base,
            "anti_fraud_report": {"fraud_report_id": "FR1", "result": "人工复核"},
            "phone_verification": {"verification_id": "PV1", "conclusion": "通过"},
        })["result"]
        self.assertEqual(material["recommended_decision"], "补充材料")
        self.assertEqual(review["recommended_decision"], "转人工")

    def test_decision_support_escalates_when_doubts_exist_even_without_doubt_conclusion(self):
        result = self.execute("aggregate_decision_support", {
            "application": {"application_id": "APP001"},
            "risk_assessment": {
                "assessment_id": "RA1", "risk_level": "低", "pd_value": 0.02,
            },
            "anti_fraud_report": {"fraud_report_id": "FR1", "result": "通过"},
            "phone_verification": {
                "verification_id": "PV1", "conclusion": "信息基本一致",
                "doubts": "收入仍需核验",
            },
        })["result"]
        self.assertEqual(result["recommended_decision"], "转人工")
        self.assertIn("PHONE_DOUBT", [item["code"] for item in result["decision_reasons"]])

    def test_decision_support_preserves_hard_reject_as_recommendation(self):
        result = self.execute("aggregate_decision_support", {
            "application": {"application_id": "APP001"},
            "risk_assessment": {"assessment_id": "RA1", "risk_level": "拒绝", "pd_value": 0.3},
            "anti_fraud_report": {"fraud_report_id": "FR1", "result": "通过"},
            "phone_verification": {"verification_id": "PV1", "conclusion": "通过"},
        })["result"]
        self.assertEqual(result["recommended_decision"], "拒绝")

    def test_evidence_explanation_builds_lineage_without_raw_sensitive_values(self):
        decision = envelope({"recommended_decision": "转人工", "decision_reasons": []})
        response = self.execute("build_evidence_explanation", {
            "application": {"application_id": "APP001", "application_status": "评估中"},
            "risk_assessment": {"assessment_id": "RA1", "risk_level": "中", "pd_value": 0.1},
            "anti_fraud_report": {"fraud_report_id": "FR1", "result": "人工复核"},
            "phone_verification": {"verification_id": "PV1", "conclusion": "存疑"},
            "decision_support": decision,
        })
        self.assertTrue(response["complete"])
        self.assertEqual(len(response["result"]["evidence_chain"]), 5)
        self.assertEqual(response["result"]["lineage"]["ontology_version"], "1.1.0")
        serialized = json.dumps(response, ensure_ascii=False)
        self.assertNotIn("id_number", serialized)
        self.assertNotIn("phone", serialized)

    def test_evidence_explanation_marks_missing_sources_incomplete(self):
        response = self.execute("build_evidence_explanation", {
            "application": {"application_id": "APP001"},
            "risk_assessment": None,
            "anti_fraud_report": None,
            "phone_verification": None,
            "decision_support": None,
        })
        self.assertFalse(response["complete"])
        self.assertGreaterEqual(len(response["result"]["lineage"]["missing_sources"]), 4)


if __name__ == "__main__":
    unittest.main()
