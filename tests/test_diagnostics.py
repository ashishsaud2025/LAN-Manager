from __future__ import annotations

from queue import Queue
import socket
import threading
from typing import Any
from uuid import uuid4

import pytest

from core.diagnostics import (
    DiagnosticsService, NeighborSnapshot, ProbeResult, parse_linux_neighbors,
    parse_ping_rtt, parse_windows_neighbors,
)
from core.protocol import envelope, recv_message, send_message, validate_envelope


def _events() -> tuple[Queue[tuple[str, Any]], Any]:
    events: Queue[tuple[str, Any]] = Queue()

    def emit(kind: str, value: Any) -> bool:
        events.put((kind, value))
        return True

    return events, emit


def test_windows_neighbor_json_is_validated_and_deduplicated() -> None:
    payload = """[
      {"IPAddress":"192.168.1.20","LinkLayerAddress":"AA-BB-CC-DD-EE-FF",
       "InterfaceAlias":"Wi-Fi","State":5},
      {"IPAddress":"192.168.1.20","LinkLayerAddress":"aa-bb-cc-dd-ee-ff",
       "InterfaceAlias":"Wi-Fi","State":"Stale"},
      {"IPAddress":"255.255.255.255","LinkLayerAddress":"FF-FF-FF-FF-FF-FF",
       "InterfaceAlias":"Wi-Fi","State":6},
      {"IPAddress":"ff02::1","LinkLayerAddress":"bad","InterfaceAlias":"Wi-Fi"}
    ]"""
    entries = parse_windows_neighbors(payload)
    assert len(entries) == 1
    assert entries[0].address == "192.168.1.20"
    assert entries[0].mac_address == "aa:bb:cc:dd:ee:ff"
    assert entries[0].interface == "Wi-Fi"
    assert entries[0].state == "reachable"


def test_linux_neighbor_json_preserves_cache_evidence() -> None:
    payload = """[
      {"dst":"192.168.1.1","dev":"eth0","lladdr":"00:11:22:33:44:55",
       "state":["REACHABLE"]},
      {"dst":"192.168.1.30","dev":"wlan0","state":["INCOMPLETE"]}
    ]"""
    entries = parse_linux_neighbors(payload)
    assert [entry.address for entry in entries] == ["192.168.1.1", "192.168.1.30"]
    assert entries[0].state == "REACHABLE"
    assert entries[1].mac_address is None


def test_inventory_refresh_emits_one_bounded_snapshot() -> None:
    events, emit = _events()

    def run(command: list[str], timeout: float) -> tuple[int, str, str]:
        assert command[:3] == ["ip", "-j", "neigh"]
        assert timeout > 0
        return 0, '[{"dst":"10.0.0.2","dev":"eth0","state":["STALE"]}]', ""

    service = DiagnosticsService(emit, platform="linux", command_runner=run)
    service.start()
    identifier = service.refresh_neighbors()
    kind, snapshot = events.get(timeout=2)
    service.stop()
    assert service.join(2)
    assert kind == "neighbor_snapshot"
    assert isinstance(snapshot, NeighborSnapshot)
    assert snapshot.request_id == identifier
    assert snapshot.entries[0].address == "10.0.0.2"
    assert snapshot.error is None


def test_ping_failure_remains_separate_from_tcp_reachability() -> None:
    events, emit = _events()

    def run(command: list[str], timeout: float) -> tuple[int, str, str]:
        del command, timeout
        return 1, "Request timed out", ""

    service = DiagnosticsService(emit, platform="win32", command_runner=run)
    service.start()
    identifier = service.ping("192.168.1.20")
    assert events.get(timeout=2)[0] == "diagnostic_started"
    kind, result = events.get(timeout=2)
    service.stop()
    assert service.join(2)
    assert kind == "diagnostic_result"
    assert isinstance(result, ProbeResult)
    assert result.request_id == identifier
    assert result.state == "no_reply"


def test_tcp_check_reports_success_without_sending_data() -> None:
    events, emit = _events()
    received = bytearray()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(2)

        def accept() -> None:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(0.2)
                try:
                    received.extend(conn.recv(1))
                except TimeoutError:
                    pass

        server = threading.Thread(target=accept, daemon=True)
        server.start()
        service = DiagnosticsService(emit)
        service.start()
        service.tcp_connect("127.0.0.1", listener.getsockname()[1])
        assert events.get(timeout=2)[0] == "diagnostic_started"
        kind, result = events.get(timeout=2)
        service.stop()
        assert service.join(2)
        server.join(2)
    assert kind == "diagnostic_result"
    assert result.state == "reachable"
    assert result.duration_ms >= 0
    assert received == b""


