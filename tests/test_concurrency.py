from __future__ import annotations

import hashlib
import socket
import threading
import time
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any
from uuid import uuid4

import pytest

from core.chat import ChatService
from core.discovery import Hello
from core.protocol import envelope
from core.transfer import TransferError, TransferService

EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _hello(tcp_port: int) -> Hello:
    return Hello(str(uuid4()), str(uuid4()), "Probe", tcp_port, ("chat_v1", "file_v1"))


def test_start_stop_join_leaves_no_service_thread() -> None:
    service = ChatService(_hello(_free_port()), discovery_port=_free_port())
    service.start()
    service.stop()
    assert service.join(10)
    assert all(thread not in threading.enumerate() for thread in service._threads)


def test_inbound_queue_bound_closes_excess() -> None:
    service = ChatService(_hello(_free_port()))
    held: list[socket.socket] = []
    try:
        for _ in range(16):
            client, server = socket.socketpair()
            held.append(client)
            service._incoming.put_nowait(server)
        assert service._incoming.qsize() <= 16
        extra_client, extra_server = socket.socketpair()
        held.append(extra_client)
        with pytest.raises(Full):
            service._incoming.put_nowait(extra_server)
        extra_server.close()
        assert extra_server.fileno() == -1
        assert service._incoming.qsize() <= 16
    finally:
        while True:
            try:
                service._incoming.get_nowait().close()
            except Empty:
                break
        for sock in held:
            sock.close()


def _drain(events: Queue[tuple[str, Any]], timeout: float) -> tuple[str, Any]:
    return events.get(timeout=timeout)


def test_stop_mid_transfer_cleans_up(tmp_path: Path) -> None:
    receiver_hello = Hello(str(uuid4()), str(uuid4()), "Receiver", 50001, ("file_v1",))
    events: Queue[tuple[str, Any]] = Queue()

    def emit(kind: str, value: Any) -> bool:
        events.put((kind, value))
        return True

    service = TransferService(receiver_hello, emit)
    sender, session, identifier = str(uuid4()), str(uuid4()), str(uuid4())
    client, server = socket.socketpair()
    target = tmp_path / "received.bin"
    offer = envelope("FILE_OFFER", sender, session,
                     {"transfer_id": identifier, "name": "data.bin", "size": 104857600,
                      "sha256": hashlib.sha256(b"abc").hexdigest(),
                      "to_session": receiver_hello.session_id})
    try:
        service.receive(server, offer)
        assert _drain(events, 2)[0] == "file_offer"
        assert service.decide(identifier, target)
        service.stop()
        assert service.join(5)
        state = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            kind, value = _drain(events, 5)
            if kind == "transfer" and value["id"] == identifier \
                    and value["state"] in {"failed", "cancelled"}:
                state = value["state"]
                break
        assert state in {"failed", "cancelled"}
        assert not target.exists()
        assert list(tmp_path.glob(".lman-*.part")) == []
    finally:
        client.close()
        service.stop()
        assert service.join(5)


def test_transfer_limit_rejected() -> None:
    service = TransferService(_hello(50001), lambda kind, value: True)
    pairs = [socket.socketpair() for _ in range(4)]
    try:
        for _, server in pairs:
            offer = envelope("FILE_OFFER", str(uuid4()), str(uuid4()),
                             {"transfer_id": str(uuid4()), "name": "a.bin", "size": 0,
                              "sha256": EMPTY_SHA, "to_session": service.hello.session_id})
            service.receive(server, offer)
        extra_client, extra_server = socket.socketpair()
        try:
            with pytest.raises(TransferError, match="limit"):
                service.receive(extra_server, envelope(
                    "FILE_OFFER", str(uuid4()), str(uuid4()),
                    {"transfer_id": str(uuid4()), "name": "b.bin", "size": 0,
                     "sha256": EMPTY_SHA, "to_session": service.hello.session_id}))
        finally:
            extra_client.close()
            extra_server.close()
    finally:
        for client, _ in pairs:
            client.close()
        service.stop()
        assert service.join(5)


def test_listener_released_after_stop() -> None:
    tcp_port, udp_port = _free_port(), _free_port()
    service = ChatService(_hello(tcp_port), discovery_port=udp_port)
    service.start()
    service.stop()
    assert service.join(10)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("0.0.0.0", tcp_port))
    finally:
        probe.close()


class _SignalSocket(socket.socket):
    """Report the first blocked read so the test knows the worker is inside recv."""

    entered: threading.Event | None = None

    def recv(self, *args: object, **kwargs: object) -> bytes:
        if _SignalSocket.entered is not None:
            _SignalSocket.entered.set()
        return super().recv(*args, **kwargs)  # type: ignore[arg-type]


def test_blocked_receive_woken_by_shutdown() -> None:
    service = ChatService(_hello(_free_port()))
    entered = threading.Event()
    _SignalSocket.entered = entered
    worker = threading.Thread(target=service._receive_worker, daemon=True)
    worker.start()
    client, raw_server = socket.socketpair()
    server = _SignalSocket(fileno=raw_server.detach())
    try:
        service._incoming.put(server)
        assert entered.wait(3)
        service.stop()
        worker.join(3)
        assert not worker.is_alive()
    finally:
        client.close()
        try:
            server.close()
        except OSError:
            pass
        service.stop()
        worker.join(3)
