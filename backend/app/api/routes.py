import json
import logging
from contextvars import ContextVar
from datetime import datetime,timezone
from typing import Any
from uuid import UUID,uuid4

from fastapi import APIRouter,HTTPException,Query,Request,Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel,ConfigDict,Field,SecretStr
from ..core.config import *
from ..postgres.client import PostgresService
from ..postgres.business_lookup import BusinessLookupError,PersonalCreditBusinessLookup
from ..kg.nebula import NebulaGraphService
from ..llm.client import (
    LLMClient,
    LLMTransientError,
    normalize_llm_endpoint,
    provider_catalog,
)
from ..llm.config_repository import (
    LLMConfigurationConflict,
    LLMConfigurationEncryptionError,
    LLMConfigurationNotFound,
    PostgresLLMConfigurationRepository,
)
from ..ontology.generator import generate
from ..ontology.models import OntologyProposal
from ..ontology.validator import validate
from ..ontology.baseline import build_personal_credit_baseline
from ..ontology.audit import audit_personal_credit_proposal
from ..ontology.axiom_executor import axiom_executability,execute_axioms
from ..core.security import (
    external_llm_auth_error,
    mask_graph_snapshot,
    mask_payload,
)
from ..ontology.versioning import VersionWorkflowError,review_release_candidate
from ..ontology.version_repository import OntologyVersionRepository
from ..ontology.git_repository import OntologyGitError,OntologyGitRepository
from ..ontology.data_quality import validate_catalog_data_quality
from ..action.execution import (
    ActionAuthorizationError,
    ActionExecutionNotFound,
    ActionIdempotencyConflict,
    ActionRequestError,
)
from ..action.execution_repository import PostgresActionExecutionRepository
from ..action.action_design_catalog import (
    ACTION_RUNTIME_DESIGNS,
    validate_action_design_catalog,
)
from ..action.capabilities import (
    ActionCapabilityContractError,
    build_action_capability_catalog,
)
from ..action.business_functions import build_personal_credit_readonly_registry
from ..action.reusable_actions import (
    ONTOLOGY_PERSONAL_CREDIT,
    PERSONAL_CREDIT_ACTION_REUSE_PROFILES,
    ReusableActionAdapterRegistry,
    ReusableActionNotFound,
    ReusableActionRequestInvalid,
    build_action_reuse_audit,
)
from ..action.outbox import OutboxError,PostgresActionOutboxRepository
from ..action.outbox_replay import (
    OutboxReplayConflict,
    OutboxReplayError,
    OutboxReplayService,
    PostgresOutboxReplayRepository,
    build_outbox_replay_workers,
)
from ..action.operations import (
    DisabledAlertNotifier,
    ExternalCircuitBreaker,
    ExternalCircuitOpenError,
    OperationsError,
    OperationalAlertService,
    PostgresCircuitBreakerRepository,
    PostgresOperationalAlertRepository,
    WebhookAlertNotifier,
)
from ..action.create_loan_application import (
    CreateLoanApplicationActionService,
    CreateLoanApplicationError,
)
from ..action.generate_risk_assessment import (
    GenerateRiskAssessmentActionService,
    GenerateRiskAssessmentError,
)
from ..action.credit_provider import (
    DisabledConsentVerifier,
    DisabledCreditProvider,
    PostgresCreditProviderCoordinator,
    SandboxConsentVerifier,
    SandboxCreditProvider,
)
from ..action.request_credit_report import (
    RequestCreditReportActionService,
    RequestCreditReportError,
)
from ..action.fraud_provider import (
    DisabledFraudProvider,
    PostgresFraudProviderCoordinator,
    SandboxFraudProvider,
)
from ..action.request_fraud_report import (
    RequestFraudReportActionService,
    RequestFraudReportError,
)
from ..action.phone_dispatch import (
    DisabledPhoneTaskDispatcher,
    PostgresPhoneDispatchCoordinator,
    SandboxPhoneTaskDispatcher,
)
from ..action.create_phone_verification_task import (
    CreatePhoneVerificationTaskActionService,
    CreatePhoneVerificationTaskError,
)
from ..action.record_phone_verification import (
    RecordPhoneVerificationActionService,
    RecordPhoneVerificationError,
)
from ..action.trigger_manual_review import (
    TriggerManualReviewActionService,
    TriggerManualReviewError,
)
from ..action.record_decision_recommendation import (
    RECOMMENDATION_ONLY_ASSERTION,
    RecordDecisionRecommendationActionService,
    RecordDecisionRecommendationError,
)
from ..action.function_data_adapter import (
    FunctionDataAdapterError,
    PersonalCreditFunctionDataAdapter,
)
from ..agent.personal_credit import (
    AgentPlanningError,
    PersonalCreditAgentService,
    READ_ONLY_AGENT_TOOLS,
)
from ..agent.conversation import (
    AgentConversationBusyError,
    AgentConversationError,
)
from ..agent.runtime import (
    AgentRuntimeUnavailableError,
    build_personal_credit_agent_runtime,
    personal_credit_agent_runtime_status,
    require_personal_credit_agent_runtime,
)
from ..action.workflow import (
    WORKFLOW_ACTIONS,
    WORKFLOW_ACTION_LABELS,
    WORKFLOW_DEPENDENCIES,
    WORKFLOW_STATUSES,
    OPTIONAL_WORKFLOW_ACTIONS,
    PostgresPersonalCreditWorkflowRepository,
    WorkflowError,
    workflow_request_digest,
)

router=APIRouter()
logger=logging.getLogger(__name__)
def postgres(): return PostgresService(POSTGRES_HOST,POSTGRES_PORT,POSTGRES_DATABASE,POSTGRES_USER,POSTGRES_PASSWORD)
def nebula(): return NebulaGraphService(NEBULA_HOST,NEBULA_PORT,NEBULA_USER,NEBULA_PASSWORD,NEBULA_SPACE)
version_store=OntologyVersionRepository(postgres())
ontology_git=OntologyGitRepository(postgres())
_ACTION_ONTOLOGY_VERSION_OVERRIDE:ContextVar[str|None]=ContextVar(
    "action_ontology_version_override",default=None
)

def error_detail(exc):
    message=str(exc).strip()
    return mask_payload(message or f"{type(exc).__name__}: 未提供详细错误信息")

class LLMConfig(BaseModel):
    model_config=ConfigDict(extra="forbid")
    api_key:SecretStr=Field(min_length=1,max_length=4096)
    base_url:str=Field(min_length=1,max_length=2048)
    model:str=Field(min_length=1,max_length=160)
    temperature:float=Field(default=0,ge=0,le=2)
    provider:str=Field(default="openai_compatible",min_length=1,max_length=32)
    api_version:str|None=Field(default=None,max_length=32)

class LLMManagedConfigCreate(BaseModel):
    model_config=ConfigDict(extra="forbid")
    name:str=Field(min_length=1,max_length=80)
    provider:str=Field(default="openai_compatible",min_length=1,max_length=32)
    base_url:str=Field(default="",max_length=2048)
    model:str=Field(min_length=1,max_length=160)
    temperature:float=Field(default=0,ge=0,le=2)
    api_version:str|None=Field(default=None,max_length=32)
    api_key:SecretStr=Field(min_length=1,max_length=4096)

class LLMManagedConfigUpdate(BaseModel):
    model_config=ConfigDict(extra="forbid")
    version:int=Field(ge=1)
    name:str=Field(min_length=1,max_length=80)
    provider:str=Field(default="openai_compatible",min_length=1,max_length=32)
    base_url:str=Field(default="",max_length=2048)
    model:str=Field(min_length=1,max_length=160)
    temperature:float=Field(default=0,ge=0,le=2)
    api_version:str|None=Field(default=None,max_length=32)
    # Missing, null and blank all mean "retain the existing encrypted key".
    api_key:SecretStr|None=Field(default=None,max_length=4096)

class PersonalCreditAgentRunRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    application_id:str=Field(min_length=1,max_length=32)
    instruction:str=Field(min_length=1,max_length=500)
    ontology_version:str|None=Field(default=None,max_length=64)
    execute:bool=True
    max_steps:int=Field(default=1,ge=1,le=4)
    conversation_id:str|None=Field(
        default=None,pattern=r"^pc_[0-9a-f]{32}$"
    )

class PersonalCreditAgentToolRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    application_id:str=Field(min_length=1,max_length=32)
    ontology_version:str|None=Field(default=None,max_length=64)

class ProfileRequest(BaseModel):
    limit:int=20

class GenerateRequest(BaseModel):
    llm:LLMConfig
    limit:int=20

class ProposalRequest(BaseModel):
    proposal:OntologyProposal

class DraftRequest(BaseModel):
    proposal:OntologyProposal
    notes:str=""

class WorkflowNoteRequest(BaseModel):
    note:str=""

class OntologyGitCommitRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    branch:str="main"
    proposal:OntologyProposal
    message:str
    author:str
    expected_head:str|None=None

class OntologyGitBranchRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    name:str
    from_ref:str="main"

class OntologyGitTagRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    name:str
    ref:str="main"
    message:str=""

class GraphQueryRequest(BaseModel):
    query:str=""
    hops:int=2
    limit:int=300
    offset:int=0
    mode:str="all"

class GenerateRiskAssessmentRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    application_id:str
    assessment_version:str
    data_snapshot_policy:str="repeatable_read_read_only"
    request_id:str

class NaturalPersonInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    id_number:str
    name:str
    phone:str|None=None
    gender:str|None=None
    address:str|None=None
    marital_status:str|None=None
    monthly_income:float|None=None
    employer:str|None=None
    guarantee_rating:str|None=None
    guarantee_count:int|None=None
    guarantee_total:float|None=None

class VehicleInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    vin:str
    brand:str
    model:str
    invoice_price:float
    guide_price:float|None=None
    pledge_status:str|None=None

class DealerInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    credit_code:str
    dealer_name:str
    location:str

class LoanApplicationInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    submit_date:str
    channel:str
    loan_amount:float
    loan_term:int
    interest_rate:float
    application_status:str="资料待补充"

class CreateLoanApplicationRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    application_id:str
    application:LoanApplicationInput
    borrower:NaturalPersonInput
    vehicle:VehicleInput
    dealer:DealerInput|None=None
    guarantor:NaturalPersonInput|None=None
    co_borrower:NaturalPersonInput|None=None
    request_id:str

class RequestCreditReportRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    application_id:str
    subject_id_number:str
    authorization_reference:str
    query_purpose:str="贷款审批"
    request_id:str

class RequestFraudReportRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    application_id:str
    request_id:str

class CreatePhoneVerificationTaskRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    application_id:str
    verification_round:int
    trigger_reason:str
    request_id:str

class RecordPhoneVerificationRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    verification_id:str
    callback_id:str
    verifier_identity:str
    call_time:str
    duration_seconds:int
    conclusion:str
    doubts:str|None=None
    recording_file_id:str|None=None

class TriggerManualReviewRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    application_id:str
    review_reason:str
    source_execution_id:str
    request_id:str

class RecordDecisionRecommendationRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    application_id:str
    recommendation_version:str
    approver_id:str
    approval_assertion:str
    request_id:str

class OutboxReplayRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    reason:str
    request_id:str

class AlertActionRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    note:str

class StartPersonalCreditWorkflowRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    application_id:str
    entry_mode:str="EXISTING"
    request_id:str

class ExecutePersonalCreditWorkflowStepRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    payload:dict[str,Any]

class WorkflowReasonRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    reason:str

class WorkflowDataSearchRequest(BaseModel):
    model_config=ConfigDict(extra="forbid")
    entity_type:str
    query:str
    limit:int=8

def _alert_notifier():
    if not ALERT_WEBHOOK_ENABLED:
        return DisabledAlertNotifier()
    return WebhookAlertNotifier(
        ALERT_WEBHOOK_URL,
        timeout_seconds=ALERT_WEBHOOK_TIMEOUT_SECONDS,
        allow_insecure_local=ALERT_WEBHOOK_ALLOW_INSECURE_LOCAL,
    )

def _alert_service(service_postgres):
    return OperationalAlertService(
        PostgresOperationalAlertRepository(service_postgres),
        _alert_notifier(),
        escalation_seconds=ALERT_ESCALATION_SECONDS,
        notification_cooldown_seconds=ALERT_NOTIFICATION_COOLDOWN_SECONDS,
    )

def _external_circuit(service_postgres,dependency_name:str):
    return ExternalCircuitBreaker(
        dependency_name,
        PostgresCircuitBreakerRepository(service_postgres),
        _alert_service(service_postgres),
        failure_threshold=EXTERNAL_CIRCUIT_FAILURE_THRESHOLD,
        open_seconds=EXTERNAL_CIRCUIT_OPEN_SECONDS,
    )

def _llm_configuration_repository(service_postgres=None):
    return PostgresLLMConfigurationRepository(
        service_postgres or postgres(),LLM_CONFIG_ENCRYPTION_KEY
    )

def _secret_text(value:SecretStr|str|None)->str:
    if isinstance(value,SecretStr):
        return value.get_secret_value()
    return str(value or "")

def _llm_client_from_values(values)->LLMClient:
    if isinstance(values,BaseModel):
        values=values.model_dump()
    payload=dict(values)
    payload["api_key"]=_secret_text(payload.get("api_key"))
    return LLMClient(**{
        key:payload.get(key)
        for key in (
            "api_key","base_url","model","temperature","provider","api_version"
        )
    })

def _normalize_managed_llm(values)->dict:
    name=values.name.strip()
    model=values.model.strip()
    if not name:
        raise ValueError("请填写模型配置名称")
    if not model:
        raise ValueError("请填写 LLM 模型名称")
    provider,base_url=normalize_llm_endpoint(values.provider,values.base_url)
    # Constructing without making a request validates model, temperature and
    # provider-specific settings (including Azure API version).
    validated=LLMClient(
        api_key="validation-only",
        provider=provider,
        base_url=base_url,
        model=model,
        temperature=values.temperature,
        api_version=values.api_version,
    )
    return {
        "name":name,
        "provider":validated.provider,
        "base_url":validated.base_url,
        "model":validated.model,
        "temperature":validated.temperature,
        "api_version":(
            validated.api_version if validated.provider=="azure_openai" else None
        ),
    }

def _raise_llm_configuration_http(error):
    if isinstance(error,LLMConfigurationNotFound):
        raise HTTPException(404,error_detail(error))
    if isinstance(error,LLMConfigurationConflict):
        raise HTTPException(409,error_detail(error))
    if isinstance(error,LLMConfigurationEncryptionError):
        raise HTTPException(503,error_detail(error))
    if isinstance(error,ValueError):
        raise HTTPException(422,error_detail(error))
    # Database/extension/driver failures are infrastructure failures. Do not
    # mislabel them as user input or expose connection/SQL diagnostics.
    logger.exception("LLM configuration operation failed")
    raise HTTPException(503,"模型配置服务暂不可用，请稍后重试")

def _store_llm_test_result(
    repository,configuration_id:str,runtime:dict,*,success:bool,error=None
):
    try:
        return repository.mark_test_result(
            configuration_id,tested_version=runtime["version"],
            success=success,error=error,
        )
    except Exception as storage_error:
        _raise_llm_configuration_http(storage_error)

def _record_llm_circuit_failure(circuit,code:str,detail):
    if not circuit:
        return
    try:
        circuit.record_failure(code,detail)
    except Exception as storage_error:
        _raise_llm_configuration_http(storage_error)

def _workflow_repository(service_postgres=None):
    return PostgresPersonalCreditWorkflowRepository(
        service_postgres or postgres(),
        dispatch_lease_seconds=WORKFLOW_DISPATCH_LEASE_SECONDS,
    )

def _workflow_http_status(code:str)->int:
    if code in {"WORKFLOW_NOT_FOUND","WORKFLOW_APPLICATION_NOT_FOUND"}: return 404
    if code in {
        "WORKFLOW_REQUEST_INVALID","WORKFLOW_STEP_NOT_FOUND",
        "WORKFLOW_APPLICATION_MISMATCH","WORKFLOW_SOURCE_EXECUTION_INVALID",
    }: return 422
    if code in {
        "WORKFLOW_IDEMPOTENCY_CONFLICT","WORKFLOW_APPLICATION_ACTIVE",
        "WORKFLOW_APPLICATION_ALREADY_EXISTS","WORKFLOW_NOT_READY",
        "WORKFLOW_STEP_NOT_READY","WORKFLOW_STEP_CONFLICT",
        "WORKFLOW_STEP_REQUIRED","WORKFLOW_TERMINAL",
        "WORKFLOW_ACTION_IN_PROGRESS","WORKFLOW_ONTOLOGY_VERSION_UNAVAILABLE",
    }: return 409
    return 500

def _raise_workflow_http(error:WorkflowError):
    raise HTTPException(
        _workflow_http_status(error.code),
        mask_payload({"code":error.code,"message":error_detail(error)}),
    )

def _proposal_from_record(record):
    return OntologyProposal.model_validate(record["proposal"])

def published_version_proposal(version:str):
    ensure_version_registry()
    record=version_store.get(str(version or "").strip())
    if record["status"]!="published":
        raise VersionWorkflowError(f"本体版本尚未发布：{version}")
    return _proposal_from_record(record)

def ensure_version_registry():
    if version_store.count(): return
    proposal=build_personal_credit_baseline(); profile=postgres().validation_profile()
    audit=review_release_candidate(proposal,profile)
    n=nebula()
    try:
        report=n.mapping_report(proposal)
        active=bool(report.get("valid") and report.get("tbox_version")==proposal.version)
    finally:
        n.close()
    version_store.bootstrap(proposal,audit,active)

def active_version_proposal():
    overridden_version=_ACTION_ONTOLOGY_VERSION_OVERRIDE.get()
    if overridden_version:
        return published_version_proposal(overridden_version)
    ensure_version_registry()
    active=version_store.active()
    return _proposal_from_record(active) if active else build_personal_credit_baseline()

def _agent_proposal(ontology_version:str|None):
    return (
        published_version_proposal(ontology_version)
        if ontology_version and ontology_version.strip()
        else active_version_proposal()
    )

def _personal_credit_agent(ontology_version:str|None):
    def legacy_factory():
        proposal=_agent_proposal(ontology_version)
        return PersonalCreditAgentService(
            PersonalCreditFunctionDataAdapter(
                postgres(),nebula,proposal=proposal
            ),
            ontology_version=proposal.version,
        )

    return build_personal_credit_agent_runtime(
        mode=PERSONAL_CREDIT_AGENT_MODE,
        legacy_factory=legacy_factory,
    )


def _agent_runtime_mode_or_http():
    try:
        return require_personal_credit_agent_runtime(
            PERSONAL_CREDIT_AGENT_MODE
        )
    except AgentRuntimeUnavailableError as error:
        raise HTTPException(503,{
            "code":error.code,
            "message":error_detail(error),
            "agent_mode":error.mode.value,
            "rollback_mode":"legacy",
        }) from error

def ensure_ontology_git():
    ontology_git.bootstrap(active_version_proposal())

def _risk_action_http_status(code:str)->int:
    if code=="ACTION_ROLE_DENIED": return 403
    if code in {
        "ACTION_IDEMPOTENCY_CONFLICT","ACTION_ASSESSMENT_ALREADY_EXISTS",
        "ACTION_EXECUTION_IN_PROGRESS",
    }: return 409
    if code in {
        "ACTION_REQUEST_INVALID","ACTION_ASSESSMENT_PREREQUISITE_FAILED",
        "ACTION_MONTHLY_PAYMENT_UNAVAILABLE","ACTION_FUNCTION_NOT_BOUND",
        "ACTION_FUNCTION_FAILED","ACTION_FUNCTION_OUTPUT_INVALID",
    }: return 422
    return 500

def _create_application_http_status(code:str)->int:
    if code=="ACTION_ROLE_DENIED": return 403
    if code in {
        "ACTION_IDEMPOTENCY_CONFLICT","ACTION_APPLICATION_ALREADY_EXISTS",
        "ACTION_MASTER_DATA_CONFLICT","ACTION_EXECUTION_IN_PROGRESS",
    }: return 409
    if code in {"ACTION_REQUEST_INVALID","ACTION_AXIOM_VIOLATION"}: return 422
    return 500

def _credit_report_http_status(code:str)->int:
    if code in {"ACTION_ROLE_DENIED","ACTION_CREDIT_CONSENT_MISSING"}: return 403
    if code=="ACTION_SUBJECT_NOT_FOUND": return 404
    if code in {
        "ACTION_IDEMPOTENCY_CONFLICT","ACTION_CREDIT_REPORT_ALREADY_EXISTS",
        "ACTION_EXECUTION_IN_PROGRESS",
    }: return 409
    if code=="ACTION_CREDIT_PROVIDER_NOT_CONFIGURED": return 503
    if code=="ACTION_EXTERNAL_CIRCUIT_OPEN": return 503
    if code=="ACTION_EXTERNAL_TIMEOUT": return 504
    if code=="ACTION_EXTERNAL_RESPONSE_INVALID": return 502
    if code=="ACTION_REQUEST_INVALID": return 422
    return 500

def _credit_consent_verifier():
    if CREDIT_CONSENT_MODE=="sandbox":
        return SandboxConsentVerifier(CREDIT_SANDBOX_CONSENT_PREFIX)
    return DisabledConsentVerifier()

def _credit_provider():
    if CREDIT_PROVIDER_MODE=="sandbox": return SandboxCreditProvider()
    return DisabledCreditProvider()

def _fraud_report_http_status(code:str)->int:
    if code=="ACTION_ROLE_DENIED": return 403
    if code in {
        "ACTION_IDEMPOTENCY_CONFLICT","ACTION_FRAUD_REPORT_ALREADY_EXISTS",
        "ACTION_EXECUTION_IN_PROGRESS",
    }: return 409
    if code=="ACTION_FRAUD_PROVIDER_NOT_CONFIGURED": return 503
    if code=="ACTION_EXTERNAL_CIRCUIT_OPEN": return 503
    if code=="ACTION_EXTERNAL_TIMEOUT": return 504
    if code=="ACTION_EXTERNAL_RESPONSE_INVALID": return 502
    if code in {
        "ACTION_REQUEST_INVALID","ACTION_APPLICATION_INCOMPLETE",
        "ACTION_FUNCTION_NOT_BOUND","ACTION_FUNCTION_FAILED",
        "ACTION_FUNCTION_OUTPUT_INVALID",
    }: return 422
    return 500

