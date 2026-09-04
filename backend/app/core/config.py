import os
from dotenv import load_dotenv
from .agent_runtime import parse_personal_credit_agent_mode
load_dotenv()
POSTGRES_HOST=os.getenv("POSTGRES_HOST","localhost")
POSTGRES_PORT=int(os.getenv("POSTGRES_PORT","5432"))
POSTGRES_DATABASE=os.getenv("POSTGRES_DATABASE","credit_ontology")
POSTGRES_USER=os.getenv("POSTGRES_USER","credit")
POSTGRES_PASSWORD=os.getenv("POSTGRES_PASSWORD","credit123")
NEBULA_HOST=os.getenv("NEBULA_HOST","localhost")
NEBULA_PORT=int(os.getenv("NEBULA_PORT","9669"))
NEBULA_USER=os.getenv("NEBULA_USER","root")
NEBULA_PASSWORD=os.getenv("NEBULA_PASSWORD","nebula")
NEBULA_SPACE=os.getenv("NEBULA_SPACE","credit_ontology")
CORS_ORIGINS=os.getenv(
    "CORS_ORIGINS",
    "http://localhost:8080,http://127.0.0.1:8080,"
    "http://localhost:4173,http://127.0.0.1:4173",
)
CORS_ORIGINS=[item.strip().rstrip("/") for item in CORS_ORIGINS.split(",") if item.strip()]
MVP_API_KEY=os.getenv("MVP_API_KEY","").strip()
MVP_AUTH_MODE=os.getenv("MVP_AUTH_MODE","api_key").strip().lower()
if MVP_AUTH_MODE not in {"api_key","local_session"}:
    MVP_AUTH_MODE="api_key"
