import unittest

from app.postgres.business_lookup import (
    BusinessLookupError,
    PersonalCreditBusinessLookup,
)


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, parameters=None):
        self.executions.append((sql, parameters))

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor


class FakePostgres:
    def __init__(self, rows):
        self.cursor_instance = FakeCursor(rows)

    def conn(self):
        return FakeConnection(self.cursor_instance)


class PersonalCreditBusinessLookupTests(unittest.TestCase):
    def test_rejects_unknown_entity_and_short_query_before_database_access(self):
        postgres = FakePostgres([])
        lookup = PersonalCreditBusinessLookup(postgres)
        with self.assertRaises(BusinessLookupError):
            lookup.search("arbitrary_table", "张伟")
        with self.assertRaises(BusinessLookupError):
            lookup.search("natural_person", "张")
        self.assertEqual(postgres.cursor_instance.executions, [])

    def test_natural_person_search_is_read_only_escaped_and_preview_masked(self):
        raw = {
            "id_number": "110101198803120011",
            "name": "张伟",
            "phone": "13800000001",
            "gender": "男",
            "address": "北京市朝阳区",
            "monthly_income": 25000,
            "employer": "华信科技有限公司",
        }
        postgres = FakePostgres([raw])
        result = PersonalCreditBusinessLookup(postgres).search(
            "natural_person", "  1101%_  ", 999
        )

        executions = postgres.cursor_instance.executions
        self.assertIn("READ ONLY", executions[0][0])
        self.assertEqual(executions[1][1][0], "%1101\\%\\_%")
        self.assertEqual(executions[1][1][-1], 20)
        self.assertEqual(result["selection_scope"], "authenticated_action_prefill")
        item = result["results"][0]
        self.assertNotIn(raw["id_number"], item["subtitle"])
        self.assertNotIn(raw["phone"], item["subtitle"])
        self.assertNotEqual(item["title"], raw["name"])
        self.assertEqual(item["values"]["id_number"], raw["id_number"])
        self.assertEqual(item["values"]["monthly_income"], 25000)

    def test_application_template_keeps_current_workflow_identity_and_event_time(self):
        row = {
            "application_id": "APP-OLD-001",
            "channel": "线上H5",
            "loan_amount": 200000,
            "loan_term": 36,
            "interest_rate": 0.045,
            "borrower__id_number": "110101198803120011",
            "borrower__name": "张伟",
            "borrower__phone": "13800000001",
            "vehicle__vin": "LHGCM82633A000001",
            "vehicle__brand": "奥迪",
            "vehicle__model": "A4L",
            "vehicle__invoice_price": 268000,
            "dealer__credit_code": None,
            "guarantor__id_number": None,
            "co_borrower__id_number": None,
        }
        result = PersonalCreditBusinessLookup(FakePostgres([row])).search(
            "loan_application", "APP-OLD"
        )
        values = result["results"][0]["values"]

        self.assertNotIn("application_id", values)
        self.assertNotIn("submit_date", values["application"])
        self.assertNotIn("application_status", values["application"])
        self.assertEqual(values["borrower"]["id_number"], row["borrower__id_number"])
        self.assertEqual(values["vehicle"]["vin"], row["vehicle__vin"])
        self.assertNotIn("dealer", values)
        self.assertNotIn("guarantor", values)
        self.assertNotIn("co_borrower", values)


if __name__ == "__main__":
    unittest.main()
