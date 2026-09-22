"""Canonical peer evidence assembled from discovery and explicit diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
import threading

from core.diagnostics import NeighborSnapshot, ProbeResult
from core.discovery import Hello
from core.roster import Peer

STALE_RETENTION = 300.0
MAX_RETAINED_PEERS = 512
MAX_PENDING_PROBES = 256


class DiscoveryState(Enum):
    """Presence derived only from recent discovery announcements."""

    NEARBY = "nearby"
    STALE = "stale"


class ReachabilityState(Enum):
    """Endpoint reachability derived from an explicit check."""

    UNKNOWN = "unknown"
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"


class CompatibilityState(Enum):
    """Protocol compatibility derived from a correlated exchange."""

    UNKNOWN = "unknown"
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"


class TrustState(Enum):
    """Cryptographic identity state; pairing is not implemented yet."""

    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class PeerRecord:
    """Immutable evidence for one advertised session."""

    hello: Hello
    ip: str
    last_seen: float
    discovery_state: DiscoveryState = DiscoveryState.NEARBY
    reachability_state: ReachabilityState = ReachabilityState.UNKNOWN
    compatibility_state: CompatibilityState = CompatibilityState.UNKNOWN
    trust_state: TrustState = TrustState.UNVERIFIED
    stale_since: float | None = None
    hostname: str | None = None
    platform: str | None = None
    architecture: str | None = None
    mac_address: str | None = None
    mac_source: str | None = None
    latency_ms: float | None = None
    latency_source: str | None = None
    services: tuple[str, ...] = ()

    @property
    def session_id(self) -> str:
        """Return the actionable session key."""
        return self.hello.session_id

    @property
    def nearby(self) -> bool:
        """Return whether discovery presence is currently fresh."""
        return self.discovery_state is DiscoveryState.NEARBY

    def as_peer(self) -> Peer:
        """Return the active transport shape used by existing services."""
        return Peer(self.hello, self.ip, self.last_seen)


@dataclass(frozen=True)
class OverviewSummary:
    """Non-contradictory counts and measured RTT statistics."""

    observed: int = 0
    nearby: int = 0
    stale: int = 0
    responsive: int = 0
    measured: int = 0
    min_ms: float | None = None
    avg_ms: float | None = None
    max_ms: float | None = None


@dataclass(frozen=True)
class PeerRepositoryEvent:
    """One immutable repository revision for non-Qt consumers."""

    revision: int
    added: tuple[str, ...]
    updated: tuple[str, ...]
    removed: tuple[str, ...]
    snapshot: tuple[PeerRecord, ...]


@dataclass(frozen=True)
class _PendingProbe:
    session_id: str
    address: str
    port: int | None
    kind: str


class PeerRepository:
    """Retain bounded, session-keyed peer evidence independently of the UI."""

    def __init__(self, stale_retention: float = STALE_RETENTION,
                 max_retained: int = MAX_RETAINED_PEERS) -> None:
        if not math.isfinite(stale_retention) or stale_retention <= 0:
            raise ValueError("stale_retention must be finite and positive")
        if max_retained <= 0:
            raise ValueError("max_retained must be positive")
        self.stale_retention = stale_retention
        self.max_retained = max_retained
        self._records: dict[str, PeerRecord] = {}
        self._pending_probes: dict[str, _PendingProbe] = {}
        self._revision = 0
        self._lock = threading.Lock()

    def reconcile_presence(self, peers: tuple[Peer, ...], now: float) -> PeerRepositoryEvent | None:
        """Apply one complete active-roster snapshot and retain expired sessions."""
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        with self._lock:
            before = dict(self._records)
            active_ids = {peer.hello.session_id for peer in peers}
            for peer in peers:
                previous = self._records.get(peer.hello.session_id)
                same_endpoint = (previous is not None
                                 and previous.ip == peer.ip
                                 and previous.hello.tcp_port == peer.hello.tcp_port)
                same_capabilities = (previous is not None
                                     and previous.hello.capabilities
                                     == peer.hello.capabilities)
                self._records[peer.hello.session_id] = PeerRecord(
                    hello=peer.hello,
                    ip=peer.ip,
                    last_seen=peer.last_seen,
                    discovery_state=DiscoveryState.NEARBY,
                    reachability_state=(previous.reachability_state if same_endpoint
                                        else ReachabilityState.UNKNOWN),
                    compatibility_state=(previous.compatibility_state
                                         if same_endpoint and same_capabilities
                                           else CompatibilityState.UNKNOWN),
                    trust_state=TrustState.UNVERIFIED,
                    stale_since=None,
                    hostname=previous.hostname if previous else None,
                    platform=previous.platform if previous else None,
                    architecture=previous.architecture if previous else None,
                    mac_address=previous.mac_address if same_endpoint else None,
                    mac_source=previous.mac_source if same_endpoint else None,
                    latency_ms=previous.latency_ms if same_endpoint else None,
                    latency_source=previous.latency_source if same_endpoint else None,
                    services=previous.services if same_endpoint else (),
                )
            for session_id, record in tuple(self._records.items()):
                if session_id not in active_ids and record.nearby:
                    self._records[session_id] = replace(
                        record, discovery_state=DiscoveryState.STALE,
                        stale_since=now)
            self._purge_stale(now)
            return self._event(before)

    def apply_neighbor_snapshot(self, snapshot: NeighborSnapshot) -> PeerRepositoryEvent | None:
        """Attach endpoint-scoped MAC evidence without merging peer identities."""
        if snapshot.error is not None:
            return None
        with self._lock:
            before = dict(self._records)
            by_address = {entry.address: entry for entry in snapshot.entries
                          if entry.mac_address is not None}
            for session_id, record in tuple(self._records.items()):
                neighbor = by_address.get(record.ip)
                self._records[session_id] = replace(
                    record,
                    mac_address=(neighbor.mac_address
                                 if neighbor is not None else None),
                    mac_source=(snapshot.source if neighbor is not None else None))
            return self._event(before)

    def register_probe(self, request_id: str, session_id: str,
                       address: str, port: int | None, kind: str) -> None:
        """Correlate a diagnostic request with one exact session endpoint."""
        with self._lock:
            if len(self._pending_probes) >= MAX_PENDING_PROBES:
                del self._pending_probes[next(iter(self._pending_probes))]
            self._pending_probes[request_id] = _PendingProbe(
                session_id, address, port, kind)

    def apply_probe_result(self, result: ProbeResult) -> PeerRepositoryEvent | None:
        """Apply supported probe evidence only when request and endpoint still match."""
        with self._lock:
            pending = self._pending_probes.pop(result.request_id, None)
            if pending is None:
                return None
            record = self._records.get(pending.session_id)
            if (record is None or record.ip != pending.address
                    or result.address != pending.address
                    or result.kind != pending.kind
                    or pending.port is not None
                    and record.hello.tcp_port != pending.port
                    or result.port != pending.port):
                return None
            before = dict(self._records)
            reachable = record.reachability_state
            compatible = record.compatibility_state
            latency_ms = record.latency_ms
            latency_source = record.latency_source
            if result.kind == "ping" and result.state == "reachable":
                reachable = ReachabilityState.REACHABLE
                if _valid_latency(result.rtt_ms):
                    latency_ms = result.rtt_ms
                    latency_source = "ping"
            elif result.kind == "tcp":
                if result.state in {"reachable", "refused"}:
                    reachable = ReachabilityState.REACHABLE
                    if _valid_latency(result.rtt_ms):
                        latency_ms = result.rtt_ms
                        latency_source = "tcp"
                elif result.state in {"timed_out", "network_unreachable"}:
                    reachable = ReachabilityState.UNREACHABLE
            elif result.kind == "echo":
                if result.state == "compatible":
                    reachable = ReachabilityState.REACHABLE
                    compatible = CompatibilityState.COMPATIBLE
                    if _valid_latency(result.rtt_ms):
                        latency_ms = result.rtt_ms
                        latency_source = "echo"
                elif result.state == "incompatible":
                    reachable = ReachabilityState.REACHABLE
                    compatible = CompatibilityState.INCOMPATIBLE
                    if _valid_latency(result.rtt_ms):
                        latency_ms = result.rtt_ms
                        latency_source = "echo"
                elif result.state == "refused":
                    reachable = ReachabilityState.REACHABLE
                    if _valid_latency(result.rtt_ms):
                        latency_ms = result.rtt_ms
                        latency_source = "tcp"
                elif result.state in {"timed_out", "network_unreachable"}:
                    reachable = ReachabilityState.UNREACHABLE
            self._records[pending.session_id] = replace(
                record, reachability_state=reachable,
                compatibility_state=compatible, latency_ms=latency_ms,
                latency_source=latency_source)
            return self._event(before)

    def snapshot(self) -> tuple[PeerRecord, ...]:
        """Return deterministic immutable repository records."""
        with self._lock:
            return self._snapshot()

    def get(self, session_id: str | None) -> PeerRecord | None:
        """Return one record by exact session ID."""
        if session_id is None:
            return None
        with self._lock:
            return self._records.get(session_id)

    def nearby(self) -> tuple[PeerRecord, ...]:
        """Return records with current discovery presence."""
        return tuple(record for record in self.snapshot() if record.nearby)

    def supporting(self, capability: str) -> tuple[PeerRecord, ...]:
        """Return nearby records advertising one capability."""
        return tuple(record for record in self.nearby()
                     if capability in record.hello.capabilities)

    def overview_summary(self) -> OverviewSummary:
        """Return non-contradictory counts and measured RTT statistics."""
        with self._lock:
            records = self._snapshot()
        nearby = [record for record in records if record.nearby]
        stale = len(records) - len(nearby)
        responsive = sum(1 for record in nearby
                         if record.reachability_state is ReachabilityState.REACHABLE)
        latencies = [record.latency_ms for record in nearby
                     if _valid_latency(record.latency_ms)]
        if not latencies:
            return OverviewSummary(len(records), len(nearby), stale, responsive, 0)
        return OverviewSummary(
            len(records), len(nearby), stale, responsive, len(latencies),
            min(latencies), sum(latencies) / len(latencies), max(latencies))

    def _purge_stale(self, now: float) -> None:
        expired = [session_id for session_id, record in self._records.items()
                   if record.stale_since is not None
                   and now - record.stale_since >= self.stale_retention]
        for session_id in expired:
            del self._records[session_id]
        overflow = max(0, len(self._records) - self.max_retained)
        stale = sorted(
            (record for record in self._records.values()
             if record.stale_since is not None),
            key=lambda record: (record.stale_since or 0.0, record.session_id))
        for record in stale[:overflow]:
            del self._records[record.session_id]

    def _event(self, before: dict[str, PeerRecord]) -> PeerRepositoryEvent | None:
        added = tuple(sorted(self._records.keys() - before.keys()))
        removed = tuple(sorted(before.keys() - self._records.keys()))
        updated = tuple(sorted(
            session_id for session_id in self._records.keys() & before.keys()
            if self._records[session_id] != before[session_id]))
        if not (added or removed or updated):
            return None
        self._revision += 1
        return PeerRepositoryEvent(
            self._revision, added, updated, removed, self._snapshot())

    def _snapshot(self) -> tuple[PeerRecord, ...]:
        records = tuple(self._records.values())
        return (tuple(record for record in records if record.nearby)
                + tuple(record for record in records if not record.nearby))


def _valid_latency(value: float | int | None) -> bool:
    """Return whether a latency sample can be presented as measured latency."""
    return (isinstance(value, (float, int)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and 0 < float(value) < 60000)
