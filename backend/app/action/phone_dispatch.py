from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from .operations import ExternalCircuitOpenError

try:
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:  # Unit tests do not require the PostgreSQL driver.
    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, value):
            self.obj = value


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")


class PhoneDispatchError(RuntimeError):
    pass


class PhoneDispatchNotConfigured(PhoneDispatchError):
    pass


class PhoneDispatchTimeout(PhoneDispatchError):
    pass


class PhoneDispatchResponseInvalid(PhoneDispatchError):
    pass


@dataclass(frozen=True)
class PhoneTaskDispatchRequest:
    dispatch_id: str
    verification_id: str
    application_id: str
    verification_round: int
    verification_target: str
    trigger_reason: str
    questions: tuple[Mapping[str, Any], ...]

    def context_hash(self) -> str:
        payload = {
            "dispatch_id": self.dispatch_id,
            "verification_id": self.verification_id,
            "application_id": self.application_id,
            "verification_round": self.verification_round,
            "verification_target": self.verification_target,
            "trigger_reason": self.trigger_reason,
            "questions": [dict(item) for item in self.questions],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PhoneTaskDispatchResponse:
    dispatch_id: str
    verification_id: str
    external_task_id: str
    accepted_at: datetime
    provider_name: str
    provider_mode: str
    synthetic: bool = False

    def cache_payload(self) -> dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "verification_id": self.verification_id,
            "external_task_id": self.external_task_id,
            "accepted_at": self.accepted_at.isoformat(),
            "provider_name": self.provider_name,
            "provider_mode": self.provider_mode,
            "synthetic": self.synthetic,
        }


class PhoneTaskDispatcher(Protocol):
    provider_name: str
    provider_mode: str
    supports_idempotent_dispatch_ids: bool

    def dispatch(
        self, request: PhoneTaskDispatchRequest, *, timeout_seconds: int
    ) -> PhoneTaskDispatchResponse: ...


class DisabledPhoneTaskDispatcher:
    provider_name = "unconfigured"
    provider_mode = "disabled"
    supports_idempotent_dispatch_ids = True

    def dispatch(
        self, request: PhoneTaskDispatchRequest, *, timeout_seconds: int
    ) -> PhoneTaskDispatchResponse:
        del request, timeout_seconds
        raise PhoneDispatchNotConfigured("真实电核任务系统未配置，任务保持待投递")


class SandboxPhoneTaskDispatcher:
    """Explicit deterministic adapter for local integration testing only."""

    provider_name = "local-phone-sandbox"
    provider_mode = "sandbox"
    supports_idempotent_dispatch_ids = True

    def dispatch(
        self, request: PhoneTaskDispatchRequest, *, timeout_seconds: int
    ) -> PhoneTaskDispatchResponse:
        if timeout_seconds <= 0:
            raise PhoneDispatchTimeout("电核沙箱投递超时")
        digest = hashlib.sha256(request.dispatch_id.encode("utf-8")).hexdigest()
        return PhoneTaskDispatchResponse(
            dispatch_id=request.dispatch_id,
            verification_id=request.verification_id,
            external_task_id=f"SBXPV{digest[:24].upper()}",
            accepted_at=datetime.now(timezone.utc),
            provider_name=self.provider_name,
            provider_mode=self.provider_mode,
            synthetic=True,
        )


def deterministic_phone_dispatch_id(
    application_id: str, verification_round: int
) -> str:
    digest = hashlib.sha256(
        f"{application_id}|{verification_round}".encode("utf-8")
    ).hexdigest()[:28].upper()
    return f"PVD{digest}"


