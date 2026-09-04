import unittest

from app.ontology.baseline import build_personal_credit_baseline
from app.ontology.data_binding import (
    OntologyDataBinding,
    OntologyDataBindingError,
    quote_identifier,
)
from app.ontology.mapping import build_mapping_contract


class OntologyDataBindingTests(unittest.TestCase):
    def test_baseline_resolves_allowlisted_table_and_property(self):
        binding = OntologyDataBinding(build_personal_credit_baseline())
        self.assertEqual(
            binding.table_sql("LoanApplication"),
            '"public"."loan_application"',
        )
        self.assertEqual(
            binding.select_sql(
                "LoanApplication", "loan_amount", alias="application"
            ),
            '"application"."loan_amount" AS "loan_amount"',
        )
        self.assertEqual(binding.ontology_version, "1.1.0")
        self.assertEqual(len(binding.binding_digest), 64)

    def test_physical_column_can_change_without_changing_logical_output(self):
        proposal = build_personal_credit_baseline().model_copy(deep=True)
        for prop in proposal.properties:
            if prop.domain == "LoanApplication":
                prop.source_table = "credit_case"
            if prop.domain == "LoanApplication" and prop.name == "loan_amount":
                prop.source_column = "requested_amount"
        binding = OntologyDataBinding(proposal)
        self.assertEqual(
            binding.table_sql("LoanApplication"),
            '"public"."credit_case"',
        )
        self.assertEqual(
            binding.select_sql(
                "LoanApplication", "loan_amount", alias="application"
            ),
            '"application"."requested_amount" AS "loan_amount"',
        )
        contract = build_mapping_contract(proposal)
        self.assertEqual(
            contract["id_keys"]["credit_case"], "application_id"
        )

    def test_raw_sql_or_malicious_identifiers_are_rejected(self):
        for value in ("loan_application; DROP TABLE x", "public.table", 'x"y', ""):
            with self.subTest(value=value), self.assertRaises(
                OntologyDataBindingError
            ):
                quote_identifier(value)

    def test_missing_required_property_fails_closed(self):
        proposal = build_personal_credit_baseline().model_copy(deep=True)
        target = next(
            prop
            for prop in proposal.properties
            if prop.domain == "NaturalPerson" and prop.name == "monthly_income"
        )
        target.source_table = None
        target.source_column = None
        binding = OntologyDataBinding(proposal)
        with self.assertRaisesRegex(
            OntologyDataBindingError, "NaturalPerson.monthly_income"
        ):
            binding.require({"NaturalPerson": ("monthly_income",)})


if __name__ == "__main__":
    unittest.main()
