from __future__ import annotations

import hashlib
import math
import re
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

try:
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:  # Unit tests do not require the PostgreSQL driver.
    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, value):
            self.obj = value


REPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")
FRAUD_RESULTS = frozenset({"通过", "拒绝", "人工复核"})


class FraudProviderError(RuntimeError):
    pass


class FraudProviderNotConfigured(FraudProviderError):
    pass


class FraudProviderTimeout(FraudProviderError):
    pass


class FraudProviderResponseInvalid(FraudProviderError):
    pass


@dataclass(frozen=True)
class FraudProviderRequest:
    provider_request_id: str
    application_id: str
    borrower_id_number: str
    borrower_phone: str | None
    borrower_address: str | None
    vehicle_vin: str
    application_channel: str | None

    def context_hash(self) -> str:
        normalized = "|".join(
            (
                self.application_id,
                self.borrower_id_number,
                self.borrower_phone or "",
                self.borrower_address or "",
                self.vehicle_vin,
                self.application_channel or "",
            )
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FraudProviderResponse:
    provider_request_id: str
    application_id: str
    fraud_report_id: str
    fraud_score: float
    result: str
    hit_details: str | None
    provider_name: str
    provider_mode: str
    synthetic: bool = False

    def cache_payload(self) -> dict[str, Any]:
        return {
            "provider_request_id": self.provider_request_id,
            "application_id": self.application_id,
            "fraud_report_id": self.fraud_report_id,
            "fraud_score": self.fraud_score,
            "result": self.result,
            "hit_details": self.hit_details,
            "provider_name": self.provider_name,
            "provider_mode": self.provider_mode,
            "synthetic": self.synthetic,
        }


class FraudProvider(Protocol):
    provider_name: str
    provider_mode: str
    supports_idempotent_request_ids: bool

    def query(
        self, request: FraudProviderRequest, *, timeout_seconds: int
    ) -> FraudProviderResponse: ...


class DisabledFraudProvider:
    provider_name = "unconfigured"
    provider_mode = "disabled"
    supports_idempotent_request_ids = True

    def query(
        self, request: FraudProviderRequest, *, timeout_seconds: int
    ) -> FraudProviderResponse:
        del request, timeout_seconds
        raise FraudProviderNotConfigured("真实反欺诈供应商未配置，已拒绝发起查询")


class SandboxFraudProvider:
    provider_name = "local-fraud-sandbox"
    provider_mode = "sandbox"
    supports_idempotent_request_ids = True

    def query(
        self, request: FraudProviderRequest, *, timeout_seconds: int
    ) -> FraudProviderResponse:
        if timeout_seconds <= 0:
            raise FraudProviderTimeout("反欺诈沙箱查询超时")
        digest = hashlib.sha256(request.provider_request_id.encode("utf-8")).hexdigest()
        score = float(int(digest[:4], 16) % 10001) / 100
        if score >= 85:
            result, details = "拒绝", "沙箱高风险规则命中（合成数据）"
        elif score >= 55:
            result, details = "人工复核", "沙箱复核规则命中（合成数据）"
        else:
            result, details = "通过", "未命中沙箱风险规则（合成数据）"
        return FraudProviderResponse(
            provider_request_id=request.provider_request_id,
            application_id=request.application_id,
            fraud_report_id=f"SBXFR{digest[:24].upper()}",
            fraud_score=score,
            result=result,
            hit_details=details,
            provider_name=self.provider_name,
            provider_mode=self.provider_mode,
            synthetic=True,
        )


def deterministic_fraud_provider_request_id(application_id: str) -> str:
    digest = hashlib.sha256(application_id.encode("utf-8")).hexdigest()[:28].upper()
    return f"FP{digest}"


def validate_fraud_provider_response(
    response: FraudProviderResponse,
    request: FraudProviderRequest,
    *,
    expected_provider_name: str,
    expected_provider_mode: str,
) -> FraudProviderResponse:
    if not isinstance(response, FraudProviderResponse):
        raise FraudProviderResponseInvalid("反欺诈供应商响应类型无效")
    if response.provider_request_id != request.provider_request_id:
        raise FraudProviderResponseInvalid("反欺诈响应请求编号不匹配")
    if response.application_id != request.application_id:
        raise FraudProviderResponseInvalid("反欺诈响应申请编号不匹配")
    if response.provider_name != expected_provider_name:
        raise FraudProviderResponseInvalid("反欺诈响应来源不匹配")
    if response.provider_mode != expected_provider_mode:
        raise FraudProviderResponseInvalid("反欺诈响应模式不匹配")
    if not REPORT_ID_PATTERN.fullmatch(response.fraud_report_id):
        raise FraudProviderResponseInvalid("反欺诈报告编号格式无效")
    if (
        isinstance(response.fraud_score, bool)
        or not isinstance(response.fraud_score, (int, float))
        or not math.isfinite(float(response.fraud_score))
        or not 0 <= float(response.fraud_score) <= 100
    ):
        raise FraudProviderResponseInvalid("欺诈评分必须是0到100的有限数")
    if response.result not in FRAUD_RESULTS:
        raise FraudProviderResponseInvalid("反欺诈结果不在受控枚举中")
    details = response.hit_details.strip() if isinstance(response.hit_details, str) else ""
    if len(details) > 10000:
        raise FraudProviderResponseInvalid("反欺诈命中明细超出长度限制")
    if (
        response.result in {"拒绝", "人工复核"}
        or float(response.fraud_score) > 70
    ) and not details:
        raise FraudProviderResponseInvalid("命中反欺诈规则时必须返回命中明细")
    if response.provider_mode == "sandbox" and not response.synthetic:
        raise FraudProviderResponseInvalid("反欺诈沙箱响应必须标记为合成数据")
    if response.provider_mode != "sandbox" and response.synthetic:
        raise FraudProviderResponseInvalid("生产反欺诈响应不能标记为合成数据")
    return response


FRAUD_RECEIPT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS mvp_control.fraud_provider_receipts (
        provider_request_id varchar(64) PRIMARY KEY,
        application_id varchar(32) NOT NULL UNIQUE,
        request_context_hash char(64) NOT NULL,
        provider_name varchar(80) NOT NULL,
        provider_mode varchar(24) NOT NULL,
        synthetic boolean NOT NULL DEFAULT false,
        status varchar(16) NOT NULL CHECK (status IN ('PENDING','SUCCEEDED','FAILED')),
        attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        response_payload jsonb,
        last_error_code varchar(64),
        last_error_message varchar(500),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS fraud_provider_receipt_status_idx
    ON mvp_control.fraud_provider_receipts (status,updated_at)
    """,
)


class PostgresFraudProviderCoordinator:
    """Persist the external response before writing AntiFraudReport."""

    _schema_lock = threading.Lock()
    _schema_ready = False

    def __init__(self, postgres_service, provider: FraudProvider, circuit_breaker=None):
        self.postgres = postgres_service
        self.provider = provider
        self.circuit_breaker = circuit_breaker

    def ensure_schema(self) -> None:
        if type(self)._schema_ready:
            return
        with type(self)._schema_lock:
            if type(self)._schema_ready:
                return
            with self.postgres.conn() as connection, connection.cursor() as cursor:
                cursor.execute("CREATE SCHEMA IF NOT EXISTS mvp_control")
                for statement in FRAUD_RECEIPT_SCHEMA:
                    cursor.execute(statement)
            type(self)._schema_ready = True

    def _reserve(self, request: FraudProviderRequest) -> Mapping[str, Any]:
        self.ensure_schema()
        context_hash = request.context_hash()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"request_fraud_report:{request.application_id}",),
            )
            cursor.execute(
                """
                INSERT INTO mvp_control.fraud_provider_receipts (
                    provider_request_id,application_id,request_context_hash,
                    provider_name,provider_mode,synthetic,status
                ) VALUES (%s,%s,%s,%s,%s,false,'PENDING')
                ON CONFLICT (application_id) DO NOTHING
                """,
                (
                    request.provider_request_id,
                    request.application_id,
                    context_hash,
                    self.provider.provider_name,
                    self.provider.provider_mode,
                ),
            )
            cursor.execute(
                """
                SELECT * FROM mvp_control.fraud_provider_receipts
                WHERE application_id=%s FOR UPDATE
                """,
                (request.application_id,),
            )
            row = cursor.fetchone()
            if not row or row["provider_request_id"] != request.provider_request_id:
                raise FraudProviderResponseInvalid("反欺诈供应商幂等收据冲突")
            if row["request_context_hash"] != context_hash:
                raise FraudProviderResponseInvalid("申请核心上下文已变化，拒绝复用既有查询")
            if (
                row["provider_name"] != self.provider.provider_name
                or row["provider_mode"] != self.provider.provider_mode
            ):
                raise FraudProviderResponseInvalid("既有收据与当前反欺诈供应商不一致")
            return dict(row)

    def _from_cache(
        self, payload: Mapping[str, Any]
    ) -> FraudProviderResponse:
        try:
            return FraudProviderResponse(
                provider_request_id=str(payload["provider_request_id"]),
                application_id=str(payload["application_id"]),
                fraud_report_id=str(payload["fraud_report_id"]),
                fraud_score=float(payload["fraud_score"]),
                result=str(payload["result"]),
                hit_details=(
                    str(payload["hit_details"])
                    if payload.get("hit_details") is not None
                    else None
                ),
                provider_name=str(payload["provider_name"]),
                provider_mode=str(payload["provider_mode"]),
                synthetic=bool(payload["synthetic"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FraudProviderResponseInvalid("已缓存的反欺诈响应无效") from exc

    def _save_success(
        self, request: FraudProviderRequest, response: FraudProviderResponse
    ) -> None:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE mvp_control.fraud_provider_receipts
                SET status='SUCCEEDED',attempt_count=attempt_count+1,
                    response_payload=%s,synthetic=%s,last_error_code=NULL,
                    last_error_message=NULL,updated_at=now()
                WHERE provider_request_id=%s
                """,
                (
                    Jsonb(response.cache_payload()),
                    response.synthetic,
                    request.provider_request_id,
                ),
            )

    def _save_failure(
        self, request: FraudProviderRequest, code: str, message: str
    ) -> None:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE mvp_control.fraud_provider_receipts
                SET status='FAILED',attempt_count=attempt_count+1,
                    last_error_code=%s,last_error_message=%s,updated_at=now()
                WHERE provider_request_id=%s
                """,
                (code[:64], message[:500], request.provider_request_id),
            )

    def query(
        self,
        request: FraudProviderRequest,
        *,
        timeout_seconds: int = 30,
        max_attempts: int = 2,
    ) -> tuple[FraudProviderResponse, bool]:
        if self.provider.provider_mode == "disabled":
            raise FraudProviderNotConfigured("真实反欺诈供应商未配置，已拒绝发起查询")
        if not self.provider.supports_idempotent_request_ids:
            raise FraudProviderNotConfigured(
                "反欺诈供应商不保证请求编号幂等，已拒绝可能重复计费的查询"
            )
        receipt = self._reserve(request)
        if receipt["status"] == "SUCCEEDED" and receipt.get("response_payload"):
            cached = self._from_cache(receipt["response_payload"])
            return validate_fraud_provider_response(
                cached,
                request,
                expected_provider_name=self.provider.provider_name,
                expected_provider_mode=self.provider.provider_mode,
            ), True

        attempts = max(1, int(max_attempts))
        for attempt in range(1, attempts + 1):
            try:
                if self.circuit_breaker:
                    self.circuit_breaker.before_call()
                response = self.provider.query(request, timeout_seconds=timeout_seconds)
                response = validate_fraud_provider_response(
                    response,
                    request,
                    expected_provider_name=self.provider.provider_name,
                    expected_provider_mode=self.provider.provider_mode,
                )
                self._save_success(request, response)
                if self.circuit_breaker:
                    self.circuit_breaker.record_success()
                return response, False
            except FraudProviderTimeout as exc:
                self._save_failure(request, "ACTION_EXTERNAL_TIMEOUT", str(exc))
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure(
                        "ACTION_EXTERNAL_TIMEOUT", str(exc)
                    )
                if attempt == attempts:
                    raise
            except FraudProviderResponseInvalid as exc:
                self._save_failure(
                    request, "ACTION_EXTERNAL_RESPONSE_INVALID", str(exc)
                )
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure(
                        "ACTION_EXTERNAL_RESPONSE_INVALID", str(exc)
                    )
                raise
        raise FraudProviderTimeout("反欺诈供应商查询超时")
