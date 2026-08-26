from __future__ import annotations

import hashlib
import ipaddress
import threading
from datetime import date, datetime
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse
from uuid import uuid4

import httpx

try:
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:  # Unit tests use in-memory repositories.
    class Jsonb:  # type: ignore[no-redef]
        def __init__(self, value):
            self.obj = value

from ..core.security import mask_payload


ALERT_SEVERITIES = ("INFO", "WARNING", "CRITICAL")
ALERT_STATUSES = ("OPEN", "ACKNOWLEDGED", "RESOLVED")
CIRCUIT_STATES = ("CLOSED", "OPEN", "HALF_OPEN")
_SEVERITY_SQL = ",".join(f"'{item}'" for item in ALERT_SEVERITIES)
_ALERT_STATUS_SQL = ",".join(f"'{item}'" for item in ALERT_STATUSES)
_CIRCUIT_STATE_SQL = ",".join(f"'{item}'" for item in CIRCUIT_STATES)


OPERATIONS_SCHEMA = (
    "CREATE SCHEMA IF NOT EXISTS mvp_control",
    f"""
    CREATE TABLE IF NOT EXISTS mvp_control.operational_alerts (
        alert_id varchar(36) PRIMARY KEY,
        alert_key varchar(200) NOT NULL UNIQUE,
        source_type varchar(48) NOT NULL,
        source_id varchar(160) NOT NULL,
        category varchar(64) NOT NULL,
        severity varchar(16) NOT NULL CHECK (severity IN ({_SEVERITY_SQL})),
        status varchar(20) NOT NULL CHECK (status IN ({_ALERT_STATUS_SQL})),
        title varchar(200) NOT NULL,
        message varchar(500) NOT NULL,
        occurrence_count integer NOT NULL DEFAULT 1 CHECK (occurrence_count > 0),
        escalation_level integer NOT NULL DEFAULT 0 CHECK (escalation_level BETWEEN 0 AND 3),
        metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        first_occurred_at timestamptz NOT NULL DEFAULT now(),
        last_occurred_at timestamptz NOT NULL DEFAULT now(),
        next_escalation_at timestamptz,
        last_notified_at timestamptz,
        acknowledged_at timestamptz,
        acknowledged_by varchar(128),
        resolved_at timestamptz,
        resolved_by varchar(128),
        resolution_note varchar(500),
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS operational_alerts_status_escalation_idx
    ON mvp_control.operational_alerts (status,next_escalation_at,last_occurred_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS mvp_control.operational_alert_events (
        event_id bigserial PRIMARY KEY,
        alert_id varchar(36) NOT NULL
            REFERENCES mvp_control.operational_alerts(alert_id) ON DELETE RESTRICT,
        event_type varchar(32) NOT NULL,
        actor_id varchar(128) NOT NULL,
        details jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS operational_alert_events_alert_idx
    ON mvp_control.operational_alert_events (alert_id,created_at,event_id)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS mvp_control.external_circuit_breakers (
        dependency_name varchar(80) PRIMARY KEY,
        state varchar(16) NOT NULL CHECK (state IN ({_CIRCUIT_STATE_SQL})),
        failure_count integer NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
        failure_threshold integer NOT NULL CHECK (failure_threshold > 0),
        open_seconds integer NOT NULL CHECK (open_seconds > 0),
        opened_at timestamptz,
        next_probe_at timestamptz,
        last_failure_at timestamptz,
        last_success_at timestamptz,
        last_error_code varchar(64),
        last_error_message varchar(500),
        version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
)


class OperationsError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ExternalCircuitOpenError(OperationsError):
    def __init__(self, dependency_name: str, retry_at: datetime | None = None):
        suffix = f"，可在 {retry_at.isoformat()} 后探测" if retry_at else ""
        super().__init__(
            "ACTION_EXTERNAL_CIRCUIT_OPEN",
            f"外部依赖 {dependency_name} 的熔断器已打开{suffix}",
        )
        self.dependency_name = dependency_name
        self.retry_at = retry_at


def _json_safe(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _safe_details(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    return dict(_json_safe(mask_payload(dict(value))))


class PostgresOperationalAlertRepository:
    _schema_lock = threading.Lock()
    _schema_ready = False

    def __init__(self, postgres_service):
        self.postgres = postgres_service

    def ensure_schema(self) -> None:
        if type(self)._schema_ready:
            return
        with type(self)._schema_lock:
            if type(self)._schema_ready:
                return
            with self.postgres.conn() as connection, connection.cursor() as cursor:
                for statement in OPERATIONS_SCHEMA:
                    cursor.execute(statement)
            type(self)._schema_ready = True

    @staticmethod
    def _event(
        cursor,
        alert_id: str,
        event_type: str,
        actor_id: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO mvp_control.operational_alert_events (
                alert_id,event_type,actor_id,details
            ) VALUES (%s,%s,%s,%s)
            """,
            (alert_id, event_type, actor_id[:128], Jsonb(_safe_details(details))),
        )

    def raise_alert(
        self,
        *,
        alert_key: str,
        source_type: str,
        source_id: str,
        category: str,
        severity: str,
        title: str,
        message: str,
        metadata: Mapping[str, Any] | None,
        actor_id: str,
        escalation_seconds: int,
        notification_cooldown_seconds: int,
    ) -> dict[str, Any]:
        self.ensure_schema()
        severity = severity.upper()
        if severity not in ALERT_SEVERITIES:
            raise OperationsError("ALERT_SEVERITY_INVALID", "未知告警等级")
        safe_message = str(mask_payload(message))[:500]
        safe_metadata = _safe_details(metadata)
        alert_id = str(uuid4())
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM mvp_control.operational_alerts
                WHERE alert_key=%s FOR UPDATE
                """,
                (alert_key[:200],),
            )
            current = cursor.fetchone()
            created = current is None
            if created:
                cursor.execute(
                    """
                    INSERT INTO mvp_control.operational_alerts (
                        alert_id,alert_key,source_type,source_id,category,severity,
                        status,title,message,metadata,next_escalation_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,'OPEN',%s,%s,%s,
                              now()+(%s*interval '1 second'))
                    RETURNING *
                    """,
                    (
                        alert_id,
                        alert_key[:200],
                        source_type[:48],
                        source_id[:160],
                        category[:64],
                        severity,
                        title[:200],
                        safe_message,
                        Jsonb(safe_metadata),
                        max(5, int(escalation_seconds)),
                    ),
                )
                row = cursor.fetchone()
                self._event(
                    cursor,
                    alert_id,
                    "CREATED",
                    actor_id,
                    {"severity": severity, "category": category},
                )
            else:
                rank = {"INFO": 1, "WARNING": 2, "CRITICAL": 3}
                effective = (
                    severity
                    if rank[severity] >= rank[current["severity"]]
                    else current["severity"]
                )
                cursor.execute(
                    """
                    UPDATE mvp_control.operational_alerts
                    SET source_type=%s,source_id=%s,category=%s,severity=%s,
                        status=CASE WHEN status='RESOLVED' THEN 'OPEN' ELSE status END,
                        title=%s,message=%s,metadata=%s,
                        occurrence_count=occurrence_count+1,
                        last_occurred_at=now(),updated_at=now(),
                        resolved_at=NULL,resolved_by=NULL,resolution_note=NULL,
                        next_escalation_at=CASE WHEN status='RESOLVED' OR next_escalation_at IS NULL
                            THEN now()+(%s*interval '1 second') ELSE next_escalation_at END
                    WHERE alert_id=%s RETURNING *
                    """,
                    (
                        source_type[:48],
                        source_id[:160],
                        category[:64],
                        effective,
                        title[:200],
                        safe_message,
                        Jsonb(safe_metadata),
                        max(5, int(escalation_seconds)),
                        current["alert_id"],
                    ),
                )
                row = cursor.fetchone()
                self._event(
                    cursor,
                    row["alert_id"],
                    "REPEATED" if current["status"] != "RESOLVED" else "REOPENED",
                    actor_id,
                    {"severity": effective, "occurrence_count": row["occurrence_count"]},
                )
            cooldown = max(0, int(notification_cooldown_seconds))
            should_notify = (
                row.get("last_notified_at") is None
                or created
                or current is not None
                and current["status"] == "RESOLVED"
            )
            if not should_notify and cooldown:
                cursor.execute(
                    "SELECT last_notified_at <= now()-(%s*interval '1 second') AS ready FROM mvp_control.operational_alerts WHERE alert_id=%s",
                    (cooldown, row["alert_id"]),
                )
                should_notify = bool(cursor.fetchone()["ready"])
            result = dict(row)
            result["should_notify"] = should_notify
            result["created"] = created
            return result

    def record_notification(
        self,
        alert_id: str,
        *,
        success: bool,
        channel: str,
        actor_id: str,
        error_message: str | None = None,
    ) -> None:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            if success:
                cursor.execute(
                    "UPDATE mvp_control.operational_alerts SET last_notified_at=now(),updated_at=now() WHERE alert_id=%s",
                    (alert_id,),
                )
            self._event(
                cursor,
                alert_id,
                "NOTIFIED" if success else "NOTIFICATION_FAILED",
                actor_id,
                {"channel": channel, "error": str(error_message or "")[:200]},
            )

    def list_alerts(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        limit = max(1, min(int(limit), 500))
        if status is not None and status not in ALERT_STATUSES:
            raise OperationsError("ALERT_STATUS_INVALID", "未知告警状态")
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            if status:
                cursor.execute(
                    """
                    SELECT * FROM mvp_control.operational_alerts
                    WHERE status=%s ORDER BY last_occurred_at DESC LIMIT %s
                    """,
                    (status, limit),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM mvp_control.operational_alerts
                    ORDER BY CASE status WHEN 'OPEN' THEN 1 WHEN 'ACKNOWLEDGED' THEN 2 ELSE 3 END,
                             last_occurred_at DESC LIMIT %s
                    """,
                    (limit,),
                )
            return [dict(row) for row in cursor.fetchall()]

    def summary(self) -> dict[str, Any]:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,severity,count(*) AS count FROM mvp_control.operational_alerts GROUP BY status,severity"
            )
            rows = cursor.fetchall()
        by_status = {item: 0 for item in ALERT_STATUSES}
        by_severity = {item: 0 for item in ALERT_SEVERITIES}
        for row in rows:
            by_status[row["status"]] += row["count"]
            if row["status"] != "RESOLVED":
                by_severity[row["severity"]] += row["count"]
        active = by_status["OPEN"] + by_status["ACKNOWLEDGED"]
        return {
            "healthy": active == 0,
            "active": active,
            "by_status": by_status,
            "active_by_severity": by_severity,
        }

    def transition(
        self,
        alert_id: str,
        *,
        target_status: str,
        actor_id: str,
        note: str,
    ) -> dict[str, Any]:
        self.ensure_schema()
        note = " ".join(str(note or "").strip().split())
        if len(note) < 5 or len(note) > 500:
            raise OperationsError("ALERT_NOTE_INVALID", "处理说明需要 5–500 个字符")
        if target_status not in {"ACKNOWLEDGED", "RESOLVED"}:
            raise OperationsError("ALERT_TRANSITION_INVALID", "不支持的告警操作")
        safe_note = str(mask_payload(note))[:500]
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM mvp_control.operational_alerts WHERE alert_id=%s FOR UPDATE",
                (alert_id,),
            )
            current = cursor.fetchone()
            if not current:
                raise OperationsError("ALERT_NOT_FOUND", "告警不存在")
            if current["status"] == "RESOLVED":
                return dict(current)
            if target_status == "ACKNOWLEDGED":
                cursor.execute(
                    """
                    UPDATE mvp_control.operational_alerts
                    SET status='ACKNOWLEDGED',acknowledged_at=COALESCE(acknowledged_at,now()),
                        acknowledged_by=COALESCE(acknowledged_by,%s),updated_at=now()
                    WHERE alert_id=%s RETURNING *
                    """,
                    (actor_id[:128], alert_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE mvp_control.operational_alerts
                    SET status='RESOLVED',resolved_at=now(),resolved_by=%s,
                        resolution_note=%s,next_escalation_at=NULL,updated_at=now()
                    WHERE alert_id=%s RETURNING *
                    """,
                    (actor_id[:128], safe_note, alert_id),
                )
            row = cursor.fetchone()
            self._event(
                cursor,
                alert_id,
                target_status,
                actor_id,
                {
                    "note_digest": hashlib.sha256(
                        safe_note.encode("utf-8")
                    ).hexdigest()
                },
            )
            return dict(row)

    def resolve_by_key(self, alert_key: str, *, actor_id: str, note: str) -> None:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT alert_id,status FROM mvp_control.operational_alerts WHERE alert_key=%s",
                (alert_key,),
            )
            row = cursor.fetchone()
        if row and row["status"] != "RESOLVED":
            self.transition(
                row["alert_id"], target_status="RESOLVED", actor_id=actor_id, note=note
            )

    def due_for_escalation(
        self, *, escalation_seconds: int, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM mvp_control.operational_alerts
                WHERE status IN ('OPEN','ACKNOWLEDGED')
                  AND escalation_level < 3
                  AND next_escalation_at <= now()
                ORDER BY next_escalation_at FOR UPDATE SKIP LOCKED LIMIT %s
                """,
                (max(1, min(int(limit), 500)),),
            )
            due = cursor.fetchall()
            results = []
            for current in due:
                new_level = current["escalation_level"] + 1
                severity = "CRITICAL" if new_level >= 2 else current["severity"]
                cursor.execute(
                    """
                    UPDATE mvp_control.operational_alerts
                    SET escalation_level=%s,severity=%s,
                        next_escalation_at=now()+(%s*interval '1 second'),updated_at=now()
                    WHERE alert_id=%s RETURNING *
                    """,
                    (
                        new_level,
                        severity,
                        max(5, int(escalation_seconds)),
                        current["alert_id"],
                    ),
                )
                row = cursor.fetchone()
                self._event(
                    cursor,
                    row["alert_id"],
                    "ESCALATED",
                    "operations-monitor",
                    {"level": new_level, "severity": severity},
                )
                results.append(dict(row))
            return results


class AlertNotifier(Protocol):
    channel_name: str

    def notify(self, alert: Mapping[str, Any]) -> None: ...


class DisabledAlertNotifier:
    channel_name = "disabled"

    def notify(self, alert: Mapping[str, Any]) -> None:
        del alert


class WebhookAlertNotifier:
    channel_name = "webhook"

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: int = 5,
        allow_insecure_local: bool = False,
    ):
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise OperationsError("ALERT_WEBHOOK_INVALID", "告警 Webhook 地址无效")
        if parsed.username or parsed.password or parsed.fragment:
            raise OperationsError("ALERT_WEBHOOK_INVALID", "Webhook 地址不得包含凭据或片段")
        if parsed.scheme != "https":
            try:
                is_local = parsed.hostname == "localhost" or ipaddress.ip_address(
                    parsed.hostname
                ).is_loopback
            except ValueError:
                is_local = False
            if not (allow_insecure_local and is_local):
                raise OperationsError(
                    "ALERT_WEBHOOK_INSECURE", "Webhook 默认必须使用 HTTPS"
                )
        self.url = parsed.geturl()
        self.timeout_seconds = max(1, int(timeout_seconds))

    def notify(self, alert: Mapping[str, Any]) -> None:
        payload = {
            "event": "mvp_operational_alert",
            "alert_id": alert["alert_id"],
            "category": alert["category"],
            "severity": alert["severity"],
            "title": alert["title"],
            "occurrence_count": alert["occurrence_count"],
            "escalation_level": alert["escalation_level"],
        }
        response = httpx.post(self.url, json=payload, timeout=self.timeout_seconds)
        response.raise_for_status()


