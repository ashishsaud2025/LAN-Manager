from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from queue import Empty
import socket
import ssl
import threading
import time
from typing import Any, Callable
from uuid import uuid4

import pytest

from core.chat import ChatService
from core.discovery import Hello
from core.identity import DeviceIdentity
from core.protocol import envelope, recv_message, send_message
from core.post_signatures import verify_post
from core.roster import Peer
from core.secure_transport import PairingCandidate, SecureTransport
from core.storage import JsonLinesPostStore, PostStore
from core.trust import TrustStore


@dataclass
class SecureNode:
    identity: DeviceIdentity
    trust: TrustStore
    hello: Hello
    transport: SecureTransport
    service: ChatService


def _free_port(sock_type: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, sock_type) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _node(tmp_path: Path, name: str,
          post_store: PostStore | None = None) -> SecureNode:
    peer_id = str(uuid4())
    identity = DeviceIdentity.load_or_create(
        tmp_path / f"{name}-{peer_id}.pem", peer_id)
    hello = Hello(
        peer_id, str(uuid4()), name, _free_port(),
        ("chat_v1", "file_v1", "posts_v1", "secure_transport_v1"),
        _free_port(), identity.fingerprint)
    trust = TrustStore(tmp_path / f"{name}-{peer_id}-trust.json")
    transport = SecureTransport(identity, trust, hello)
    service = ChatService(
        hello, discovery_port=_free_port(socket.SOCK_DGRAM),
        broadcast="127.0.0.1", post_store=post_store,
        secure_transport=transport)
    return SecureNode(identity, trust, hello, transport, service)


def _peer(node: SecureNode, hello: Hello | None = None) -> Peer:
    return Peer(hello or node.hello, "127.0.0.1", time.monotonic())


def _pair_direct(first: SecureNode, second: SecureNode) -> None:
    first.trust.pair(second.hello.peer_id,
                     second.identity.certificate_der, second.hello.name)
    second.trust.pair(first.hello.peer_id,
                      first.identity.certificate_der, first.hello.name)


