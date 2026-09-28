from __future__ import annotations

import socket
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Self
from uuid import uuid4

import pytest

from core.discovery import Hello
from core.identity import DeviceIdentity
from core.protocol import recv_message, send_message
from core.roster import Peer
from core.secure_transport import (
    SecureChannel,
    SecureTransport,
    SecureTransportError,
)
from core.trust import KeyChangedError, TrustStore


@dataclass
class Node:
    identity: DeviceIdentity
    trust: TrustStore
    transport: SecureTransport
    hello: Hello


def _node(tmp_path: Path, name: str, port: int,
          peer_id: str | None = None, **transport_options: object) -> Node:
    identity = DeviceIdentity.load_or_create(
        tmp_path / f"{name}-{uuid4()}.pem", peer_id or str(uuid4()))
    hello = Hello(
        identity.peer_id, str(uuid4()), name, capabilities=(
            "secure_transport_v1",), secure_port=port,
        certificate_sha256=identity.fingerprint)
    trust = TrustStore(tmp_path / f"{name}-{uuid4()}-trust.json")
    transport = SecureTransport(identity, trust, hello, **transport_options)
    return Node(identity, trust, transport, hello)


def _peer(node: Node, *, fingerprint: str | None = None,
          peer_id: str | None = None) -> Peer:
    hello = Hello(
        peer_id or node.hello.peer_id, node.hello.session_id, node.hello.name,
        capabilities=node.hello.capabilities, secure_port=node.hello.secure_port,
        certificate_sha256=fingerprint or node.hello.certificate_sha256)
    return Peer(hello, "127.0.0.1", time.monotonic())


class OneShotServer:
    def __init__(self, transport: SecureTransport) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(3)
        self.port = self.listener.getsockname()[1]
        self.transport = transport
        self.result: SecureChannel | None = None
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.thread.join(4)
        self.listener.close()
        assert not self.thread.is_alive()

    def _run(self) -> None:
        try:
            raw_socket, _ = self.listener.accept()
            self.result = self.transport.accept(raw_socket)
        except (OSError, SecureTransportError) as error:
            self.error = error


def _serve(node: Node) -> OneShotServer:
    server = OneShotServer(node.transport)
    node.hello = Hello(
        node.hello.peer_id, node.hello.session_id, node.hello.name,
        capabilities=node.hello.capabilities, secure_port=server.port,
        certificate_sha256=node.hello.certificate_sha256)
    node.transport.local_hello = node.hello
    return server


def _pair(client: Node, server_node: Node) -> None:
    with _serve(server_node) as server:
        local_candidate = client.transport.request_pair(_peer(server_node))
    assert server.error is None
    remote_candidates = server_node.transport.pending()
    assert len(remote_candidates) == 1
    remote_candidate = remote_candidates[0]
    assert remote_candidate.comparison_code == local_candidate.comparison_code
    assert remote_candidate.peer_id == client.identity.peer_id
    assert local_candidate.peer_id == server_node.identity.peer_id
    assert client.trust.get(server_node.identity.peer_id) is None
    client.transport.accept_outbound_pair(local_candidate.request_id)
    server_node.transport.accept_pair(remote_candidate.request_id)


def _connect(client: Node, server_node: Node) -> tuple[SecureChannel,
                                                       SecureChannel]:
    with _serve(server_node) as server:
        client_channel = client.transport.connect(_peer(server_node))
    assert server.error is None
    assert server.result is not None
    return client_channel, server.result


def test_bilateral_pairing_then_authenticated_channels_both_directions(
        tmp_path: Path) -> None:
    first = _node(tmp_path, "First", 1)
    second = _node(tmp_path, "Second", 1)

    _pair(first, second)
    first_to_second, accepted_first = _connect(first, second)
    second_to_first, accepted_second = _connect(second, first)
    try:
        assert isinstance(first_to_second, SecureChannel)
        assert isinstance(first_to_second.socket, ssl.SSLSocket)
        assert first_to_second.encrypted
        assert first_to_second.peer_id == second.identity.peer_id
        assert accepted_first.peer_id == first.identity.peer_id
        assert second_to_first.peer_id == first.identity.peer_id
        assert accepted_second.peer_id == second.identity.peer_id
        send_message(first_to_second.socket, {"protected": "hello"})
        assert recv_message(accepted_first.socket) == {"protected": "hello"}
    finally:
        for channel in (first_to_second, accepted_first,
                        second_to_first, accepted_second):
            channel.close()