def _fraud_provider():
    if FRAUD_PROVIDER_MODE=="sandbox": return SandboxFraudProvider()
    return DisabledFraudProvider()

def _phone_task_http_status(code:str)->int:
    if code=="ACTION_ROLE_DENIED": return 403
    if code in {
        "ACTION_IDEMPOTENCY_CONFLICT","ACTION_PHONE_ROUND_ALREADY_EXISTS",
        "ACTION_EXECUTION_IN_PROGRESS",
    }: return 409
    if code in {
        "ACTION_REQUEST_INVALID","ACTION_PHONE_PREREQUISITE_FAILED",
        "ACTION_PHONE_TRIGGER_NOT_MET","ACTION_FUNCTION_NOT_BOUND",
        "ACTION_FUNCTION_FAILED","ACTION_QUESTION_SET_INVALID",
    }: return 422
    if code=="ACTION_DISPATCH_PENDING": return 503
    if code=="ACTION_EXTERNAL_CIRCUIT_OPEN": return 503
    if code=="ACTION_EXTERNAL_TIMEOUT": return 504
    if code=="ACTION_EXTERNAL_RESPONSE_INVALID": return 502
    return 500

def _phone_task_dispatcher():
    if PHONE_TASK_DISPATCH_MODE=="sandbox": return SandboxPhoneTaskDispatcher()
    return DisabledPhoneTaskDispatcher()

def _phone_result_http_status(code:str)->int:
    if code in {"ACTION_ROLE_DENIED","ACTION_PHONE_TASK_NOT_ASSIGNED"}: return 403
    if code=="ACTION_PHONE_TASK_NOT_FOUND": return 404
    if code in {
        "ACTION_IDEMPOTENCY_CONFLICT","ACTION_TERMINAL_STATE_CONFLICT",
        "ACTION_EXECUTION_IN_PROGRESS",
    }: return 409
    if code in {"ACTION_REQUEST_INVALID","ACTION_PHONE_RESULT_INVALID"}: return 422
    return 500

def _manual_review_http_status(code:str)->int:
    if code=="ACTION_ROLE_DENIED": return 403
    if code in {
        "ACTION_IDEMPOTENCY_CONFLICT","ACTION_EXECUTION_IN_PROGRESS",
    }: return 409
    if code in {
        "ACTION_REQUEST_INVALID","ACTION_MANUAL_REVIEW_RULE_NOT_MET",
        "ACTION_FUNCTION_NOT_BOUND","ACTION_EVIDENCE_INCOMPLETE",
    }: return 422
    return 500

def _decision_recommendation_http_status(code:str)->int:
    if code in {"ACTION_ROLE_DENIED","ACTION_APPROVAL_REQUIRED"}: return 403
    if code in {
        "ACTION_IDEMPOTENCY_CONFLICT","ACTION_EXECUTION_IN_PROGRESS",
    }: return 409
    if code in {
        "ACTION_REQUEST_INVALID","ACTION_REQUIRED_REPORT_MISSING",
        "ACTION_FUNCTION_NOT_BOUND","ACTION_RECOMMENDATION_INVALID",
        "ACTION_HIGH_RISK_PASS_FORBIDDEN",
    }: return 422
    return 500

@router.post("/actions/create-loan-application/execute")
async def execute_create_loan_application(req:CreateLoanApplicationRequest):
    service_postgres=postgres()
    execution_repository=PostgresActionExecutionRepository(service_postgres)
    proposal=active_version_proposal()
    service=CreateLoanApplicationActionService(
        postgres_service=service_postgres,
        nebula_factory=nebula,
        proposal=proposal,
        execution_repository=execution_repository,
        outbox_max_attempts=ACTION_OUTBOX_MAX_ATTEMPTS,
    )
    try:
        result=mask_payload(service.execute(
            req.model_dump(),
            ontology_version=proposal.version,
            actor_id=MVP_CREATE_APPLICATION_ACTOR_ID,
            actor_role=MVP_CREATE_APPLICATION_ACTOR_ROLE,
            correlation_id=req.request_id,
        ))
        if result.get("status")=="COMMITTING":
            return JSONResponse(status_code=202,content=jsonable_encoder(result))
        return result
    except ActionAuthorizationError as e:
        raise HTTPException(403,mask_payload({"code":"ACTION_ROLE_DENIED","message":error_detail(e)}))
    except ActionIdempotencyConflict as e:
        raise HTTPException(409,mask_payload({"code":"ACTION_IDEMPOTENCY_CONFLICT","message":error_detail(e)}))
    except ActionRequestError as e:
        raise HTTPException(422,mask_payload({"code":"ACTION_REQUEST_INVALID","message":error_detail(e)}))
    except CreateLoanApplicationError as e:
        raise HTTPException(_create_application_http_status(e.code),mask_payload({
            "code":e.code,"message":error_detail(e),
            "execution_id":e.execution_id,"status":e.status,
        }))

@router.post("/actions/generate-risk-assessment/execute")
async def execute_generate_risk_assessment(req:GenerateRiskAssessmentRequest):
    service_postgres=postgres()
    execution_repository=PostgresActionExecutionRepository(service_postgres)
    proposal=active_version_proposal()
    service=GenerateRiskAssessmentActionService(
        postgres_service=service_postgres,
        nebula_factory=nebula,
        proposal=proposal,
        execution_repository=execution_repository,
        outbox_max_attempts=ACTION_OUTBOX_MAX_ATTEMPTS,
    )
    try:
        result=mask_payload(service.execute(
            req.model_dump(),
            ontology_version=proposal.version,
            actor_id=MVP_ACTION_ACTOR_ID,
            actor_role=MVP_ACTION_ACTOR_ROLE,
            correlation_id=req.request_id,
        ))
        if result.get("status")=="COMMITTING":
            return JSONResponse(status_code=202,content=jsonable_encoder(result))
        return result
    except ActionAuthorizationError as e:
        raise HTTPException(403,mask_payload({"code":"ACTION_ROLE_DENIED","message":error_detail(e)}))
    except ActionIdempotencyConflict as e:
        raise HTTPException(409,mask_payload({"code":"ACTION_IDEMPOTENCY_CONFLICT","message":error_detail(e)}))
    except ActionRequestError as e:
        raise HTTPException(422,mask_payload({"code":"ACTION_REQUEST_INVALID","message":error_detail(e)}))
    except GenerateRiskAssessmentError as e:
        raise HTTPException(_risk_action_http_status(e.code),mask_payload({
            "code":e.code,"message":error_detail(e),
            "execution_id":e.execution_id,"status":e.status,
        }))

@router.post("/actions/request-credit-report/execute")
async def execute_request_credit_report(req:RequestCreditReportRequest):
    service_postgres=postgres()
    execution_repository=PostgresActionExecutionRepository(service_postgres)
    proposal=active_version_proposal()
    service=RequestCreditReportActionService(
        postgres_service=service_postgres,
        nebula_factory=nebula,
        proposal=proposal,
        execution_repository=execution_repository,
        consent_verifier=_credit_consent_verifier(),
        provider_coordinator=PostgresCreditProviderCoordinator(
            service_postgres,_credit_provider(),
            _external_circuit(service_postgres,"credit-provider")
        ),
        outbox_max_attempts=ACTION_OUTBOX_MAX_ATTEMPTS,
    )
    try:
        result=mask_payload(service.execute(
            req.model_dump(),
            ontology_version=proposal.version,
            actor_id=MVP_CREDIT_REPORT_ACTOR_ID,
            actor_role=MVP_CREDIT_REPORT_ACTOR_ROLE,
            correlation_id=req.request_id,
        ))
        if result.get("status")=="COMMITTING":
            return JSONResponse(status_code=202,content=jsonable_encoder(result))
        return result
    except ActionAuthorizationError as e:
        raise HTTPException(403,mask_payload({"code":"ACTION_ROLE_DENIED","message":error_detail(e)}))
    except ActionIdempotencyConflict as e:
        raise HTTPException(409,mask_payload({"code":"ACTION_IDEMPOTENCY_CONFLICT","message":error_detail(e)}))
    except ActionRequestError as e:
        raise HTTPException(422,mask_payload({"code":"ACTION_REQUEST_INVALID","message":error_detail(e)}))
    except RequestCreditReportError as e:
        raise HTTPException(_credit_report_http_status(e.code),mask_payload({
            "code":e.code,"message":error_detail(e),
            "execution_id":e.execution_id,"status":e.status,
        }))

@router.post("/actions/request-fraud-report/execute")
async def execute_request_fraud_report(req:RequestFraudReportRequest):
    service_postgres=postgres()
    execution_repository=PostgresActionExecutionRepository(service_postgres)
    proposal=active_version_proposal()
    service=RequestFraudReportActionService(
        postgres_service=service_postgres,
        nebula_factory=nebula,
        proposal=proposal,
        execution_repository=execution_repository,
        provider_coordinator=PostgresFraudProviderCoordinator(
            service_postgres,_fraud_provider(),
            _external_circuit(service_postgres,"fraud-provider")
        ),
        outbox_max_attempts=ACTION_OUTBOX_MAX_ATTEMPTS,
    )
    try:
        result=mask_payload(service.execute(
            req.model_dump(),
            ontology_version=proposal.version,
            actor_id=MVP_FRAUD_REPORT_ACTOR_ID,
            actor_role=MVP_FRAUD_REPORT_ACTOR_ROLE,
            correlation_id=req.request_id,
        ))
        if result.get("status")=="COMMITTING":
            return JSONResponse(status_code=202,content=jsonable_encoder(result))
        return result
    except ActionAuthorizationError as e:
        raise HTTPException(403,mask_payload({"code":"ACTION_ROLE_DENIED","message":error_detail(e)}))
    except ActionIdempotencyConflict as e:
        raise HTTPException(409,mask_payload({"code":"ACTION_IDEMPOTENCY_CONFLICT","message":error_detail(e)}))
    except ActionRequestError as e:
        raise HTTPException(422,mask_payload({"code":"ACTION_REQUEST_INVALID","message":error_detail(e)}))
    except RequestFraudReportError as e:
        raise HTTPException(_fraud_report_http_status(e.code),mask_payload({
            "code":e.code,"message":error_detail(e),
            "execution_id":e.execution_id,"status":e.status,
        }))

