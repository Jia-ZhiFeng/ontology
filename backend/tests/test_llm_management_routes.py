import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.api import routes


ROOT=Path(__file__).resolve().parents[2]
GOLDEN=json.loads(
    (ROOT / "backend/tests/golden/personal_credit_phase1_contract.json")
    .read_text(encoding="utf-8")
)


class FakeManagedRepository:
    def __init__(self,active=None,runtime=None):
        self.active=active
        self.runtime=runtime
        self.metadata_calls=0
        self.runtime_calls=0

    def active_metadata(self):
        self.metadata_calls+=1
        return self.active

    def active_runtime(self):
        self.runtime_calls+=1
        return self.runtime


class FakeAgentService:
    def __init__(self,response=None):
        self.clients=[]
        self.calls=[]
        self.response=response

    async def run(self,application_id,instruction,*,llm_client,**kwargs):
        self.clients.append(llm_client)
        self.calls.append({
            "application_id":application_id,
            "instruction":instruction,
            "llm_client":llm_client,
            **kwargs,
        })
        return self.response or {
            "planner_mode":"llm" if llm_client else "deterministic"
        }


class FakeCircuit:
    def __init__(self):
        self.before_calls=0
        self.success_calls=0
        self.failures=[]

    def before_call(self):
        self.before_calls+=1

    def record_success(self):
        self.success_calls+=1

    def record_failure(self,*args):
        self.failures.append(args)


def request_with_key(value=None,authenticated=False):
    headers=[] if value is None else [(b"x-api-key",value.encode())]
    request=Request({
        "type":"http","method":"POST",
        "path":"/api/agents/personal-credit/run",
        "headers":headers,
    })
    if authenticated:
        request.state.mvp_authenticated=True
        request.state.mvp_auth_mode="local_session"
    return request


