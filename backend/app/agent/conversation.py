from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from uuid import uuid4


class AgentConversationError(ValueError):
    """Conversation cannot be resumed safely."""


class AgentConversationBusyError(AgentConversationError):
    """Another turn is currently running for the same conversation."""


_CONVERSATION_ID = re.compile(r"^pc_[0-9a-f]{32}$")
_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(api[_ -]?key|authorization|bearer|token|secret)\s*[:=]?\s*\S+"),
    re.compile(r"(?i)(application[_ -]?id|id[_ -]?number|身份证号|申请编号)\s*[:=：]?\s*[\w-]+"),
    re.compile(r"(?i)(姓名|客户姓名|地址|住址|车架号|vin|银行卡号)\s*[:=：]?\s*[^\s,，;；]{2,80}"),
    re.compile(r"(?i)\b(?:app|loan|case|cust|customer|borrower|vehicle|vin|ra|cr|fr|pv)[-_][a-z0-9_-]{3,}\b"),
    re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\w)"),
    re.compile(r"(?<!\d)\d{7,17}(?!\d)"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)


def sanitize_conversation_text(value: Any, *, max_chars: int = 300) -> str:
    """Keep conversational intent while removing common credentials and identifiers."""
    text = " ".join(str(value or "").split())
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub("[已脱敏]", text)
    return text[:max_chars]


@dataclass(frozen=True)
class ConversationLease:
    conversation_id: str
    scope_digest: str
    turn_index: int
    history: tuple[dict[str, Any], ...]


@dataclass
class _Conversation:
    scope_digest: str
    next_turn_index: int = 1
    turns: list[dict[str, Any]] = field(default_factory=list)
    touched_at: float = 0.0
    inflight_turn_index: int | None = None
    lease_started_at: float | None = None