@router.post("/actions/create-phone-verification-task/execute")
async def execute_create_phone_verification_task(
    req:CreatePhoneVerificationTaskRequest,
):
    service_postgres=postgres()
    execution_repository=PostgresActionExecutionRepository(service_postgres)
    proposal=active_version_proposal()
    service=CreatePhoneVerificationTaskActionService(
        postgres_service=service_postgres,
        nebula_factory=nebula,
        proposal=proposal,
        execution_repository=execution_repository,
        dispatch_coordinator=PostgresPhoneDispatchCoordinator(
            service_postgres,_phone_task_dispatcher(),
            _external_circuit(service_postgres,"phone-task-dispatch")
        ),
        callback_actor_id=MVP_PHONE_RESULT_ACTOR_ID,
        callback_actor_role=MVP_PHONE_RESULT_ACTOR_ROLE,
        outbox_max_attempts=ACTION_OUTBOX_MAX_ATTEMPTS,
    )
    try:
        result=mask_payload(service.execute(
            req.model_dump(),
            ontology_version=proposal.version,
            actor_id=MVP_PHONE_TASK_ACTOR_ID,
            actor_role=MVP_PHONE_TASK_ACTOR_ROLE,
            correlation_id=req.request_id,
        ))
        if result.get("status")=="COMMITTING":
            return JSONResponse(status_code=202,content=jsonable_encoder(result))
        return result
    except ActionAuthorizationError as e:
        raise HTTPException(403,mask_payload({
            "code":"ACTION_ROLE_DENIED","message":error_detail(e)
        }))
    except ActionIdempotencyConflict as e:
        raise HTTPException(409,mask_payload({
            "code":"ACTION_IDEMPOTENCY_CONFLICT","message":error_detail(e)
        }))
    except ActionRequestError as e:
        raise HTTPException(422,mask_payload({
            "code":"ACTION_REQUEST_INVALID","message":error_detail(e)
        }))
    except CreatePhoneVerificationTaskError as e:
        raise HTTPException(_phone_task_http_status(e.code),mask_payload({
            "code":e.code,"message":error_detail(e),
            "execution_id":e.execution_id,"status":e.status,
        }))

@router.post("/actions/record-phone-verification/execute")
async def execute_record_phone_verification(req:RecordPhoneVerificationRequest):
    service_postgres=postgres()
    execution_repository=PostgresActionExecutionRepository(service_postgres)
    proposal=active_version_proposal()
    service=RecordPhoneVerificationActionService(
        postgres_service=service_postgres,
        nebula_factory=nebula,
        proposal=proposal,
        execution_repository=execution_repository,
        outbox_max_attempts=ACTION_OUTBOX_MAX_ATTEMPTS,
    )
    try:
        result=mask_payload(service.execute(
            req.model_dump(),ontology_version=proposal.version,
            actor_id=MVP_PHONE_RESULT_ACTOR_ID,
            actor_role=MVP_PHONE_RESULT_ACTOR_ROLE,
            correlation_id=req.callback_id,
        ))
        if result.get("status")=="COMMITTING":
            return JSONResponse(status_code=202,content=jsonable_encoder(result))
        return result
    except ActionAuthorizationError as e:
        raise HTTPException(403,mask_payload({
            "code":"ACTION_ROLE_DENIED","message":error_detail(e)
        }))
    except ActionIdempotencyConflict as e:
        raise HTTPException(409,mask_payload({
            "code":"ACTION_IDEMPOTENCY_CONFLICT","message":error_detail(e)
        }))
    except ActionRequestError as e:
        raise HTTPException(422,mask_payload({
            "code":"ACTION_PHONE_RESULT_INVALID","message":error_detail(e)
        }))
    except RecordPhoneVerificationError as e:
        raise HTTPException(_phone_result_http_status(e.code),mask_payload({
            "code":e.code,"message":error_detail(e),
            "execution_id":e.execution_id,"status":e.status,
        }))

@router.post("/actions/trigger-manual-review/execute")
async def execute_trigger_manual_review(req:TriggerManualReviewRequest):
    service_postgres=postgres()
    execution_repository=PostgresActionExecutionRepository(service_postgres)
    proposal=active_version_proposal()
    service=TriggerManualReviewActionService(
        postgres_service=service_postgres,
        nebula_factory=nebula,
        proposal=proposal,
        execution_repository=execution_repository,
        outbox_max_attempts=ACTION_OUTBOX_MAX_ATTEMPTS,
    )
    try:
        result=mask_payload(service.execute(
            req.model_dump(),ontology_version=proposal.version,
            actor_id=MVP_MANUAL_REVIEW_ACTOR_ID,
            actor_role=MVP_MANUAL_REVIEW_ACTOR_ROLE,
            correlation_id=req.request_id,
        ))
        if result.get("status")=="COMMITTING":
            return JSONResponse(status_code=202,content=jsonable_encoder(result))
        return result
    except ActionAuthorizationError as e:
        raise HTTPException(403,mask_payload({
            "code":"ACTION_ROLE_DENIED","message":error_detail(e)
        }))
    except ActionIdempotencyConflict as e:
        raise HTTPException(409,mask_payload({
            "code":"ACTION_IDEMPOTENCY_CONFLICT","message":error_detail(e)
        }))
    except ActionRequestError as e:
        raise HTTPException(422,mask_payload({
            "code":"ACTION_REQUEST_INVALID","message":error_detail(e)
        }))
    except TriggerManualReviewError as e:
        raise HTTPException(_manual_review_http_status(e.code),mask_payload({
            "code":e.code,"message":error_detail(e),
            "execution_id":e.execution_id,"status":e.status,
        }))

@router.post("/actions/record-decision-recommendation/execute")
async def execute_record_decision_recommendation(
    req:RecordDecisionRecommendationRequest,
):
    service_postgres=postgres()
    execution_repository=PostgresActionExecutionRepository(service_postgres)
    proposal=active_version_proposal()
    service=RecordDecisionRecommendationActionService(
        postgres_service=service_postgres,
        nebula_factory=nebula,
        proposal=proposal,
        execution_repository=execution_repository,
        outbox_max_attempts=ACTION_OUTBOX_MAX_ATTEMPTS,
    )
    try:
        result=mask_payload(service.execute(
            req.model_dump(),ontology_version=proposal.version,
            actor_id=MVP_DECISION_RECOMMENDATION_ACTOR_ID,
            actor_role=MVP_DECISION_RECOMMENDATION_ACTOR_ROLE,
            correlation_id=req.request_id,
        ))
        if result.get("status")=="COMMITTING":
            return JSONResponse(status_code=202,content=jsonable_encoder(result))
        return result
    except ActionAuthorizationError as e:
        raise HTTPException(403,mask_payload({
            "code":"ACTION_ROLE_DENIED","message":error_detail(e)
        }))
    except ActionIdempotencyConflict as e:
        raise HTTPException(409,mask_payload({
            "code":"ACTION_IDEMPOTENCY_CONFLICT","message":error_detail(e)
        }))
    except ActionRequestError as e:
        raise HTTPException(422,mask_payload({
            "code":"ACTION_REQUEST_INVALID","message":error_detail(e)
        }))
    except RecordDecisionRecommendationError as e:
        raise HTTPException(_decision_recommendation_http_status(e.code),mask_payload({
            "code":e.code,"message":error_detail(e),
            "execution_id":e.execution_id,"status":e.status,
        }))

@router.get("/actions/executions/{execution_id}")
async def get_action_execution(execution_id:str):
    repository=PostgresActionExecutionRepository(postgres())
    try:
        record=repository.get(execution_id)
        events=repository.list_audit_events(execution_id)
        outbox_events=PostgresActionOutboxRepository(repository.postgres).list_for_execution(execution_id)
        return mask_payload({
            "execution":record.__dict__,
            "audit_events":events,
            "outbox_events":outbox_events,
            "audit_chain_valid":repository.verify_audit_chain(execution_id),
        })
    except ActionExecutionNotFound as e:
        raise HTTPException(404,error_detail(e))

_PERSONAL_CREDIT_ACTION_ADAPTERS = {
    "create_loan_application": (
        CreateLoanApplicationRequest,execute_create_loan_application
    ),
    "request_credit_report": (
        RequestCreditReportRequest,execute_request_credit_report
    ),
    "request_fraud_report": (
        RequestFraudReportRequest,execute_request_fraud_report
    ),
    "generate_risk_assessment": (
        GenerateRiskAssessmentRequest,execute_generate_risk_assessment
    ),
    "create_phone_verification_task": (
        CreatePhoneVerificationTaskRequest,execute_create_phone_verification_task
    ),
    "record_phone_verification": (
        RecordPhoneVerificationRequest,execute_record_phone_verification
    ),
    "trigger_manual_review": (
        TriggerManualReviewRequest,execute_trigger_manual_review
    ),
    "record_decision_recommendation": (
        RecordDecisionRecommendationRequest,execute_record_decision_recommendation
    ),
}

_ACTION_ADAPTER_REGISTRY = ReusableActionAdapterRegistry()
for _action_name,(_request_model,_executor) in _PERSONAL_CREDIT_ACTION_ADAPTERS.items():
    _ACTION_ADAPTER_REGISTRY.register(
        ontology_id=ONTOLOGY_PERSONAL_CREDIT,
        ontology_version="*",
        action_name=_action_name,
        request_model=_request_model,
        executor=_executor,
        profile=PERSONAL_CREDIT_ACTION_REUSE_PROFILES[_action_name],
    )

_READ_ONLY_FUNCTION_REGISTRY = build_personal_credit_readonly_registry()


def _personal_credit_action_capability_catalog(proposal:OntologyProposal):
    return build_action_capability_catalog(
        ontology_id=ONTOLOGY_PERSONAL_CREDIT,
        proposal=proposal,
        adapter_registry=_ACTION_ADAPTER_REGISTRY,
        expected_write_bindings=_PERSONAL_CREDIT_ACTION_ADAPTERS,
        read_tools=READ_ONLY_AGENT_TOOLS,
        read_request_model=PersonalCreditAgentToolRequest,
        read_executor=PersonalCreditAgentService.execute_tool,
        read_function_registry=_READ_ONLY_FUNCTION_REGISTRY,
    )


@router.get("/actions/reuse-catalog")
async def action_reuse_catalog():
    return {
        **build_action_reuse_audit(),
        "registered_ontologies":[ONTOLOGY_PERSONAL_CREDIT],
    }


@router.get("/ontologies/{ontology_id}/actions")
async def list_registered_ontology_actions(ontology_id:str):
    try:
        proposal=active_version_proposal()
        _require_registered_action_version(
            ontology_id,proposal.version,action_name=None
        )
        return _ACTION_ADAPTER_REGISTRY.describe(
            ontology_id,ontology_version=proposal.version
        )
    except ReusableActionNotFound as error:
        raise HTTPException(404,error_detail(error))
    except VersionWorkflowError as error:
        raise HTTPException(409,error_detail(error))


@router.get("/ontologies/{ontology_id}/versions/{ontology_version}/actions")
async def list_registered_ontology_version_actions(
    ontology_id:str,ontology_version:str
):
    try:
        _require_registered_action_version(
            ontology_id,ontology_version,action_name=None
        )
        return _ACTION_ADAPTER_REGISTRY.describe(
            ontology_id,ontology_version=ontology_version
        )
    except ReusableActionNotFound as error:
        raise HTTPException(404,error_detail(error))
    except VersionWorkflowError as error:
        raise HTTPException(409,error_detail(error))


