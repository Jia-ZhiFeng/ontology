import json
import math
import unittest

from app.agent.ontology_decisions import (
    AskUserDecision,
    CallReadActionsDecision,
    FinalDecision,
    LookupOntologyDecision,
    MAX_DECISION_BYTES,
    OntologyAgentDecisionParseError,
    ProposeWriteActionDecision,
    ontology_agent_decision_instruction,
    ontology_agent_decision_json_schema,
    parse_ontology_agent_decision,
)


class OntologyAgentDecisionProtocolTests(unittest.TestCase):
    def assert_error(self, code, payload):
        with self.assertRaises(OntologyAgentDecisionParseError) as captured:
            parse_ontology_agent_decision(payload)
        self.assertEqual(captured.exception.code, code)
        self.assertEqual(
            set(captured.exception.public()),
            {"code", "message"},
        )
        return captured.exception

    def test_accepts_exactly_the_five_single_step_decisions(self):
        cases = (
            (
                {
                    "decision": "LOOKUP_ONTOLOGY",
                    "capability_names": ["preview_risk_assessment"],
                },
                LookupOntologyDecision,
            ),
            (
                {
                    "decision": "CALL_READ_ACTIONS",
                    "calls": [
                        {
                            "capability_name": "inspect_case_readiness",
                            "arguments": {},
                        },
                        {
                            "capability_name": "preview_risk_assessment",
                            "arguments": {},
                        },
                    ],
                },
                CallReadActionsDecision,
            ),
            (
                {
                    "decision": "PROPOSE_WRITE_ACTION",
                    "capability_name": "trigger_manual_review",
                    "arguments": {"review_reason": "需要人工复核"},
                },
                ProposeWriteActionDecision,
            ),
            (
                {"decision": "ASK_USER", "question": "请补充您希望检查的范围。"},
                AskUserDecision,
            ),
            (
                {
                    "decision": "FINAL",
                    "answer": "当前材料齐全，该结论不代表最终审批。",
                    "observation_ids": ["obs_1", "obs_2"],
                    "boundary": "NOT_FINAL_APPROVAL",
                },
                FinalDecision,
            ),
        )
        for payload, expected_type in cases:
            with self.subTest(decision=payload["decision"]):
                parsed_mapping = parse_ontology_agent_decision(payload)
                parsed_json = parse_ontology_agent_decision(
                    json.dumps(payload, ensure_ascii=False)
                )
                self.assertIsInstance(parsed_mapping, expected_type)
                self.assertEqual(parsed_mapping, parsed_json)

    def test_every_branch_forbids_mixed_extra_and_reasoning_fields(self):
        invalid = (
            {
                "decision": "LOOKUP_ONTOLOGY",
                "capability_names": ["preview_risk_assessment"],
                "calls": [],
            },
            {
                "decision": "CALL_READ_ACTIONS",
                "calls": [{"capability_name": "inspect_case_readiness", "arguments": {}}],
                "reason": "hidden reasoning",
            },
            {
                "decision": "PROPOSE_WRITE_ACTION",
                "capability_name": "trigger_manual_review",
                "arguments": {"review_reason": "需要复核"},
                "analysis": "hidden analysis",
            },
            {
                "decision": "ASK_USER",
                "question": "请补充信息。",
                "thought": "hidden thought",
            },
            {
                "decision": "FINAL",
                "answer": "信息不足。",
                "observation_ids": [],
                "boundary": "NOT_FINAL_APPROVAL",
                "chain_of_thought": "hidden",
            },
        )
        for payload in invalid:
            with self.subTest(decision=payload["decision"]):
                self.assert_error("ONTOLOGY_DECISION_CONTRACT_INVALID", payload)

    def test_unknown_execute_and_case_variants_are_not_decisions(self):
        for decision in (
            "EXECUTE_WRITE_ACTION",
            "CALL_WRITE_ACTION",
            "call_read_actions",
            "FINAL ",
            "",
            None,
        ):
            with self.subTest(decision=decision):
                self.assert_error(
                    "ONTOLOGY_DECISION_CONTRACT_INVALID",
                    {"decision": decision},
                )

    def test_read_batch_requires_one_to_four_unique_capabilities_and_objects(self):
        valid_calls = [
            {"capability_name": f"read_capability_{index}", "arguments": {}}
            for index in range(4)
        ]
        parsed = parse_ontology_agent_decision({
            "decision": "CALL_READ_ACTIONS",
            "calls": valid_calls,
        })
        self.assertEqual(len(parsed.calls), 4)

        invalid_calls = (
            [],
            valid_calls + [{"capability_name": "read_capability_5", "arguments": {}}],
            [valid_calls[0], valid_calls[0]],
            [{"capability_name": "read_capability", "arguments": []}],
            [{"capability_name": "read_capability"}],
            [{"capability_name": "Read-Capability", "arguments": {}}],
            [{
                "capability_name": "read_capability",
                "arguments": {},
                "executor": "internal.executor",
            }],
        )
        for calls in invalid_calls:
            with self.subTest(calls=calls):
                self.assert_error(
                    "ONTOLOGY_DECISION_CONTRACT_INVALID",
                    {"decision": "CALL_READ_ACTIONS", "calls": calls},
                )

    def test_lookup_names_and_final_observation_ids_must_be_unique(self):
        self.assert_error(
            "ONTOLOGY_DECISION_CONTRACT_INVALID",
            {
                "decision": "LOOKUP_ONTOLOGY",
                "capability_names": [
                    "preview_risk_assessment",
                    "preview_risk_assessment",
                ],
            },
        )
        self.assert_error(
            "ONTOLOGY_DECISION_CONTRACT_INVALID",
            {
                "decision": "FINAL",
                "answer": "已完成。",
                "observation_ids": ["obs_1", "obs_1"],
                "boundary": "NOT_FINAL_APPROVAL",
            },
        )

    def test_final_boundary_is_required_and_fixed(self):
        base = {
            "decision": "FINAL",
            "answer": "该结果仅供决策支持。",
            "observation_ids": [],
        }
        self.assert_error("ONTOLOGY_DECISION_CONTRACT_INVALID", base)
        self.assert_error(
            "ONTOLOGY_DECISION_CONTRACT_INVALID",
            {**base, "boundary": "FINAL_APPROVAL"},
        )
        parsed = parse_ontology_agent_decision({
            **base,
            "boundary": "NOT_FINAL_APPROVAL",
        })
        self.assertEqual(parsed.boundary, "NOT_FINAL_APPROVAL")

    def test_strict_json_rejects_duplicate_keys_fences_and_trailing_text(self):
        self.assert_error(
            "ONTOLOGY_DECISION_DUPLICATE_KEY",
            '{"decision":"ASK_USER","question":"一","question":"二"}',
        )
        for payload in (
            '```json\n{"decision":"ASK_USER","question":"请补充。"}\n```',
            '{"decision":"ASK_USER","question":"请补充。"} trailing',
            '[{"decision":"ASK_USER","question":"请补充。"}]',
            '',
        ):
            with self.subTest(payload=payload[:20]):
                self.assert_error("ONTOLOGY_DECISION_INVALID_JSON", payload)

    def test_non_finite_numbers_are_rejected_for_string_and_mapping_inputs(self):
        for literal in ("NaN", "Infinity", "-Infinity"):
            self.assert_error(
                "ONTOLOGY_DECISION_NON_FINITE_NUMBER",
                (
                    '{"decision":"PROPOSE_WRITE_ACTION",'
                    '"capability_name":"trigger_manual_review",'
                    f'"arguments":{{"value":{literal}}}}}'
                ),
            )
        for value in (math.nan, math.inf, -math.inf):
            self.assert_error(
                "ONTOLOGY_DECISION_NON_FINITE_NUMBER",
                {
                    "decision": "PROPOSE_WRITE_ACTION",
                    "capability_name": "trigger_manual_review",
                    "arguments": {"value": value},
                },
            )

    def test_size_depth_and_complexity_limits_apply_before_contract_validation(self):
        oversized = '{"untrusted":"' + ("x" * MAX_DECISION_BYTES) + '"}'
        self.assert_error("ONTOLOGY_DECISION_TOO_LARGE", oversized)

        nested = '"leaf"'
        for _ in range(14):
            nested = '{"nested":' + nested + "}"
        too_deep = (
            '{"decision":"PROPOSE_WRITE_ACTION",'
            '"capability_name":"trigger_manual_review",'
            f'"arguments":{nested}}}'
        )
        self.assert_error("ONTOLOGY_DECISION_TOO_DEEP", too_deep)

        too_complex = {
            "decision": "PROPOSE_WRITE_ACTION",
            "capability_name": "trigger_manual_review",
            "arguments": {"items": [None] * 2048},
        }
        self.assert_error("ONTOLOGY_DECISION_TOO_COMPLEX", too_complex)

    def test_mapping_input_is_copied_and_must_be_json_compatible(self):
        payload = {
            "decision": "PROPOSE_WRITE_ACTION",
            "capability_name": "trigger_manual_review",
            "arguments": {"review_reason": "需要复核"},
        }
        parsed = parse_ontology_agent_decision(payload)
        payload["arguments"]["review_reason"] = "mutated"
        self.assertEqual(parsed.arguments["review_reason"], "需要复核")

        self.assert_error(
            "ONTOLOGY_DECISION_INVALID_JSON",
            {
                "decision": "PROPOSE_WRITE_ACTION",
                "capability_name": "trigger_manual_review",
                "arguments": {"unsupported": object()},
            },
        )

    def test_errors_never_retain_or_echo_raw_model_content(self):
        sentinel = "password=DO_NOT_ECHO_THIS_SECRET"
        error = self.assert_error(
            "ONTOLOGY_DECISION_CONTRACT_INVALID",
            {
                "decision": "ASK_USER",
                "question": "请补充信息。",
                "raw_response": sentinel,
            },
        )
        self.assertNotIn(sentinel, str(error))
        self.assertNotIn(sentinel, json.dumps(error.public(), ensure_ascii=False))
        self.assertFalse(hasattr(error, "raw"))
        self.assertFalse(hasattr(error, "payload"))

    def test_model_schema_and_instruction_expose_only_the_five_safe_branches(self):
        schema = ontology_agent_decision_json_schema()
        serialized = json.dumps(schema, ensure_ascii=False)
        expected = {
            "LOOKUP_ONTOLOGY",
            "CALL_READ_ACTIONS",
            "PROPOSE_WRITE_ACTION",
            "ASK_USER",
            "FINAL",
        }

        def values_for_key(value, key):
            output = []
            if isinstance(value, dict):
                for item_key, nested in value.items():
                    if item_key == key:
                        output.append(nested)
                    output.extend(values_for_key(nested, key))
            elif isinstance(value, list):
                for nested in value:
                    output.extend(values_for_key(nested, key))
            return output

        self.assertEqual(
            {value for value in values_for_key(schema, "const") if value in expected},
            expected,
        )
        self.assertNotIn("EXECUTE_WRITE_ACTION", serialized)
        property_maps = values_for_key(schema, "properties")
        exposed_fields = {
            field for properties in property_maps for field in properties
        }
        self.assertTrue({
            "decision",
            "capability_names",
            "calls",
            "capability_name",
            "arguments",
            "question",
            "answer",
            "observation_ids",
            "boundary",
        }.issubset(exposed_fields))
        self.assertFalse(
            {"reason", "rationale", "analysis", "thought", "chain_of_thought"}
            & exposed_fields
        )
        instruction = ontology_agent_decision_instruction()
        self.assertIn("NOT_FINAL_APPROVAL", instruction)
        self.assertIn("不要提交分析、思维过程、理由", instruction)
        self.assertNotIn("EXECUTE_WRITE_ACTION", instruction)

        schema["injected"] = True
        self.assertNotIn("injected", ontology_agent_decision_json_schema())


if __name__ == "__main__":
    unittest.main()
