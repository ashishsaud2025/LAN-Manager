"""Session-based presence tracking, independent of sockets and UI frameworks."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from core.discovery import Hello

PEER_TIMEOUT = 6.0


MAX_ENDPOINT_CANDIDATES = 16


@dataclass(frozen=True)
class EndpointCandidate:
    """One observed source address for a session."""

    ip: str
    last_seen: float


@dataclass(frozen=True)
class Peer:
    """Immutable snapshot of a session and its most recent observed endpoint."""

    hello: Hello
    ip: str
    last_seen: float
    endpoint_candidates: tuple[EndpointCandidate, ...] = field(
        default=(), compare=False)


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
        if not isinstance(ip, str) or not ip:
            raise ValueError("ip must be a nonempty string")
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        previous = self._peers.get(hello.session_id)
        candidates = _merge_candidates(previous, ip, now)
        self._peers[hello.session_id] = Peer(hello, ip, now, candidates)
        return previous is None or previous.hello != hello or previous.ip != ip

    def expire(self, now: float) -> tuple[Peer, ...]:
        """Expire stale candidates and return fully expired sessions."""
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        expired: list[Peer] = []
        for session_id, peer in tuple(self._peers.items()):
            live = tuple(item for item in _all_candidates(peer)
                         if now - item.last_seen < self.timeout)
            if not live:
                expired.append(peer)
                del self._peers[session_id]
                continue
            preferred = max(live, key=lambda item: (item.last_seen, item.ip))
            ordered = _order_candidates(preferred.ip, live)
            if (peer.ip != preferred.ip or peer.last_seen != preferred.last_seen
                    or peer.endpoint_candidates != ordered):
                self._peers[session_id] = Peer(
                    peer.hello, preferred.ip, preferred.last_seen, ordered)
        return tuple(expired)

    def snapshot(self) -> tuple[Peer, ...]:
        """Return deterministic immutable snapshots of currently active sessions."""
        return tuple(sorted(self._peers.values(),
                            key=lambda peer: (peer.hello.name,
                                              peer.hello.session_id)))


def _all_candidates(peer: Peer | None) -> tuple[EndpointCandidate, ...]:
    """Return stored candidates with legacy single endpoint fallback."""
    if peer is None:
        return ()
    if peer.endpoint_candidates:
        return peer.endpoint_candidates
    return (EndpointCandidate(peer.ip, peer.last_seen),)


def _order_candidates(
        preferred_ip: str,
        candidates: tuple[EndpointCandidate, ...]) -> tuple[EndpointCandidate, ...]:
    """Order candidates with preferred first for deterministic snapshots."""
    by_ip = {item.ip: item for item in candidates}
    preferred = by_ip.get(preferred_ip)
    if preferred is None:
        return ()
    others = sorted((item for item in candidates if item.ip != preferred_ip),
                    key=lambda item: (-item.last_seen, item.ip))
    return (preferred, *others)[:MAX_ENDPOINT_CANDIDATES]


def _merge_candidates(previous: Peer | None, ip: str,
                       now: float) -> tuple[EndpointCandidate, ...]:
    """Merge one observation into bounded session candidates."""
    by_seen = {item.ip: item.last_seen for item in _all_candidates(previous)}
    by_seen[ip] = now
    merged = tuple(EndpointCandidate(address, seen)
                   for address, seen in by_seen.items())
    return _order_candidates(ip, merged)


MAX_CONNECTION_CANDIDATES = 3


def candidate_ips(peer: Peer, limit: int = MAX_CONNECTION_CANDIDATES) -> tuple[str, ...]:
    """Return bounded deduped addresses with preferred endpoint first."""
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be a positive int")
    if peer.endpoint_candidates:
        ordered = [peer.ip]
        ordered.extend(item.ip for item in peer.endpoint_candidates
                       if item.ip != peer.ip)
    else:
        ordered = [peer.ip]
    seen: set[str] = set()
    result: list[str] = []
    for address in ordered:
        if address not in seen:
            seen.add(address)
            result.append(address)
        if len(result) >= limit:
            break
    return tuple(result)
