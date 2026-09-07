import asyncio
import json
import unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import MagicMock,patch

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.api import routes
from app.agent.capability_registry import (
    PublishedReadProjectionAttestation,
)
from app.agent.read_projection_repository import (
    ReadProjectionAttestationRepositoryError,
)
from app.ontology.audit import proposal_digest
from app.ontology.baseline import build_personal_credit_baseline


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


class FakePublishedVersionStore:
    def __init__(self,record):
        self.record=record

    def active(self):
        return self.record

    def get(self,version):
        if version!=self.record["version"]:
            raise AssertionError("unexpected ontology version")
        return self.record


class FakeReadAttestationRepository:
    def __init__(self,*,artifact=None,error=None):
        self.artifact=artifact
        self.error=error
        self.get_calls=[]

    def get_publication(self,**scope):
        self.get_calls.append(scope)
        if self.error is not None:
            raise self.error
        return self.artifact


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

    @staticmethod
    def published_record():
        proposal=build_personal_credit_baseline()
        digest=proposal_digest(proposal)
        return proposal,{
            "version":proposal.version,
            "status":"published",
            "proposal":proposal.model_dump(mode="json"),
            "proposal_digest":digest,
            "audit_digest":digest,
            "is_active":True,
            "published_at":datetime(2026,9,7,tzinfo=timezone.utc),
        }

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

    async def test_ontology_mode_requires_auth_before_runtime_or_secret(self):
        repository=FakeManagedRepository(active={"id":"model-1"})
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(routes,"PERSONAL_CREDIT_AGENT_MODE","ontology_agent"),
            patch.object(routes,"ONTOLOGY_AGENT_CONTEXT_HMAC_SECRET","c"*32),
            patch.object(
                routes,"ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY","r"*32
            ),
            patch.object(
                routes,"_llm_configuration_repository",return_value=repository
            ) as repository_factory,
            patch.object(routes,"_personal_credit_agent") as agent_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                await routes.run_personal_credit_agent(
                    self.run_request(),request_with_key()
                )
        self.assertEqual(raised.exception.status_code,401)
        self.assertEqual(repository.metadata_calls,0)
        self.assertEqual(repository.runtime_calls,0)
        repository_factory.assert_not_called()
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

    async def test_ontology_mode_missing_safe_config_fails_before_model_secret(self):
        repository=FakeManagedRepository(
            active={"id":"model-1"},
            runtime={"api_key":"must-not-be-decrypted"},
        )
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(routes,"PERSONAL_CREDIT_AGENT_MODE","ontology_agent"),
            patch.object(routes,"ONTOLOGY_AGENT_CONTEXT_HMAC_SECRET",""),
            patch.object(routes,"ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY",""),
            patch.object(
                routes,"_llm_configuration_repository",return_value=repository
            ) as repository_factory,
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
        repository_factory.assert_not_called()
        self.assertEqual(repository.metadata_calls,0)
        self.assertEqual(repository.runtime_calls,0)

    async def test_ontology_mode_requires_active_model_without_keyword_fallback(self):
        repository=FakeManagedRepository(active=None)
        with (
            patch.object(routes,"PERSONAL_CREDIT_AGENT_MODE","ontology_agent"),
            patch.object(routes,"ONTOLOGY_AGENT_CONTEXT_HMAC_SECRET","c"*32),
            patch.object(
                routes,"ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY","r"*32
            ),
            patch.object(
                routes,"_llm_configuration_repository",return_value=repository
            ),
            patch.object(routes,"_personal_credit_agent") as agent_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                await routes.run_personal_credit_agent(
                    self.run_request(),request_with_key(authenticated=True)
                )

        self.assertEqual(raised.exception.status_code,503)
        self.assertEqual(
            raised.exception.detail["code"],"ONTOLOGY_AGENT_MODEL_REQUIRED"
        )
        self.assertEqual(repository.runtime_calls,0)
        agent_factory.assert_not_called()

    async def test_missing_read_attestation_fails_before_model_secret(self):
        repository=FakeManagedRepository(
            active={"id":"model-1"},
            runtime={"api_key":"must-not-be-decrypted"},
        )
        error=ReadProjectionAttestationRepositoryError(
            "AGENT_READ_ATTESTATION_MISSING","缺少发布证明"
        )
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(routes,"PERSONAL_CREDIT_AGENT_MODE","ontology_agent"),
            patch.object(routes,"ONTOLOGY_AGENT_CONTEXT_HMAC_SECRET","c"*32),
            patch.object(
                routes,"ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY","r"*32
            ),
            patch.object(
                routes,"_llm_configuration_repository",return_value=repository
            ),
            patch.object(
                routes,"_build_ontology_personal_credit_agent",side_effect=error
            ) as ontology_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                await routes.run_personal_credit_agent(
                    self.run_request(),request_with_key("server-secret")
                )

        self.assertEqual(raised.exception.status_code,503)
        self.assertEqual(
            raised.exception.detail["code"],"AGENT_READ_ATTESTATION_MISSING"
        )
        self.assertEqual(repository.runtime_calls,0)
        ontology_factory.assert_called_once()

    async def test_attestation_scope_and_storage_fail_before_secret_or_snapshot(self):
        _proposal,record=self.published_record()
        for code in (
            "AGENT_READ_ATTESTATION_SCOPE_MISMATCH",
            "AGENT_READ_ATTESTATION_UNAVAILABLE",
        ):
            with self.subTest(code=code):
                managed=FakeManagedRepository(
                    active={"id":"model-1"},
                    runtime={"api_key":"must-not-be-decrypted"},
                )
                proof_repository=FakeReadAttestationRepository(error=(
                    ReadProjectionAttestationRepositoryError(
                        code,
                        "SELECT secret_value FROM internal_attestation_table",
                    )
                ))
                with (
                    patch.object(routes,"MVP_API_KEY","server-secret"),
                    patch.object(
                        routes,"PERSONAL_CREDIT_AGENT_MODE","ontology_agent"
                    ),
                    patch.object(
                        routes,
                        "ONTOLOGY_AGENT_CONTEXT_HMAC_SECRET",
                        "context-runtime-secret-at-least-32-bytes",
                    ),
                    patch.object(
                        routes,
                        "ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY",
                        "attestation-verification-key-at-least-32-bytes",
                    ),
                    patch.object(
                        routes,"version_store",FakePublishedVersionStore(record)
                    ),
                    patch.object(
                        routes,"_llm_configuration_repository",return_value=managed
                    ),
                    patch.object(
                        routes,
                        "_read_projection_attestation_repository",
                        return_value=proof_repository,
                    ),
                    patch.object(
                        routes,
                        "PersonalCreditFunctionDataAdapter",
                        side_effect=AssertionError("snapshot adapter must not exist"),
                    ) as adapter_factory,
                ):
                    with self.assertRaises(HTTPException) as raised:
                        await routes.run_personal_credit_agent(
                            self.run_request(),request_with_key("server-secret")
                        )

                self.assertEqual(raised.exception.status_code,503)
                self.assertEqual(raised.exception.detail["code"],code)
                self.assertEqual(raised.exception.detail["rollback_mode"],"legacy")
                self.assertNotIn("SELECT",json.dumps(
                    raised.exception.detail,ensure_ascii=False
                ))
                self.assertEqual(managed.runtime_calls,0)
                self.assertEqual(len(proof_repository.get_calls),1)
                adapter_factory.assert_not_called()

    async def test_wrong_attestation_signature_fails_before_secret_or_snapshot(self):
        proposal,record=self.published_record()
        wrong_artifact=PublishedReadProjectionAttestation.sign_publication_identity(
            ontology_id="personal-credit",
            ontology_version=proposal.version,
            ontology_digest=proposal_digest(proposal),
            catalog=routes._personal_credit_action_capability_catalog(proposal),
            signing_key="wrong-publication-key-at-least-32-bytes",
        )
        proof_repository=FakeReadAttestationRepository(artifact=wrong_artifact)
        managed=FakeManagedRepository(
            active={"id":"model-1"},
            runtime={"api_key":"must-not-be-decrypted"},
        )
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(routes,"PERSONAL_CREDIT_AGENT_MODE","ontology_agent"),
            patch.object(
                routes,
                "ONTOLOGY_AGENT_CONTEXT_HMAC_SECRET",
                "context-runtime-secret-at-least-32-bytes",
            ),
            patch.object(
                routes,
                "ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY",
                "correct-verification-key-at-least-32-bytes",
            ),
            patch.object(
                routes,"version_store",FakePublishedVersionStore(record)
            ),
            patch.object(
                routes,"_llm_configuration_repository",return_value=managed
            ),
            patch.object(
                routes,
                "_read_projection_attestation_repository",
                return_value=proof_repository,
            ),
            patch.object(
                routes,
                "PersonalCreditFunctionDataAdapter",
                side_effect=AssertionError("snapshot adapter must not exist"),
            ) as adapter_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                await routes.run_personal_credit_agent(
                    self.run_request(),request_with_key("server-secret")
                )

        self.assertEqual(raised.exception.status_code,503)
        self.assertEqual(
            raised.exception.detail,
            {
                "code":"AGENT_CAPABILITY_ATTESTATION_REJECTED",
                "message":"只读能力发布证明无效",
                "rollback_mode":"legacy",
            },
        )
        self.assertEqual(managed.runtime_calls,0)
        self.assertEqual(len(proof_repository.get_calls),1)
        adapter_factory.assert_not_called()

    async def test_catalog_integrity_failure_is_safe_before_secret_or_snapshot(self):
        proposal,record=self.published_record()
        verification_key="attestation-verification-key-at-least-32-bytes"
        artifact=PublishedReadProjectionAttestation.sign_publication_identity(
            ontology_id="personal-credit",
            ontology_version=proposal.version,
            ontology_digest=proposal_digest(proposal),
            catalog=routes._personal_credit_action_capability_catalog(proposal),
            signing_key=verification_key,
        )
        proof_repository=FakeReadAttestationRepository(artifact=artifact)
        managed=FakeManagedRepository(
            active={"id":"model-1"},
            runtime={"api_key":"must-not-be-decrypted"},
        )
        catalog_error=RuntimeError("SELECT executor_binding FROM internal_catalog")
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(routes,"PERSONAL_CREDIT_AGENT_MODE","ontology_agent"),
            patch.object(
                routes,
                "ONTOLOGY_AGENT_CONTEXT_HMAC_SECRET",
                "context-runtime-secret-at-least-32-bytes",
            ),
            patch.object(
                routes,"ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY",verification_key
            ),
            patch.object(
                routes,"version_store",FakePublishedVersionStore(record)
            ),
            patch.object(
                routes,"_llm_configuration_repository",return_value=managed
            ),
            patch.object(
                routes,
                "_read_projection_attestation_repository",
                return_value=proof_repository,
            ),
            patch.object(
                routes,
                "_personal_credit_action_capability_catalog",
                side_effect=catalog_error,
            ),
            patch.object(
                routes,
                "PersonalCreditFunctionDataAdapter",
                side_effect=AssertionError("snapshot adapter must not exist"),
            ) as adapter_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                await routes.run_personal_credit_agent(
                    self.run_request(),request_with_key("server-secret")
                )

        self.assertEqual(raised.exception.status_code,503)
        self.assertEqual(raised.exception.detail,{
            "code":"AGENT_CAPABILITY_CATALOG_REJECTED",
            "message":"当前能力目录未通过后端校验",
            "rollback_mode":"legacy",
        })
        self.assertNotIn("SELECT",json.dumps(
            raised.exception.detail,ensure_ascii=False
        ))
        self.assertEqual(managed.runtime_calls,0)
        adapter_factory.assert_not_called()

    async def test_attested_catalog_is_pinned_before_case_snapshot(self):
        proposal,record=self.published_record()
        verification_key="attestation-verification-key-at-least-32-bytes"
        catalog=routes._personal_credit_action_capability_catalog(proposal)
        artifact=PublishedReadProjectionAttestation.sign_publication_identity(
            ontology_id="personal-credit",
            ontology_version=proposal.version,
            ontology_digest=proposal_digest(proposal),
            catalog=catalog,
            signing_key=verification_key,
        )
        proof_repository=FakeReadAttestationRepository(artifact=artifact)
        adapter=MagicMock()
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(
                routes,
                "ONTOLOGY_AGENT_CONTEXT_HMAC_SECRET",
                "context-runtime-secret-at-least-32-bytes",
            ),
            patch.object(
                routes,"ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY",verification_key
            ),
            patch.object(routes,"version_store",FakePublishedVersionStore(record)),
            patch.object(
                routes,
                "_read_projection_attestation_repository",
                return_value=proof_repository,
            ),
            patch.object(
                routes,
                "_personal_credit_action_capability_catalog",
                side_effect=(
                    catalog,
                    RuntimeError("late SELECT executor_binding FROM private_catalog"),
                ),
            ) as live_catalog_builder,
            patch.object(routes,"postgres",return_value=object()),
            patch.object(
                routes,"PersonalCreditFunctionDataAdapter",return_value=adapter
            ),
        ):
            service=routes._build_ontology_personal_credit_agent(
                ontology_version=proposal.version,
                request=request_with_key("server-secret"),
            )
            refreshed=service.registry._fresh_catalog()

        self.assertEqual(
            refreshed.model_dump(mode="json"),catalog.model_dump(mode="json")
        )
        self.assertEqual(live_catalog_builder.call_count,1)
        adapter.load_postgres_snapshot.assert_not_called()

    async def test_context_integrity_failure_is_safe_before_secret_or_snapshot(self):
        _proposal,record=self.published_record()
        record={**record,"audit_digest":"0"*64}
        managed=FakeManagedRepository(
            active={"id":"model-1"},
            runtime={"api_key":"must-not-be-decrypted"},
        )
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(routes,"PERSONAL_CREDIT_AGENT_MODE","ontology_agent"),
            patch.object(
                routes,
                "ONTOLOGY_AGENT_CONTEXT_HMAC_SECRET",
                "context-runtime-secret-at-least-32-bytes",
            ),
            patch.object(
                routes,
                "ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY",
                "attestation-verification-key-at-least-32-bytes",
            ),
            patch.object(
                routes,"version_store",FakePublishedVersionStore(record)
            ),
            patch.object(
                routes,"_llm_configuration_repository",return_value=managed
            ),
            patch.object(
                routes,"_read_projection_attestation_repository"
            ) as proof_repository_factory,
            patch.object(
                routes,
                "PersonalCreditFunctionDataAdapter",
                side_effect=AssertionError("snapshot adapter must not exist"),
            ) as adapter_factory,
        ):
            with self.assertRaises(HTTPException) as raised:
                await routes.run_personal_credit_agent(
                    self.run_request(),request_with_key("server-secret")
                )

        self.assertEqual(raised.exception.status_code,503)
        self.assertEqual(raised.exception.detail,{
            "code":"ONTOLOGY_CONTEXT_DIGEST_MISMATCH",
            "message":"当前已发布本体上下文未通过完整性校验",
            "rollback_mode":"legacy",
        })
        self.assertEqual(managed.runtime_calls,0)
        proof_repository_factory.assert_not_called()
        adapter_factory.assert_not_called()

    async def test_ontology_mode_selects_only_explicit_ontology_factory(self):
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
            "response_type":"CHAT",
            "answer":"本体 Agent 已完成。",
            "final_answer":"本体 Agent 已完成。",
            "executed":False,
            "execution_summary":{"status":"NEEDS_CLARIFICATION"},
            "synthesis":{
                "status":"COMPLETED","reason_code":None,"model_called":True,
            },
            "model_calls":1,
        })
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(routes,"PERSONAL_CREDIT_AGENT_MODE","ontology_agent"),
            patch.object(routes,"ONTOLOGY_AGENT_CONTEXT_HMAC_SECRET","c"*32),
            patch.object(
                routes,"ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY","r"*32
            ),
            patch.object(
                routes,"_llm_configuration_repository",return_value=repository
            ),
            patch.object(
                routes,"_build_ontology_personal_credit_agent",return_value=service
            ) as ontology_factory,
            patch.object(routes,"_external_circuit",return_value=FakeCircuit()),
            patch.object(
                routes,"PersonalCreditFunctionDataAdapter",
                side_effect=AssertionError("legacy factory must not run"),
            ),
        ):
            result=await routes.run_personal_credit_agent(
                self.run_request(),request_with_key("server-secret")
            )

        self.assertEqual(result["agent_mode"],"ontology_agent")
        self.assertEqual(repository.runtime_calls,1)
        ontology_factory.assert_called_once()

    async def test_ontology_route_enforces_one_hard_deadline(self):
        runtime={
            "api_key":"provider-secret",
            "provider":"deepseek",
            "base_url":"https://api.deepseek.com",
            "model":"deepseek-chat",
            "temperature":0,
            "api_version":None,
        }
        repository=FakeManagedRepository(active={"id":"model-1"},runtime=runtime)

        class SlowAgentService(FakeAgentService):
            async def run(self,*args,**kwargs):
                await asyncio.sleep(1)
                return await super().run(*args,**kwargs)

        service=SlowAgentService()
        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(routes,"PERSONAL_CREDIT_AGENT_MODE","ontology_agent"),
            patch.object(routes,"ONTOLOGY_AGENT_CONTEXT_HMAC_SECRET","c"*32),
            patch.object(
                routes,"ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY","r"*32
            ),
            patch.object(routes,"ONTOLOGY_AGENT_HARD_TIMEOUT_SECONDS",0.1),
            patch.object(
                routes,"_llm_configuration_repository",return_value=repository
            ),
            patch.object(
                routes,"_build_ontology_personal_credit_agent",return_value=service
            ),
            patch.object(routes,"_external_circuit",return_value=FakeCircuit()),
        ):
            with self.assertRaises(HTTPException) as raised:
                await routes.run_personal_credit_agent(
                    self.run_request(),request_with_key("server-secret")
                )

        self.assertEqual(raised.exception.status_code,503)
        self.assertEqual(
            raised.exception.detail["code"],"ONTOLOGY_AGENT_HARD_TIMEOUT"
        )

    async def test_ontology_telemetry_failure_keeps_completed_result(self):
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
            "answer":"已保留后端 Observation。",
            "final_answer":"已保留后端 Observation。",
            "executed":True,
            "execution_summary":{"status":"COMPLETED"},
            "synthesis":{
                "status":"COMPLETED","reason_code":None,"model_called":True,
            },
            "model_calls":2,
        })

        class BrokenTelemetry(FakeCircuit):
            def record_success(self):
                raise RuntimeError(
                    "SELECT password FROM private_table; password=hunter2"
                )

        with (
            patch.object(routes,"MVP_API_KEY","server-secret"),
            patch.object(routes,"PERSONAL_CREDIT_AGENT_MODE","ontology_agent"),
            patch.object(routes,"ONTOLOGY_AGENT_CONTEXT_HMAC_SECRET","c"*32),
            patch.object(
                routes,"ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY","r"*32
            ),
            patch.object(
                routes,"_llm_configuration_repository",return_value=repository
            ),
            patch.object(
                routes,"_build_ontology_personal_credit_agent",return_value=service
            ),
            patch.object(routes,"_external_circuit",return_value=BrokenTelemetry()),
            patch.object(routes.logger,"exception") as logged,
        ):
            result=await routes.run_personal_credit_agent(
                self.run_request(),request_with_key("server-secret")
            )

        self.assertEqual(result["final_answer"],"已保留后端 Observation。")
        self.assertEqual(result["agent_mode"],"ontology_agent")
        logged.assert_called_once()
        self.assertNotIn("hunter2",json.dumps(result,ensure_ascii=False))

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

    async def test_ontology_tool_catalog_reports_bound_runtime_contract(self):
        with (
            patch.object(
                routes,"PERSONAL_CREDIT_AGENT_MODE","ontology_agent"
            ),
            patch.object(
                routes,"ONTOLOGY_AGENT_CONTEXT_HMAC_SECRET","c" * 32
            ),
            patch.object(
                routes,"ONTOLOGY_AGENT_READ_ATTESTATION_HMAC_KEY","r" * 32
            ),
        ):
            result=await routes.personal_credit_agent_tools()

        self.assertEqual(result["tools"],[])
        self.assertEqual(result["agent_runtime"],{
            "mode":"ontology_agent",
            "available":True,
            "rollback_mode":"legacy",
            "ontology_agent_status":"ready",
        })
        planning=result["planning_boundary"]
        self.assertEqual(planning["response_types"],[
            "CHAT","NO_ACTION","ACTION_PLAN","ACTION_RUN",
        ])
        self.assertEqual(planning["max_llm_calls_per_turn"],3)
        self.assertEqual(planning["max_ontology_lookups_per_turn"],1)
        self.assertEqual(
            planning["explicit_business_routing"],
            "ontology_manifest_decide_observe_replan",
        )
        self.assertEqual(
            planning["execution_semantics"],
            "same_snapshot_observe_replan",
        )
        self.assertEqual(
            planning["result_synthesis"],
            "bounded_replan_with_deterministic_fallback",
        )
        self.assertEqual(planning["conversation"]["scope_bound_to"],[
            "application_id","ontology_id","ontology_version",
            "ontology_digest","principal",
        ])
        self.assertEqual(planning["trace_policy"]["stages"],[
            "plan","ontology_lookup","action","observation","replan",
            "clarification","answer","fallback",
        ])
        self.assertTrue(planning["model_arguments_allowed"])
        self.assertEqual(planning["unrecognized_instruction"],"model_decision")

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
