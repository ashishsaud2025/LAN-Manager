"""Session-based presence tracking, independent of sockets and UI frameworks."""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.discovery import Hello

PEER_TIMEOUT = 6.0


@dataclass(frozen=True)
class Peer:
    """Immutable snapshot of a session and its most recent observed endpoint."""

    hello: Hello
    ip: str
    last_seen: float


class PeerRoster:
    """Own active sessions on one thread; callers supply monotonic timestamps."""

    def __init__(self, session_id: str, timeout: float = PEER_TIMEOUT) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self.session_id = session_id
        self.timeout = timeout
        self._peers: dict[str, Peer] = {}

    def update(self, hello: Hello, ip: str, now: float) -> bool:
        """Refresh presence and report new sessions or changed metadata/endpoints."""
        if hello.session_id == self.session_id:
            return False
        previous = self._peers.get(hello.session_id)
        self._peers[hello.session_id] = Peer(hello, ip, now)
        return previous is None or previous.hello != hello or previous.ip != ip

    def expire(self, now: float) -> tuple[Peer, ...]:
        """Remove sessions at or beyond the timeout and return expired snapshots."""
        expired = tuple(peer for peer in self._peers.values()
                        if now - peer.last_seen >= self.timeout)
        for peer in expired:
            del self._peers[peer.hello.session_id]
        return expired

    def snapshot(self) -> tuple[Peer, ...]:
        """Return deterministic immutable snapshots of currently active sessions."""
        return tuple(sorted(self._peers.values(),
                            key=lambda peer: (peer.hello.name,
                                              peer.hello.session_id)))
