import json
import unittest

from app.core.security import (
    anonymize_profile_for_llm,
    api_key_matches,
    mask_graph_snapshot,
    mask_payload,
    validate_llm_base_url,
)


class SecurityTests(unittest.TestCase):
    def test_api_key_comparison_rejects_missing_and_wrong_values(self):
        self.assertTrue(api_key_matches("server-secret","server-secret"))
        self.assertFalse(api_key_matches("server-secret",None))
        self.assertFalse(api_key_matches("server-secret","wrong"))
        self.assertFalse(api_key_matches("","server-secret"))

    def test_recursive_masking_covers_personal_and_nested_values(self):
        source={
            "name":"张伟",
            "id_number":"110101199001011234",
            "phone":"13800138000",
            "monthly_income":25000,
            "record":"natural_person/110101199001011234 contacted 13800138000; alternate 11010119900101123X",
            "properties":{"props":json.dumps({"borrower_id":"110101199001011234","risk_factors":"高负债"},ensure_ascii=False)},
        }
        masked=mask_payload(source)
        text=json.dumps(masked,ensure_ascii=False)
        for raw in ("张伟","110101199001011234","11010119900101123X","13800138000","25000","高负债"):
            self.assertNotIn(raw,text)
        self.assertIn("110*************34",text)
        self.assertIn("138****8000",text)

    def test_graph_masking_keeps_edge_endpoints_resolvable(self):
        snapshot={
            "nodes":[
                {"id":"110101199001011234","tags":["entity"],"properties":{"props":"{}"}},
                {"id":"P001","tags":["entity"],"properties":{"props":"{}"}},
            ],
            "edges":[{"source":"110101199001011234","target":"P001","type":"related","properties":{}}],
        }
        masked=mask_graph_snapshot(snapshot)
        node_ids={node["id"] for node in masked["nodes"]}
        self.assertIn(masked["edges"][0]["source"],node_ids)
        self.assertIn(masked["edges"][0]["target"],node_ids)
        self.assertNotIn("110101199001011234",json.dumps(masked,ensure_ascii=False))

    def test_llm_profile_anonymization_keeps_shape_without_values(self):
        profile={
            "database":"credit_ontology",
            "tables":{"natural_person":[{"column_name":"name"}]},
            "samples":{"natural_person":[{
                "id_number":"110101199001011234",
                "name":"张伟",
                "phone":"13800138000",
                "monthly_income":25000,
                "address":None,
            }]},
        }
        safe=anonymize_profile_for_llm(profile)
        text=json.dumps(safe,ensure_ascii=False)
        for raw in ("110101199001011234","张伟","13800138000","25000"):
            self.assertNotIn(raw,text)
        row=safe["samples"]["natural_person"][0]
        self.assertEqual(set(row),set(profile["samples"]["natural_person"][0]))
        self.assertIsNone(row["address"])
        self.assertEqual(row["monthly_income"],0)

    def test_llm_base_url_requires_exact_https_allowlist_match(self):
        allowed=["https://api.openai.com/v1"]
        self.assertEqual(
            validate_llm_base_url("https://api.openai.com/v1/",allowed),
            "https://api.openai.com/v1",
        )
        for rejected in (
            "http://api.openai.com/v1",
            "https://api.openai.com/v1?key=secret",
            "https://api.openai.com/v1/extra",
            "https://evil.example/v1",
        ):
            with self.subTest(rejected=rejected),self.assertRaises(ValueError):
                validate_llm_base_url(rejected,allowed)

    def test_insecure_http_is_limited_to_explicit_local_allowlist(self):
        allowed=["http://127.0.0.1:11434/v1"]
        self.assertEqual(
            validate_llm_base_url("http://127.0.0.1:11434/v1",allowed,True),
            "http://127.0.0.1:11434/v1",
        )
        with self.assertRaises(ValueError):
            validate_llm_base_url("http://127.0.0.1:11434/v1",allowed,False)


if __name__=="__main__":
    unittest.main()
