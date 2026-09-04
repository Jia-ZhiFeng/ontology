import unittest
from unittest.mock import AsyncMock,patch

from app.llm.client import (
    LLMClient,
    normalize_llm_endpoint,
    normalize_provider,
    provider_catalog,
)
from app.llm.config_repository import (
    LLMConfigurationConflict,
    LLMConfigurationEncryptionError,
    PostgresLLMConfigurationRepository,
)


class LLMProviderSecurityTests(unittest.TestCase):
    def test_provider_catalog_contains_required_management_presets(self):
        providers={item["id"] for item in provider_catalog()}
        self.assertEqual(providers,{
            "openai_compatible","deepseek","aliyun_bailian",
            "azure_openai","custom",
        })
        self.assertEqual(normalize_provider("OpenAI"),"openai_compatible")
        self.assertEqual(normalize_provider("dashscope"),"aliyun_bailian")
        self.assertEqual(normalize_provider("azure-openai"),"azure_openai")

    def test_bailian_shared_and_workspace_compatible_endpoints_are_allowed(self):
        accepted=(
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/",
            "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
            "https://ws-123abc.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "https://llm-abc123.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
        )
        for value in accepted:
            with self.subTest(value=value):
                provider,canonical=normalize_llm_endpoint("aliyun_bailian",value)
                self.assertEqual(provider,"aliyun_bailian")
                self.assertTrue(canonical.startswith("https://"))
                self.assertTrue(canonical.endswith("/compatible-mode/v1"))

    def test_bailian_endpoint_rejects_ssrf_shapes(self):
        rejected=(
            "http://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope.aliyuncs.com.evil.test/compatible-mode/v1",
            "https://user:secret@dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope.aliyuncs.com:8443/compatible-mode/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1?api_key=secret",
            "https://dashscope.aliyuncs.com/other/v1",
            "https://llm-abc.unknown-region.maas.aliyuncs.com/compatible-mode/v1",
        )
        for value in rejected:
            with self.subTest(value=value),self.assertRaises(ValueError):
                normalize_llm_endpoint("aliyun_bailian",value)

    def test_custom_and_azure_require_exact_administrator_allowlist(self):
        allowlist=[
            "https://gateway.example.test/v1",
            "https://tenant.openai.azure.com",
        ]
        with patch("app.llm.client.LLM_ALLOWED_BASE_URLS",allowlist):
            self.assertEqual(
                normalize_llm_endpoint(
                    "custom","https://gateway.example.test/v1/"
                )[1],
                "https://gateway.example.test/v1",
            )
            self.assertEqual(
                normalize_llm_endpoint(
                    "azure_openai","https://tenant.openai.azure.com"
                )[1],
                "https://tenant.openai.azure.com",
            )
            for rejected in (
                "https://gateway.example.test/v1/extra",
                "https://tenant.openai.azure.com.evil.test",
                "http://gateway.example.test/v1",
            ):
                with self.subTest(rejected=rejected),self.assertRaises(ValueError):
                    normalize_llm_endpoint("custom",rejected)

    def test_azure_request_uses_api_key_header_and_generated_version_query(self):
        with patch(
            "app.llm.client.LLM_ALLOWED_BASE_URLS",
            ["https://tenant.openai.azure.com"],
        ):
            client=LLMClient(
                api_key="top-secret",
                provider="azure_openai",
                base_url="https://tenant.openai.azure.com",
                model="credit planner",
                temperature=0,
                api_version="2025-01-01-preview",
            )
        url,headers=client._request()
        self.assertEqual(headers["api-key"],"top-secret")
        self.assertNotIn("Authorization",headers)
        self.assertIn("/credit%20planner/chat/completions",url)
        self.assertTrue(url.endswith("api-version=2025-01-01-preview"))


class LLMConnectionContractTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        return LLMClient(
            api_key="test-secret",provider="deepseek",
            base_url="https://api.deepseek.com",model="deepseek-chat",
        )

    async def test_connection_test_requires_a_json_object_response(self):
        client=self.make_client()
        client.chat_json=AsyncMock(return_value='{"ping":"pong"}')
        self.assertEqual(await client.test(),'{"ping":"pong"}')

        for invalid in ("pong",'"pong"',"[1,2,3]"):
            with self.subTest(invalid=invalid):
                client.chat_json=AsyncMock(return_value=invalid)
                with self.assertRaises(RuntimeError):
                    await client.test()