def test_connect_rejects_unpaired_peer_before_opening_socket(
        tmp_path: Path) -> None:
    client = _node(tmp_path, "Client", 1)
    target = _node(tmp_path, "Target", 65534)

    with pytest.raises(SecureTransportError, match="not paired"):
        client.transport.connect(_peer(target))


def test_accept_rejects_authenticated_peer_without_server_trust(
        tmp_path: Path) -> None:
    client = _node(tmp_path, "Client", 1)
    target = _node(tmp_path, "Target", 1)
    client.trust.pair(
        target.identity.peer_id, target.identity.certificate_der, "Target")

    with _serve(target) as server, pytest.raises(SecureTransportError):
        client.transport.connect(_peer(target))
    assert isinstance(server.error, SecureTransportError)
    assert server.result is None


def test_pairing_rejects_advertised_fingerprint_mismatch(
        tmp_path: Path) -> None:
    client = _node(tmp_path, "Client", 1)
    target = _node(tmp_path, "Target", 1)

    with _serve(target) as server, pytest.raises(
            SecureTransportError, match="advertisement"):
        client.transport.request_pair(_peer(target, fingerprint="0" * 64))
    assert isinstance(server.error, SecureTransportError)
    assert client.trust.get(target.identity.peer_id) is None
    assert target.transport.pending() == ()


def test_pairing_binds_certificate_to_selected_peer_id(tmp_path: Path) -> None:
    client = _node(tmp_path, "Client", 1)
    target = _node(tmp_path, "Target", 1)

    with _serve(target) as server, pytest.raises(
            SecureTransportError, match="common name"):
        client.transport.request_pair(
            _peer(target, peer_id=str(uuid4())))
    assert isinstance(server.error, SecureTransportError)
    assert client.trust.snapshot().records == ()


def test_key_change_never_overwrites_existing_trust(tmp_path: Path) -> None:
    peer_id = str(uuid4())
    client = _node(tmp_path, "Client", 1)
    original = _node(tmp_path, "Original", 1, peer_id=peer_id)
    replacement = _node(tmp_path, "Replacement", 1, peer_id=peer_id)
    original_record = client.trust.pair(
        peer_id, original.identity.certificate_der, "Original")

    with pytest.raises(KeyChangedError):
        client.transport.request_pair(_peer(replacement))
    assert client.trust.get(peer_id) == original_record
    assert replacement.transport.pending() == ()


def test_pending_limit_decline_and_expiry(tmp_path: Path) -> None:
    server_node = _node(
        tmp_path, "Server", 1, max_pending=1, pairing_ttl=0.08)
    first = _node(tmp_path, "First", 1)
    second = _node(tmp_path, "Second", 1)

    with _serve(server_node) as server:
        first.transport.request_pair(_peer(server_node))
    assert server.error is None
    first_pending = server_node.transport.pending()[0]
    with _serve(server_node) as server, pytest.raises(SecureTransportError):
        second.transport.request_pair(_peer(server_node))
    assert isinstance(server.error, SecureTransportError)
    assert server_node.transport.decline_pair(first_pending.request_id) \
        == first_pending

    with _serve(server_node) as server:
        second.transport.request_pair(_peer(server_node))
    assert server.error is None
    time.sleep(0.1)
    assert server_node.transport.pending() == ()


def test_outbound_pairing_requires_local_approval(tmp_path: Path) -> None:
    client = _node(tmp_path, "Client", 1)
    target = _node(tmp_path, "Target", 1)

    with _serve(target) as server:
        candidate = client.transport.request_pair(_peer(target))

    assert server.error is None
    assert client.trust.get(target.identity.peer_id) is None
    assert client.transport.decline_outbound_pair(candidate.request_id) == candidate
    assert client.trust.get(target.identity.peer_id) is None


def test_accept_rejects_bad_pairing_signature(tmp_path: Path) -> None:
    attacker = _node(tmp_path, "Attacker", 1)
    server_node = _node(tmp_path, "Server", 1)

    original_sign = attacker.identity.sign
    attacker.identity.sign = (  # type: ignore[method-assign]
        lambda data: original_sign(data)[:-1] + b"x")
    with _serve(server_node) as server, pytest.raises(SecureTransportError):
        attacker.transport.request_pair(_peer(server_node))
    assert isinstance(server.error, SecureTransportError)
    assert server_node.transport.pending() == ()
    assert attacker.trust.get(server_node.identity.peer_id) is None