def _require_registered_action_version(
    ontology_id:str,ontology_version:str,action_name:str|None
):
    if ontology_id!=ONTOLOGY_PERSONAL_CREDIT:
        if action_name:
            _ACTION_ADAPTER_REGISTRY.resolve(
                ontology_id,action_name,ontology_version=ontology_version
            )
        else:
            _ACTION_ADAPTER_REGISTRY.describe(
                ontology_id,ontology_version=ontology_version
            )
        raise VersionWorkflowError(
            f"本体版本仓储尚未绑定：{ontology_id}.{ontology_version}"
        )
    proposal=published_version_proposal(ontology_version)
    runtime_errors=_action_runtime_compatibility_errors(proposal)
    if runtime_errors:
        raise VersionWorkflowError(
            "本体版本与当前 Action 运行合同不兼容："
            +"；".join(runtime_errors)
        )
    names={item.name for item in proposal.action_types}
    if action_name:
        _ACTION_ADAPTER_REGISTRY.resolve(
            ontology_id,action_name,ontology_version=ontology_version
        )
        if action_name not in names:
            raise VersionWorkflowError(
                f"本体版本未声明 Action：{ontology_version}.{action_name}"
            )
    return proposal


def _action_runtime_compatibility_errors(proposal:OntologyProposal)->list[str]:
    errors=list(validate_action_design_catalog(proposal))
    try:
        _personal_credit_action_capability_catalog(proposal)
    except ActionCapabilityContractError as error:
        errors.extend(error.errors)
    for action_name,design in ACTION_RUNTIME_DESIGNS.items():
        try:
            request_model=_ACTION_ADAPTER_REGISTRY.request_model(
                ONTOLOGY_PERSONAL_CREDIT,action_name,
                ontology_version=proposal.version,
            )
        except ReusableActionNotFound:
            errors.append(f"Action 缺少运行适配器：{action_name}")
            continue
        if set(request_model.model_fields)!=set(design.request_fields):
            errors.append(f"Action 请求模型与运行合同不兼容：{action_name}")
    return list(dict.fromkeys(errors))


async def _execute_registered_action_version(
    ontology_id:str,ontology_version:str,action_name:str,payload:dict[str,Any]
):
    _require_registered_action_version(
        ontology_id,ontology_version,action_name=action_name
    )
    token=_ACTION_ONTOLOGY_VERSION_OVERRIDE.set(ontology_version)
    try:
        return await _ACTION_ADAPTER_REGISTRY.execute(
            ontology_id,action_name,payload,
            ontology_version=ontology_version,
        )
    finally:
        _ACTION_ONTOLOGY_VERSION_OVERRIDE.reset(token)


@router.post("/ontologies/{ontology_id}/actions/{action_name}/execute")
async def execute_registered_ontology_action(
    ontology_id:str,action_name:str,payload:dict[str,Any]
):
    try:
        proposal=active_version_proposal()
        return await _execute_registered_action_version(
            ontology_id,proposal.version,action_name,payload
        )
    except ReusableActionNotFound as error:
        raise HTTPException(404,error_detail(error))
    except ReusableActionRequestInvalid as error:
        raise HTTPException(422,mask_payload({
            "code":"ACTION_REQUEST_INVALID",
            "message":error_detail(error),
            "errors":error.errors,
        }))
    except VersionWorkflowError as error:
        raise HTTPException(409,mask_payload({
            "code":"ACTION_ONTOLOGY_VERSION_INCOMPATIBLE",
            "message":error_detail(error),
        }))


@router.post(
    "/ontologies/{ontology_id}/versions/{ontology_version}/actions/"
    "{action_name}/execute"
)
async def execute_registered_ontology_version_action(
    ontology_id:str,ontology_version:str,action_name:str,payload:dict[str,Any]
):
    try:
        return await _execute_registered_action_version(
            ontology_id,ontology_version,action_name,payload
        )
    except ReusableActionNotFound as error:
        raise HTTPException(404,error_detail(error))
    except ReusableActionRequestInvalid as error:
        raise HTTPException(422,mask_payload({
            "code":"ACTION_REQUEST_INVALID",
            "message":error_detail(error),
            "errors":error.errors,
        }))
    except VersionWorkflowError as error:
        raise HTTPException(409,mask_payload({
            "code":"ACTION_ONTOLOGY_VERSION_INCOMPATIBLE",
            "message":error_detail(error),
        }))

def _workflow_action_payload(
    *,
    action_name:str,
    payload:dict[str,Any],
    application_id:str,
    dispatch_request_id:str,
    ontology_version:str,
):
    model_type=_ACTION_ADAPTER_REGISTRY.request_model(
        ONTOLOGY_PERSONAL_CREDIT,action_name,
        ontology_version=ontology_version,
    )
    normalized=dict(payload)
    if "application_id" in model_type.model_fields:
        supplied=normalized.get("application_id")
        if supplied is not None and supplied!=application_id:
            raise WorkflowError(
                "WORKFLOW_APPLICATION_MISMATCH",
                "Action application_id 与流程申请不一致",
            )
        normalized["application_id"]=application_id
    if "request_id" in model_type.model_fields:
        normalized["request_id"]=dispatch_request_id
    if action_name=="record_phone_verification":
        normalized["callback_id"]=dispatch_request_id
    if action_name=="record_decision_recommendation":
        supplied_approver=normalized.get("approver_id")
        if supplied_approver not in {None,MVP_DECISION_RECOMMENDATION_ACTOR_ID}:
            raise WorkflowError(
                "WORKFLOW_REQUEST_INVALID",
                "approver_id 必须与服务端决策建议执行身份一致",
            )
        normalized["approver_id"]=MVP_DECISION_RECOMMENDATION_ACTOR_ID
    try:
        return model_type.model_validate(normalized)
    except Exception as error:
        validation_errors=[]
        if hasattr(error,"errors"):
            validation_errors=[
                {
                    "loc":item.get("loc",()),
                    "type":item.get("type"),
                    "msg":item.get("msg"),
                }
                for item in error.errors()
            ]
        raise WorkflowError(
            "WORKFLOW_REQUEST_INVALID",
            f"{action_name} 请求字段校验失败：{validation_errors or type(error).__name__}",
        ) from error

def _workflow_action_result(value):
    if isinstance(value,JSONResponse):
        try:
            return json.loads(value.body.decode("utf-8"))
        except Exception as error:
            raise WorkflowError(
                "WORKFLOW_ACTION_RESPONSE_INVALID","Action 返回了无效 JSON"
            ) from error
    if not isinstance(value,dict):
        raise WorkflowError(
            "WORKFLOW_ACTION_RESPONSE_INVALID","Action 返回结果不是对象"
        )
    return value

@router.post("/workflows/personal-credit")
async def start_personal_credit_workflow(req:StartPersonalCreditWorkflowRequest):
    try:
        proposal=active_version_proposal()
        return mask_payload(_workflow_repository().create(
            application_id=req.application_id,
            entry_mode=req.entry_mode,
            ontology_version=proposal.version,
            actor_id=MVP_WORKFLOW_ACTOR_ID,
            actor_role=MVP_WORKFLOW_ACTOR_ROLE,
            correlation_id=req.request_id,
        ))
    except WorkflowError as error:
        _raise_workflow_http(error)

@router.get("/workflows/personal-credit/summary")
async def personal_credit_workflow_summary():
    return _workflow_repository().summary()

@router.get("/workflows/personal-credit")
async def list_personal_credit_workflows(
    status:str|None=None,application_id:str|None=None,limit:int=100
):
    try:
        return {"workflows":mask_payload(_workflow_repository().list(
            status=status,application_id=application_id,limit=limit
        ))}
    except WorkflowError as error:
        _raise_workflow_http(error)

@router.get("/workflows/personal-credit/definition")
async def personal_credit_workflow_definition():
    proposal=active_version_proposal()
    return {
        "workflow_version":"personal-credit-workflow-v1",
        "ontology_id":ONTOLOGY_PERSONAL_CREDIT,
        "ontology_version":proposal.version,
        "statuses":WORKFLOW_STATUSES,
        "recommendation_only_assertion":RECOMMENDATION_ONLY_ASSERTION,
        "actions":[
            {
                "position":position,
                "action_name":action_name,
                "label":WORKFLOW_ACTION_LABELS[action_name],
                "dependencies":WORKFLOW_DEPENDENCIES[action_name],
                "optional":action_name in OPTIONAL_WORKFLOW_ACTIONS,
                "request_fields":list(
                    _ACTION_ADAPTER_REGISTRY.request_model(
                        ONTOLOGY_PERSONAL_CREDIT,action_name,
                        ontology_version=proposal.version,
                    ).model_fields
                ),
                "archetype":_ACTION_ADAPTER_REGISTRY.resolve(
                    ONTOLOGY_PERSONAL_CREDIT,action_name,
                    ontology_version=proposal.version,
                ).profile.archetype,
                "reuse_mode":_ACTION_ADAPTER_REGISTRY.resolve(
                    ONTOLOGY_PERSONAL_CREDIT,action_name,
                    ontology_version=proposal.version,
                ).profile.reuse_mode,
            }
            for position,action_name in enumerate(WORKFLOW_ACTIONS,start=1)
        ],
    }

@router.post("/workflows/personal-credit/data-search")
async def search_personal_credit_workflow_data(
    req:WorkflowDataSearchRequest,response:Response
):
    try:
        # This authenticated endpoint intentionally returns the selected source values
        # needed by the Action form. Search previews are masked and the query is POSTed.
        response.headers["Cache-Control"]="no-store"
        return PersonalCreditBusinessLookup(postgres()).search(
            req.entity_type,req.query,req.limit
        )
    except BusinessLookupError as error:
        raise HTTPException(400,error_detail(error))

@router.get("/workflows/personal-credit/{workflow_id}")
async def get_personal_credit_workflow(workflow_id:str):
    try:
        return mask_payload(_workflow_repository().get(workflow_id))
    except WorkflowError as error:
        _raise_workflow_http(error)

@router.post("/workflows/personal-credit/{workflow_id}/refresh")
async def refresh_personal_credit_workflow(workflow_id:str):
    try:
        return mask_payload(_workflow_repository().reconcile(
            workflow_id,actor_id=MVP_WORKFLOW_ACTOR_ID
        ))
    except WorkflowError as error:
        _raise_workflow_http(error)

