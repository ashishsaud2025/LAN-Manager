from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from core.discovery import (
    HEADER, DiscoveryError, DiscoveryTransport, Hello, decode_hello, encode_hello,
)
from m1_discovery import load_identity, run

PEER = "00000000-0000-4000-8000-000000000001"
SESSION = "00000000-0000-4000-8000-000000000002"
GOLDEN = (b'LMAN\x01{"version":1,"peer_id":"00000000-0000-4000-8000-000000000001",'
          b'"session_id":"00000000-0000-4000-8000-000000000002",'
          b'"name":"Alice","tcp_port":50001,"capabilities":[]}')


def test_golden_packet() -> None:
    hello = Hello(PEER, SESSION, "Alice")
    assert encode_hello(hello) == GOLDEN
    assert decode_hello(GOLDEN) == hello


def test_unicode_and_optional_field() -> None:
    hello = Hello(PEER, SESSION, "नमस्ते")
    assert decode_hello(encode_hello(hello)) == hello
    value = json.loads(GOLDEN[5:])
    value["future"] = "ignored"
    assert decode_hello(HEADER + json.dumps(value).encode()).name == "Alice"


@pytest.mark.parametrize("packet", [b"", b"LMAN", b"LMAN\x02{}", HEADER + b"\xff",
                                    HEADER + b"{", HEADER + b"[]",
                                    HEADER + b"x" * 1200])
def test_bad_packets(packet: bytes) -> None:
    with pytest.raises(DiscoveryError):
        decode_hello(packet)


@pytest.mark.parametrize("key,value", [("version", True), ("version", 2),
                                      ("tcp_port", True), ("tcp_port", 0),
                                      ("tcp_port", 65536), ("name", ""),
                                      ("name", "\n"), ("peer_id", "bad"),
                                      ("capabilities", [3])])
def test_bad_fields(key: str, value: object) -> None:
    data = json.loads(GOLDEN[5:])
    data[key] = value
    with pytest.raises(DiscoveryError):
        decode_hello(HEADER + json.dumps(data).encode())


def test_transport_filtering_and_recovery() -> None:
    receiver, sender = Mock(), Mock()
    with patch("core.discovery.socket.socket", side_effect=[receiver, sender]):
        transport = DiscoveryTransport(SESSION)
        own = GOLDEN
        other = encode_hello(Hello(PEER, PEER, "Other"))
        receiver.recvfrom.side_effect = [(own, ("192.0.2.1", 1234)),
                                        (b"bad", ("192.0.2.1", 1234)),
                                        (other, ("192.0.2.1", 1234))]
        assert transport.receive() is None
        assert transport.receive() is None
        assert transport.receive()[0].name == "Other"
        transport.announce(Hello(PEER, SESSION, "Alice"))
        sender.sendto.assert_called_once_with(GOLDEN, ("255.255.255.255", 50000))
        receiver.bind.assert_called_once_with(("0.0.0.0", 50000))
        transport.close()
        receiver.close.assert_called_once()
        sender.close.assert_called_once()


def test_cleanup_after_bind_failure() -> None:
    receiver = Mock()
    receiver.bind.side_effect = OSError("occupied")
    with patch("core.discovery.socket.socket", return_value=receiver):
        with pytest.raises(OSError):
            DiscoveryTransport(SESSION)
    receiver.close.assert_called_once()


def test_persistent_identity(tmp_path: Path) -> None:
    path = tmp_path / "profile" / "identity"
    assert load_identity(path) == load_identity(path)
    path.write_text("invalid", encoding="ascii")
    with pytest.raises(ValueError):
        load_identity(path)


def test_announcements_continue_while_receiving() -> None:
    transport = Mock()
    transport.receive.return_value = None
    hello = Hello(PEER, SESSION, "Alice")
    with patch("m1_discovery.time.monotonic", side_effect=[0, 0, 0, 2, 2]), \
            patch("m1_discovery.select.select",
                  side_effect=[([transport.receiver], [], []), KeyboardInterrupt]):
        with pytest.raises(KeyboardInterrupt):
            run(hello, transport)
    assert transport.announce.call_count == 2
    transport.receive.assert_called_once()
