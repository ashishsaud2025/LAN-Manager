from __future__ import annotations

import socket
from unittest.mock import Mock, patch
from uuid import uuid4

from core.discovery import (
    HEADER, DiscoveryTransport, Hello, decode_hello, encode_hello,
    ipv6_supported, local_ipv6_addresses,
)

PEER = "00000000-0000-4000-8000-000000000001"
SESSION = "00000000-0000-4000-8000-000000000002"


def _records() -> list[tuple[int, int, int, str, tuple[str, int]]]:
    return [
        (23, 1, 0, "", ("::1", 0, 0, 0)),
        (23, 1, 0, "", ("ff02::1", 0, 0, 0)),
        (23, 1, 0, "", ("fe80::2", 0, 0, 3)),
        (23, 1, 0, "", ("2001:db8::9", 0, 0, 0)),
        (23, 1, 0, "", ("fe80::2", 0, 0, 3)),
    ]


def test_ipv6_enumeration_keeps_scoped_link_local() -> None:
    with patch("core.discovery.socket.gethostname", return_value="desktop"), \
            patch("core.discovery.socket.getaddrinfo", return_value=_records()), \
            patch("core.discovery.socket.if_indextoname",
                  side_effect=OSError("no name")):
        addresses = local_ipv6_addresses()
    assert "fe80::2%3" in addresses
    assert "2001:db8::9" in addresses
    assert not any(item.startswith("::1") or item.startswith("ff02")
                   for item in addresses)
    assert len(addresses) == len(set(addresses))


def test_ipv6_supported_reports_socket_availability() -> None:
    with patch("core.discovery.socket.socket",
               side_effect=OSError("no ipv6")):
        assert ipv6_supported() is False
    probe = Mock()
    with patch("core.discovery.socket.socket", return_value=probe):
        assert ipv6_supported() is True
    probe.close.assert_called_once()


def test_ipv6_capability_round_trip() -> None:
    hello = Hello(PEER, SESSION, "Alice", 50001, ("chat_v1", "ipv6_v1"))
    assert decode_hello(encode_hello(hello)) == hello


def test_scoped_announcement_uses_multicast_scope() -> None:
    receiver4, receiver6, sender6 = Mock(), Mock(), Mock()
    with patch("core.discovery.socket.socket",
               side_effect=[receiver4, receiver6, sender6]), \
            patch("core.discovery.local_ipv6_addresses", return_value=()):
        transport = DiscoveryTransport(
            SESSION, port=51234, source_addresses=("fe80::1%3",),
            include_fallback=False)
        assert transport.ipv6_available is True
        assert transport.senders == []
        transport.announce(Hello(PEER, SESSION, "Alice"))
        sender6.sendto.assert_called_once()
        packet, destination = sender6.sendto.call_args[0]
        assert packet.startswith(HEADER)
        assert destination == ("ff02::1", 51234, 0, 3)
        assert transport.bound_addresses() == ("fe80::1%3",)
        transport.close()
    receiver4.close.assert_called_once()
    receiver6.close.assert_called_once()
    sender6.close.assert_called_once()


def test_strict_ipv6_selection_sends_no_ipv4() -> None:
    receiver4, receiver6, sender6 = Mock(), Mock(), Mock()
    with patch("core.discovery.socket.socket",
               side_effect=[receiver4, receiver6, sender6]), \
            patch("core.discovery.local_ipv6_addresses", return_value=()):
        transport = DiscoveryTransport(
            SESSION, source_addresses=("2001:db8::7",),
            include_fallback=False)
        assert transport.senders == []
        assert len(transport._senders6) == 1
        transport.close()


def test_receive_v6_preserves_scope_context() -> None:
    receiver4 = Mock()
    receiver6 = Mock()
    fallback4 = Mock()
    other = encode_hello(Hello(PEER, str(uuid4()), "Other"))
    receiver6.recvfrom.return_value = (
        other, ("fe80::2", 50000, 0, 3))
    with patch("core.discovery.socket.socket",
               side_effect=[receiver4, receiver6]), \
            patch("core.discovery.local_ipv6_addresses", return_value=()), \
            patch("core.discovery.socket.if_indextoname",
                  side_effect=OSError("no name")):
        transport = DiscoveryTransport(
            SESSION, source_addresses=(), include_fallback=False,
            enable_ipv6=True)
        result = transport.receive_v6()
        assert result is not None
        assert result[0].name == "Other"
        assert result[1][0] == "fe80::2%3"
        assert result[1][1] == 50000
        assert transport.receivers == [receiver4, receiver6]
        transport.close()


