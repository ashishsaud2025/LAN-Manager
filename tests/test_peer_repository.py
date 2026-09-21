from __future__ import annotations

from uuid import uuid4

from core.diagnostics import Neighbor, NeighborSnapshot, ProbeResult
from core.discovery import Hello
from core.peer_repository import (
    CompatibilityState, DiscoveryState, PeerRepository, ReachabilityState,
    TrustState,
)
from core.roster import Peer


def _peer(name: str = "Peer", address: str = "192.168.1.20",
          peer_id: str | None = None) -> Peer:
    hello = Hello(peer_id or str(uuid4()), str(uuid4()), name, 50001,
                  ("chat_v1", "echo_v1"))
    return Peer(hello, address, 10.0)


def test_presence_creates_refreshes_and_stales_one_session() -> None:
    repository = PeerRepository(stale_retention=30)
    peer = _peer()
    event = repository.reconcile_presence((peer,), 10)
    assert event is not None
    assert event.added == (peer.hello.session_id,)
    record = repository.get(peer.hello.session_id)
    assert record is not None and record.nearby
    assert record.trust_state is TrustState.UNVERIFIED

    refreshed = Peer(peer.hello, "192.168.1.21", 12)
    event = repository.reconcile_presence((refreshed,), 12)
    assert event is not None
    assert event.updated == (peer.hello.session_id,)
    assert repository.get(peer.hello.session_id).ip == "192.168.1.21"

    repository.reconcile_presence((), 16)
    record = repository.get(peer.hello.session_id)
    assert record is not None
    assert record.discovery_state is DiscoveryState.STALE
    assert repository.nearby() == ()


def test_stale_retention_reappearance_and_purge_are_deterministic() -> None:
    repository = PeerRepository(stale_retention=5)
    peer = _peer()
    repository.reconcile_presence((peer,), 10)
    repository.reconcile_presence((), 11)
    repository.reconcile_presence((Peer(peer.hello, peer.ip, 12),), 12)
    assert repository.get(peer.hello.session_id).nearby
    repository.reconcile_presence((), 13)
    event = repository.reconcile_presence((), 18)
    assert event is not None
    assert event.removed == (peer.hello.session_id,)
    assert repository.snapshot() == ()


def test_concurrent_sessions_are_not_merged_by_installation_or_address() -> None:
    repository = PeerRepository()
    installation = str(uuid4())
    first = _peer("Same", peer_id=installation)
    second = _peer("Same", peer_id=installation)
    second = Peer(second.hello, first.ip, second.last_seen)
    repository.reconcile_presence((first, second), 10)
    assert {record.session_id for record in repository.snapshot()} == {
        first.hello.session_id, second.hello.session_id}


def test_unknown_fields_remain_unknown_until_supported_evidence_exists() -> None:
    repository = PeerRepository()
    peer = _peer()
    repository.reconcile_presence((peer,), 10)
    record = repository.get(peer.hello.session_id)
    assert record is not None
    assert (record.hostname, record.platform, record.architecture,
            record.mac_address, record.latency_ms, record.services) == (
                None, None, None, None, None, ())

    event = repository.apply_neighbor_snapshot(NeighborSnapshot(
        "neighbors", (Neighbor(peer.ip, "00:11:22:33:44:55", "Wi-Fi", "stale"),),
        "OS neighbor cache"))
    assert event is not None
    record = repository.get(peer.hello.session_id)
    assert record.mac_address == "00:11:22:33:44:55"
    assert record.mac_source == "OS neighbor cache"


def test_correlated_tcp_and_echo_results_update_independent_evidence() -> None:
    repository = PeerRepository()
    peer = _peer()
    repository.reconcile_presence((peer,), 10)
    repository.register_probe("tcp", peer.hello.session_id, peer.ip,
                              peer.hello.tcp_port, "tcp")
    repository.apply_probe_result(ProbeResult(
        "tcp", "tcp", peer.ip, peer.hello.tcp_port, "reachable", 2.0))
    record = repository.get(peer.hello.session_id)
    assert record.reachability_state is ReachabilityState.REACHABLE
    assert record.compatibility_state is CompatibilityState.UNKNOWN

    repository.register_probe("echo", peer.hello.session_id, peer.ip,
                              peer.hello.tcp_port, "echo")
    repository.apply_probe_result(ProbeResult(
        "echo", "echo", peer.ip, peer.hello.tcp_port, "compatible", 3.0))
    record = repository.get(peer.hello.session_id)
    assert record.reachability_state is ReachabilityState.REACHABLE
    assert record.compatibility_state is CompatibilityState.COMPATIBLE
    assert record.latency_ms is None