@pytest.mark.parametrize("matching_identity", [True, False])
def test_echo_requires_correlation_body_and_advertised_identity(
        matching_identity: bool) -> None:
    events, emit = _events()
    remote_peer_id, remote_session_id = str(uuid4()), str(uuid4())
    expected_session_id = remote_session_id if matching_identity else str(uuid4())
    errors: list[Exception] = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(2)

        def respond() -> None:
            try:
                conn, _ = listener.accept()
                with conn:
                    request = recv_message(conn)
                    validate_envelope(request)
                    send_message(conn, envelope(
                        "ECHO_REPLY", remote_peer_id, remote_session_id,
                        request["body"], request["message_id"]))
            except Exception as error:
                errors.append(error)

        server = threading.Thread(target=respond, daemon=True)
        server.start()
        service = DiagnosticsService(emit)
        service.start()
        service.echo("127.0.0.1", listener.getsockname()[1],
                     remote_peer_id, expected_session_id)
        assert events.get(timeout=2)[0] == "diagnostic_started"
        kind, result = events.get(timeout=2)
        service.stop()
        assert service.join(2)
        server.join(2)
    assert not errors
    assert kind == "diagnostic_result"
    assert result.state == ("compatible" if matching_identity else "incompatible")


def test_validation_and_queue_bounds_reject_unsafe_requests() -> None:
    _, emit = _events()
    service = DiagnosticsService(emit)
    with pytest.raises(ValueError, match="numeric IPv4"):
        service.ping("example.com")
    with pytest.raises(ValueError, match="port"):
        service.tcp_connect("127.0.0.1", 0)
    with pytest.raises(ValueError, match="peer ID"):
        service.echo("127.0.0.1", 50002, "not-a-uuid", str(uuid4()))
    for _ in range(16):
        service.ping("127.0.0.1")
    with pytest.raises(RuntimeError, match="queue is full"):
        service.ping("127.0.0.1")
    service.stop()
    assert service.join(1)


def test_parse_ping_rtt_prefers_measured_average_without_invention() -> None:
    windows = ("Reply from 192.168.1.20: bytes=32 time=1ms TTL=128\n\n"
               "Ping statistics:\n    Minimum = 1ms, Maximum = 1ms, Average = 1ms")
    assert parse_ping_rtt(windows) == 1.0
    linux = ("64 bytes from 192.168.1.20: icmp_seq=1 ttl=64 time=0.42 ms\n\n"
             "rtt min/avg/max/mdev = 0.410/0.420/0.430/0.010 ms")
    assert parse_ping_rtt(linux) == 0.42
    assert parse_ping_rtt("Reply from 192.168.1.20: bytes=32 time<1ms TTL=128\n"
                          "Average = 0ms") is None
    assert parse_ping_rtt("") is None
    assert parse_ping_rtt("no timing information here") is None


def test_echo_result_carries_exchange_rtt_for_compatible_reply() -> None:
    events, emit = _events()
    remote_peer_id, remote_session_id = str(uuid4()), str(uuid4())
    errors: list[Exception] = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(2)

        def respond() -> None:
            try:
                conn, _ = listener.accept()
                with conn:
                    request = recv_message(conn)
                    send_message(conn, envelope(
                        "ECHO_REPLY", remote_peer_id, remote_session_id,
                        request["body"], request["message_id"]))
            except Exception as error:
                errors.append(error)

        server = threading.Thread(target=respond, daemon=True)
        server.start()
        service = DiagnosticsService(emit)
        service.start()
        service.echo("127.0.0.1", listener.getsockname()[1],
                     remote_peer_id, remote_session_id)
        assert events.get(timeout=2)[0] == "diagnostic_started"
        kind, result = events.get(timeout=2)
        service.stop()
        assert service.join(2)
        server.join(2)
    assert not errors
    assert kind == "diagnostic_result"
    assert result.state == "compatible"
    assert result.rtt_ms is not None and 0 < result.rtt_ms < 60000
    assert result.duration_ms is not None and result.rtt_ms <= result.duration_ms