class LLMConfigurationRepositorySafetyTests(unittest.TestCase):
    def test_summary_is_an_explicit_public_projection(self):
        row={
            "id":"19a9d86a-5971-4ab7-88a8-1acbb54018c0",
            "name":"Planner",
            "provider":"deepseek",
            "base_url":"https://api.deepseek.com",
            "model":"deepseek-chat",
            "temperature":0,
            "api_version":None,
            "status":"verified",
            "is_active":True,
            "api_key_configured":True,
            "version":2,
            "tested_version":2,
            "last_tested_at":None,
            "last_error":"Authorization: Bearer response-secret",
            "created_at":None,
            "updated_at":None,
            "api_key":"must-never-leak",
            "api_key_ciphertext":b"must-never-leak-either",
        }
        summary=PostgresLLMConfigurationRepository._summary(row)
        self.assertNotIn("api_key",summary)
        self.assertNotIn("api_key_ciphertext",summary)
        self.assertTrue(summary["api_key_configured"])
        self.assertEqual(summary["api_key_masked"],"••••••••")
        self.assertNotIn("response-secret",summary["last_error"])

    def test_missing_master_key_fails_closed_only_when_secret_is_needed(self):
        repository=PostgresLLMConfigurationRepository(object(),"")
        with self.assertRaises(LLMConfigurationEncryptionError):
            repository._key()


class MemoryCursor:
    def __init__(self,postgres):
        self.postgres=postgres
        self.result=None
        self.rowcount=0

    def __enter__(self):
        return self

    def __exit__(self,*_args):
        return False

    def execute(self,query,params=()):
        sql=" ".join(str(query).split())
        self.postgres.queries.append((sql,params))
        row=self.postgres.row
        self.result=None
        self.rowcount=0
        if "pg_advisory_xact_lock" in sql:
            self.result={"locked":True}
        elif sql.startswith("SELECT name,provider"):
            self.result=dict(row) if row else None
        elif "verification_status AS status" in sql and "WHERE id=%s" in sql:
            self.result=self.postgres.summary_row()
        elif sql.startswith("UPDATE mvp_control.llm_configurations SET name="):
            expected=params[-1]
            if row and row["version"]==expected:
                row.update({
                    "name":params[0],"provider":params[1],"base_url":params[2],
                    "model":params[3],"temperature":params[4],
                    "api_version":params[5],
                })
                if "verification_status='unverified'" in sql:
                    row.update({
                        "status":"unverified","tested_version":None,
                        "is_active":False,"last_error":None,
                    })
                elif row["tested_version"] is not None:
                    row["tested_version"]=row["version"]+1
                row["version"]+=1
                self.rowcount=1
        elif sql.startswith("UPDATE mvp_control.llm_configurations SET verification_status"):
            status,tested_version,error,success,_identifier,expected=params
            if row and row["version"]==expected:
                row.update({
                    "status":status,"tested_version":tested_version,
                    "last_error":error,
                })
                if not success:
                    row["is_active"]=False
                self.result={"id":row["id"]}
                self.rowcount=1
        elif sql.startswith("SELECT 1 AS found"):
            self.result={"found":1} if row else None
        elif sql.startswith("SELECT verification_status"):
            self.result={
                "verification_status":row["status"],
                "version":row["version"],
                "tested_version":row["tested_version"],
            } if row else None
        elif sql.startswith("SELECT is_active,version"):
            self.result={
                "is_active":row["is_active"],"version":row["version"]
            } if row else None
        elif sql.startswith("UPDATE mvp_control.llm_configurations SET is_active=false WHERE is_active"):
            self.rowcount=0
        elif sql.startswith("UPDATE mvp_control.llm_configurations SET is_active=true"):
            row["is_active"]=True
            self.rowcount=1
        elif sql.startswith("DELETE FROM mvp_control.llm_configurations"):
            expected=params[1]
            if row and row["version"]==expected:
                self.postgres.row=None
                self.rowcount=1

    def fetchone(self):
        return self.result


class MemoryConnection:
    def __init__(self,postgres):
        self.postgres=postgres

    def __enter__(self):
        return self

    def __exit__(self,*_args):
        return False

    def cursor(self):
        return MemoryCursor(self.postgres)