def test_probe_result_cannot_cross_session_or_changed_endpoint() -> None:
    repository = PeerRepository()
    first, second = _peer(), _peer()
    second = Peer(second.hello, first.ip, second.last_seen)
    repository.reconcile_presence((first, second), 10)
    repository.register_probe("probe", first.hello.session_id, first.ip,
                              first.hello.tcp_port, "echo")
    moved = Peer(first.hello, "192.168.1.99", 11)
    repository.reconcile_presence((moved, second), 11)
    assert repository.apply_probe_result(ProbeResult(
        "probe", "echo", first.ip, first.hello.tcp_port, "compatible")) is None
    assert repository.get(first.hello.session_id).compatibility_state is (
        CompatibilityState.UNKNOWN)
    assert repository.get(second.hello.session_id).compatibility_state is (
        CompatibilityState.UNKNOWN)


def test_endpoint_change_clears_endpoint_scoped_evidence() -> None:
    repository = PeerRepository()
    peer = _peer()
    repository.reconcile_presence((peer,), 10)
    repository.apply_neighbor_snapshot(NeighborSnapshot(
        "neighbors", (Neighbor(peer.ip, "00:11:22:33:44:55", None, "reachable"),),
        "OS neighbor cache"))
    repository.register_probe("echo", peer.hello.session_id, peer.ip,
                              peer.hello.tcp_port, "echo")
    repository.apply_probe_result(ProbeResult(
        "echo", "echo", peer.ip, peer.hello.tcp_port, "compatible"))

    moved = Peer(peer.hello, "192.168.1.99", 11)
    repository.reconcile_presence((moved,), 11)
    record = repository.get(peer.hello.session_id)
    assert record.reachability_state is ReachabilityState.UNKNOWN
    assert record.compatibility_state is CompatibilityState.UNKNOWN
    assert record.mac_address is None


def test_probe_kind_must_match_and_refusal_proves_host_response() -> None:
    repository = PeerRepository()
    peer = _peer()
    repository.reconcile_presence((peer,), 10)
    repository.register_probe("wrong-kind", peer.hello.session_id, peer.ip,
                              peer.hello.tcp_port, "tcp")
    assert repository.apply_probe_result(ProbeResult(
        "wrong-kind", "echo", peer.ip, peer.hello.tcp_port, "compatible")) is None
    assert repository.get(peer.hello.session_id).compatibility_state is (
        CompatibilityState.UNKNOWN)

    repository.register_probe("refused", peer.hello.session_id, peer.ip,
                              peer.hello.tcp_port, "tcp")
    repository.apply_probe_result(ProbeResult(
        "refused", "tcp", peer.ip, peer.hello.tcp_port, "refused"))
    assert repository.get(peer.hello.session_id).reachability_state is (
        ReachabilityState.REACHABLE)


def test_complete_neighbor_refresh_clears_missing_cache_evidence() -> None:
    repository = PeerRepository()
    peer = _peer()
    repository.reconcile_presence((peer,), 10)
    repository.apply_neighbor_snapshot(NeighborSnapshot(
        "one", (Neighbor(peer.ip, "00:11:22:33:44:55", None, "stale"),),
        "OS neighbor cache"))
    repository.apply_neighbor_snapshot(NeighborSnapshot(
        "two", (), "OS neighbor cache"))
    assert repository.get(peer.hello.session_id).mac_address is None


def test_no_reply_and_unknown_requests_do_not_invent_state() -> None:
    repository = PeerRepository()
    peer = _peer()
    repository.reconcile_presence((peer,), 10)
    assert repository.apply_probe_result(ProbeResult(
        "unknown", "ping", peer.ip, None, "reachable")) is None
    repository.register_probe("ping", peer.hello.session_id, peer.ip, None, "ping")
    repository.apply_probe_result(ProbeResult(
        "ping", "ping", peer.ip, None, "no_reply", 100.0))
    assert repository.get(peer.hello.session_id).reachability_state is (
        ReachabilityState.UNKNOWN)


def test_stale_history_is_bounded_without_dropping_nearby_records() -> None:
    repository = PeerRepository(max_retained=2)
    peers = tuple(_peer(str(index)) for index in range(3))
    repository.reconcile_presence(peers, 10)
    repository.reconcile_presence((peers[2],), 11)
    snapshot = repository.snapshot()
    assert peers[2].hello.session_id in {record.session_id for record in snapshot}
    assert len(snapshot) == 2