class OperationalAlertService:
    def __init__(
        self,
        repository: PostgresOperationalAlertRepository,
        notifier: AlertNotifier | None = None,
        *,
        escalation_seconds: int = 900,
        notification_cooldown_seconds: int = 300,
    ):
        self.repository = repository
        self.repository.ensure_schema()
        self.notifier = notifier or DisabledAlertNotifier()
        self.escalation_seconds = max(5, int(escalation_seconds))
        self.notification_cooldown_seconds = max(
            0, int(notification_cooldown_seconds)
        )

    def raise_alert(self, **values) -> dict[str, Any]:
        alert = self.repository.raise_alert(
            **values,
            escalation_seconds=self.escalation_seconds,
            notification_cooldown_seconds=self.notification_cooldown_seconds,
        )
        if alert["should_notify"] and self.notifier.channel_name != "disabled":
            try:
                self.notifier.notify(alert)
                self.repository.record_notification(
                    alert["alert_id"],
                    success=True,
                    channel=self.notifier.channel_name,
                    actor_id="operations-notifier",
                )
            except Exception as exc:
                self.repository.record_notification(
                    alert["alert_id"],
                    success=False,
                    channel=self.notifier.channel_name,
                    actor_id="operations-notifier",
                    error_message=str(exc) or type(exc).__name__,
                )
        return alert

    def escalate_due(self) -> list[dict[str, Any]]:
        alerts = self.repository.due_for_escalation(
            escalation_seconds=self.escalation_seconds
        )
        if self.notifier.channel_name == "disabled":
            return alerts
        for alert in alerts:
            try:
                self.notifier.notify(alert)
                self.repository.record_notification(
                    alert["alert_id"],
                    success=True,
                    channel=self.notifier.channel_name,
                    actor_id="operations-notifier",
                )
            except Exception as exc:
                self.repository.record_notification(
                    alert["alert_id"],
                    success=False,
                    channel=self.notifier.channel_name,
                    actor_id="operations-notifier",
                    error_message=str(exc) or type(exc).__name__,
                )
        return alerts