@router.post("/workflows/personal-credit/{workflow_id}/steps/{action_name}/execute")
async def execute_personal_credit_workflow_step(
    workflow_id:str,
    action_name:str,
    req:ExecutePersonalCreditWorkflowStepRequest,
):
    if action_name not in WORKFLOW_ACTIONS:
        _raise_workflow_http(WorkflowError("WORKFLOW_STEP_NOT_FOUND","流程步骤不存在"))
    repository=_workflow_repository()
    try:
        detail=repository.get(workflow_id)
        workflow=detail["workflow"]
        pinned_ontology_version=workflow["ontology_version"]
        try:
            _require_registered_action_version(
                ONTOLOGY_PERSONAL_CREDIT,
                pinned_ontology_version,
                action_name=action_name,
            )
        except (ReusableActionNotFound,VersionWorkflowError) as error:
            raise WorkflowError(
                "WORKFLOW_ONTOLOGY_VERSION_UNAVAILABLE",
                f"流程锁定的本体版本不可执行：{error}",
            ) from error
        dispatch_request_id=f"wf-{uuid4()}"
        model=_workflow_action_payload(
            action_name=action_name,
            payload=req.payload,
            application_id=workflow["application_id"],
            dispatch_request_id=dispatch_request_id,
            ontology_version=pinned_ontology_version,
        )
        model_payload=model.model_dump(mode="json")
        input_summary=model.model_dump(mode="json",exclude_none=True)
        repository.validate_step_scope(workflow_id,action_name,model_payload)
        repository.begin_step(
            workflow_id,
            action_name,
            dispatch_request_id=dispatch_request_id,
            request_digest=workflow_request_digest(model_payload),
            input_summary=input_summary,
            actor_id=MVP_WORKFLOW_ACTOR_ID,
        )
        try:
            token=_ACTION_ONTOLOGY_VERSION_OVERRIDE.set(pinned_ontology_version)
            try:
                action_value=await _ACTION_ADAPTER_REGISTRY.execute_model(
                    ONTOLOGY_PERSONAL_CREDIT,action_name,model,
                    ontology_version=pinned_ontology_version,
                )
            finally:
                _ACTION_ONTOLOGY_VERSION_OVERRIDE.reset(token)
            result=_workflow_action_result(action_value)
        except HTTPException as action_error:
            detail_value=action_error.detail
            action_detail=detail_value if isinstance(detail_value,dict) else {}
            error_code=str(action_detail.get("code") or "WORKFLOW_ACTION_FAILED")
            error_message=str(
                action_detail.get("message")
                or (detail_value if isinstance(detail_value,str) else "Action 执行失败")
            )
            repository.record_dispatch_failure(
                workflow_id,
                action_name,
                execution_id=action_detail.get("execution_id"),
                error_code=error_code,
                error_message=error_message,
                actor_id=MVP_WORKFLOW_ACTOR_ID,
                retryable_without_execution=not bool(
                    action_detail.get("execution_id")
                ),
            )
            raise
        execution_id=result.get("execution_id")
        action_status=result.get("status")
        if not execution_id or not action_status:
            raise WorkflowError(
                "WORKFLOW_ACTION_RESPONSE_INVALID",
                "Action 响应缺少 execution_id 或 status",
            )
        return mask_payload(repository.attach_action_result(
            workflow_id,
            action_name,
            execution_id=str(execution_id),
            action_status=str(action_status),
            result_summary=result,
            error_code=result.get("error_code"),
            error_message=result.get("error_message"),
            actor_id=MVP_WORKFLOW_ACTOR_ID,
        ))
    except WorkflowError as error:
        try:
            current=repository.get(workflow_id)
            matching=next(
                (item for item in current["steps"] if item["action_name"]==action_name),
                None,
            )
            if matching and matching["status"] in {"RUNNING","WAITING"}:
                repository.record_dispatch_failure(
                    workflow_id,
                    action_name,
                    execution_id=matching.get("execution_id"),
                    error_code=error.code,
                    error_message=error_detail(error),
                    actor_id=MVP_WORKFLOW_ACTOR_ID,
                )
        except Exception:
            pass
        _raise_workflow_http(error)

@router.post("/workflows/personal-credit/{workflow_id}/steps/{action_name}/skip")
async def skip_personal_credit_workflow_step(
    workflow_id:str,action_name:str,req:WorkflowReasonRequest
):
    try:
        return mask_payload(_workflow_repository().skip_optional_step(
            workflow_id,action_name,reason=req.reason,actor_id=MVP_WORKFLOW_ACTOR_ID
        ))
    except WorkflowError as error:
        _raise_workflow_http(error)

@router.post("/workflows/personal-credit/{workflow_id}/cancel")
async def cancel_personal_credit_workflow(
    workflow_id:str,req:WorkflowReasonRequest
):
    try:
        return mask_payload(_workflow_repository().cancel(
            workflow_id,reason=req.reason,actor_id=MVP_WORKFLOW_ACTOR_ID
        ))
    except WorkflowError as error:
        _raise_workflow_http(error)

@router.get("/actions/outbox")
async def list_action_outbox(status:str|None=None,limit:int=100):
    try:
        return mask_payload({
            "events":PostgresActionOutboxRepository(postgres()).list_events(
                status=status,limit=limit
            )
        })
    except OutboxError as e:
        raise HTTPException(400,error_detail(e))

@router.get("/actions/outbox/status")
async def action_outbox_status():
    return PostgresActionOutboxRepository(postgres()).status_summary(
        lease_seconds=ACTION_OUTBOX_LEASE_SECONDS
    )

@router.get("/actions/outbox/alerts")
async def action_outbox_alerts(limit:int=100):
    repository=PostgresOutboxReplayRepository(postgres())
    return mask_payload(repository.alerts(
        lease_seconds=ACTION_OUTBOX_LEASE_SECONDS,limit=limit
    ))

@router.get("/actions/outbox/replays")
async def action_outbox_replays(limit:int=100):
    repository=PostgresOutboxReplayRepository(postgres())
    return mask_payload({"replays":repository.list_requests(limit=limit)})

@router.post("/actions/outbox/{event_id}/replay")
async def replay_action_outbox(event_id:str,req:OutboxReplayRequest):
    service_postgres=postgres()
    execution_repository=PostgresActionExecutionRepository(service_postgres)
    outbox_repository=PostgresActionOutboxRepository(service_postgres)
    replay_repository=PostgresOutboxReplayRepository(service_postgres)

    def proposal_loader(version:str):
        return _proposal_from_record(version_store.get(version))

    workers=build_outbox_replay_workers(
        postgres_service=service_postgres,
        nebula_factory=nebula,
        proposal_loader=proposal_loader,
        outbox_repository=outbox_repository,
        execution_repository=execution_repository,
        phone_dispatcher=_phone_task_dispatcher(),
        phone_circuit_breaker=_external_circuit(
            service_postgres,"phone-task-dispatch"
        ),
        lease_seconds=ACTION_OUTBOX_LEASE_SECONDS,
        retry_base_seconds=ACTION_OUTBOX_RETRY_BASE_SECONDS,
        retry_max_seconds=ACTION_OUTBOX_RETRY_MAX_SECONDS,
    )
    service=OutboxReplayService(
        repository=replay_repository,
        execution_repository=execution_repository,
        workers=workers,
    )
    try:
        result=mask_payload(service.execute(
            event_id=event_id,request_id=req.request_id,reason=req.reason,
            actor_id=MVP_OUTBOX_OPERATOR_ID,actor_role=MVP_OUTBOX_OPERATOR_ROLE,
        ))
        if result.get("status")=="FAILED":
            return JSONResponse(status_code=502,content=jsonable_encoder(result))
        return result
    except OutboxReplayConflict as e:
        raise HTTPException(409,mask_payload({"code":e.code,"message":error_detail(e)}))
    except OutboxReplayError as e:
        status=404 if e.code=="ACTION_OUTBOX_EVENT_NOT_FOUND" else 422
        raise HTTPException(status,mask_payload({"code":e.code,"message":error_detail(e)}))

@router.get("/operations/summary")
async def operations_summary():
    service_postgres=postgres()
    alerts=PostgresOperationalAlertRepository(service_postgres).summary()
    circuits=PostgresCircuitBreakerRepository(service_postgres).list_circuits()
    open_circuits=sum(1 for item in circuits if item["state"]!="CLOSED")
    return mask_payload({
        **alerts,
        "open_circuits":open_circuits,
        "circuit_count":len(circuits),
        "webhook_enabled":ALERT_WEBHOOK_ENABLED,
    })

@router.get("/operations/alerts")
async def operations_alerts(status:str|None=None,limit:int=100):
    try:
        return mask_payload({
            "alerts":PostgresOperationalAlertRepository(postgres()).list_alerts(
                status=status,limit=limit
            )
        })
    except OperationsError as e:
        raise HTTPException(422,mask_payload({"code":e.code,"message":error_detail(e)}))

@router.get("/operations/circuits")
async def operations_circuits():
    return mask_payload({
        "circuits":PostgresCircuitBreakerRepository(postgres()).list_circuits()
    })

@router.post("/operations/alerts/{alert_id}/acknowledge")
async def acknowledge_operational_alert(alert_id:str,req:AlertActionRequest):
    try:
        record=PostgresOperationalAlertRepository(postgres()).transition(
            alert_id,
            target_status="ACKNOWLEDGED",
            actor_id=MVP_OUTBOX_OPERATOR_ID,
            note=req.note,
        )
        return mask_payload({"status":"acknowledged","alert":record})
    except OperationsError as e:
        status=404 if e.code=="ALERT_NOT_FOUND" else 409
        raise HTTPException(status,mask_payload({"code":e.code,"message":error_detail(e)}))

@router.post("/operations/alerts/{alert_id}/resolve")
async def resolve_operational_alert(alert_id:str,req:AlertActionRequest):
    try:
        record=PostgresOperationalAlertRepository(postgres()).transition(
            alert_id,
            target_status="RESOLVED",
            actor_id=MVP_OUTBOX_OPERATOR_ID,
            note=req.note,
        )
        return mask_payload({"status":"resolved","alert":record})
    except OperationsError as e:
        status=404 if e.code=="ALERT_NOT_FOUND" else 409
        raise HTTPException(status,mask_payload({"code":e.code,"message":error_detail(e)}))

@router.post("/operations/circuits/{dependency_name}/reset")
async def reset_external_circuit(dependency_name:str,req:AlertActionRequest):
    try:
        service_postgres=postgres()
        circuit=PostgresCircuitBreakerRepository(service_postgres).reset(
            dependency_name
        )
        PostgresOperationalAlertRepository(service_postgres).resolve_by_key(
            f"circuit:{dependency_name}",
            actor_id=MVP_OUTBOX_OPERATOR_ID,
            note=req.note,
        )
        return mask_payload({"status":"reset","circuit":circuit})
    except OperationsError as e:
        status=404 if e.code=="CIRCUIT_NOT_FOUND" else 409
        raise HTTPException(status,mask_payload({"code":e.code,"message":error_detail(e)}))

@router.get("/health")
async def health():
    n=nebula()
    try:
        try: postgresql=postgres().ping()
        except Exception: postgresql=False
        try: nebulagraph=n.ping()
        except Exception: nebulagraph=False
        return {"status":"ok" if postgresql and nebulagraph else "degraded","postgresql":postgresql,"nebulagraph":nebulagraph}
    finally:
        n.close()

@router.get("/auth/check")
async def check_mvp_authentication(request:Request):
    """Return the authentication mode resolved by the shared middleware."""
    expires_at=getattr(request.state,"mvp_auth_expires_at",None)
    return {
        "authenticated":bool(getattr(request.state,"mvp_authenticated",False)),
        "mode":getattr(request.state,"mvp_auth_mode",None),
        "expires_at":(
            datetime.fromtimestamp(expires_at,tz=timezone.utc).isoformat()
            if expires_at is not None else None
        ),
    }

@router.get("/postgres/schema")
async def schema():
    return postgres().schema()

@router.post("/postgres/profile")
async def profile(req:ProfileRequest):
    return mask_payload(postgres().profile(req.limit))

@router.get("/postgres/data/{table}")
async def postgres_data(table:str,limit:int=100,offset:int=0):
    try:
        return mask_payload(postgres().table_data(table,limit,offset))
    except ValueError as e:
        raise HTTPException(400,error_detail(e))

@router.get("/llm/configs")
async def list_llm_configurations():
    try:
        items=_llm_configuration_repository().list_configs()
        active=next((item["id"] for item in items if item["is_active"]),None)
        return {
            "items":items,
            "active_config_id":active,
            "providers":provider_catalog(),
        }
    except Exception as error:
        _raise_llm_configuration_http(error)

