import json
import base64
import hashlib
import hmac
import unittest

from app.core.security import (
    anonymize_profile_for_llm,
    api_key_matches,
    cookie_write_origin_is_allowed,
    external_llm_auth_error,
    is_allowed_local_origin,
    is_loopback_request_host,
    is_trusted_local_session_request,
    is_unauthenticated_workbench_path,
    issue_local_session_token,
    local_session_secret_is_usable,
    mask_graph_snapshot,
    mask_payload,
    validate_llm_base_url,
    verify_local_session_token,
)


class SecurityTests(unittest.TestCase):
    SESSION_SECRET="test-local-session-signing-secret-123456789"
    LOCAL_ORIGINS=[
        "http://127.0.0.1:4173","http://localhost:8080"
    ]

    def test_no_login_workbench_scope_is_explicit_and_bounded(self):
        self.assertTrue(is_unauthenticated_workbench_path(
            "/api/workflows/personal-credit",True
        ))
        self.assertTrue(is_unauthenticated_workbench_path(
            "/api/workflows/personal-credit/definition",True
        ))
        self.assertTrue(is_unauthenticated_workbench_path(
            "/api/agents/personal-credit/run",True,"POST"
        ))
        self.assertTrue(is_unauthenticated_workbench_path(
            "/api/agents/personal-credit/tools/preview_risk_assessment/execute",
            True,"POST"
        ))
        self.assertTrue(is_unauthenticated_workbench_path(
            "/api/workflows/personal-credit/workflow-1",True,"GET"
        ))
        for protected_path in (
            "/api/workflows/personal-credit",
            "/api/workflows/personal-credit/data-search",
            "/api/workflows/personal-credit/workflow-1/refresh",
            "/api/workflows/personal-credit/workflow-1/steps/generate_risk_assessment/execute",
            "/api/workflows/personal-credit/workflow-1/steps/trigger_manual_review/skip",
            "/api/workflows/personal-credit/workflow-1/cancel",
        ):
            with self.subTest(protected_path=protected_path):
                self.assertFalse(is_unauthenticated_workbench_path(
                    protected_path,True,"POST"
                ))
        self.assertFalse(is_unauthenticated_workbench_path(
            "/api/workflows/personal-credit",False
        ))
        self.assertFalse(is_unauthenticated_workbench_path(
            "/api/workflows/personal-credit-admin",True
        ))
        self.assertFalse(is_unauthenticated_workbench_path(
            "/api/actions/create-loan-application/execute",True
        ))
        self.assertFalse(is_unauthenticated_workbench_path(
            "/api/agents/personal-credit-admin/run",True
        ))
        self.assertFalse(is_unauthenticated_workbench_path(
            "/api/llm/test",True,"POST"
        ))
        self.assertFalse(is_unauthenticated_workbench_path(
            "/api/auth/check",True,"GET"
        ))

    def test_api_key_comparison_rejects_missing_and_wrong_values(self):
        self.assertTrue(api_key_matches("server-secret","server-secret"))
        self.assertFalse(api_key_matches("server-secret",None))
        self.assertFalse(api_key_matches("server-secret","wrong"))
        self.assertFalse(api_key_matches("","server-secret"))

    def test_local_session_round_trip_is_signed_bounded_and_expires(self):
        self.assertTrue(local_session_secret_is_usable(self.SESSION_SECRET))
        self.assertFalse(local_session_secret_is_usable("short-secret"))
        first,expires_at=issue_local_session_token(
            self.SESSION_SECRET,900,now=1_700_000_000
        )
        second,_=issue_local_session_token(
            self.SESSION_SECRET,900,now=1_700_000_000
        )
        self.assertNotEqual(first,second)
        self.assertEqual(expires_at,1_700_000_900)
        self.assertEqual(
            verify_local_session_token(
                self.SESSION_SECRET,first,900,now=1_700_000_001
            ),
            expires_at,
        )
        self.assertIsNone(verify_local_session_token(
            self.SESSION_SECRET,first,900,now=expires_at
        ))
        self.assertIsNone(verify_local_session_token(
            self.SESSION_SECRET,first+"tampered",900,now=1_700_000_001
        ))
        self.assertIsNone(verify_local_session_token(
            "different-local-session-secret-123456789",first,900,
            now=1_700_000_001,
        ))

    def test_local_session_rejects_malformed_and_non_object_payloads(self):
        def signed(payload):
            encoded=base64.urlsafe_b64encode(
                json.dumps(payload,separators=(",",":")).encode()
            ).rstrip(b"=").decode()
            signature=base64.urlsafe_b64encode(hmac.new(
                self.SESSION_SECRET.encode(),encoded.encode(),hashlib.sha256
            ).digest()).rstrip(b"=").decode()
            return encoded+"."+signature

        malformed=(
            None,"",".","one-part","a.b.c","💥.💥","A"*2049,
            signed([]),signed("not-an-object"),signed({}),
        )
        for token in malformed:
            with self.subTest(token=str(token)[:30]):
                self.assertIsNone(verify_local_session_token(
                    self.SESSION_SECRET,token,900,now=1_700_000_000
                ))

    def test_local_session_host_origin_and_proxy_boundary_is_exact(self):
        self.assertTrue(is_loopback_request_host("127.0.0.1:8000"))
        self.assertTrue(is_loopback_request_host("localhost:8000"))
        self.assertTrue(is_loopback_request_host("[::1]:8000"))
        self.assertFalse(is_loopback_request_host("192.168.1.2:8000"))
        self.assertFalse(is_loopback_request_host("localhost.evil.test:8000"))
        self.assertTrue(is_allowed_local_origin(
            "http://127.0.0.1:4173",self.LOCAL_ORIGINS
        ))
        self.assertFalse(is_allowed_local_origin(
            "http://127.0.0.1:9999",self.LOCAL_ORIGINS
        ))
        self.assertFalse(is_allowed_local_origin(
            "https://evil.example",self.LOCAL_ORIGINS
        ))
        baseline={
            "host_header":"127.0.0.1:8000",
            "origin":"http://127.0.0.1:4173",
            "allowed_origins":self.LOCAL_ORIGINS,
            "fetch_site":"same-site",
            "require_origin":True,
        }
        self.assertTrue(is_trusted_local_session_request(
            client_host="127.0.0.1",trust_local_proxy=False,**baseline
        ))
        self.assertFalse(is_trusted_local_session_request(
            client_host="172.20.0.1",trust_local_proxy=False,**baseline
        ))
        self.assertTrue(is_trusted_local_session_request(
            client_host="172.20.0.1",trust_local_proxy=True,**baseline
        ))
        self.assertFalse(is_trusted_local_session_request(
            client_host="172.20.0.1",trust_local_proxy=True,
            **{**baseline,"origin":None}
        ))
        self.assertFalse(is_trusted_local_session_request(
            client_host="127.0.0.1",trust_local_proxy=False,
            **{**baseline,"origin":"https://evil.example"}
        ))
        self.assertFalse(is_trusted_local_session_request(
            client_host="127.0.0.1",trust_local_proxy=False,
            **{**baseline,"fetch_site":"cross-site"}
        ))

    def test_safe_cookie_reads_allow_trusted_proxy_without_origin_but_writes_do_not(self):
        self.assertTrue(is_trusted_local_session_request(
            client_host="172.20.0.1",
            host_header="127.0.0.1:8000",
            origin=None,
            allowed_origins=self.LOCAL_ORIGINS,
            trust_local_proxy=True,
        ))
        self.assertTrue(cookie_write_origin_is_allowed(
            "GET",None,self.LOCAL_ORIGINS
        ))
        self.assertFalse(cookie_write_origin_is_allowed(
            "POST",None,self.LOCAL_ORIGINS
        ))
        self.assertFalse(cookie_write_origin_is_allowed(
            "POST","https://evil.example",self.LOCAL_ORIGINS,"cross-site"
        ))
        self.assertTrue(cookie_write_origin_is_allowed(
            "POST","http://127.0.0.1:4173",self.LOCAL_ORIGINS,"same-site"
        ))

    def test_external_llm_requires_mvp_auth_but_deterministic_run_does_not(self):
        self.assertIsNone(external_llm_auth_error("server-secret",None,False))
        self.assertIsNone(external_llm_auth_error(
            "server-secret","server-secret",True
        ))
        self.assertEqual(
            external_llm_auth_error("server-secret",None,True)[0],401
        )
        self.assertEqual(
            external_llm_auth_error("server-secret","wrong",True)[0],401
        )
        self.assertEqual(external_llm_auth_error("",None,True)[0],503)
        self.assertIsNone(external_llm_auth_error(
            "",None,True,authenticated=True
        ))

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

    def test_recursive_masking_never_returns_authentication_secrets(self):
        source={
            "api_key":"sk-live-secret",
            "X-API-Key":"mvp-access-token",
            "api_key_ciphertext":"encrypted-secret-bytes",
            "authorization":"Bearer provider-secret",
            "access_token":"oauth-access-token",
            "nested":{"secret":"another-secret","token":"session-token"},
        }
        text=json.dumps(mask_payload(source),ensure_ascii=False)
        for secret in (
            "sk-live-secret","mvp-access-token","encrypted-secret-bytes",
            "Bearer provider-secret","oauth-access-token","another-secret",
            "session-token",
        ):
            self.assertNotIn(secret,text)

    def test_plain_text_error_masking_removes_credential_assignments(self):
        source=(
            "api_key=sk-live-secret; Authorization: Bearer auth-secret "
            "access_token=oauth-secret token=session-secret secret=root-secret"
        )
        masked=mask_payload(source)
        for secret in (
            "sk-live-secret","auth-secret","oauth-secret",
            "session-secret","root-secret",
        ):
            self.assertNotIn(secret,masked)
        self.assertGreaterEqual(masked.count("[已脱敏]"),5)

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
