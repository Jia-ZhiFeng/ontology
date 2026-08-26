import unittest

from app.ontology.baseline import build_personal_credit_baseline
from app.ontology.data_quality import validate_catalog_data_quality


def profile(samples=None,**overrides):
    samples=samples or {}
    value={
        "samples":samples,
        "row_counts":{table:len(rows) for table,rows in samples.items()},
        "truncated_tables":[],
        "snapshot_mode":"repeatable_read_read_only",
        "snapshot_consistent":True,
        "snapshot_id":"test-snapshot",
        "snapshot_started_at":"2000-01-01T00:00:00Z",
    }
    value.update(overrides)
    return value


def result_for(report,property_name):
    return next(item for item in report["results"] if item["property"]==property_name)


class DataQualityTests(unittest.TestCase):
    def setUp(self):
        self.proposal=build_personal_credit_baseline()

    def test_empty_complete_snapshot_passes_all_catalog_rules(self):
        report=validate_catalog_data_quality(self.proposal,profile())
        self.assertTrue(report["valid"],report)
        self.assertEqual(report["summary"]["property_rule_count"],74)
        self.assertEqual(report["condition_coverage"]["unknown_conditions"],[])

    def test_conditional_required_is_not_applied_before_business_stage(self):
        report=validate_catalog_data_quality(self.proposal,profile({
            "natural_person":[{"id_number":"110101199001011234","name":"张伟","phone":None,"address":None,"monthly_income":None}],
        }))
        self.assertTrue(report["valid"],report)
        self.assertEqual(result_for(report,"NaturalPerson.phone")["required_applicable_rows"],0)

    def test_application_presence_triggers_person_submission_fields(self):
        report=validate_catalog_data_quality(self.proposal,profile({
            "natural_person":[{"id_number":"110101199001011234","name":"张伟","phone":None,"address":None,"monthly_income":None}],
            "loan_application":[{"application_id":"P001","borrower_id":"110101199001011234"}],
        }))
        phone=result_for(report,"NaturalPerson.phone")
        self.assertEqual(phone["required_applicable_rows"],1)
        self.assertEqual(phone["violation_count"],1)

    def test_evaluation_stage_requires_external_debt_monthly_payment(self):
        report=validate_catalog_data_quality(self.proposal,profile({
            "loan_application":[{
                "application_id":"P001","borrower_id":"110101199001011234",
                "application_status":"评估中",
            }],
            "external_debt":[{
                "debt_id":"D001","borrower_id":"110101199001011234",
                "monthly_payment":None,
            }],
        }))
        payment=result_for(report,"ExternalDebt.monthly_payment")
        self.assertEqual(payment["required_applicable_rows"],1)
        self.assertEqual(payment["violation_count"],1)

    def test_incomplete_application_allows_fields_waiting_for_collection(self):
        report=validate_catalog_data_quality(self.proposal,profile({
            "loan_application":[{
                "application_id":"P001","application_status":"资料待补充","channel":None,
            }],
        }))
        channel=result_for(report,"LoanApplication.channel")
        self.assertEqual(channel["required_applicable_rows"],0)
        self.assertEqual(channel["violation_count"],0)

    def test_preset_threshold_and_pending_manual_review_are_not_completed_stages(self):
        report=validate_catalog_data_quality(self.proposal,profile({
            "risk_assessment_report":[{
                "assessment_id":"R001","application_id":"P001","threshold":0.05,
                "pd_value":None,"assessment_channel":"自动决策","check_result":"待人工复核",
                "checker":None,
            }],
        }))
        self.assertEqual(result_for(report,"RiskAssessmentReport.pd_value")["required_applicable_rows"],0)
        self.assertEqual(result_for(report,"RiskAssessmentReport.checker")["required_applicable_rows"],0)

    def test_enum_and_type_violations_are_detected(self):
        report=validate_catalog_data_quality(self.proposal,profile({
            "natural_person":[{"id_number":"110101199001011234","name":"张伟","gender":"未知","monthly_income":"not-a-number"}],
        }))
        self.assertEqual(result_for(report,"NaturalPerson.gender")["violations"][0]["rule"],"enum")
        self.assertEqual(result_for(report,"NaturalPerson.monthly_income")["violations"][0]["rule"],"type")

    def test_duplicate_business_key_is_detected(self):
        report=validate_catalog_data_quality(self.proposal,profile({
            "natural_person":[
                {"id_number":"110101199001011234","name":"张伟"},
                {"id_number":"110101199001011234","name":"李伟"},
            ],
        }))
        key_result=result_for(report,"NaturalPerson.id_number")
        self.assertEqual(key_result["violation_count"],1)
        self.assertEqual(key_result["violations"][0]["rule"],"business_key_unique")

    def test_empty_string_is_not_treated_as_valid_optional_value(self):
        report=validate_catalog_data_quality(self.proposal,profile({
            "natural_person":[{"id_number":"110101199001011234","name":"张伟","employer":"   "}],
        }))
        self.assertEqual(result_for(report,"NaturalPerson.employer")["violations"][0]["rule"],"empty_string")

    def test_unknown_required_condition_blocks_validation(self):
        proposal=self.proposal.model_copy(deep=True)
        proposal.properties[0].required_when="未注册的新阶段"
        report=validate_catalog_data_quality(proposal,profile())
        self.assertFalse(report["valid"])
        self.assertEqual(report["condition_coverage"]["unknown_conditions"],["未注册的新阶段"])

    def test_inconsistent_or_truncated_snapshot_cannot_pass(self):
        inconsistent=validate_catalog_data_quality(self.proposal,profile(snapshot_consistent=False))
        truncated=validate_catalog_data_quality(self.proposal,profile(truncated_tables=["natural_person"]))
        self.assertFalse(inconsistent["valid"])
        self.assertFalse(truncated["valid"])


if __name__=="__main__":
    unittest.main()