class ManagedAgentResolutionTests(unittest.IsolatedAsyncioTestCase):
    def run_request(self):
        return routes.PersonalCreditAgentRunRequest(
            application_id="APP-001",instruction="检查当前申请"
        )

    async def test_anonymous_active_model_rejects_before_secret_decryption(self):
        repository=FakeManagedRepository(active={"id":"model-1"})
        service=FakeAgentService()
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(routes,"_llm_configuration_repository",return_value=repository),
            patch.object(routes,"_personal_credit_agent",return_value=service),
        ):
            with self.assertRaises(HTTPException) as raised:
                await routes.run_personal_credit_agent(
                    self.run_request(),request_with_key()
                )
        self.assertEqual(raised.exception.status_code,401)
        self.assertEqual(repository.metadata_calls,1)
        self.assertEqual(repository.runtime_calls,0)
        self.assertEqual(service.clients,[])

    async def test_ontology_mode_fails_before_auth_or_model_metadata(self):
        repository=FakeManagedRepository(active={"id":"model-1"})
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(routes,"PERSONAL_CREDIT_AGENT_MODE","ontology_agent"),
            patch.object(
                routes,"_llm_configuration_repository",return_value=repository
            ),
            patch.object(routes,"_personal_credit_agent") as agent_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                await routes.run_personal_credit_agent(
                    self.run_request(),request_with_key()
                )
        self.assertEqual(raised.exception.status_code,503)
        self.assertEqual(
            raised.exception.detail["code"],"ONTOLOGY_AGENT_NOT_READY"
        )
        self.assertEqual(repository.metadata_calls,0)
        self.assertEqual(repository.runtime_calls,0)
        agent_factory.assert_not_called()

    async def test_authenticated_agent_uses_only_verified_active_runtime(self):
        runtime={
            "api_key":"provider-secret",
            "provider":"deepseek",
            "base_url":"https://api.deepseek.com",
            "model":"deepseek-chat",
            "temperature":0,
            "api_version":None,
        }
        repository=FakeManagedRepository(active={"id":"model-1"},runtime=runtime)
        service=FakeAgentService()
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(routes,"_llm_configuration_repository",return_value=repository),
            patch.object(routes,"_personal_credit_agent",return_value=service),
            patch.object(routes,"_external_circuit",return_value=FakeCircuit()),
        ):
            result=await routes.run_personal_credit_agent(
                self.run_request(),request_with_key("server-secret")
            )
        self.assertEqual(result["planner_mode"],"llm")
        self.assertEqual(result["agent_mode"],"legacy")
        self.assertEqual(repository.runtime_calls,1)
        self.assertEqual(len(service.clients),1)

    async def test_ontology_mode_fails_before_secret_or_runtime(self):
        repository=FakeManagedRepository(
            active={"id":"model-1"},
            runtime={"api_key":"must-not-be-decrypted"},
        )
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(routes,"PERSONAL_CREDIT_AGENT_MODE","ontology_agent"),
            patch.object(
                routes,"_llm_configuration_repository",return_value=repository
            ),
            patch.object(routes,"_personal_credit_agent") as agent_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                await routes.run_personal_credit_agent(
                    self.run_request(),request_with_key("server-secret")
                )

        self.assertEqual(raised.exception.status_code,503)
        self.assertEqual(
            raised.exception.detail["code"],"ONTOLOGY_AGENT_NOT_READY"
        )
        self.assertEqual(raised.exception.detail["agent_mode"],"ontology_agent")
        self.assertEqual(raised.exception.detail["rollback_mode"],"legacy")
        self.assertEqual(repository.metadata_calls,0)
        self.assertEqual(repository.runtime_calls,0)
        agent_factory.assert_not_called()

    async def test_local_session_agent_uses_verified_active_runtime_without_header(self):
        runtime={
            "api_key":"provider-secret",
            "provider":"deepseek",
            "base_url":"https://api.deepseek.com",
            "model":"deepseek-chat",
            "temperature":0,
            "api_version":None,
        }
        repository=FakeManagedRepository(active={"id":"model-1"},runtime=runtime)
        service=FakeAgentService()
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(routes,"_llm_configuration_repository",return_value=repository),
            patch.object(routes,"_personal_credit_agent",return_value=service),
            patch.object(routes,"_external_circuit",return_value=FakeCircuit()),
        ):
            result=await routes.run_personal_credit_agent(
                self.run_request(),request_with_key(authenticated=True)
            )
        self.assertEqual(result["planner_mode"],"llm")
        self.assertEqual(repository.runtime_calls,1)
        self.assertEqual(len(service.clients),1)

    async def test_no_active_model_preserves_deterministic_agent_without_login(self):
        repository=FakeManagedRepository(active=None)
        service=FakeAgentService()
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(routes,"_llm_configuration_repository",return_value=repository),
            patch.object(routes,"_personal_credit_agent",return_value=service),
        ):
            result=await routes.run_personal_credit_agent(
                self.run_request(),request_with_key()
            )
        self.assertEqual(result["planner_mode"],"deterministic")
        self.assertEqual(repository.runtime_calls,0)
        self.assertEqual(service.clients,[None])

    async def test_run_preserves_legacy_request_arguments_and_only_adds_mode(self):
        repository=FakeManagedRepository(active=None)
        service=FakeAgentService({"existing_contract":"preserved"})
        request=routes.PersonalCreditAgentRunRequest(
            application_id="APP-001",
            instruction="检查风险和材料",
            ontology_version="1.1.0",
            execute=False,
            max_steps=4,
            conversation_id="pc_0123456789abcdef0123456789abcdef",
        )
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(
                routes,"_llm_configuration_repository",return_value=repository
            ),
            patch.object(
                routes,"_personal_credit_agent",return_value=service
            ) as agent_factory,
        ):
            result=await routes.run_personal_credit_agent(
                request,request_with_key()
            )

        agent_factory.assert_called_once_with("1.1.0")
        self.assertEqual(service.calls,[{
            "application_id":"APP-001",
            "instruction":"检查风险和材料",
            "llm_client":None,
            "execute":False,
            "max_steps":4,
            "conversation_id":"pc_0123456789abcdef0123456789abcdef",
        }])
        self.assertEqual(result,{
            "existing_contract":"preserved",
            "agent_mode":"legacy",
        })
        self.assertEqual(
            set(result)-{"existing_contract"},
            set(GOLDEN["agent"]["route_additive_fields"]),
        )

    async def test_synthesis_fallback_records_model_quality_failure_but_returns_result(self):
        runtime={
            "api_key":"provider-secret",
            "provider":"deepseek",
            "base_url":"https://api.deepseek.com",
            "model":"deepseek-chat",
            "temperature":0,
            "api_version":None,
        }
        repository=FakeManagedRepository(active={"id":"model-1"},runtime=runtime)
        service=FakeAgentService({
            "response_type":"ACTION_RUN",
            "answer":"已保留确定性风险结果。",
            "final_answer":"已保留确定性风险结果。",
            "executed":True,
            "degraded":True,
            "execution_summary":{"status":"COMPLETED"},
            "synthesis":{
                "status":"FALLBACK",
                "reason_code":"MODEL_SYNTHESIS_PROCESS_ONLY",
                "model_called":True,
            },
            "model_calls":1,
        })
        circuit=FakeCircuit()
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(routes,"_llm_configuration_repository",return_value=repository),
            patch.object(routes,"_personal_credit_agent",return_value=service),
            patch.object(routes,"_external_circuit",return_value=circuit),
        ):
            result=await routes.run_personal_credit_agent(
                self.run_request(),request_with_key("server-secret")
            )

        self.assertEqual(result["answer"],"已保留确定性风险结果。")
        self.assertEqual(circuit.success_calls,0)
        self.assertEqual(len(circuit.failures),1)
        self.assertEqual(circuit.failures[0][0],"AGENT_MODEL_QUALITY_DEGRADED")
        self.assertIn("MODEL_SYNTHESIS_PROCESS_ONLY",circuit.failures[0][1])

    def test_agent_rejects_legacy_inline_llm_override(self):
        with self.assertRaises(ValidationError):
            routes.PersonalCreditAgentRunRequest.model_validate({
                "application_id":"APP-001",
                "instruction":"检查当前申请",
                "llm":{
                    "api_key":"must-not-be-accepted",
                    "provider":"deepseek",
                    "base_url":"https://api.deepseek.com",
                    "model":"deepseek-chat",
                },
            })

    def test_agent_request_cannot_override_server_runtime_mode(self):
        with self.assertRaises(ValidationError):
            routes.PersonalCreditAgentRunRequest.model_validate({
                "application_id":"APP-001",
                "instruction":"检查当前申请",
                "agent_mode":"ontology_agent",
            })

    async def test_tool_catalog_reports_server_runtime_mode(self):
        with patch.object(routes,"PERSONAL_CREDIT_AGENT_MODE","legacy"):
            result=await routes.personal_credit_agent_tools()
        self.assertEqual(result["agent_runtime"],{
            "mode":"legacy",
            "available":True,
            "rollback_mode":"legacy",
            "ontology_agent_status":"not_selected",
        })

    async def test_ontology_mode_blocks_readiness_and_direct_tool_factory(self):
        request=routes.PersonalCreditAgentToolRequest(
            application_id="APP-001"
        )
        with (
            patch.object(routes,"PERSONAL_CREDIT_AGENT_MODE","ontology_agent"),
            patch.object(routes,"_personal_credit_agent") as agent_factory,
        ):
            with self.assertRaises(HTTPException) as readiness_error:
                await routes.personal_credit_agent_readiness("APP-001")
            with self.assertRaises(HTTPException) as tool_error:
                await routes.execute_personal_credit_agent_tool(
                    "inspect_case_readiness",request
                )
        self.assertEqual(readiness_error.exception.status_code,503)
        self.assertEqual(tool_error.exception.status_code,503)
        agent_factory.assert_not_called()


class ManagedConfigurationRequestTests(unittest.TestCase):
    def test_secret_is_redacted_in_request_representation(self):
        request=routes.LLMManagedConfigCreate(
            name="Planner",
            provider="deepseek",
            model="deepseek-chat",
            api_key="provider-secret-value",
        )
        self.assertNotIn("provider-secret-value",repr(request))
        self.assertEqual(
            request.api_key.get_secret_value(),"provider-secret-value"
        )


if __name__=="__main__":
    unittest.main()
