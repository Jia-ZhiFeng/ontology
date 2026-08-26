import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI,Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from .api.routes import router
from .action.execution_repository import PostgresActionExecutionRepository
from .action.loan_application_repository import PostgresLoanApplicationRepository
from .action.credit_report_repository import PostgresCreditReportRepository
from .action.anti_fraud_report_repository import PostgresAntiFraudReportRepository
from .action.outbox import (
    AntiFraudReportOutboxWorker,
    CreditReportOutboxWorker,
    LoanApplicationOutboxWorker,
    PostgresActionOutboxRepository,
    PhoneVerificationTaskOutboxWorker,
    PhoneVerificationResultOutboxWorker,
    ManualReviewOutboxWorker,
    DecisionRecommendationOutboxWorker,
    RiskAssessmentOutboxWorker,
)
from .action.phone_dispatch import (
    DisabledPhoneTaskDispatcher,
    PostgresPhoneDispatchCoordinator,
    SandboxPhoneTaskDispatcher,
)
from .action.phone_verification_repository import (
    PostgresPhoneVerificationTaskRepository,
)
from .action.phone_verification_result_repository import (
    PostgresPhoneVerificationResultRepository,
)
from .action.manual_review_repository import PostgresManualReviewRepository
from .action.decision_recommendation_repository import (
    PostgresDecisionRecommendationRepository,
)
from .action.outbox_replay import PostgresOutboxReplayRepository
from .action.operations import (
    DisabledAlertNotifier,
    ExternalCircuitBreaker,
    OperationalAlertService,
    OperationsMonitor,
    PostgresCircuitBreakerRepository,
    PostgresOperationalAlertRepository,
    WebhookAlertNotifier,
)
from .action.risk_assessment_repository import PostgresRiskAssessmentRepository
from .action.workflow import PostgresPersonalCreditWorkflowRepository
from .core.config import (
    CORS_ORIGINS,
    ACTION_OUTBOX_LEASE_SECONDS,
    ACTION_OUTBOX_POLL_SECONDS,
    ACTION_OUTBOX_RETRY_BASE_SECONDS,
    ACTION_OUTBOX_RETRY_MAX_SECONDS,
    ALERT_ESCALATION_SECONDS,
    ALERT_NOTIFICATION_COOLDOWN_SECONDS,
    ALERT_WEBHOOK_ALLOW_INSECURE_LOCAL,
    ALERT_WEBHOOK_ENABLED,
    ALERT_WEBHOOK_TIMEOUT_SECONDS,
    ALERT_WEBHOOK_URL,
    EXTERNAL_CIRCUIT_FAILURE_THRESHOLD,
    EXTERNAL_CIRCUIT_OPEN_SECONDS,
    MVP_API_KEY,
    MVP_WORKFLOW_ACTOR_ID,
    NEBULA_HOST,
    NEBULA_PASSWORD,
    NEBULA_PORT,
    NEBULA_SPACE,
    NEBULA_USER,
    POSTGRES_DATABASE,
    POSTGRES_HOST,
    POSTGRES_PASSWORD,
    POSTGRES_PORT,
    POSTGRES_USER,
    PHONE_TASK_DISPATCH_MODE,
    OPERATIONS_SCAN_SECONDS,
    WORKFLOW_DISPATCH_LEASE_SECONDS,
)
from .core.security import api_key_matches
from .kg.nebula import NebulaGraphService
from .ontology.models import OntologyProposal
from .ontology.version_repository import OntologyVersionRepository
from .postgres.client import PostgresService