class PostgresCircuitBreakerRepository:
    def __init__(self, postgres_service):
        self.postgres = postgres_service
        PostgresOperationalAlertRepository(postgres_service).ensure_schema()

    def before_call(
        self,
        dependency_name: str,
        *,
        failure_threshold: int,
        open_seconds: int,
    ) -> dict[str, Any]:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO mvp_control.external_circuit_breakers (
                    dependency_name,state,failure_threshold,open_seconds
                ) VALUES (%s,'CLOSED',%s,%s)
                ON CONFLICT (dependency_name) DO NOTHING
                """,
                (dependency_name, failure_threshold, open_seconds),
            )
            cursor.execute(
                "SELECT * FROM mvp_control.external_circuit_breakers WHERE dependency_name=%s FOR UPDATE",
                (dependency_name,),
            )
            row = cursor.fetchone()
            if row["state"] == "OPEN":
                if row["next_probe_at"] and row["next_probe_at"] <= datetime.now(
                    row["next_probe_at"].tzinfo
                ):
                    cursor.execute(
                        """
                        UPDATE mvp_control.external_circuit_breakers
                        SET state='HALF_OPEN',version=version+1,updated_at=now()
                        WHERE dependency_name=%s RETURNING *
                        """,
                        (dependency_name,),
                    )
                    return {"allowed": True, "record": dict(cursor.fetchone())}
                return {"allowed": False, "record": dict(row)}
            if row["state"] == "HALF_OPEN":
                return {"allowed": False, "record": dict(row)}
            return {"allowed": True, "record": dict(row)}

    def record_success(self, dependency_name: str) -> dict[str, Any]:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM mvp_control.external_circuit_breakers WHERE dependency_name=%s FOR UPDATE",
                (dependency_name,),
            )
            current = cursor.fetchone()
            if not current:
                raise OperationsError("CIRCUIT_NOT_FOUND", "熔断器不存在")
            was_open = current["state"] != "CLOSED" or current["failure_count"] > 0
            cursor.execute(
                """
                UPDATE mvp_control.external_circuit_breakers
                SET state='CLOSED',failure_count=0,opened_at=NULL,next_probe_at=NULL,
                    last_success_at=now(),last_error_code=NULL,last_error_message=NULL,
                    version=version+1,updated_at=now()
                WHERE dependency_name=%s RETURNING *
                """,
                (dependency_name,),
            )
            return {"closed": was_open, "record": dict(cursor.fetchone())}

    def record_failure(
        self,
        dependency_name: str,
        *,
        failure_threshold: int,
        open_seconds: int,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        safe_message = str(mask_payload(error_message))[:500]
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM mvp_control.external_circuit_breakers WHERE dependency_name=%s FOR UPDATE",
                (dependency_name,),
            )
            current = cursor.fetchone()
            if not current:
                cursor.execute(
                    """
                    INSERT INTO mvp_control.external_circuit_breakers (
                        dependency_name,state,failure_count,failure_threshold,open_seconds
                    ) VALUES (%s,'CLOSED',0,%s,%s) RETURNING *
                    """,
                    (dependency_name, failure_threshold, open_seconds),
                )
                current = cursor.fetchone()
            new_count = current["failure_count"] + 1
            opened = current["state"] == "HALF_OPEN" or new_count >= failure_threshold
            cursor.execute(
                """
                UPDATE mvp_control.external_circuit_breakers
                SET state=CASE WHEN %s THEN 'OPEN' ELSE 'CLOSED' END,
                    failure_count=%s,failure_threshold=%s,open_seconds=%s,
                    opened_at=CASE WHEN %s THEN now() ELSE opened_at END,
                    next_probe_at=CASE WHEN %s THEN now()+(%s*interval '1 second') ELSE next_probe_at END,
                    last_failure_at=now(),last_error_code=%s,last_error_message=%s,
                    version=version+1,updated_at=now()
                WHERE dependency_name=%s RETURNING *
                """,
                (
                    opened,
                    new_count,
                    failure_threshold,
                    open_seconds,
                    opened,
                    opened,
                    open_seconds,
                    error_code[:64],
                    safe_message,
                    dependency_name,
                ),
            )
            return {
                "opened": opened and current["state"] != "OPEN",
                "record": dict(cursor.fetchone()),
            }

    def list_circuits(self) -> list[dict[str, Any]]:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM mvp_control.external_circuit_breakers ORDER BY dependency_name"
            )
            return [dict(row) for row in cursor.fetchall()]

    def reset(self, dependency_name: str) -> dict[str, Any]:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE mvp_control.external_circuit_breakers
                SET state='CLOSED',failure_count=0,opened_at=NULL,next_probe_at=NULL,
                    last_error_code=NULL,last_error_message=NULL,version=version+1,updated_at=now()
                WHERE dependency_name=%s RETURNING *
                """,
                (dependency_name,),
            )
            row = cursor.fetchone()
        if not row:
            raise OperationsError("CIRCUIT_NOT_FOUND", "熔断器不存在")
        return dict(row)