def test_ipv6_unavailable_keeps_ipv4_baseline() -> None:
    receiver4 = Mock()
    calls: list[tuple[int, int]] = []

    def fake_socket(family: int, kind: int) -> Mock:
        calls.append((family, kind))
        if family == socket.AF_INET6:
            raise OSError("no ipv6 stack")
        return receiver4

    with patch("core.discovery.socket.socket", side_effect=fake_socket):
        transport = DiscoveryTransport(
            SESSION, source_addresses=(), enable_ipv6=True)
        assert transport.ipv6_available is False
        assert transport.receivers == [receiver4]
        assert transport.receive_v6() is None
        transport.close()
    assert (socket.AF_INET6, socket.SOCK_DGRAM) in calls
    receiver4.close.assert_called()


def test_each_ipv6_sender_uses_its_own_scope() -> None:
    receiver4, receiver6, first, second = Mock(), Mock(), Mock(), Mock()
    with patch("core.discovery.socket.socket",
               side_effect=[receiver4, receiver6, first, second]), \
            patch("core.discovery.local_ipv6_addresses", return_value=()):
        transport = DiscoveryTransport(
            SESSION, port=51235,
            source_addresses=("fe80::1%3", "fe80::2%5"),
            include_fallback=False)
        transport.announce(Hello(PEER, SESSION, "Alice"))
        destinations = sorted(
            call[0][1][3] for call in first.sendto.call_args_list
            + second.sendto.call_args_list)
        assert destinations == [3, 5]
        by_sender = {}
        for sender in (first, second):
            scope = sender.sendto.call_args[0][1][3]
            by_sender.setdefault(scope, 0)
            by_sender[scope] += 1
        assert sorted(by_sender) == [3, 5]
        transport.close()


def test_unknown_scope_name_is_skipped() -> None:
    receiver4, receiver6 = Mock(), Mock()
    with patch("core.discovery.socket.socket",
               side_effect=[receiver4, receiver6]), \
            patch("core.discovery.local_ipv6_addresses", return_value=()):
        transport = DiscoveryTransport(
            SESSION, source_addresses=("fe80::1%nosuchif0",),
            include_fallback=False)
        assert transport._senders6 == []
        assert transport.bound_addresses() == ()
        with __import__("pytest").raises(OSError):
            transport.announce(Hello(PEER, SESSION, "Alice"))
        transport.close()


def test_v6_only_session_disables_ipv4_actions(
        monkeypatch: object) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")  # type: ignore[union-attr]
    from PySide6.QtWidgets import QApplication
    from gui.main_window import MainWindow
    from core.chat import ChatService
    from core.discovery import Hello
    from core.peer_repository import PeerRecord
    from core.roster import EndpointCandidate
    from uuid import uuid4 as _uuid4

    app = QApplication.instance() or QApplication([])
    service = ChatService(Hello(str(_uuid4()), str(_uuid4()), "Local"))
    window = MainWindow(service)
    try:
        hello = Hello(str(_uuid4()), str(_uuid4()), "Remote",
                      50001, ("chat_v1", "file_v1", "ipv6_v1"))
        record = PeerRecord(
            hello, "fe80::9%4", 1.0,
            endpoint_candidates=(EndpointCandidate("fe80::9%4", 1.0),))
        window._update_peer_records((record,))
        window.peer_selection.select(record.session_id)
        assert window.peer_message_button.isEnabled() is False
        assert window.peer_file_button.isEnabled() is False
        assert "IPv6 discovery only" in window.peer_warning.text()
    finally:
        window.close()
        app.processEvents()


def test_dual_stack_close_releases_both_families() -> None:
    receiver4, receiver6, fallback4, sender6 = (
        Mock(), Mock(), Mock(), Mock())
    with patch("core.discovery.socket.socket",
               side_effect=[receiver4, receiver6, fallback4, sender6]), \
            patch("core.discovery.local_ipv6_addresses", return_value=()):
        transport = DiscoveryTransport(
            SESSION, source_addresses=("fe80::5%3",))
        transport.close()
        transport.close()
    for sock in (receiver4, receiver6, fallback4, sender6):
        sock.close.assert_called()
