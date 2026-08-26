from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

try:
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:  # Unit tests do not require the PostgreSQL driver.
    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, value):
            self.obj = value


REPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")
REPORTING_AGENCIES = frozenset({"央行征信", "百行征信", "朴道征信"})


class CreditProviderError(RuntimeError):
    """Controlled provider/consent boundary error."""


class CreditProviderNotConfigured(CreditProviderError):
    pass


class CreditConsentMissing(CreditProviderError):
    pass


class CreditProviderTimeout(CreditProviderError):
    pass


class CreditProviderResponseInvalid(CreditProviderError):
    pass


@dataclass(frozen=True)
class CreditProviderRequest:
    provider_request_id: str
    application_id: str
    subject_id_number: str
    query_purpose: str
    application_channel: str


@dataclass(frozen=True)
class CreditProviderResponse:
    provider_request_id: str
    report_id: str
    subject_id_number: str
    query_time: datetime
    application_channel: str
    outstanding_loan_count: int
    overdue_3y_count: int
    total_overdue_count: int
    inquiry_count: int
    reporting_agency: str
    provider_name: str
    provider_mode: str
    synthetic: bool = False

    def cache_payload(self) -> dict[str, Any]:
        """Exclude the raw identity number; the receipt keeps only its hash."""
        return {
            "provider_request_id": self.provider_request_id,
            "report_id": self.report_id,
            "query_time": self.query_time.isoformat(),
            "application_channel": self.application_channel,
            "outstanding_loan_count": self.outstanding_loan_count,
            "overdue_3y_count": self.overdue_3y_count,
            "total_overdue_count": self.total_overdue_count,
            "inquiry_count": self.inquiry_count,
            "reporting_agency": self.reporting_agency,
            "provider_name": self.provider_name,
            "provider_mode": self.provider_mode,
            "synthetic": self.synthetic,
        }


class CreditConsentVerifier(Protocol):
    def verify(
        self,
        *,
        application_id: str,
        subject_id_number: str,
        authorization_reference: str,
        query_purpose: str,
    ) -> Mapping[str, Any]: ...


class CreditProvider(Protocol):
    provider_name: str
    provider_mode: str
    supports_idempotent_request_ids: bool

    def query(
        self, request: CreditProviderRequest, *, timeout_seconds: int
    ) -> CreditProviderResponse: ...


class DisabledConsentVerifier:
    def verify(self, **_values) -> Mapping[str, Any]:
        raise CreditProviderNotConfigured("征信授权校验器未配置，已拒绝发起查询")


class SandboxConsentVerifier:
    """Explicit local-only consent fixture. It is not evidence of real consent."""

    def __init__(self, prefix: str = "SANDBOX-CONSENT:"):
        self.prefix = prefix

    def verify(
        self,
        *,
        application_id: str,
        subject_id_number: str,
        authorization_reference: str,
        query_purpose: str,
    ) -> Mapping[str, Any]:
        del subject_id_number
        expected = f"{self.prefix}{application_id}"
        if query_purpose != "贷款审批" or authorization_reference != expected:
            raise CreditConsentMissing("沙箱授权引用无效或查询用途不允许")
        return {
            "verified": True,
            "verifier": "sandbox",
            "authorization_reference_hash": hashlib.sha256(
                authorization_reference.encode("utf-8")
            ).hexdigest(),
        }


class DisabledCreditProvider:
    provider_name = "unconfigured"
    provider_mode = "disabled"
    supports_idempotent_request_ids = True

    def query(
        self, request: CreditProviderRequest, *, timeout_seconds: int
    ) -> CreditProviderResponse:
        del request, timeout_seconds
        raise CreditProviderNotConfigured("真实征信供应商未配置，已拒绝发起查询")


class SandboxCreditProvider:
    """Deterministic local provider used only for integration tests and demos."""

    provider_name = "local-credit-sandbox"
    provider_mode = "sandbox"
    supports_idempotent_request_ids = True

    def query(
        self, request: CreditProviderRequest, *, timeout_seconds: int
    ) -> CreditProviderResponse:
        if timeout_seconds <= 0:
            raise CreditProviderTimeout("征信沙箱查询超时")
        digest = hashlib.sha256(request.provider_request_id.encode("utf-8")).hexdigest()
        values = [int(digest[index : index + 2], 16) for index in range(0, 8, 2)]
        return CreditProviderResponse(
            provider_request_id=request.provider_request_id,
            report_id=f"SBXCR{digest[:24].upper()}",
            subject_id_number=request.subject_id_number,
            query_time=datetime.now(timezone.utc),
            application_channel=request.application_channel,
            outstanding_loan_count=values[0] % 6,
            overdue_3y_count=values[1] % 4,
            total_overdue_count=values[2] % 8,
            inquiry_count=values[3] % 12,
            reporting_agency="百行征信",
            provider_name=self.provider_name,
            provider_mode=self.provider_mode,
            synthetic=True,
        )