MVP_LOCAL_SESSION_SECRET=(
    os.getenv("MVP_LOCAL_SESSION_SECRET","").strip() or MVP_API_KEY
)
MVP_LOCAL_SESSION_TTL_SECONDS=max(
    60,min(int(os.getenv("MVP_LOCAL_SESSION_TTL_SECONDS","900")),3600)
)
MVP_LOCAL_SESSION_TRUST_PROXY=os.getenv(
    "MVP_LOCAL_SESSION_TRUST_PROXY","false"
).strip().lower() in {"1","true","yes","on"}
MVP_LOCAL_SESSION_COOKIE_SECURE=os.getenv(
    "MVP_LOCAL_SESSION_COOKIE_SECURE","false"
).strip().lower() in {"1","true","yes","on"}
MVP_LOCAL_SESSION_ALLOWED_ORIGINS=[
    item.strip().rstrip("/")
    for item in os.getenv(
        "MVP_LOCAL_SESSION_ALLOWED_ORIGINS",",".join(CORS_ORIGINS)
    ).split(",")
    if item.strip()
]
LLM_CONFIG_ENCRYPTION_KEY=os.getenv("LLM_CONFIG_ENCRYPTION_KEY","").strip() or MVP_API_KEY
LLM_REQUEST_TIMEOUT_SECONDS=max(
    5.0,min(float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS","60")),300.0)
)
WORKBENCH_UNAUTHENTICATED=os.getenv(
    "WORKBENCH_UNAUTHENTICATED","false"
).strip().lower() in {"1","true","yes","on"}
PERSONAL_CREDIT_AGENT_MODE=parse_personal_credit_agent_mode(
    os.getenv("PERSONAL_CREDIT_AGENT_MODE","legacy")
).value
MVP_ACTION_ACTOR_ID=os.getenv("MVP_ACTION_ACTOR_ID","mvp-local-operator").strip()
MVP_ACTION_ACTOR_ROLE=os.getenv("MVP_ACTION_ACTOR_ROLE","风控专员").strip()
MVP_CREATE_APPLICATION_ACTOR_ID=os.getenv(
    "MVP_CREATE_APPLICATION_ACTOR_ID","mvp-origination-adapter"
).strip()
MVP_CREATE_APPLICATION_ACTOR_ROLE=os.getenv(
    "MVP_CREATE_APPLICATION_ACTOR_ROLE","系统适配器"
).strip()
MVP_CREDIT_REPORT_ACTOR_ID=os.getenv(
    "MVP_CREDIT_REPORT_ACTOR_ID","mvp-credit-adapter"
).strip()
MVP_CREDIT_REPORT_ACTOR_ROLE=os.getenv(
    "MVP_CREDIT_REPORT_ACTOR_ROLE","系统适配器"
).strip()
CREDIT_PROVIDER_MODE=os.getenv("CREDIT_PROVIDER_MODE","disabled").strip().lower()
CREDIT_CONSENT_MODE=os.getenv("CREDIT_CONSENT_MODE","disabled").strip().lower()
CREDIT_SANDBOX_CONSENT_PREFIX=os.getenv(
    "CREDIT_SANDBOX_CONSENT_PREFIX","SANDBOX-CONSENT:"
)
MVP_FRAUD_REPORT_ACTOR_ID=os.getenv(
    "MVP_FRAUD_REPORT_ACTOR_ID","mvp-fraud-adapter"
).strip()
MVP_FRAUD_REPORT_ACTOR_ROLE=os.getenv(
    "MVP_FRAUD_REPORT_ACTOR_ROLE","系统适配器"
).strip()
FRAUD_PROVIDER_MODE=os.getenv("FRAUD_PROVIDER_MODE","disabled").strip().lower()
MVP_PHONE_TASK_ACTOR_ID=os.getenv(
    "MVP_PHONE_TASK_ACTOR_ID","mvp-phone-task-adapter"
).strip()
MVP_PHONE_TASK_ACTOR_ROLE=os.getenv(
    "MVP_PHONE_TASK_ACTOR_ROLE","系统适配器"
).strip()
PHONE_TASK_DISPATCH_MODE=os.getenv(
    "PHONE_TASK_DISPATCH_MODE","disabled"
).strip().lower()
MVP_PHONE_RESULT_ACTOR_ID=os.getenv(
    "MVP_PHONE_RESULT_ACTOR_ID","mvp-phone-result-adapter"
).strip()
MVP_PHONE_RESULT_ACTOR_ROLE=os.getenv(
    "MVP_PHONE_RESULT_ACTOR_ROLE","数字人适配器"
).strip()
MVP_MANUAL_REVIEW_ACTOR_ID=os.getenv(
    "MVP_MANUAL_REVIEW_ACTOR_ID","mvp-manual-review-router"
).strip()
MVP_MANUAL_REVIEW_ACTOR_ROLE=os.getenv(
    "MVP_MANUAL_REVIEW_ACTOR_ROLE","系统适配器"
).strip()
MVP_DECISION_RECOMMENDATION_ACTOR_ID=os.getenv(
    "MVP_DECISION_RECOMMENDATION_ACTOR_ID","mvp-risk-manager"
).strip()
MVP_DECISION_RECOMMENDATION_ACTOR_ROLE=os.getenv(
    "MVP_DECISION_RECOMMENDATION_ACTOR_ROLE","风险经理"
).strip()
MVP_OUTBOX_OPERATOR_ID=os.getenv(
    "MVP_OUTBOX_OPERATOR_ID","mvp-outbox-operator"
).strip()
MVP_OUTBOX_OPERATOR_ROLE=os.getenv(
    "MVP_OUTBOX_OPERATOR_ROLE","系统运维"
).strip()
MVP_WORKFLOW_ACTOR_ID=os.getenv(
    "MVP_WORKFLOW_ACTOR_ID","mvp-workflow-orchestrator"
).strip()
MVP_WORKFLOW_ACTOR_ROLE=os.getenv(
    "MVP_WORKFLOW_ACTOR_ROLE","系统适配器"
).strip()
ACTION_OUTBOX_MAX_ATTEMPTS=max(1,int(os.getenv("ACTION_OUTBOX_MAX_ATTEMPTS","5")))
ACTION_OUTBOX_LEASE_SECONDS=max(5,int(os.getenv("ACTION_OUTBOX_LEASE_SECONDS","60")))
ACTION_OUTBOX_POLL_SECONDS=max(1,int(os.getenv("ACTION_OUTBOX_POLL_SECONDS","5")))
ACTION_OUTBOX_RETRY_BASE_SECONDS=max(1,int(os.getenv("ACTION_OUTBOX_RETRY_BASE_SECONDS","5")))
ACTION_OUTBOX_RETRY_MAX_SECONDS=max(ACTION_OUTBOX_RETRY_BASE_SECONDS,int(os.getenv("ACTION_OUTBOX_RETRY_MAX_SECONDS","300")))
OPERATIONS_SCAN_SECONDS=max(5,int(os.getenv("OPERATIONS_SCAN_SECONDS","15")))
WORKFLOW_DISPATCH_LEASE_SECONDS=max(30,int(os.getenv("WORKFLOW_DISPATCH_LEASE_SECONDS","120")))
ALERT_ESCALATION_SECONDS=max(5,int(os.getenv("ALERT_ESCALATION_SECONDS","900")))
ALERT_NOTIFICATION_COOLDOWN_SECONDS=max(0,int(os.getenv("ALERT_NOTIFICATION_COOLDOWN_SECONDS","300")))
EXTERNAL_CIRCUIT_FAILURE_THRESHOLD=max(1,int(os.getenv("EXTERNAL_CIRCUIT_FAILURE_THRESHOLD","3")))
EXTERNAL_CIRCUIT_OPEN_SECONDS=max(5,int(os.getenv("EXTERNAL_CIRCUIT_OPEN_SECONDS","60")))
ALERT_WEBHOOK_ENABLED=os.getenv("ALERT_WEBHOOK_ENABLED","false").strip().lower() in {"1","true","yes","on"}
ALERT_WEBHOOK_URL=os.getenv("ALERT_WEBHOOK_URL","").strip()
ALERT_WEBHOOK_TIMEOUT_SECONDS=max(1,int(os.getenv("ALERT_WEBHOOK_TIMEOUT_SECONDS","5")))
ALERT_WEBHOOK_ALLOW_INSECURE_LOCAL=os.getenv("ALERT_WEBHOOK_ALLOW_INSECURE_LOCAL","false").strip().lower() in {"1","true","yes","on"}
LLM_ALLOWED_BASE_URLS=[
    item.strip().rstrip("/")
    for item in os.getenv("LLM_ALLOWED_BASE_URLS","https://api.openai.com/v1").split(",")
    if item.strip()
]
LLM_ALLOW_INSECURE_LOCAL=os.getenv("LLM_ALLOW_INSECURE_LOCAL","false").strip().lower() in {"1","true","yes","on"}