logger=logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app:FastAPI):
    postgres=PostgresService(
        POSTGRES_HOST,POSTGRES_PORT,POSTGRES_DATABASE,POSTGRES_USER,POSTGRES_PASSWORD
    )
    execution_repository=PostgresActionExecutionRepository(postgres)
    execution_repository.ensure_schema()
    workflow_repository=PostgresPersonalCreditWorkflowRepository(
        postgres,dispatch_lease_seconds=WORKFLOW_DISPATCH_LEASE_SECONDS
    )
    workflow_repository.ensure_schema()
    outbox_repository=PostgresActionOutboxRepository(postgres)
    outbox_repository.ensure_schema()
    PostgresOutboxReplayRepository(postgres).ensure_schema()
    alert_repository=PostgresOperationalAlertRepository(postgres)
    alert_repository.ensure_schema()
    notifier=(
        WebhookAlertNotifier(
            ALERT_WEBHOOK_URL,
            timeout_seconds=ALERT_WEBHOOK_TIMEOUT_SECONDS,
            allow_insecure_local=ALERT_WEBHOOK_ALLOW_INSECURE_LOCAL,
        )
        if ALERT_WEBHOOK_ENABLED
        else DisabledAlertNotifier()
    )
    alert_service=OperationalAlertService(
        alert_repository,
        notifier,
        escalation_seconds=ALERT_ESCALATION_SECONDS,
        notification_cooldown_seconds=ALERT_NOTIFICATION_COOLDOWN_SECONDS,
    )
    circuit_repository=PostgresCircuitBreakerRepository(postgres)
    version_repository=OntologyVersionRepository(postgres)

    def proposal_loader(version:str):
        return OntologyProposal.model_validate(version_repository.get(version)["proposal"])

    def nebula_factory():
        return NebulaGraphService(
            NEBULA_HOST,NEBULA_PORT,NEBULA_USER,NEBULA_PASSWORD,NEBULA_SPACE
        )

    risk_worker=RiskAssessmentOutboxWorker(
        postgres_service=postgres,
        nebula_factory=nebula_factory,
        proposal_loader=proposal_loader,
        outbox_repository=outbox_repository,
        execution_repository=execution_repository,
        report_repository=PostgresRiskAssessmentRepository(postgres),
        lease_seconds=ACTION_OUTBOX_LEASE_SECONDS,
        retry_base_seconds=ACTION_OUTBOX_RETRY_BASE_SECONDS,
        retry_max_seconds=ACTION_OUTBOX_RETRY_MAX_SECONDS,
    )
    application_worker=LoanApplicationOutboxWorker(
        postgres_service=postgres,
        nebula_factory=nebula_factory,
        proposal_loader=proposal_loader,
        outbox_repository=outbox_repository,
        execution_repository=execution_repository,
        application_repository=PostgresLoanApplicationRepository(postgres),
        lease_seconds=ACTION_OUTBOX_LEASE_SECONDS,
        retry_base_seconds=ACTION_OUTBOX_RETRY_BASE_SECONDS,
        retry_max_seconds=ACTION_OUTBOX_RETRY_MAX_SECONDS,
    )
    credit_report_worker=CreditReportOutboxWorker(
        postgres_service=postgres,
        nebula_factory=nebula_factory,
        proposal_loader=proposal_loader,
        outbox_repository=outbox_repository,
        execution_repository=execution_repository,
        report_repository=PostgresCreditReportRepository(postgres),
        lease_seconds=ACTION_OUTBOX_LEASE_SECONDS,
        retry_base_seconds=ACTION_OUTBOX_RETRY_BASE_SECONDS,
        retry_max_seconds=ACTION_OUTBOX_RETRY_MAX_SECONDS,
    )
    fraud_report_worker=AntiFraudReportOutboxWorker(
        postgres_service=postgres,
        nebula_factory=nebula_factory,
        proposal_loader=proposal_loader,
        outbox_repository=outbox_repository,
        execution_repository=execution_repository,
        report_repository=PostgresAntiFraudReportRepository(postgres),
        lease_seconds=ACTION_OUTBOX_LEASE_SECONDS,
        retry_base_seconds=ACTION_OUTBOX_RETRY_BASE_SECONDS,
        retry_max_seconds=ACTION_OUTBOX_RETRY_MAX_SECONDS,
    )
    phone_task_repository=PostgresPhoneVerificationTaskRepository(postgres)
    phone_task_dispatcher=(
        SandboxPhoneTaskDispatcher()
        if PHONE_TASK_DISPATCH_MODE=="sandbox"
        else DisabledPhoneTaskDispatcher()
    )
    phone_task_worker=PhoneVerificationTaskOutboxWorker(
        postgres_service=postgres,
        nebula_factory=nebula_factory,
        proposal_loader=proposal_loader,
        outbox_repository=outbox_repository,
        execution_repository=execution_repository,
        task_repository=phone_task_repository,
        dispatch_coordinator=PostgresPhoneDispatchCoordinator(
            postgres,phone_task_dispatcher,
            ExternalCircuitBreaker(
                "phone-task-dispatch",
                circuit_repository,
                alert_service,
                failure_threshold=EXTERNAL_CIRCUIT_FAILURE_THRESHOLD,
                open_seconds=EXTERNAL_CIRCUIT_OPEN_SECONDS,
            ),
        ),
        lease_seconds=ACTION_OUTBOX_LEASE_SECONDS,
        retry_base_seconds=ACTION_OUTBOX_RETRY_BASE_SECONDS,
        retry_max_seconds=ACTION_OUTBOX_RETRY_MAX_SECONDS,
    )
    phone_result_repository=PostgresPhoneVerificationResultRepository(
        postgres,phone_task_repository
    )
    phone_result_worker=PhoneVerificationResultOutboxWorker(
        postgres_service=postgres,
        nebula_factory=nebula_factory,
        proposal_loader=proposal_loader,
        outbox_repository=outbox_repository,
        execution_repository=execution_repository,
        result_repository=phone_result_repository,
        lease_seconds=ACTION_OUTBOX_LEASE_SECONDS,
        retry_base_seconds=ACTION_OUTBOX_RETRY_BASE_SECONDS,
        retry_max_seconds=ACTION_OUTBOX_RETRY_MAX_SECONDS,
    )
    manual_review_repository=PostgresManualReviewRepository(postgres)
    manual_review_worker=ManualReviewOutboxWorker(
        postgres_service=postgres,
        nebula_factory=nebula_factory,
        proposal_loader=proposal_loader,
        outbox_repository=outbox_repository,
        execution_repository=execution_repository,
        review_repository=manual_review_repository,
        lease_seconds=ACTION_OUTBOX_LEASE_SECONDS,
        retry_base_seconds=ACTION_OUTBOX_RETRY_BASE_SECONDS,
        retry_max_seconds=ACTION_OUTBOX_RETRY_MAX_SECONDS,
    )
    decision_recommendation_repository=PostgresDecisionRecommendationRepository(
        postgres
    )
    decision_recommendation_worker=DecisionRecommendationOutboxWorker(
        postgres_service=postgres,
        nebula_factory=nebula_factory,
        proposal_loader=proposal_loader,
        outbox_repository=outbox_repository,
        execution_repository=execution_repository,
        recommendation_repository=decision_recommendation_repository,
        lease_seconds=ACTION_OUTBOX_LEASE_SECONDS,
        retry_base_seconds=ACTION_OUTBOX_RETRY_BASE_SECONDS,
        retry_max_seconds=ACTION_OUTBOX_RETRY_MAX_SECONDS,
    )
    workers=(
        risk_worker,application_worker,credit_report_worker,fraud_report_worker,
        phone_task_worker,phone_result_worker,manual_review_worker,
        decision_recommendation_worker,
    )
    stop_event=asyncio.Event()
    operations_monitor=OperationsMonitor(
        postgres,
        alert_service,
        outbox_lease_seconds=ACTION_OUTBOX_LEASE_SECONDS,
    )

    async def run_outbox_worker():
        while not stop_event.is_set():
            for worker in workers:
                try:
                    await asyncio.to_thread(worker.drain,20)
                except Exception:
                    logger.exception(
                        "Action outbox worker cycle failed: %s",
                        worker.ACTION_NAME,
                    )
            try:
                await asyncio.wait_for(
                    stop_event.wait(),timeout=ACTION_OUTBOX_POLL_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    worker_task=asyncio.create_task(run_outbox_worker())
    async def run_operations_monitor():
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(operations_monitor.run_cycle)
            except Exception:
                logger.exception("Operations alert monitor cycle failed")
            try:
                await asyncio.to_thread(
                    workflow_repository.reconcile_active,
                    actor_id=MVP_WORKFLOW_ACTOR_ID,
                    limit=100,
                )
            except Exception:
                logger.exception("Personal credit workflow reconcile cycle failed")
            try:
                await asyncio.wait_for(
                    stop_event.wait(),timeout=OPERATIONS_SCAN_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    operations_task=asyncio.create_task(run_operations_monitor())
    try:
        yield
    finally:
        stop_event.set()
        await asyncio.gather(worker_task,operations_task)


app=FastAPI(
    title="Credit Ontology PostgreSQL + NebulaGraph MVP",
    version="1.1.0",
    lifespan=lifespan,
)

@app.exception_handler(RequestValidationError)
async def safe_request_validation_error(_request:Request,exc:RequestValidationError):
    # FastAPI's default error includes the rejected input. Origination requests contain
    # personal/financial data, so expose only location, type and the public message.
    errors=[
        {"loc":item.get("loc",()),"type":item.get("type"),"msg":item.get("msg")}
        for item in exc.errors()
    ]
    return JSONResponse(status_code=422,content={"detail":errors})

@app.middleware("http")
async def require_mvp_api_key(request:Request,call_next):
    if request.method=="OPTIONS" or request.url.path=="/api/health" or not request.url.path.startswith("/api/"):
        return await call_next(request)
    if not MVP_API_KEY:
        return JSONResponse(status_code=503,content={"detail":"服务端尚未配置 MVP_API_KEY，受保护接口已关闭"})
    if not api_key_matches(MVP_API_KEY,request.headers.get("X-API-Key")):
        return JSONResponse(status_code=401,content={"detail":"访问令牌缺失或无效，请在“访问与 LLM 配置”中填写"})
    return await call_next(request)

# CORS must wrap the early authentication responses so browsers can read the
# JSON error body instead of collapsing it into an opaque "Failed to fetch".
app.add_middleware(CORSMiddleware,allow_origins=CORS_ORIGINS,allow_credentials=True,allow_methods=["*"],allow_headers=["*"])
app.include_router(router,prefix="/api")