@router.post("/llm/configs",status_code=201)
async def create_llm_configuration(req:LLMManagedConfigCreate):
    try:
        values=_normalize_managed_llm(req)
        api_key=_secret_text(req.api_key).strip()
        if not api_key:
            raise ValueError("请填写 LLM API Key")
        return _llm_configuration_repository().create(
            **values,api_key=api_key
        )
    except Exception as error:
        _raise_llm_configuration_http(error)

@router.put("/llm/configs/{configuration_id}")
async def update_llm_configuration(
    configuration_id:UUID,req:LLMManagedConfigUpdate
):
    try:
        values=_normalize_managed_llm(req)
        api_key=_secret_text(req.api_key).strip() if req.api_key else None
        return _llm_configuration_repository().update(
            str(configuration_id),**values,api_key=api_key,
            expected_version=req.version,
        )
    except Exception as error:
        _raise_llm_configuration_http(error)

@router.post("/llm/configs/{configuration_id}/test")
async def test_llm_configuration(configuration_id:UUID):
    repository=_llm_configuration_repository()
    circuit=None
    runtime=None
    request_started=False
    provider_completed=False
    configuration_id_text=str(configuration_id)
    try:
        runtime=repository.get_runtime(configuration_id_text)
        client=_llm_client_from_values(runtime)
        circuit=_external_circuit(postgres(),"llm-service")
        circuit.before_call()
        request_started=True
        await client.test()
        provider_completed=True
        circuit.record_success()
        return _store_llm_test_result(
            repository,configuration_id_text,runtime,success=True
        )
    except (LLMConfigurationNotFound,LLMConfigurationConflict,
            LLMConfigurationEncryptionError) as error:
        _raise_llm_configuration_http(error)
    except ExternalCircuitOpenError as error:
        # No provider request was attempted, so do not count another failure or
        # invalidate a previously verified configuration.
        raise HTTPException(503,error_detail(error))
    except LLMTransientError as error:
        detail=error_detail(error)
        _record_llm_circuit_failure(
            circuit,"LLM_CONFIGURATION_TEST_TRANSIENT",detail
        )
        if runtime:
            _store_llm_test_result(
                repository,configuration_id_text,runtime,
                success=False,error=detail,
            )
        raise HTTPException(503,detail)
    except Exception as error:
        if isinstance(error,HTTPException):
            raise
        if provider_completed or not request_started:
            _raise_llm_configuration_http(error)
        detail=error_detail(error)
        _record_llm_circuit_failure(
            circuit,"LLM_CONFIGURATION_TEST_FAILED",detail
        )
        if runtime:
            _store_llm_test_result(
                repository,configuration_id_text,runtime,
                success=False,error=detail,
            )
        raise HTTPException(422,detail)

@router.post("/llm/configs/{configuration_id}/activate")
async def activate_llm_configuration(configuration_id:UUID):
    try:
        return _llm_configuration_repository().activate(str(configuration_id))
    except Exception as error:
        _raise_llm_configuration_http(error)

@router.post("/llm/configs/{configuration_id}/deactivate")
async def deactivate_llm_configuration(configuration_id:UUID):
    try:
        return _llm_configuration_repository().deactivate(str(configuration_id))
    except Exception as error:
        _raise_llm_configuration_http(error)

@router.delete("/llm/configs/{configuration_id}")
async def delete_llm_configuration(
    configuration_id:UUID,version:int=Query(...,ge=1)
):
    try:
        configuration_id_text=str(configuration_id)
        _llm_configuration_repository().delete(
            configuration_id_text,expected_version=version
        )
        return {"deleted":True,"id":configuration_id_text}
    except Exception as error:
        _raise_llm_configuration_http(error)

@router.post("/llm/test")
async def llm_test(cfg:LLMConfig):
    circuit=None
    try:
        client=_llm_client_from_values(cfg)
        service_postgres=postgres()
        circuit=_external_circuit(service_postgres,"llm-service")
        circuit.before_call()
        result=await client.test()
        circuit.record_success()
        return {"result":result}
    except ExternalCircuitOpenError as e:
        raise HTTPException(503,error_detail(e))
    except Exception as e:
        if circuit:
            circuit.record_failure("LLM_REQUEST_FAILED",error_detail(e))
        raise HTTPException(400,error_detail(e))

@router.get("/agents/personal-credit/tools")
async def personal_credit_agent_tools():
    return {
        "tools":PersonalCreditAgentService.catalog(),
        "agent_runtime":personal_credit_agent_runtime_status(
            PERSONAL_CREDIT_AGENT_MODE
        ).public(),
        "execution_boundary":{
            "read_only_only":True,
            "raw_sql_allowed":False,
            "business_writes_allowed":False,
            "final_approval_allowed":False,
        },
        "planning_boundary":{
            "max_steps":4,
            "default_steps":1,
            "execution_mode":"sequential",
            "response_types":["CHAT","NEEDS_CLARIFICATION","ACTION_PLAN"],
            "multi_turn":True,
            "conversation":{
                "storage":"in_memory",
                "ttl_seconds":1800,
                "max_context_turns":8,
                "scope_bound_to":["application_id","ontology_version"],
                "single_inflight_turn":True,
            },
            "result_synthesis":"single_call_llm_with_deterministic_fallback",
            "max_llm_calls_per_turn":1,
            "explicit_business_routing":"deterministic_then_llm_synthesis",
            "model_planner_result":"deterministic",
            "execution_semantics":"same_snapshot_independent",
            "response_contract":["answer","final_answer","trace","synthesis"],
            "trace_policy":{
                "stages":["planner","readiness","action","synthesis","fallback"],
                "contains_reasoning":False,
                "contains_sensitive_data":False,
            },
            "model_arguments_allowed":False,
            "write_actions_are_proposals_only":True,
            "unrecognized_instruction":"NO_ACTION",
            "clarification_status":"NEEDS_CLARIFICATION",
        },
    }

@router.get("/agents/personal-credit/{application_id}/readiness")
async def personal_credit_agent_readiness(
    application_id:str,ontology_version:str|None=None
):
    try:
        _agent_runtime_mode_or_http()
        service=_personal_credit_agent(ontology_version)
        return service.inspect_case_readiness(application_id)
    except (VersionWorkflowError,FunctionDataAdapterError,ValueError) as e:
        raise HTTPException(422,error_detail(e))

@router.post("/agents/personal-credit/tools/{tool_name}/execute")
async def execute_personal_credit_agent_tool(
    tool_name:str,req:PersonalCreditAgentToolRequest
):
    if tool_name not in READ_ONLY_AGENT_TOOLS:
        raise HTTPException(404,"只读 Agent 工具不存在")
    try:
        _agent_runtime_mode_or_http()
        service=_personal_credit_agent(req.ontology_version)
        return {
            "tool":tool_name,
            "read_only":True,
            "result":service.execute_tool(req.application_id,tool_name),
        }
    except (VersionWorkflowError,FunctionDataAdapterError,AgentPlanningError,ValueError) as e:
        raise HTTPException(422,error_detail(e))

@router.post("/agents/personal-credit/run")
async def run_personal_credit_agent(
    req:PersonalCreditAgentRunRequest,request:Request
):
    # The reserved ontology mode must fail before model-configuration schema
    # setup, credential access, Agent construction, or any Action execution.
    agent_mode=_agent_runtime_mode_or_http()
    # Inspect only non-secret metadata first. An anonymous UAT request is
    # rejected before the active key is decrypted or a client is constructed.
    managed_repository=_llm_configuration_repository()
    try:
        active_metadata=managed_repository.active_metadata()
    except Exception as error:
        _raise_llm_configuration_http(error)
    auth_error=external_llm_auth_error(
        MVP_API_KEY,request.headers.get("X-API-Key"),
        active_metadata is not None,
        authenticated=bool(getattr(request.state,"mvp_authenticated",False)),
    )
    if auth_error:
        raise HTTPException(*auth_error)
    circuit=None
    try:
        service=_personal_credit_agent(req.ontology_version)
        if active_metadata is not None:
            runtime=managed_repository.active_runtime()
            client=_llm_client_from_values(runtime) if runtime else None
        else:
            client=None
        if client:
            circuit=_external_circuit(postgres(),"llm-service")
            circuit.before_call()
        result=await service.run(
            req.application_id,
            req.instruction,
            llm_client=client,
            execute=req.execute,
            max_steps=req.max_steps,
            conversation_id=req.conversation_id,
        )
        result["agent_mode"]=agent_mode.value
        synthesis=result.get("synthesis") or {}
        log_method=logger.warning if result.get("degraded") else logger.info
        log_method(
            "personal-credit agent completed mode=%s response_type=%s execution_status=%s "
            "synthesis_status=%s synthesis_reason=%s model_calls=%s",
            agent_mode.value,
            result.get("response_type"),
            (result.get("execution_summary") or {}).get("status"),
            synthesis.get("status"),
            synthesis.get("reason_code"),
            result.get("model_calls"),
        )
        model_quality_failed=(
            synthesis.get("status")=="FALLBACK"
            and bool(synthesis.get("model_called"))
        )
        if circuit and model_quality_failed:
            reason_code=str(
                synthesis.get("reason_code") or "MODEL_RESPONSE_DEGRADED"
            )[:64]
            circuit.record_failure(
                "AGENT_MODEL_QUALITY_DEGRADED",
                "模型响应未通过质量门禁："+reason_code,
            )
        elif circuit:
            circuit.record_success()
        return result
    except ExternalCircuitOpenError as e:
        raise HTTPException(503,error_detail(e))
    except AgentConversationBusyError as e:
        raise HTTPException(409,error_detail(e))
    except AgentConversationError as e:
        raise HTTPException(422,error_detail(e))
    except (LLMConfigurationNotFound,LLMConfigurationConflict,
            LLMConfigurationEncryptionError) as e:
        _raise_llm_configuration_http(e)
    except (VersionWorkflowError,FunctionDataAdapterError,AgentPlanningError,ValueError) as e:
        if circuit:
            circuit.record_failure("AGENT_PLANNING_FAILED",error_detail(e))
        raise HTTPException(422,error_detail(e))
    except Exception as e:
        if circuit:
            circuit.record_failure("LLM_REQUEST_FAILED",error_detail(e))
        raise HTTPException(400,error_detail(e))

@router.post("/ontology/generate")
async def gen(req:GenerateRequest):
    circuit=None
    try:
        prof=postgres().profile(min(req.limit,5))
        client=_llm_client_from_values(req.llm)
        circuit=_external_circuit(postgres(),"llm-service")
        circuit.before_call()
        p=await generate(client,prof)
        circuit.record_success()
        return {"proposal":p.model_dump(),"validation_errors":validate(p)}
    except ExternalCircuitOpenError as e:
        raise HTTPException(503,error_detail(e))
    except Exception as e:
        if circuit:
            circuit.record_failure("LLM_REQUEST_FAILED",error_detail(e))
        raise HTTPException(400,error_detail(e))

@router.get("/ontology/baseline")
async def ontology_baseline():
    proposal=build_personal_credit_baseline()
    return {"proposal":proposal.model_dump(),"validation_errors":validate(proposal),"axiom_executability":axiom_executability(proposal)}