def deterministic_provider_request_id(
    application_id: str, subject_id_number: str
) -> str:
    digest = hashlib.sha256(
        f"{application_id}|{subject_id_number}".encode("utf-8")
    ).hexdigest()[:28].upper()
    return f"CP{digest}"


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CreditProviderResponseInvalid(f"供应商响应 {field_name} 必须是非负整数")
    return value


def validate_provider_response(
    response: CreditProviderResponse,
    request: CreditProviderRequest,
    *,
    expected_provider_name: str,
    expected_provider_mode: str,
) -> CreditProviderResponse:
    if not isinstance(response, CreditProviderResponse):
        raise CreditProviderResponseInvalid("供应商响应类型无效")
    if response.provider_request_id != request.provider_request_id:
        raise CreditProviderResponseInvalid("供应商响应的请求编号不匹配")
    if response.subject_id_number != request.subject_id_number:
        raise CreditProviderResponseInvalid("供应商响应主体与申请主体不匹配")
    if response.provider_name != expected_provider_name:
        raise CreditProviderResponseInvalid("供应商响应来源不匹配")
    if response.provider_mode != expected_provider_mode:
        raise CreditProviderResponseInvalid("供应商响应模式不匹配")
    if not REPORT_ID_PATTERN.fullmatch(response.report_id):
        raise CreditProviderResponseInvalid("供应商报告编号格式无效")
    if response.query_time.tzinfo is None or response.query_time.utcoffset() is None:
        raise CreditProviderResponseInvalid("供应商查询时间必须包含时区")
    if response.query_time > datetime.now(timezone.utc).astimezone(
        response.query_time.tzinfo
    ):
        raise CreditProviderResponseInvalid("供应商查询时间不能晚于当前时间")
    if response.application_channel != request.application_channel:
        raise CreditProviderResponseInvalid("供应商响应渠道与申请渠道不匹配")
    for field_name in (
        "outstanding_loan_count",
        "overdue_3y_count",
        "total_overdue_count",
        "inquiry_count",
    ):
        _integer(getattr(response, field_name), field_name)
    if response.reporting_agency not in REPORTING_AGENCIES:
        raise CreditProviderResponseInvalid("供应商响应报送机构不在受控枚举中")
    if response.provider_mode == "sandbox" and not response.synthetic:
        raise CreditProviderResponseInvalid("沙箱响应必须显式标记为合成数据")
    if response.provider_mode != "sandbox" and response.synthetic:
        raise CreditProviderResponseInvalid("生产供应商响应不能标记为合成数据")
    return response