class AgentConversationStore:
    """Small in-process, TTL-bound store containing only redacted dialogue context."""

    def __init__(
        self,
        *,
        max_conversations: int = 256,
        max_turns: int = 8,
        ttl_seconds: float = 1800,
        processing_timeout_seconds: float = 600,
        clock: Callable[[], float] | None = None,
        scope_secret: bytes | None = None,
    ):
        if (
            max_conversations < 1 or max_turns < 1 or ttl_seconds <= 0
            or processing_timeout_seconds <= 0
        ):
            raise ValueError("会话存储边界必须为正数")
        self.max_conversations = int(max_conversations)
        self.max_turns = int(max_turns)
        self.ttl_seconds = float(ttl_seconds)
        self.processing_timeout_seconds = float(processing_timeout_seconds)
        self._clock = clock or time.monotonic
        self._scope_secret = scope_secret or secrets.token_bytes(32)
        self._items: OrderedDict[str, _Conversation] = OrderedDict()
        self._lock = threading.RLock()

    def _scope(self, application_id: str, ontology_version: str) -> str:
        message = (
            str(application_id).strip() + "\0" + str(ontology_version).strip()
        ).encode("utf-8")
        return hmac.new(self._scope_secret, message, hashlib.sha256).hexdigest()

    def _purge(self, now: float) -> None:
        for conversation in self._items.values():
            if (
                conversation.inflight_turn_index is not None
                and conversation.lease_started_at is not None
                and now - conversation.lease_started_at
                >= self.processing_timeout_seconds
            ):
                conversation.inflight_turn_index = None
                conversation.lease_started_at = None
        expired = [
            conversation_id
            for conversation_id, conversation in self._items.items()
            if now - conversation.touched_at >= self.ttl_seconds
        ]
        for conversation_id in expired:
            self._items.pop(conversation_id, None)

    def _enforce_capacity(self) -> None:
        while len(self._items) > self.max_conversations:
            removable = next((
                conversation_id
                for conversation_id, conversation in self._items.items()
                if conversation.inflight_turn_index is None
            ), None)
            if removable is None:
                return
            self._items.pop(removable, None)

    def begin(
        self,
        conversation_id: str | None,
        *,
        application_id: str,
        ontology_version: str,
    ) -> ConversationLease:
        """Reserve a unique turn index before any asynchronous model work starts."""
        now = self._clock()
        scope_digest = self._scope(application_id, ontology_version)
        with self._lock:
            self._purge(now)
            if conversation_id is None:
                while len(self._items) >= self.max_conversations:
                    removable = next((
                        item_id
                        for item_id, item in self._items.items()
                        if item.inflight_turn_index is None
                    ), None)
                    if removable is None:
                        raise AgentConversationBusyError(
                            "当前会话容量已满，请稍后重试"
                        )
                    self._items.pop(removable, None)
                conversation_id = f"pc_{uuid4().hex}"
                while conversation_id in self._items:
                    conversation_id = f"pc_{uuid4().hex}"
                self._items[conversation_id] = _Conversation(
                    scope_digest=scope_digest,
                    touched_at=now,
                )
            else:
                conversation_id = str(conversation_id).strip()
                if not _CONVERSATION_ID.fullmatch(conversation_id):
                    raise AgentConversationError("conversation_id 格式不正确")
                conversation = self._items.get(conversation_id)
                if conversation is None:
                    raise AgentConversationError("会话不存在或已过期，请创建新会话")
                if not hmac.compare_digest(conversation.scope_digest, scope_digest):
                    raise AgentConversationError("会话与当前案件或本体版本不匹配")

            conversation = self._items[conversation_id]
            if conversation.inflight_turn_index is not None:
                raise AgentConversationBusyError("该会话已有一轮正在处理中，请稍后重试")
            turn_index = conversation.next_turn_index
            conversation.next_turn_index += 1
            conversation.inflight_turn_index = turn_index
            conversation.lease_started_at = now
            conversation.touched_at = now
            self._items.move_to_end(conversation_id)
            self._enforce_capacity()
            history = tuple(dict(turn) for turn in conversation.turns)
        return ConversationLease(
            conversation_id=conversation_id,
            scope_digest=scope_digest,
            turn_index=turn_index,
            history=history,
        )

    def commit(
        self,
        lease: ConversationLease,
        *,
        user: str,
        assistant: str,
        response_type: str,
        tool_names: list[str] | tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        now = self._clock()
        turn = {
            "turn_index": lease.turn_index,
            "user": sanitize_conversation_text(user),
            "assistant": sanitize_conversation_text(assistant),
            "response_type": str(response_type or "NO_ACTION").strip().upper(),
            "tool_names": [
                str(name) for name in tool_names
                if str(name) in {
                    "inspect_case_readiness",
                    "preview_risk_assessment",
                    "generate_verification_questions",
                    "generate_decision_support",
                }
            ],
        }
        with self._lock:
            self._purge(now)
            conversation = self._items.get(lease.conversation_id)
            if conversation is None:
                raise AgentConversationError("会话已过期，请创建新会话")
            if not hmac.compare_digest(
                conversation.scope_digest, lease.scope_digest
            ):
                raise AgentConversationError("会话作用域发生变化")
            if conversation.inflight_turn_index != lease.turn_index:
                raise AgentConversationError("会话轮次租约已失效")
            conversation.turns.append(turn)
            conversation.turns.sort(key=lambda item: item["turn_index"])
            conversation.turns = conversation.turns[-self.max_turns :]
            conversation.touched_at = now
            conversation.inflight_turn_index = None
            conversation.lease_started_at = None
            self._items.move_to_end(lease.conversation_id)
            self._enforce_capacity()
            return [dict(item) for item in conversation.turns]

    def abort(self, lease: ConversationLease) -> None:
        """Release an in-flight turn after planning/model errors without storing data."""
        now = self._clock()
        with self._lock:
            conversation = self._items.get(lease.conversation_id)
            if conversation is None:
                return
            if not hmac.compare_digest(
                conversation.scope_digest, lease.scope_digest
            ):
                return
            if conversation.inflight_turn_index == lease.turn_index:
                conversation.inflight_turn_index = None
                conversation.lease_started_at = None
                conversation.touched_at = now

    def inspect(self, conversation_id: str) -> Mapping[str, Any] | None:
        """Test/operations view. It intentionally exposes no scope source values."""
        now = self._clock()
        with self._lock:
            self._purge(now)
            conversation = self._items.get(conversation_id)
            if conversation is None:
                return None
            return {
                "turn_count": len(conversation.turns),
                "next_turn_index": conversation.next_turn_index,
                "inflight_turn_index": conversation.inflight_turn_index,
                "turns": [dict(item) for item in conversation.turns],
            }


DEFAULT_AGENT_CONVERSATION_STORE = AgentConversationStore()
