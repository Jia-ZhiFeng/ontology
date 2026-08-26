import unittest
from dataclasses import replace

from app.action import (
    ACTION_RUNTIME_DESIGNS,
    ACTION_STATE_TRANSITIONS,
    TERMINAL_ACTION_STATES,
    ActionStepDesign,
    validate_action_design_catalog,
)
from app.ontology.baseline import build_personal_credit_baseline


class ActionDesignCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proposal = build_personal_credit_baseline()
        cls.actions = {item.name: item for item in cls.proposal.action_types}
        cls.security = {item.action: item for item in cls.proposal.dynamic_security}

    def test_catalog_covers_all_eight_tbox_actions(self):
        self.assertEqual(len(ACTION_RUNTIME_DESIGNS), 8)
        self.assertEqual(set(ACTION_RUNTIME_DESIGNS), set(self.actions))
        self.assertEqual(validate_action_design_catalog(self.proposal), [])

    def test_common_state_machine_has_explicit_terminal_states(self):
        self.assertEqual(
            TERMINAL_ACTION_STATES,
            {"SUCCEEDED", "FAILED", "COMPENSATED", "COMPENSATION_FAILED"},
        )
        for state, targets in ACTION_STATE_TRANSITIONS.items():
            for target in targets:
                self.assertIn(target, ACTION_STATE_TRANSITIONS, (state, target))
        for state in TERMINAL_ACTION_STATES:
            self.assertEqual(ACTION_STATE_TRANSITIONS[state], ())

    def test_idempotency_fields_exactly_match_tbox_and_request_contracts(self):
        for name, design in ACTION_RUNTIME_DESIGNS.items():
            expected = tuple(self.actions[name].idempotency_key.split("+"))
            self.assertEqual(design.idempotency_fields, expected, name)
            self.assertTrue(set(expected).issubset(design.request_fields), name)
            self.assertEqual(
                sum(step.kind == "idempotency" for step in design.steps),
                1,
                name,
            )

    def test_function_and_effect_steps_exactly_match_tbox_contracts(self):
        for name, design in ACTION_RUNTIME_DESIGNS.items():
            functions = {step.function_name for step in design.steps if step.function_name}
            effects = {step.effect for step in design.steps if step.effect}
            self.assertEqual(functions, set(self.actions[name].invokes_functions), name)
            self.assertEqual(effects, set(self.actions[name].effects), name)
            for step in design.steps:
                if step.effect:
                    self.assertEqual(step.kind, "commit")
                    self.assertEqual(step.max_attempts, 1)

    def test_external_boundaries_and_retries_are_explicit(self):
        for name, design in ACTION_RUNTIME_DESIGNS.items():
            systems = {step.external_system for step in design.steps if step.external_system}
            expected = (
                {self.actions[name].external_system}
                if self.actions[name].external_system
                else set()
            )
            self.assertEqual(systems, expected, name)
            for step in design.steps:
                if step.max_attempts > 1:
                    self.assertIn(step.kind, {"external_call", "external_dispatch"})
                    self.assertNotEqual(step.failure_policy, "fail_closed")

    def test_every_action_has_audit_error_and_compensation_contracts(self):
        for name, design in ACTION_RUNTIME_DESIGNS.items():
            self.assertTrue(any(step.kind == "audit" for step in design.steps), name)
            self.assertTrue(
                {"action_received", "action_succeeded", "action_failed"}.issubset(
                    design.audit_events
                ),
                name,
            )
            self.assertTrue(design.error_codes, name)
            self.assertTrue(all(code.startswith("ACTION_") for code in design.error_codes))
            self.assertTrue(design.compensation, name)
            self.assertTrue(design.consistency_boundary, name)

    def test_only_recommendation_action_has_human_gate(self):
        for name, design in ACTION_RUNTIME_DESIGNS.items():
            gates = [step for step in design.steps if step.kind == "manual_gate"]
            if self.security[name].approval_required:
                self.assertEqual(len(gates), 1, name)
                self.assertTrue(design.approval_condition)
            else:
                self.assertEqual(gates, [], name)
        recommendation = ACTION_RUNTIME_DESIGNS["record_decision_recommendation"]
        self.assertIn("decision_authority", recommendation.response_fields)
        self.assertNotIn("execute_approval", ACTION_RUNTIME_DESIGNS)
        self.assertNotIn("approve_application", ACTION_RUNTIME_DESIGNS)

    def test_validator_detects_a_cycle(self):
        original = ACTION_RUNTIME_DESIGNS["create_loan_application"]
        first = original.steps[0]
        cyclic_first = replace(first, depends_on=(original.steps[-1].name,))
        invalid = dict(ACTION_RUNTIME_DESIGNS)
        invalid[original.action_name] = replace(
            original,
            steps=(cyclic_first,) + original.steps[1:],
        )
        errors = validate_action_design_catalog(self.proposal, invalid)
        self.assertTrue(any("DAG 存在环" in error for error in errors), errors)

    def test_validator_detects_undeclared_function_and_effect(self):
        original = ACTION_RUNTIME_DESIGNS["create_loan_application"]
        invalid = dict(ACTION_RUNTIME_DESIGNS)
        invalid[original.action_name] = replace(
            original,
            steps=original.steps
            + (
                ActionStepDesign(
                    name="unexpected_function",
                    kind="function",
                    function_name="calculate_dti",
                ),
                ActionStepDesign(
                    name="unexpected_write",
                    kind="commit",
                    effect="approve application",
                ),
            ),
        )
        errors = validate_action_design_catalog(self.proposal, invalid)
        self.assertTrue(any("Function 覆盖" in error for error in errors), errors)
        self.assertTrue(any("副作用覆盖" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
