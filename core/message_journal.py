"""Bounded thread-safe message history shared by desktop and portal views."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import threading
import time
from typing import Any

from core.discovery import Hello
from core.roster import Peer

MESSAGE_LIMIT = 1000
OUTCOME_DETAIL_LIMIT = 512
DELIVERY_STATES = frozenset({"queued", "accepted", "failed", "uncertain"})


@dataclass(frozen=True)
class DeliveryOutcome:
    """Latest local delivery evidence for one exact recipient session."""

    peer_id: str
    session_id: str
    peer_name: str
    state: str
    detail: str
    updated_ms: int
    authenticated: bool = False


@dataclass(frozen=True)
class MessageRecord:
    """One logical message retained in local observation order."""

    message_id: str
    direction: str
    scope: str
    text: str
    recorded_ms: int
    sender_peer_id: str
    sender_session_id: str
    sender_name: str
    to_session: str | None
    deliveries: tuple[DeliveryOutcome, ...]
    authenticated: bool = False


@dataclass(frozen=True)
class MessageJournalSnapshot:
    """Immutable revisioned message history snapshot."""

    revision: int
    entries: tuple[MessageRecord, ...]


class MessageJournal:
    """Retain process-lifetime messages independently of Qt event delivery."""

    def __init__(self, limit: int = MESSAGE_LIMIT,
                 clock_ms: Callable[[], int] | None = None) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("message limit must be a positive integer")
        self.limit = limit
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._entries: list[MessageRecord] = []
        self._incoming: set[tuple[str, str]] = set()
        self._revision = 0
        self._lock = threading.Lock()

    def record_outgoing(self, message: dict[str, Any], sender: Hello,
                        recipients: tuple[Peer, ...]) -> MessageRecord:
        """Record one logical send before workers can update its outcomes."""
        now = self._clock_ms()
        deliveries = tuple(
            DeliveryOutcome(peer.hello.peer_id, peer.hello.session_id,
                            peer.hello.name, "queued", "queued locally", now)
            for peer in recipients)
        body = message["body"]
        record = MessageRecord(
            message["message_id"], "outgoing", body["scope"], body["text"], now,
            sender.peer_id, sender.session_id, sender.name,
            body.get("to_session"), deliveries)
        with self._lock:
            self._append(record)
        return record

    def record_incoming(self, message: dict[str, Any], sender_name: str,
                        local: Hello, authenticated: bool = False) -> bool:
        """Commit one accepted incoming message unless it is a duplicate."""
        key = (message["session_id"], message["message_id"])
        now = self._clock_ms()
        body = message["body"]
        delivery = DeliveryOutcome(
            local.peer_id, local.session_id, local.name, "accepted",
            "accepted by receiving application", now, authenticated)
        record = MessageRecord(
            message["message_id"], "incoming", body["scope"], body["text"], now,
            message["peer_id"], message["session_id"], sender_name,
            body.get("to_session"), (delivery,), authenticated)
        with self._lock:
            if key in self._incoming:
                return False
            self._incoming.add(key)
            self._append(record)
            return True

    def update_delivery(self, message_id: str, session_id: str, state: str,
                        detail: str, authenticated: bool = False) -> bool:
        """Update one recipient without recreating an evicted logical message."""
        if state not in DELIVERY_STATES:
            raise ValueError(f"unknown delivery state: {state}")
        bounded_detail = detail[:OUTCOME_DETAIL_LIMIT]
        now = self._clock_ms()
        with self._lock:
            for row, record in enumerate(self._entries):
                if record.direction != "outgoing" or record.message_id != message_id:
                    continue
                deliveries = list(record.deliveries)
                for index, delivery in enumerate(deliveries):
                    if delivery.session_id == session_id:
                        deliveries[index] = replace(
                            delivery, state=state, detail=bounded_detail,
                            updated_ms=now, authenticated=authenticated)
                        self._entries[row] = replace(
                            record, deliveries=tuple(deliveries))
                        self._revision += 1
                        return True
                return False
            return False

    def snapshot(self) -> MessageJournalSnapshot:
        """Return immutable records in local observation order."""
        with self._lock:
            return MessageJournalSnapshot(self._revision, tuple(self._entries))

    def _append(self, record: MessageRecord) -> None:
        self._entries.append(record)
        if len(self._entries) > self.limit:
            removed = self._entries.pop(0)
            if removed.direction == "incoming":
                self._incoming.discard(
                    (removed.sender_session_id, removed.message_id))
        self._revision += 1
