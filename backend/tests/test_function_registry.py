import unittest

from app.action.function_registry import (
    FunctionAlreadyBoundError,
    FunctionContractError,
    FunctionExecutionContext,
    FunctionExecutionError,
    FunctionNotBoundError,
    FunctionOutput,
    FunctionRegistry,
    UnknownFunctionError,
    build_personal_credit_function_registry,
)
from app.ontology.baseline import build_personal_credit_baseline


def context():
    return FunctionExecutionContext(
        ontology_version="1.0.0",
        action_execution_id="action-test-1",
        actor="unit-test",
        data_snapshot_time="2026-08-24T00:00:00+08:00",
    )


def dti_output(value=0.4):
    return FunctionOutput(
        result={"dti": value},
        evidence=("NaturalPerson.monthly_income", "ExternalDebt.balance"),
        confidence=1.0,
    )


class FunctionRegistryTests(unittest.TestCase):
    def test_personal_credit_registry_starts_with_nine_unbound_contracts(self):
        registry = build_personal_credit_function_registry()
        self.assertEqual(len(registry), 9)
        self.assertEqual(registry.bound_names(), ())
        description = registry.describe("calculate_dti")
        self.assertFalse(description["bound"])
        self.assertEqual(description["contract"]["returns"], ["dti"])
        self.assertEqual(description["contract"]["implementation_status"], "not_bound")

    def test_unknown_function_and_unbound_execution_are_rejected(self):
        registry = build_personal_credit_function_registry()
        with self.assertRaises(UnknownFunctionError):
            registry.describe("not_in_catalog")
        with self.assertRaises(FunctionNotBoundError):
            registry.execute("calculate_dti", {}, context())

    def test_bound_function_returns_standard_validated_envelope(self):
        registry = build_personal_credit_function_registry()
        source_inputs = {"monthly_income": 20000, "balance": 8000}

        def implementation(inputs, execution_context):
            inputs["monthly_income"] = 0
            self.assertEqual(execution_context.ontology_version, "1.0.0")
            return dti_output()

        registry.bind("calculate_dti", implementation, "dti-test-1.0")
        envelope = registry.execute("calculate_dti", source_inputs, context())
        self.assertEqual(source_inputs["monthly_income"], 20000)
        self.assertEqual(envelope["function_name"], "calculate_dti")
        self.assertEqual(envelope["result"], {"dti": 0.4})
        self.assertEqual(envelope["implementation_version"], "dti-test-1.0")
        self.assertEqual(envelope["data_snapshot_time"], "2026-08-24T00:00:00+08:00")
        self.assertTrue(envelope["complete"])
        self.assertIsInstance(envelope["duration_ms"], int)

    def test_duplicate_binding_requires_explicit_replace(self):
        registry = build_personal_credit_function_registry()
        registry.bind("calculate_dti", lambda inputs, ctx: dti_output(0.4), "v1")
        with self.assertRaises(FunctionAlreadyBoundError):
            registry.bind("calculate_dti", lambda inputs, ctx: dti_output(0.5), "v2")
        registry.bind(
            "calculate_dti",
            lambda inputs, ctx: dti_output(0.5),
            "v2",
            replace=True,
        )
        self.assertEqual(registry.execute("calculate_dti", {}, context())["result"]["dti"], 0.5)
        self.assertTrue(registry.unbind("calculate_dti"))
        self.assertFalse(registry.unbind("calculate_dti"))

    def test_binding_requires_callable_and_version(self):
        registry = build_personal_credit_function_registry()
        with self.assertRaises(FunctionContractError):
            registry.bind("calculate_dti", None, "v1")
        with self.assertRaises(FunctionContractError):
            registry.bind("calculate_dti", lambda inputs, ctx: dti_output(), " ")
        with self.assertRaises(FunctionContractError):
            registry.bind("calculate_dti", lambda inputs, ctx: dti_output(), 1)
        with self.assertRaises(FunctionContractError):
            FunctionExecutionContext(ontology_version=" ")

    def test_output_must_use_function_output_and_include_declared_fields(self):
        registry = build_personal_credit_function_registry()
        registry.bind("calculate_dti", lambda inputs, ctx: {"dti": 0.4}, "bad-v1")
        with self.assertRaises(FunctionContractError):
            registry.execute("calculate_dti", {}, context())
        registry.bind(
            "calculate_dti",
            lambda inputs, ctx: FunctionOutput(result={"dti": 0.4}, evidence="bad"),
            "bad-evidence",
            replace=True,
        )
        with self.assertRaises(FunctionContractError):
            registry.execute("calculate_dti", {}, context())
        registry.bind(
            "calculate_dti",
            lambda inputs, ctx: FunctionOutput(result={}),
            "bad-v2",
            replace=True,
        )
        with self.assertRaises(FunctionContractError):
            registry.execute("calculate_dti", {}, context())

    def test_confidence_and_warning_contracts_are_validated(self):
        registry = build_personal_credit_function_registry()
        registry.bind(
            "calculate_dti",
            lambda inputs, ctx: FunctionOutput(result={"dti": 0.4}, confidence=1.1),
            "bad-confidence",
        )
        with self.assertRaises(FunctionContractError):
            registry.execute("calculate_dti", {}, context())
        registry.bind(
            "calculate_dti",
            lambda inputs, ctx: FunctionOutput(result={"dti": 0.4}, warnings=(1,)),
            "bad-warning",
            replace=True,
        )
        with self.assertRaises(FunctionContractError):
            registry.execute("calculate_dti", {}, context())

    def test_implementation_failure_is_wrapped_without_leaking_details(self):
        registry = build_personal_credit_function_registry()

        def failure(inputs, execution_context):
            raise RuntimeError("sensitive downstream response")

        registry.bind("calculate_dti", failure, "failing-v1")
        with self.assertRaises(FunctionExecutionError) as raised:
            registry.execute("calculate_dti", {}, context())
        self.assertNotIn("sensitive downstream response", str(raised.exception))

    def test_registry_rejects_duplicate_or_side_effecting_contracts(self):
        definition = build_personal_credit_baseline().functions[0]
        with self.assertRaises(FunctionContractError):
            FunctionRegistry([definition, definition])
        unsafe = definition.model_copy(update={"side_effects": True})
        with self.assertRaises(FunctionContractError):
            FunctionRegistry([unsafe])


if __name__ == "__main__":
    unittest.main()