CREDIT_RECEIPT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS mvp_control.credit_provider_receipts (
        provider_request_id varchar(64) PRIMARY KEY,
        application_id varchar(32) NOT NULL,
        subject_id_hash char(64) NOT NULL,
        provider_name varchar(80) NOT NULL,
        provider_mode varchar(24) NOT NULL,
        synthetic boolean NOT NULL DEFAULT false,
        status varchar(16) NOT NULL CHECK (status IN ('PENDING','SUCCEEDED','FAILED')),
        attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        response_payload jsonb,
        last_error_code varchar(64),
        last_error_message varchar(500),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (application_id,subject_id_hash)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS credit_provider_receipt_status_idx
    ON mvp_control.credit_provider_receipts (status,updated_at)
    """,
)


class PostgresCreditProviderCoordinator:
    """Durably caches a provider response before any business-table write."""

    _schema_lock = threading.Lock()
    _schema_ready = False

    def __init__(self, postgres_service, provider: CreditProvider, circuit_breaker=None):
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
                for statement in CREDIT_RECEIPT_SCHEMA:
                    cursor.execute(statement)
            type(self)._schema_ready = True

    @staticmethod
    def _subject_hash(subject_id_number: str) -> str:
        return hashlib.sha256(subject_id_number.encode("utf-8")).hexdigest()

    def _from_cache(
        self, payload: Mapping[str, Any], request: CreditProviderRequest
    ) -> CreditProviderResponse:
        try:
            query_time = datetime.fromisoformat(
                str(payload["query_time"]).replace("Z", "+00:00")
            )
            return CreditProviderResponse(
                provider_request_id=str(payload["provider_request_id"]),
                report_id=str(payload["report_id"]),
                subject_id_number=request.subject_id_number,
                query_time=query_time,
                application_channel=str(payload["application_channel"]),
                outstanding_loan_count=int(payload["outstanding_loan_count"]),
                overdue_3y_count=int(payload["overdue_3y_count"]),
                total_overdue_count=int(payload["total_overdue_count"]),
                inquiry_count=int(payload["inquiry_count"]),
                reporting_agency=str(payload["reporting_agency"]),
                provider_name=str(payload["provider_name"]),
                provider_mode=str(payload["provider_mode"]),
                synthetic=bool(payload["synthetic"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CreditProviderResponseInvalid("已缓存的征信供应商响应无效") from exc

    def _reserve(self, request: CreditProviderRequest) -> Mapping[str, Any]:
        self.ensure_schema()
        subject_hash = self._subject_hash(request.subject_id_number)
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"request_credit_report:{request.application_id}:{subject_hash}",),
            )
            cursor.execute(
                """
                INSERT INTO mvp_control.credit_provider_receipts (
                    provider_request_id,application_id,subject_id_hash,
                    provider_name,provider_mode,synthetic,status
                ) VALUES (%s,%s,%s,%s,%s,false,'PENDING')
                ON CONFLICT (application_id,subject_id_hash) DO NOTHING
                """,
                (
                    request.provider_request_id,
                    request.application_id,
                    subject_hash,
                    self.provider.provider_name,
                    self.provider.provider_mode,
                ),
            )
            cursor.execute(
                """
                SELECT * FROM mvp_control.credit_provider_receipts
                WHERE application_id=%s AND subject_id_hash=%s FOR UPDATE
                """,
                (request.application_id, subject_hash),
            )
            row = cursor.fetchone()
            if not row or row["provider_request_id"] != request.provider_request_id:
                raise CreditProviderResponseInvalid("征信供应商幂等收据冲突")
            if (
                row["provider_name"] != self.provider.provider_name
                or row["provider_mode"] != self.provider.provider_mode
            ):
                raise CreditProviderResponseInvalid("既有收据与当前供应商配置不一致")
            return dict(row)

    def _save_success(
        self, request: CreditProviderRequest, response: CreditProviderResponse
    ) -> None:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE mvp_control.credit_provider_receipts
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
        self, request: CreditProviderRequest, code: str, message: str
    ) -> None:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE mvp_control.credit_provider_receipts
                SET status='FAILED',attempt_count=attempt_count+1,
                    last_error_code=%s,last_error_message=%s,updated_at=now()
                WHERE provider_request_id=%s
                """,
                (code[:64], message[:500], request.provider_request_id),
            )

    def query(
        self,
        request: CreditProviderRequest,
        *,
        timeout_seconds: int = 30,
        max_attempts: int = 2,
    ) -> tuple[CreditProviderResponse, bool]:
        if self.provider.provider_mode == "disabled":
            raise CreditProviderNotConfigured("真实征信供应商未配置，已拒绝发起查询")
        if not self.provider.supports_idempotent_request_ids:
            raise CreditProviderNotConfigured(
                "征信供应商不保证请求编号幂等，已拒绝可能重复计费的查询"
            )
        receipt = self._reserve(request)
        if receipt["status"] == "SUCCEEDED" and receipt.get("response_payload"):
            cached = self._from_cache(receipt["response_payload"], request)
            return validate_provider_response(
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
                response = validate_provider_response(
                    response,
                    request,
                    expected_provider_name=self.provider.provider_name,
                    expected_provider_mode=self.provider.provider_mode,
                )
                self._save_success(request, response)
                if self.circuit_breaker:
                    self.circuit_breaker.record_success()
                return response, False
            except CreditProviderTimeout as exc:
                self._save_failure(request, "ACTION_EXTERNAL_TIMEOUT", str(exc))
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure(
                        "ACTION_EXTERNAL_TIMEOUT", str(exc)
                    )
                if attempt == attempts:
                    raise
            except CreditProviderResponseInvalid as exc:
                self._save_failure(
                    request, "ACTION_EXTERNAL_RESPONSE_INVALID", str(exc)
                )
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure(
                        "ACTION_EXTERNAL_RESPONSE_INVALID", str(exc)
                    )
                raise
        raise CreditProviderTimeout("征信供应商查询超时")
