import time
import unittest
from unittest.mock import patch

import httpx

from app import main
from app.core.security import LOCAL_SESSION_COOKIE_NAME,issue_local_session_token


class LocalSessionAuthTests(unittest.IsolatedAsyncioTestCase):
    SECRET="integration-local-session-secret-1234567890"
    ORIGIN="http://127.0.0.1:4173"
    ALLOWED=[ORIGIN,"http://localhost:8080"]

    def settings(self,**overrides):
        values={
            "MVP_AUTH_MODE":"local_session",
            "MVP_API_KEY":"integration-api-key",
            "MVP_LOCAL_SESSION_SECRET":self.SECRET,
            "MVP_LOCAL_SESSION_TTL_SECONDS":900,
            "MVP_LOCAL_SESSION_TRUST_PROXY":False,
            "MVP_LOCAL_SESSION_COOKIE_SECURE":False,
            "MVP_LOCAL_SESSION_ALLOWED_ORIGINS":self.ALLOWED,
            "WORKBENCH_UNAUTHENTICATED":False,
        }
        values.update(overrides)
        return patch.multiple(main,**values)

    def client(self,host="127.0.0.1"):
        transport=httpx.ASGITransport(
            app=main.app,raise_app_exceptions=True,client=(host,51000)
        )
        return httpx.AsyncClient(
            transport=transport,base_url="http://127.0.0.1:8000"
        )

    async def test_issue_cookie_and_auth_check_contract(self):
        with self.settings():
            async with self.client() as client:
                response=await client.post(
                    "/api/auth/session",
                    headers={"Origin":self.ORIGIN,"Sec-Fetch-Site":"same-site"},
                )
                self.assertEqual(response.status_code,200)
                payload=response.json()
                self.assertEqual(payload["authenticated"],True)
                self.assertEqual(payload["mode"],"local_session")
                self.assertIsInstance(payload["expires_at"],str)
                self.assertNotIn("token",payload)
                cookie=response.headers["set-cookie"].lower()
                for attribute in (
                    "httponly","samesite=strict","path=/api","max-age=900"
                ):
                    self.assertIn(attribute,cookie)
                self.assertEqual(response.headers["cache-control"],"no-store")

                check=await client.get("/api/auth/check")
                self.assertEqual(check.status_code,200)
                self.assertEqual(check.json()["mode"],"local_session")
                self.assertEqual(check.json()["expires_at"],payload["expires_at"])

    async def test_bootstrap_fails_closed_outside_local_mode_or_trust_boundary(self):
        cases=(
            ({"MVP_AUTH_MODE":"api_key"},"127.0.0.1",self.ORIGIN,None,403),
            ({"MVP_LOCAL_SESSION_SECRET":"short"},"127.0.0.1",self.ORIGIN,None,503),
            ({},"203.0.113.8",self.ORIGIN,None,403),
            ({},"127.0.0.1","https://evil.example",None,403),
            ({},"127.0.0.1",self.ORIGIN,"credit.example",403),
        )
        for overrides,source,origin,host,status in cases:
            with self.subTest(source=source,origin=origin,host=host,status=status):
                with self.settings(**overrides):
                    async with self.client(source) as client:
                        headers={"Origin":origin}
                        if host:
                            headers["Host"]=host
                        response=await client.post(
                            "/api/auth/session",headers=headers
                        )
                self.assertEqual(response.status_code,status)

    async def test_expired_tampered_and_malformed_cookies_are_unauthorized(self):
        expired,_=issue_local_session_token(
            self.SECRET,900,now=int(time.time())-901
        )
        valid,_=issue_local_session_token(self.SECRET,900)
        cases=(expired,valid+"tampered","not.a.valid.cookie","%F0%9F%92%A5")
        for cookie in cases:
            with self.subTest(cookie=cookie[:20]):
                with self.settings():
                    async with self.client() as client:
                        response=await client.get(
                            "/api/auth/check",
                            headers={"Cookie":f"{LOCAL_SESSION_COOKIE_NAME}={cookie}"},
                        )
                self.assertEqual(response.status_code,401)

    async def test_cookie_writes_require_exact_origin_but_api_key_cli_remains_compatible(self):
        with self.settings():
            async with self.client() as client:
                issued=await client.post(
                    "/api/auth/session",headers={"Origin":self.ORIGIN}
                )
                self.assertEqual(issued.status_code,200)
                missing=await client.post("/api/not-a-real-route")
                self.assertEqual(missing.status_code,403)
                malicious=await client.post(
                    "/api/not-a-real-route",headers={"Origin":"https://evil.example"}
                )
                self.assertEqual(malicious.status_code,403)
                allowed=await client.post(
                    "/api/not-a-real-route",headers={"Origin":self.ORIGIN}
                )
                self.assertEqual(allowed.status_code,404)

            async with self.client() as cli:
                header_auth=await cli.post(
                    "/api/not-a-real-route",
                    headers={"X-API-Key":"integration-api-key"},
                )
                self.assertEqual(header_auth.status_code,404)

    async def test_local_mode_disables_uat_anonymous_bypass(self):
        with self.settings(WORKBENCH_UNAUTHENTICATED=True):
            async with self.client() as client:
                response=await client.get("/api/agents/personal-credit/tools")
        self.assertEqual(response.status_code,401)

    async def test_explicit_proxy_trust_supports_cookie_safe_get_without_origin(self):
        with self.settings(MVP_LOCAL_SESSION_TRUST_PROXY=True):
            async with self.client("172.20.0.1") as client:
                issued=await client.post(
                    "/api/auth/session",headers={"Origin":self.ORIGIN}
                )
                self.assertEqual(issued.status_code,200)
                checked=await client.get("/api/auth/check")
                self.assertEqual(checked.status_code,200)
                self.assertEqual(checked.json()["mode"],"local_session")

    async def test_api_key_auth_check_preserves_existing_mode_and_has_no_expiry(self):
        with self.settings(MVP_AUTH_MODE="api_key"):
            async with self.client() as client:
                response=await client.get(
                    "/api/auth/check",
                    headers={"X-API-Key":"integration-api-key"},
                )
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{
            "authenticated":True,"mode":"mvp_api_key","expires_at":None,
        })


if __name__=="__main__":
    unittest.main()