def validate_phone_dispatch_response(
    response: PhoneTaskDispatchResponse,
    request: PhoneTaskDispatchRequest,
    *,
    expected_provider_name: str,
    expected_provider_mode: str,
) -> PhoneTaskDispatchResponse:
    if not isinstance(response, PhoneTaskDispatchResponse):
        raise PhoneDispatchResponseInvalid("电核系统响应类型无效")
    if response.dispatch_id != request.dispatch_id:
        raise PhoneDispatchResponseInvalid("电核系统响应的投递编号不匹配")
    if response.verification_id != request.verification_id:
        raise PhoneDispatchResponseInvalid("电核系统响应的任务编号不匹配")
    if response.provider_name != expected_provider_name:
        raise PhoneDispatchResponseInvalid("电核系统响应来源不匹配")
    if response.provider_mode != expected_provider_mode:
        raise PhoneDispatchResponseInvalid("电核系统响应模式不匹配")
    if not IDENTIFIER_PATTERN.fullmatch(response.external_task_id):
        raise PhoneDispatchResponseInvalid("电核系统任务编号格式无效")
    if response.accepted_at.tzinfo is None or response.accepted_at.utcoffset() is None:
        raise PhoneDispatchResponseInvalid("电核系统接收时间必须包含时区")
    now = datetime.now(timezone.utc).astimezone(response.accepted_at.tzinfo)
    if response.accepted_at > now:
        raise PhoneDispatchResponseInvalid("电核系统接收时间不能晚于当前时间")
    if response.provider_mode == "sandbox" and not response.synthetic:
        raise PhoneDispatchResponseInvalid("沙箱响应必须显式标记为合成数据")
    if response.provider_mode != "sandbox" and response.synthetic:
        raise PhoneDispatchResponseInvalid("生产响应不能标记为合成数据")
    return response


