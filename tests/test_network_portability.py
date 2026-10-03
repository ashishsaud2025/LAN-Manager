from __future__ import annotations

import socket
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

from core.chat import ChatService
from core.discovery import DiscoveryTransport, Hello, encode_hello
from core.roster import Peer, PeerRoster, candidate_ips
from core.peer_repository import PeerRepository
from core.diagnostics import ProbeResult


def _hello(name: str = "Peer") -> Hello:
    return Hello(str(uuid4()), str(uuid4()), name)


def test_strict_selection_omits_fallback_sender() -> None:
    receiver, wifi = Mock(), Mock()
    with patch("core.discovery.socket.socket", side_effect=[receiver, wifi]):
        transport = DiscoveryTransport(
            "self", source_addresses=("192.168.1.65",), include_fallback=False,
            enable_ipv6=False)
        assert len(transport.senders) == 1
        transport.announce(_hello())
        wifi.sendto.assert_called_once()
        transport.close()
    receiver.close.assert_called_once()
    wifi.close.assert_called_once()


def test_refresh_reuses_unchanged_senders() -> None:
    receiver, fallback, first, second, third = (
        Mock(), Mock(), Mock(), Mock(), Mock())
    with patch("core.discovery.socket.socket",
               side_effect=[receiver, fallback, first, second, third]):
        transport = DiscoveryTransport(
            "self", source_addresses=("192.168.1.10", "192.168.1.11"),
            enable_ipv6=False)
        assert transport.refresh_senders(
            ("192.168.1.11", "192.168.1.12"), True) is True
        assert set(transport.bound_addresses()) == {
            "192.168.1.11", "192.168.1.12"}
        assert transport.refresh_senders(
            ("192.168.1.11", "192.168.1.12"), True) is False
        transport.close()


def test_unavailable_selection_does_not_leak_through_fallback() -> None:
    receiver = Mock()
    failing = Mock()
    failing.bind.side_effect = OSError("down")
    with patch("core.discovery.socket.socket",
               side_effect=[receiver, failing]):
        transport = DiscoveryTransport(
            "self", source_addresses=("192.168.1.99",),
            include_fallback=False, enable_ipv6=False)
        assert transport.senders == []
        with pytest.raises(OSError):
            transport.announce(_hello())
        transport.close()


def test_roster_retains_two_addresses_for_one_session() -> None:
    roster = PeerRoster("self")
    hello = Hello("installation", "session", "Alice")
    roster.update(hello, "192.168.1.10", 0)
    roster.update(hello, "192.168.1.11", 1)
    peer = roster.snapshot()[0]
    assert peer.ip == "192.168.1.11"
    assert {item.ip for item in peer.endpoint_candidates} == {
        "192.168.1.10", "192.168.1.11"}
    assert candidate_ips(peer) == ("192.168.1.11", "192.168.1.10")


def test_roster_expires_one_candidate_keeps_session() -> None:
    roster = PeerRoster("self", timeout=6.0)
    hello = Hello("installation", "session", "Alice")
    roster.update(hello, "192.168.1.10", 0)
    roster.update(hello, "192.168.1.11", 5)
    assert roster.expire(6.5) == ()
    peer = roster.snapshot()[0]
    assert peer.ip == "192.168.1.11"
    assert [item.ip for item in peer.endpoint_candidates] == ["192.168.1.11"]
    assert roster.expire(11.5)[0].hello.session_id == "session"


def test_repository_accepts_probe_for_alternate_candidate() -> None:
    repository = PeerRepository()
    hello = Hello(str(uuid4()), str(uuid4()), "Alice")
    roster = PeerRoster("other")
    roster.update(hello, "192.168.1.10", 0)
    roster.update(hello, "192.168.1.11", 1)
    snapshot = roster.snapshot()
    assert snapshot[0].ip == "192.168.1.11"
    assert repository.reconcile_presence(snapshot, 1) is not None
    repository.register_probe("one", hello.session_id, "192.168.1.10",
                              hello.tcp_port, "tcp")
    result = ProbeResult("one", "tcp", "192.168.1.10", hello.tcp_port,
                         "reachable", rtt_ms=2.0)
    assert repository.apply_probe_result(result) is not None