class ExternalCircuitBreaker:
    def __init__(
        self,
        dependency_name: str,
        repository: PostgresCircuitBreakerRepository,
        alerts: OperationalAlertService,
        *,
        failure_threshold: int = 3,
        open_seconds: int = 60,
    ):
        self.dependency_name = dependency_name
        self.repository = repository
        self.alerts = alerts
        self.failure_threshold = max(1, int(failure_threshold))
        self.open_seconds = max(5, int(open_seconds))

    @property
    def alert_key(self) -> str:
        return f"circuit:{self.dependency_name}"

    def before_call(self) -> None:
        result = self.repository.before_call(
            self.dependency_name,
            failure_threshold=self.failure_threshold,
            open_seconds=self.open_seconds,
        )
        if not result["allowed"]:
            raise ExternalCircuitOpenError(
                self.dependency_name, result["record"].get("next_probe_at")
            )

    def record_success(self) -> None:
        result = self.repository.record_success(self.dependency_name)
        if result["closed"]:
            self.alerts.repository.resolve_by_key(
                self.alert_key,
                actor_id="circuit-breaker",
                note="外部依赖探测成功，熔断器已自动关闭",
            )

    def record_failure(self, error_code: str, error_message: str) -> None:
        result = self.repository.record_failure(
            self.dependency_name,
            failure_threshold=self.failure_threshold,
            open_seconds=self.open_seconds,
            error_code=error_code,
            error_message=error_message,
        )
        if result["opened"]:
            self.alerts.raise_alert(
                alert_key=self.alert_key,
                source_type="EXTERNAL_DEPENDENCY",
                source_id=self.dependency_name,
                category="CIRCUIT_OPEN",
                severity="CRITICAL",
                title=f"外部依赖 {self.dependency_name} 已熔断",
                message="连续外部调用失败达到阈值，已暂停调用并等待半开探测",
                metadata={
                    "failure_count": result["record"]["failure_count"],
                    "next_probe_at": result["record"].get("next_probe_at"),
                    "error_code": error_code,
                },
                actor_id="circuit-breaker",
            )