PHONE_DISPATCH_RECEIPT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS mvp_control.phone_dispatch_receipts (
        dispatch_id varchar(64) PRIMARY KEY,
        verification_id varchar(32) NOT NULL UNIQUE,
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
    CREATE INDEX IF NOT EXISTS phone_dispatch_receipt_status_idx
    ON mvp_control.phone_dispatch_receipts (status,updated_at)
    """,
)


class PostgresPhoneDispatchCoordinator:
    """Cache an accepted dispatch before the outbox event is acknowledged."""

    _schema_lock = threading.Lock()
    _schema_ready = False

    def __init__(
        self, postgres_service, dispatcher: PhoneTaskDispatcher, circuit_breaker=None
    ):
        self.postgres = postgres_service
        self.dispatcher = dispatcher
        self.circuit_breaker = circuit_breaker

    def ensure_schema(self) -> None:
        if type(self)._schema_ready:
            return
        with type(self)._schema_lock:
            if type(self)._schema_ready:
                return
            with self.postgres.conn() as connection, connection.cursor() as cursor:
                cursor.execute("CREATE SCHEMA IF NOT EXISTS mvp_control")
                for statement in PHONE_DISPATCH_RECEIPT_SCHEMA:
                    cursor.execute(statement)
            type(self)._schema_ready = True

    def _reserve(self, request: PhoneTaskDispatchRequest) -> Mapping[str, Any]:
        self.ensure_schema()
        context_hash = request.context_hash()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"phone_dispatch:{request.verification_id}",),
            )
            cursor.execute(
                """
                INSERT INTO mvp_control.phone_dispatch_receipts (
                    dispatch_id,verification_id,request_context_hash,
                    provider_name,provider_mode,status
                ) VALUES (%s,%s,%s,%s,%s,'PENDING')
                ON CONFLICT (verification_id) DO NOTHING
                """,
                (
                    request.dispatch_id,
                    request.verification_id,
                    context_hash,
                    self.dispatcher.provider_name,
                    self.dispatcher.provider_mode,
                ),
            )
            cursor.execute(
                """
                SELECT * FROM mvp_control.phone_dispatch_receipts
                WHERE verification_id=%s FOR UPDATE
                """,
                (request.verification_id,),
            )
            row = cursor.fetchone()
            if not row or row["dispatch_id"] != request.dispatch_id:
                raise PhoneDispatchResponseInvalid("电核投递幂等收据冲突")
            if row["request_context_hash"] != context_hash:
                raise PhoneDispatchResponseInvalid("同一电核任务的投递内容发生变化")
            if (
                row["provider_name"] != self.dispatcher.provider_name
                or row["provider_mode"] != self.dispatcher.provider_mode
            ):
                raise PhoneDispatchResponseInvalid("既有收据与当前电核系统配置不一致")
            return dict(row)

    def _from_cache(
        self, payload: Mapping[str, Any], request: PhoneTaskDispatchRequest
    ) -> PhoneTaskDispatchResponse:
        try:
            response = PhoneTaskDispatchResponse(
                dispatch_id=str(payload["dispatch_id"]),
                verification_id=str(payload["verification_id"]),
                external_task_id=str(payload["external_task_id"]),
                accepted_at=datetime.fromisoformat(
                    str(payload["accepted_at"]).replace("Z", "+00:00")
                ),
                provider_name=str(payload["provider_name"]),
                provider_mode=str(payload["provider_mode"]),
                synthetic=bool(payload["synthetic"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PhoneDispatchResponseInvalid("已缓存的电核投递响应无效") from exc
        return validate_phone_dispatch_response(
            response,
            request,
            expected_provider_name=self.dispatcher.provider_name,
            expected_provider_mode=self.dispatcher.provider_mode,
        )

    def _save_success(
        self, request: PhoneTaskDispatchRequest, response: PhoneTaskDispatchResponse
    ) -> None:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE mvp_control.phone_dispatch_receipts
                SET status='SUCCEEDED',attempt_count=attempt_count+1,
                    response_payload=%s,synthetic=%s,last_error_code=NULL,
                    last_error_message=NULL,updated_at=now()
                WHERE dispatch_id=%s
                """,
                (Jsonb(response.cache_payload()), response.synthetic, request.dispatch_id),
            )

    def _save_failure(
        self, request: PhoneTaskDispatchRequest, code: str, message: str
    ) -> None:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE mvp_control.phone_dispatch_receipts
                SET status='FAILED',attempt_count=attempt_count+1,
                    last_error_code=%s,last_error_message=%s,updated_at=now()
                WHERE dispatch_id=%s
                """,
                (code[:64], message[:500], request.dispatch_id),
            )

    def dispatch(
        self,
        request: PhoneTaskDispatchRequest,
        *,
        timeout_seconds: int = 10,
        max_attempts: int = 1,
    ) -> tuple[PhoneTaskDispatchResponse, bool]:
        if self.dispatcher.provider_mode == "disabled":
            raise PhoneDispatchNotConfigured("真实电核任务系统未配置，任务保持待投递")
        if not self.dispatcher.supports_idempotent_dispatch_ids:
            raise PhoneDispatchNotConfigured(
                "电核系统不保证投递编号幂等，已拒绝可能重复创建任务的调用"
            )
        receipt = self._reserve(request)
        if receipt["status"] == "SUCCEEDED" and receipt.get("response_payload"):
            return self._from_cache(receipt["response_payload"], request), True
        attempts = max(1, int(max_attempts))
        last_error: Exception | None = None
        for _attempt in range(attempts):
            try:
                if self.circuit_breaker:
                    self.circuit_breaker.before_call()
                response = self.dispatcher.dispatch(
                    request, timeout_seconds=timeout_seconds
                )
                response = validate_phone_dispatch_response(
                    response,
                    request,
                    expected_provider_name=self.dispatcher.provider_name,
                    expected_provider_mode=self.dispatcher.provider_mode,
                )
                self._save_success(request, response)
                if self.circuit_breaker:
                    self.circuit_breaker.record_success()
                return response, False
            except PhoneDispatchResponseInvalid as exc:
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure(
                        "ACTION_EXTERNAL_RESPONSE_INVALID", str(exc)
                    )
                raise
            except PhoneDispatchTimeout as exc:
                last_error = exc
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure(
                        "ACTION_EXTERNAL_TIMEOUT", str(exc)
                    )
            except PhoneDispatchError as exc:
                last_error = exc
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure(
                        "ACTION_PHONE_DISPATCH_FAILED", str(exc)
                    )
            except TimeoutError as exc:
                last_error = PhoneDispatchTimeout("电核任务投递超时")
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure(
                        "ACTION_EXTERNAL_TIMEOUT", str(last_error)
                    )
            except ExternalCircuitOpenError:
                raise
            except Exception as exc:
                last_error = PhoneDispatchError("电核任务系统调用失败")
                last_error.__cause__ = exc
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure(
                        "ACTION_PHONE_DISPATCH_FAILED", str(last_error)
                    )
        assert last_error is not None
        code = (
            "ACTION_EXTERNAL_TIMEOUT"
            if isinstance(last_error, PhoneDispatchTimeout)
            else "ACTION_PHONE_DISPATCH_FAILED"
        )
        self._save_failure(request, code, str(last_error))
        raise last_error