def test_chat_falls_back_to_second_candidate() -> None:
    service = ChatService(Hello(str(uuid4()), str(uuid4()), "Local"))
    target = Hello(str(uuid4()), str(uuid4()), "Remote")
    roster = PeerRoster("other")
    roster.update(target, "192.168.99.1", 0)
    roster.update(target, "192.168.99.2", 1)
    multi = roster.snapshot()[0]
    assert multi.ip == "192.168.99.2"
    calls: list[str] = []

    def fake_connect(address: tuple[str, int], timeout: float = 3) -> Mock:
        calls.append(address[0])
        if address[0] == "192.168.99.2":
            raise OSError("unreachable")
        return Mock()

    with patch("core.chat.socket.create_connection", side_effect=fake_connect):
        conn, authenticated = service._connect_peer(multi)
        assert authenticated is False
        conn.close()
    assert calls == ["192.168.99.2", "192.168.99.1"]


def test_discovery_selection_updates_without_sockets() -> None:
    service = ChatService(Hello(str(uuid4()), str(uuid4()), "Local"))
    assert service.discovery_selection() == (None, True, 0)
    service.set_discovery_source_addresses(("192.168.1.65",), False)
    assert service.discovery_selection() == (("192.168.1.65",), False, 1)
    service.set_discovery_source_addresses(("192.168.1.65",), False)
    assert service.discovery_selection()[2] == 1
    service.set_discovery_source_addresses(None, True)
    assert service.discovery_selection() == (None, True, 2)


def test_wire_bytes_unchanged_by_egress_selection() -> None:
    hello = Hello("00000000-0000-4000-8000-000000000001",
                  "00000000-0000-4000-8000-000000000002", "Alice")
    assert encode_hello(hello).startswith(b"LMAN\x01")


def test_loopback_presence_keeps_one_session_across_addresses() -> None:
    service = ChatService(Hello(str(uuid4()), str(uuid4()), "Local"))
    hello = Hello(str(uuid4()), str(uuid4()), "Remote")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        first = sock.getsockname()[1]
        assert isinstance(first, int)
    assert service.peer_repository.snapshot() == ()


def test_desktop_discovery_controls_apply_live(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    service = ChatService(Hello(str(uuid4()), str(uuid4()), "Local"))
    window = MainWindow(service)
    try:
        window._refresh_discovery_addresses()
        assert window.discovery_address.count() >= 1
        window.discovery_address.setCurrentIndex(0)
        window.discovery_fallback.setChecked(True)
        window._apply_discovery_selection()
        assert service.discovery_selection()[0] is None
        assert service.discovery_selection()[1] is True
        service.set_discovery_source_addresses(("192.168.1.65",), False)
        window._refresh_discovery_addresses()
        assert window.discovery_address.currentData() == "192.168.1.65"
        assert window.discovery_fallback.isChecked() is False
        assert "all interfaces" in window.discovery_status.text()
    finally:
        try:
            window.settings.setValue("network/discovery_address", "auto")
            window.settings.setValue(
                "network/discovery_include_fallback", True)
        except (ValueError, OSError, RuntimeError):
            pass
        window.close()
        app.processEvents()


def test_desktop_shows_alternate_endpoints(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from gui.main_window import MainWindow
    from gui.models import endpoint_label
    from core.peer_repository import PeerRecord
    from core.roster import EndpointCandidate

    app = QApplication.instance() or QApplication([])
    service = ChatService(Hello(str(uuid4()), str(uuid4()), "Local"))
    window = MainWindow(service)
    try:
        hello = Hello(str(uuid4()), str(uuid4()), "Remote")
        roster = PeerRoster("other")
        roster.update(hello, "192.168.1.10", 0)
        roster.update(hello, "192.168.1.11", 1)
        peer = roster.snapshot()[0]
        record = PeerRecord(
            peer.hello, peer.ip, peer.last_seen,
            endpoint_candidates=peer.endpoint_candidates)
        assert endpoint_label(record) == (
            f"{record.ip}:{record.hello.tcp_port} +1")
        window._update_peer_records((record,))
        assert "+1" in window.peer_table_model.data(
            window.peer_table_model.index(0, 1))
        window.device_search.setText("192.168.1.10")
        window._filter_devices("192.168.1.10")
        assert window.peer_list.isRowHidden(0) is False
        hello_rows = [device for device in window.admin_device_model.devices
                      if device.source == "LAN Atlas HELLO"]
        assert {device.address for device in hello_rows} == {
            "192.168.1.10", "192.168.1.11"}
        assert all(device.session_id == hello.session_id
                   for device in hello_rows)
    finally:
        window.close()
        app.processEvents()