@router.post("/ontology/validate")
async def val(req:ProposalRequest):
    e=validate(req.proposal)
    return {"valid":not e,"errors":e}

@router.post("/ontology/audit")
async def ontology_audit(req:ProposalRequest):
    try:
        profile=postgres().validation_profile()
        execution=execute_axioms(req.proposal,profile)
        return mask_payload(audit_personal_credit_proposal(req.proposal,profile["tables"],execution))
    except Exception as e:
        raise HTTPException(400,error_detail(e))

@router.get("/ontology/axioms/validate")
async def validate_current_axioms():
    try:
        proposal=build_personal_credit_baseline()
        return mask_payload(execute_axioms(proposal,postgres().validation_profile()))
    except Exception as e:
        raise HTTPException(400,error_detail(e))

@router.post("/ontology/axioms/validate")
async def validate_proposal_axioms(req:ProposalRequest):
    try:
        return mask_payload(execute_axioms(req.proposal,postgres().validation_profile()))
    except Exception as e:
        raise HTTPException(400,error_detail(e))

@router.get("/ontology/data-quality/validate")
async def validate_current_data_quality():
    try:
        proposal=active_version_proposal()
        return mask_payload(validate_catalog_data_quality(proposal,postgres().validation_profile()))
    except Exception as e:
        raise HTTPException(400,error_detail(e))

@router.post("/ontology/data-quality/validate")
async def validate_proposal_data_quality(req:ProposalRequest):
    try:
        return mask_payload(validate_catalog_data_quality(req.proposal,postgres().validation_profile()))
    except Exception as e:
        raise HTTPException(400,error_detail(e))

@router.post("/ontology/approve")
async def approve():
    raise HTTPException(409,"直接发布入口已关闭；请依次保存草稿、审核、批准并发布登记版本")

@router.get("/ontology/git/status")
async def ontology_git_status():
    try:
        ensure_ontology_git()
        branches=ontology_git.list_branches(); tags=ontology_git.list_tags()
        default=next((item for item in branches if item["is_default"]),None)
        return mask_payload({"default_branch":default["name"] if default else "main","head":default.get("head_commit_id") if default else None,"branches":branches,"tags":tags})
    except Exception as e:
        raise HTTPException(500,error_detail(e))

@router.get("/ontology/git/log")
async def ontology_git_log(ref:str="main",limit:int=50):
    try:
        ensure_ontology_git()
        return mask_payload({"ref":ref,"commits":ontology_git.log(ref,limit)})
    except OntologyGitError as e:
        raise HTTPException(404,error_detail(e))

@router.get("/ontology/git/diff")
async def ontology_git_diff(from_ref:str,to_ref:str):
    try:
        ensure_ontology_git()
        return mask_payload(ontology_git.diff(from_ref,to_ref))
    except OntologyGitError as e:
        raise HTTPException(404,error_detail(e))

@router.post("/ontology/git/commits")
async def ontology_git_commit(req:OntologyGitCommitRequest):
    try:
        ensure_ontology_git()
        return mask_payload(ontology_git.commit(req.branch,req.proposal,req.message,req.author,req.expected_head))
    except OntologyGitError as e:
        raise HTTPException(409,error_detail(e))

@router.post("/ontology/git/branches")
async def ontology_git_create_branch(req:OntologyGitBranchRequest):
    try:
        ensure_ontology_git()
        return mask_payload(ontology_git.create_branch(req.name,req.from_ref))
    except OntologyGitError as e:
        raise HTTPException(409,error_detail(e))

@router.post("/ontology/git/tags")
async def ontology_git_create_tag(req:OntologyGitTagRequest):
    try:
        ensure_ontology_git()
        return mask_payload(ontology_git.create_tag(req.name,req.ref,req.message))
    except OntologyGitError as e:
        raise HTTPException(409,error_detail(e))

@router.get("/ontology/git/show/{ref:path}")
async def ontology_git_show(ref:str):
    try:
        ensure_ontology_git()
        return mask_payload(ontology_git.show(ref))
    except OntologyGitError as e:
        raise HTTPException(404,error_detail(e))

@router.get("/ontology/versions")
async def list_ontology_versions():
    try:
        ensure_version_registry()
        return {"versions":version_store.list_versions(),"active_version":(version_store.active() or {}).get("version")}
    except Exception as e:
        raise HTTPException(500,error_detail(e))

@router.get("/ontology/version-events")
async def list_ontology_version_events(limit:int=100):
    try:
        ensure_version_registry()
        return {"events":version_store.list_events(limit)}
    except Exception as e:
        raise HTTPException(500,error_detail(e))

@router.get("/ontology/versions/{version}")
async def get_ontology_version(version:str):
    try:
        ensure_version_registry()
        return mask_payload(version_store.get(version))
    except VersionWorkflowError as e:
        raise HTTPException(404,error_detail(e))

@router.post("/ontology/versions/drafts")
async def save_ontology_draft(req:DraftRequest):
    try:
        ensure_version_registry()
        return mask_payload(version_store.save_draft(req.proposal,req.notes))
    except VersionWorkflowError as e:
        raise HTTPException(409,error_detail(e))

@router.post("/ontology/versions/{version}/review")
async def review_ontology_draft(version:str,req:WorkflowNoteRequest):
    try:
        ensure_version_registry(); record=version_store.get(version)
        if record["status"]!="draft": raise VersionWorkflowError("只有 draft 状态可以提交审核")
        proposal=_proposal_from_record(record); profile=postgres().validation_profile()
        active=version_store.active(); previous=_proposal_from_record(active) if active else None
        audit=review_release_candidate(proposal,profile,previous)
        if not audit["valid"]:
            version_store.record_review_attempt(version,audit,req.note)
            return mask_payload({"status":"review_failed","version":version,"audit":audit})
        updated=version_store.mark_reviewed(version,audit,req.note)
        return mask_payload({"status":"reviewed","version":updated,"audit":audit})
    except VersionWorkflowError as e:
        raise HTTPException(409,error_detail(e))

@router.post("/ontology/versions/{version}/approve")
async def approve_ontology_version(version:str,req:WorkflowNoteRequest):
    try:
        ensure_version_registry()
        return mask_payload({"status":"approved","version":version_store.mark_approved(version,req.note)})
    except VersionWorkflowError as e:
        raise HTTPException(409,error_detail(e))

def _switch_published_version(version:str,action:str):
    ensure_version_registry()
    with version_store.publication_guard():
        target=version_store.get(version); current=version_store.active()
        if action=="publish" and target["status"]!="approved":
            raise VersionWorkflowError("只有 approved 状态可以发布")
        if action=="rollback" and target["status"]!="published":
            raise VersionWorkflowError("只能回滚到曾经成功发布的版本")
        if current and current["version"]==version:
            raise VersionWorkflowError(f"版本 {version} 已经是当前活动版本")
        proposal=_proposal_from_record(target); previous=_proposal_from_record(current) if current else None
        runtime_errors=_action_runtime_compatibility_errors(proposal)
        if runtime_errors:
            raise VersionWorkflowError(
                "目标版本与当前 Action 运行合同不兼容："
                +"；".join(runtime_errors)
            )
        profile=postgres().validation_profile()
        audit=review_release_candidate(proposal,profile,previous)
        if not audit["valid"]:
            raise VersionWorkflowError("当前数据下重新审核未通过，发布或回滚已阻止")
        if target.get("audit_digest")!=audit["audited_digest"]:
            raise VersionWorkflowError("已批准内容与当前候选摘要不一致")
        n=nebula()
        try:
            compatibility=n.abox_compatibility_report(proposal)
            if not compatibility["valid"]:
                raise VersionWorkflowError("目标 TBox 与当前 ABox 引用不兼容，已阻止切换")
            try:
                publication=n.write_tbox(proposal,target["audit_digest"])
                synchronization=n.sync_postgres_profile(profile,proposal)
                mapping=n.mapping_report(proposal)
                if not mapping["valid"]:
                    raise RuntimeError("目标版本发布后的 ABox–TBox 映射校验未通过")
                registered=version_store.record_publication(version,action,{
                    "mapping_summary":mapping.get("summary"),
                    "compatibility":compatibility,
                })
            except Exception:
                if previous:
                    n.write_tbox(previous,current.get("audit_digest"))
                    n.sync_postgres_profile(profile,previous)
                raise
            return mask_payload({
                "status":"published" if action=="publish" else "rolled_back",
                "active_version":registered["version"],
                "publication":publication,
                "synchronization":synchronization,
                "mapping_report":mapping,
                "compatibility":compatibility,
            })
        finally:
            n.close()

@router.post("/ontology/versions/{version}/publish")
async def publish_ontology_version(version:str):
    try: return _switch_published_version(version,"publish")
    except VersionWorkflowError as e: raise HTTPException(409,error_detail(e))
    except Exception as e: raise HTTPException(500,error_detail(e))

@router.post("/ontology/versions/{version}/rollback")
async def rollback_ontology_version(version:str):
    try: return _switch_published_version(version,"rollback")
    except VersionWorkflowError as e: raise HTTPException(409,error_detail(e))
    except Exception as e: raise HTTPException(500,error_detail(e))

@router.post("/kg/sync")
async def sync(req:ProfileRequest):
    prof=postgres().validation_profile()
    proposal=active_version_proposal()
    data_quality=validate_catalog_data_quality(proposal,prof)
    if not data_quality["valid"]:
        raise HTTPException(422,mask_payload({"message":"ABox 同步被目录驱动数据质量门禁拦截","data_quality":data_quality}))
    axiom_validation=execute_axioms(proposal,prof)
    if not axiom_validation["valid"]:
        raise HTTPException(422,mask_payload({"message":"ABox 同步被错误级公理拦截","axiom_validation":axiom_validation}))
    n=nebula()
    try:
        result=n.sync_postgres_profile(prof,proposal)
        return mask_payload({"status":"synced",**result,"data_quality":data_quality,"axiom_validation":axiom_validation,"mapping_report":n.mapping_report(proposal)})
    finally:
        n.close()

@router.get("/kg/mapping/report")
async def mapping_report():
    n=nebula()
    try: return mask_payload(n.mapping_report(active_version_proposal()))
    finally: n.close()

@router.get("/kg/related/{node_id}")
async def related(node_id:str,hops:int=1):
    n=None
    try:
        n=nebula()
        return mask_payload({"node_id":node_id,"hops":hops,"paths":n.related(node_id,hops)})
    except Exception as e:
        raise HTTPException(500,error_detail(e))
    finally:
        if n: n.close()

@router.get("/kg/stats")
async def stats():
    n=nebula()
    try: return n.stats()
    finally: n.close()

@router.get("/kg/snapshot")
async def snapshot(limit:int=300,offset:int=0):
    n=None
    try:
        n=nebula()
        return mask_graph_snapshot(n.query_graph("",1,limit,offset,"all"))
    except Exception as e:
        raise HTTPException(500,error_detail(e))
    finally:
        if n: n.close()

@router.post("/kg/query")
async def query_graph(req:GraphQueryRequest):
    n=None
    try:
        n=nebula()
        return mask_graph_snapshot(n.query_graph(req.query,req.hops,req.limit,req.offset,req.mode))
    except Exception as e:
        raise HTTPException(500,error_detail(e))
    finally:
        if n: n.close()