class OperationsMonitor:
    def __init__(
        self,
        postgres_service,
        alert_service: OperationalAlertService,
        *,
        outbox_lease_seconds: int = 60,
    ):
        self.postgres = postgres_service
        self.alerts = alert_service
        self.outbox_lease_seconds = max(5, int(outbox_lease_seconds))

    def _outbox_attention(self) -> list[dict[str, Any]]:
        with self.postgres.conn() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT event.event_id,event.status,event.event_type,event.last_error_code,
                       execution.status AS action_status,execution.action_name
                FROM mvp_control.action_outbox AS event
                JOIN mvp_control.action_executions AS execution
                  ON execution.execution_id=event.execution_id
                WHERE event.status='DEAD_LETTER'
                   OR (event.status='PROCESSING'
                       AND event.locked_at < now()-(%s*interval '1 second'))
                """,
                (self.outbox_lease_seconds,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def run_cycle(self) -> dict[str, Any]:
        attention = self._outbox_attention()
        active_keys = set()
        for event in attention:
            key = f"outbox:{event['event_id']}"
            active_keys.add(key)
            stale = event["status"] == "PROCESSING"
            self.alerts.raise_alert(
                alert_key=key,
                source_type="OUTBOX",
                source_id=event["event_id"],
                category="OUTBOX_STALE" if stale else "OUTBOX_DEAD_LETTER",
                severity=(
                    "CRITICAL"
                    if event["action_status"] == "COMPENSATION_FAILED"
                    else "WARNING" if stale else "CRITICAL"
                ),
                title=(
                    f"{event['action_name']} Outbox 租约过期"
                    if stale
                    else f"{event['action_name']} Outbox 进入死信"
                ),
                message=(
                    "处理租约已超过阈值，需要检查 worker 状态"
                    if stale
                    else "自动重试已耗尽，需要排障后人工重放"
                ),
                metadata={
                    "event_type": event["event_type"],
                    "action_status": event["action_status"],
                    "error_code": event.get("last_error_code"),
                },
                actor_id="operations-monitor",
            )
        for alert in self.alerts.repository.list_alerts(limit=500):
            if (
                alert["source_type"] == "OUTBOX"
                and alert["status"] != "RESOLVED"
                and alert["alert_key"] not in active_keys
            ):
                self.alerts.repository.resolve_by_key(
                    alert["alert_key"],
                    actor_id="operations-monitor",
                    note="Outbox 已恢复为无需人工关注的状态",
                )
        escalated = self.alerts.escalate_due()
        return {
            "attention_count": len(attention),
            "escalated_count": len(escalated),
        }