class MemoryPostgres:
    def __init__(self):
        self.queries=[]
        self.row={
            "id":"19a9d86a-5971-4ab7-88a8-1acbb54018c0",
            "name":"Planner","provider":"deepseek",
            "base_url":"https://api.deepseek.com","model":"deepseek-chat",
            "temperature":0.0,"api_version":None,
            "status":"verified","is_active":True,
            "version":1,"tested_version":1,"last_error":None,
        }

    def conn(self):
        return MemoryConnection(self)

    def summary_row(self):
        if not self.row:
            return None
        return {
            **self.row,"api_key_configured":True,
            "last_tested_at":None,"created_at":None,"updated_at":None,
        }


class LLMConfigurationConcurrencyTests(unittest.TestCase):
    def make_repository(self):
        postgres=MemoryPostgres()
        repository=PostgresLLMConfigurationRepository(postgres,"master-key")
        return repository,postgres

    def test_update_rejects_stale_version_and_takes_management_lock(self):
        repository,postgres=self.make_repository()
        with (
            patch.object(PostgresLLMConfigurationRepository,"_schema_ready",True),
            self.assertRaises(LLMConfigurationConflict),
        ):
            repository.update(
                postgres.row["id"],name="Other",provider="deepseek",
                base_url="https://api.deepseek.com",model="deepseek-chat",
                temperature=0,expected_version=99,
            )
        self.assertIn("pg_advisory_xact_lock",postgres.queries[0][0])
        self.assertEqual(postgres.row["version"],1)

    def test_display_only_update_carries_tested_version_and_active_state(self):
        repository,postgres=self.make_repository()
        with patch.object(
            PostgresLLMConfigurationRepository,"_schema_ready",True
        ):
            result=repository.update(
                postgres.row["id"],name="Renamed",provider="deepseek",
                base_url="https://api.deepseek.com",model="deepseek-chat",
                temperature=0,expected_version=1,
            )
        self.assertEqual(result["version"],2)
        self.assertEqual(result["tested_version"],2)
        self.assertTrue(result["is_active"])

    def test_stale_test_cannot_verify_edited_configuration(self):
        repository,postgres=self.make_repository()
        postgres.row.update({
            "version":2,"tested_version":None,"status":"unverified",
            "is_active":False,
        })
        with (
            patch.object(PostgresLLMConfigurationRepository,"_schema_ready",True),
            self.assertRaises(LLMConfigurationConflict),
        ):
            repository.mark_test_result(
                postgres.row["id"],tested_version=1,success=True
            )
        self.assertEqual(postgres.row["status"],"unverified")

    def test_activate_requires_current_successful_test(self):
        repository,postgres=self.make_repository()
        postgres.row.update({
            "version":2,"tested_version":1,"status":"verified",
            "is_active":False,
        })
        with (
            patch.object(PostgresLLMConfigurationRepository,"_schema_ready",True),
            self.assertRaises(LLMConfigurationConflict),
        ):
            repository.activate(postgres.row["id"])
        self.assertFalse(postgres.row["is_active"])

    def test_failed_retest_atomically_deactivates_model(self):
        repository,postgres=self.make_repository()
        with patch.object(
            PostgresLLMConfigurationRepository,"_schema_ready",True
        ):
            result=repository.mark_test_result(
                postgres.row["id"],tested_version=1,
                success=False,error="temporary upstream failure",
            )
        self.assertEqual(result["status"],"failed")
        self.assertFalse(result["is_active"])

    def test_delete_rejects_active_or_stale_configuration_version(self):
        repository,postgres=self.make_repository()
        with (
            patch.object(PostgresLLMConfigurationRepository,"_schema_ready",True),
            self.assertRaises(LLMConfigurationConflict),
        ):
            repository.delete(postgres.row["id"],expected_version=1)
        postgres.row["is_active"]=False
        with (
            patch.object(PostgresLLMConfigurationRepository,"_schema_ready",True),
            self.assertRaises(LLMConfigurationConflict),
        ):
            repository.delete(postgres.row["id"],expected_version=99)
        self.assertIsNotNone(postgres.row)


if __name__=="__main__":
    unittest.main()
