import unittest
from decimal import Decimal

from app.action import (
    DTI_IMPLEMENTATION_VERSION,
    LTV_IMPLEMENTATION_VERSION,
    FunctionAlreadyBoundError,
    FunctionContractError,
    FunctionExecutionContext,
    FunctionNotBoundError,
    bind_personal_credit_ratio_functions,
    build_personal_credit_function_registry,
    build_personal_credit_ratio_registry,
)


def context():
    return FunctionExecutionContext(
        ontology_version="1.0.0",
        action_execution_id="ratio-test-1",
        actor="unit-test",
        data_snapshot_time="2026-08-24T10:00:00+08:00",
    )


class RatioFunctionTests(unittest.TestCase):
    def test_ratio_builder_binds_only_dti_and_ltv(self):
        registry = build_personal_credit_ratio_registry()
        self.assertEqual(registry.bound_names(), ("calculate_dti", "calculate_ltv"))
        self.assertEqual(
            registry.describe("calculate_dti")["implementation_version"],
            DTI_IMPLEMENTATION_VERSION,
        )
        self.assertEqual(
            registry.describe("calculate_ltv")["implementation_version"],
            LTV_IMPLEMENTATION_VERSION,
        )
        self.assertEqual(
            registry.describe("calculate_dti")["contract"]["reads_properties"],
            ["NaturalPerson.monthly_income", "ExternalDebt.monthly_payment"],
        )
        with self.assertRaises(FunctionNotBoundError):
            registry.execute("evaluate_credit_risk", {}, context())

    def test_dti_uses_monthly_payments_and_decimal_rounding(self):
        registry = build_personal_credit_ratio_registry()
        envelope = registry.execute(
            "calculate_dti",
            {
                "monthly_income": Decimal("20000"),
                "monthly_debt_payments": [Decimal("3200"), "2700", 3500],
            },
            context(),
        )
        self.assertEqual(envelope["result"]["dti"], 0.47)
        self.assertEqual(envelope["result"]["total_monthly_payment"], 9400.0)
        self.assertEqual(envelope["result"]["debt_item_count"], 3)
        self.assertEqual(envelope["confidence"], 1.0)
        self.assertTrue(envelope["complete"])

    def test_dti_without_existing_debt_is_zero(self):
        registry = build_personal_credit_ratio_registry()
        result = registry.execute(
            "calculate_dti",
            {"monthly_income": 18000, "monthly_debt_payments": []},
            context(),
        )["result"]
        self.assertEqual(result["dti"], 0.0)
        self.assertEqual(result["total_monthly_payment"], 0.0)

    def test_dti_rejects_balance_substitution_or_invalid_income(self):
        registry = build_personal_credit_ratio_registry()
        for inputs in (
            {"monthly_income": 20000, "debt_balances": [80000]},
            {"monthly_income": 0, "monthly_debt_payments": [1000]},
            {"monthly_income": -1, "monthly_debt_payments": [1000]},
            {"monthly_income": True, "monthly_debt_payments": [1000]},
        ):
            with self.subTest(inputs=inputs), self.assertRaises(FunctionContractError):
                registry.execute("calculate_dti", inputs, context())

    def test_dti_rejects_incomplete_or_negative_monthly_payments(self):
        registry = build_personal_credit_ratio_registry()
        for payments in ([1000, None], [1000, -1], [1000, float("nan")], "1000"):
            with self.subTest(payments=payments), self.assertRaises(FunctionContractError):
                registry.execute(
                    "calculate_dti",
                    {"monthly_income": 20000, "monthly_debt_payments": payments},
                    context(),
                )

    def test_ltv_uses_lower_of_invoice_and_guide_price(self):
        registry = build_personal_credit_ratio_registry()
        result = registry.execute(
            "calculate_ltv",
            {"loan_amount": 164000, "invoice_price": 200000, "guide_price": 210000},
            context(),
        )["result"]
        self.assertEqual(result["ltv"], 0.82)
        self.assertEqual(result["vehicle_value"], 200000.0)
        self.assertEqual(result["value_source"], "invoice_price")
        equal_prices = registry.execute(
            "calculate_ltv",
            {"loan_amount": 100000, "invoice_price": 200000, "guide_price": 200000},
            context(),
        )["result"]
        self.assertEqual(equal_prices["value_source"], "invoice_price")

    def test_ltv_selects_lower_guide_price_and_supports_single_price(self):
        registry = build_personal_credit_ratio_registry()
        lower_guide = registry.execute(
            "calculate_ltv",
            {"loan_amount": 150000, "invoice_price": 220000, "guide_price": 200000},
            context(),
        )["result"]
        self.assertEqual(lower_guide["ltv"], 0.75)
        self.assertEqual(lower_guide["value_source"], "guide_price")
        invoice_only = registry.execute(
            "calculate_ltv",
            {"loan_amount": 150000, "invoice_price": 200000, "guide_price": None},
            context(),
        )["result"]
        self.assertEqual(invoice_only["ltv"], 0.75)
        self.assertEqual(invoice_only["value_source"], "invoice_price")

    def test_ltv_rejects_missing_or_invalid_monetary_values(self):
        registry = build_personal_credit_ratio_registry()
        for inputs in (
            {"loan_amount": 100000},
            {"loan_amount": 0, "invoice_price": 200000},
            {"loan_amount": 100000, "invoice_price": 0},
            {"loan_amount": 100000, "invoice_price": float("inf")},
            {"loan_amount": True, "invoice_price": 200000},
        ):
            with self.subTest(inputs=inputs), self.assertRaises(FunctionContractError):
                registry.execute("calculate_ltv", inputs, context())

    def test_binding_entry_is_explicit_and_requires_replace(self):
        registry = build_personal_credit_function_registry()
        bind_personal_credit_ratio_functions(registry)
        with self.assertRaises(FunctionAlreadyBoundError):
            bind_personal_credit_ratio_functions(registry)
        bind_personal_credit_ratio_functions(registry, replace=True)
        self.assertEqual(registry.bound_names(), ("calculate_dti", "calculate_ltv"))


if __name__ == "__main__":
    unittest.main()