def _wait_until(condition: Callable[[], bool], timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise TimeoutError("condition was not reached")


def _wait_listener(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.02)
    raise TimeoutError(f"listener {port} did not start")


def _event(service: ChatService, kind: str,
           timeout: float = 8.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event_kind, value = service.events.get(
                timeout=min(0.2, deadline - time.monotonic()))
        except Empty:
            continue
        if event_kind == kind:
            return value
    raise TimeoutError(f"{kind} event was not emitted")


def _transfer_event(service: ChatService, identifier: str, state: str,
                    timeout: float = 12.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = _event(service, "transfer", deadline - time.monotonic())
        if value["id"] == identifier and value["state"] == state:
            return value
    raise TimeoutError(f"transfer {identifier} did not reach {state}")


def _stop(*nodes: SecureNode) -> None:
    for node in nodes:
        node.service.stop()
    for node in nodes:
        assert node.service.join(10)


def test_pairing_queue_and_tls_chat_ack_record_authentication(
        tmp_path: Path) -> None:
    sender = _node(tmp_path, "Sender")
    receiver = _node(tmp_path, "Receiver")
    sender.service.start()
    receiver.service.start()
    try:
        _wait_listener(receiver.hello.secure_port)
        assert sender.service.request_pair(_peer(receiver))
        inbound = _event(receiver.service, "pair_request")
        outbound = _event(sender.service, "pair_outbound")
        assert isinstance(inbound, PairingCandidate)
        assert isinstance(outbound, PairingCandidate)
        assert inbound.comparison_code == outbound.comparison_code
        sender.service.accept_outbound_pair(outbound.request_id)
        receiver.service.accept_pair(inbound.request_id)

        identifier = sender.service.send(
            "authenticated hello", (_peer(receiver),), direct=True)
        _wait_until(lambda: (
            sender.service.message_journal.snapshot().entries[0]
            .deliveries[0].state == "accepted"))
        delivery = (sender.service.message_journal.snapshot().entries[0]
                    .deliveries[0])
        assert delivery.authenticated
        incoming = receiver.service.message_journal.snapshot().entries[0]
        assert incoming.message_id == identifier
        assert incoming.authenticated
        assert incoming.deliveries[0].authenticated
    finally:
        _stop(sender, receiver)


def test_paired_plaintext_downgrade_is_rejected(tmp_path: Path) -> None:
    sender = _node(tmp_path, "Sender")
    receiver = _node(tmp_path, "Receiver")
    receiver.trust.pair(sender.hello.peer_id,
                        sender.identity.certificate_der, sender.hello.name)
    worker = threading.Thread(
        target=receiver.service._receive_worker, daemon=True)
    worker.start()
    client, accepted = socket.socketpair()
    request = envelope(
        "CHAT", sender.hello.peer_id, sender.hello.session_id,
        {"scope": "room", "text": "plaintext downgrade"})
    try:
        receiver.service._incoming.put(accepted)
        send_message(client, request)
        try:
            assert recv_message(client, timeout=2) is None
        except OSError:
            pass
        status = _event(receiver.service, "status")
        assert "plaintext downgrade rejected" in status
        assert receiver.service.message_journal.snapshot().entries == ()
    finally:
        client.close()
        receiver.service.stop()
        worker.join(3)
    assert not worker.is_alive()


def test_secure_feed_page_uses_paired_channel(tmp_path: Path) -> None:
    holder_store = JsonLinesPostStore(tmp_path / "holder-posts.jsonl")
    reader_store = JsonLinesPostStore(tmp_path / "reader-posts.jsonl")
    holder = _node(tmp_path, "Holder", holder_store)
    reader = _node(tmp_path, "Reader", reader_store)
    _pair_direct(holder, reader)
    holder_store.add({
        "post_id": str(uuid4()), "author_id": holder.hello.peer_id,
        "text": "secure feed page", "created_ms": 1, "refs": [],
    })
    holder.service.start()
    reader.service.start()
    try:
        _wait_listener(holder.hello.secure_port)
        reader.service.sync_posts(_peer(holder))
        _wait_until(lambda: reader_store.count() == 1)
        assert reader_store.page(10)[0][0]["text"] == "secure feed page"
    finally:
        _stop(holder, reader)


def test_secure_service_signs_new_local_posts(tmp_path: Path) -> None:
    store = JsonLinesPostStore(tmp_path / "signed-posts.jsonl")
    node = _node(tmp_path, "Author", store)

    identifier = node.service.publish_post("signed locally")
    post = store.get(identifier)

    assert post is not None
    verification = verify_post(post)
    assert verification.state == "signed"
    assert verification.fingerprint == node.identity.fingerprint


def test_secure_file_transfer_owns_tls_socket_and_cancels(
        tmp_path: Path) -> None:
    sender = _node(tmp_path, "Sender")
    receiver = _node(tmp_path, "Receiver")
    _pair_direct(sender, receiver)
    source = tmp_path / "source.bin"
    source.write_bytes(b"protected payload" * 10000)
    destination = tmp_path / "received.bin"
    sender.service.start()
    receiver.service.start()
    try:
        _wait_listener(receiver.hello.secure_port)
        identifier = sender.service.transfers.send(source, _peer(receiver))
        offer = _event(receiver.service, "file_offer")
        assert offer["id"] == identifier
        assert offer["authenticated"] is True
        assert receiver.service.transfers.decide(identifier, destination)
        assert _transfer_event(sender.service, identifier, "verified")[
            "authenticated"] is True
        assert _transfer_event(receiver.service, identifier, "saved")[
            "authenticated"] is True
        assert destination.read_bytes() == source.read_bytes()

        cancelled = sender.service.transfers.send(source, _peer(receiver))
        pending = _event(receiver.service, "file_offer")
        assert pending["id"] == cancelled
        sender.service.transfers.cancel(cancelled)
        assert _transfer_event(sender.service, cancelled, "cancelled")[
            "authenticated"] is True
        failed = _transfer_event(receiver.service, cancelled, "failed")
        assert failed["authenticated"] is True
    finally:
        _stop(sender, receiver)


def test_unpaired_secure_service_keeps_legacy_plaintext_chat(
        tmp_path: Path) -> None:
    sender = _node(tmp_path, "Sender")
    receiver = Hello(str(uuid4()), str(uuid4()), "Legacy", _free_port(),
                     ("chat_v1",))
    errors: list[Exception] = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", receiver.tcp_port))
        listener.listen(1)
        listener.settimeout(3)

        def respond() -> None:
            try:
                conn, _ = listener.accept()
                with conn:
                    request = recv_message(conn)
                    send_message(conn, envelope(
                        "ACK", receiver.peer_id, receiver.session_id,
                        {"status": "accepted"}, request["message_id"]))
            except Exception as error:
                errors.append(error)

        responder = threading.Thread(target=respond, daemon=True)
        worker = threading.Thread(
            target=sender.service._send_worker, daemon=True)
        responder.start()
        worker.start()
        sender.service.send(
            "legacy", (Peer(receiver, "127.0.0.1", 0),), direct=True)
        _wait_until(lambda: (
            sender.service.message_journal.snapshot().entries[0]
            .deliveries[0].state == "accepted"))
        delivery = (sender.service.message_journal.snapshot().entries[0]
                    .deliveries[0])
        assert not delivery.authenticated
        sender.service.stop()
        responder.join(4)
        worker.join(4)
    assert not errors
    assert not worker.is_alive()


def test_paired_wrong_certificate_never_falls_back_to_plaintext(
        tmp_path: Path) -> None:
    sender = _node(tmp_path, "Sender")
    target = _node(tmp_path, "Target")
    sender.trust.pair(target.hello.peer_id,
                      target.identity.certificate_der, target.hello.name)
    replacement_identity = DeviceIdentity.load_or_create(
        tmp_path / "replacement.pem", target.hello.peer_id)
    replacement_hello = Hello(
        target.hello.peer_id, target.hello.session_id, target.hello.name,
        target.hello.tcp_port, target.hello.capabilities,
        target.hello.secure_port, replacement_identity.fingerprint)
    replacement_transport = SecureTransport(
        replacement_identity,
        TrustStore(tmp_path / "replacement-trust.json"), replacement_hello)
    secure_errors: list[Exception] = []
    with (socket.socket() as plaintext_listener,
          socket.socket() as secure_listener):
        plaintext_listener.bind(("127.0.0.1", target.hello.tcp_port))
        plaintext_listener.listen(1)
        plaintext_listener.settimeout(0.5)
        secure_listener.bind(("127.0.0.1", target.hello.secure_port))
        secure_listener.listen(1)
        secure_listener.settimeout(3)

        def serve_wrong_certificate() -> None:
            try:
                conn, _ = secure_listener.accept()
                replacement_transport.accept(conn)
            except Exception as error:
                secure_errors.append(error)

        secure_worker = threading.Thread(
            target=serve_wrong_certificate, daemon=True)
        worker = threading.Thread(
            target=sender.service._send_worker, daemon=True)
        secure_worker.start()
        worker.start()
        sender.service.send(
            "must not downgrade", (_peer(target),), direct=True)
        _wait_until(lambda: (
            sender.service.message_journal.snapshot().entries[0]
            .deliveries[0].state == "failed"))
        with pytest.raises(TimeoutError):
            plaintext_listener.accept()
        delivery = (sender.service.message_journal.snapshot().entries[0]
                    .deliveries[0])
        assert not delivery.authenticated
        sender.service.stop()
        worker.join(3)
        secure_worker.join(4)
    assert secure_errors
    assert not worker.is_alive()
    assert not secure_worker.is_alive()


def test_secure_listener_workers_stop_and_release_port(tmp_path: Path) -> None:
    node = _node(tmp_path, "Node")
    node.service.start()
    secure_port = node.hello.secure_port
    assert secure_port is not None
    _wait_listener(secure_port)
    secure_workers = [
        thread for thread in node.service._threads
        if thread._target == node.service._secure_receive_worker
    ]
    assert len(secure_workers) == 2
    _stop(node)
    with socket.socket() as rebound:
        rebound.bind(("127.0.0.1", secure_port))


def test_stop_interrupts_incomplete_tls_handshake(tmp_path: Path) -> None:
    node = _node(tmp_path, "Node")
    node.service.start()
    secure_port = node.hello.secure_port
    assert secure_port is not None
    raw = socket.create_connection(("127.0.0.1", secure_port), timeout=2)
    try:
        _wait_until(lambda: any(
            isinstance(conn, ssl.SSLSocket)
            for conn in tuple(node.service._active)))
        node.service.stop()
        assert node.service.join(2)
    finally:
        raw.close()
