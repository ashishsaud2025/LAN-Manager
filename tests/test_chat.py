from __future__ import annotations

from queue import Empty
import socket
import threading
import time
from uuid import uuid4

import pytest

from core.chat import ChatService
from core.discovery import Hello
from core.protocol import envelope, recv_message, send_message
from core.roster import Peer


def test_inbound_ack_dedup_and_shutdown() -> None:
    hello = Hello(str(uuid4()), str(uuid4()), "Receiver")
    service = ChatService(hello)
    worker = threading.Thread(target=service._receive_worker, daemon=True)
    worker.start()
    request = envelope("CHAT", str(uuid4()), str(uuid4()),
                       {"scope": "dm", "to_session": hello.session_id, "text": "hi"})
    try:
        for _ in range(2):
            client, server = socket.socketpair()
            service._incoming.put(server)
            with client:
                send_message(client, request)
                reply = recv_message(client)
                assert reply["reply_to"] == request["message_id"]
                assert reply["body"]["status"] == "accepted"
        entries = service.message_journal.snapshot().entries
        assert len(entries) == 1
        assert entries[0].message_id == request["message_id"]
        assert entries[0].direction == "incoming"
        with pytest.raises(Empty):
            service.events.get_nowait()
    finally:
        service.stop()
        worker.join(2)
    assert not worker.is_alive()


def test_outbound_tcp_acceptance() -> None:
    sender = ChatService(Hello(str(uuid4()), str(uuid4()), "Sender"))
    receiver_hello = Hello(str(uuid4()), str(uuid4()), "Receiver")
    errors = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)

        def respond() -> None:
            try:
                conn, _ = listener.accept()
                with conn:
                    request = recv_message(conn)
                    send_message(conn, envelope("ACK", receiver_hello.peer_id,
                                                receiver_hello.session_id,
                                                {"status": "accepted"}, request["message_id"]))
            except Exception as error:
                errors.append(error)

        responder = threading.Thread(target=respond, daemon=True)
        worker = threading.Thread(target=sender._send_worker, daemon=True)
        responder.start()
        worker.start()
        try:
            peer = Peer(Hello(receiver_hello.peer_id, receiver_hello.session_id,
                              "Receiver", listener.getsockname()[1]), "127.0.0.1", 0)
            identifier = sender.send("hello", (peer,), direct=True)
            kind, text = sender.events.get(timeout=4)
            assert kind == "status" and text.startswith(f"Accepted {identifier}")
            record = sender.message_journal.snapshot().entries[0]
            assert record.deliveries[0].state == "accepted"
            assert record.deliveries[0].detail == "accepted by receiving application"
        finally:
            sender.stop()
            responder.join(4)
            worker.join(4)
    assert not errors
    assert not worker.is_alive()


def test_full_ui_event_queue_does_not_block_incoming_acceptance() -> None:
    hello = Hello(str(uuid4()), str(uuid4()), "Receiver")
    service = ChatService(hello)
    for index in range(service.events.maxsize):
        service.events.put_nowait(("status", str(index)))
    worker = threading.Thread(target=service._receive_worker, daemon=True)
    worker.start()
    request = envelope("CHAT", str(uuid4()), str(uuid4()),
                       {"scope": "room", "text": "retained in core"})
    client, server = socket.socketpair()
    try:
        service._incoming.put(server)
        with client:
            send_message(client, request)
            reply = recv_message(client)
        assert reply["body"]["status"] == "accepted"
        assert service.message_journal.snapshot().entries[0].text == "retained in core"
    finally:
        service.stop()
        worker.join(2)
    assert not worker.is_alive()


def test_repository_revision_retries_after_ui_queue_saturation() -> None:
    service = ChatService(Hello(str(uuid4()), str(uuid4()), "Local"))
    for index in range(service.events.maxsize):
        service.events.put_nowait(("status", str(index)))
    peer = Peer(Hello(str(uuid4()), str(uuid4()), "Remote"),
                "192.168.1.20", time.monotonic())
    event = service.peer_repository.reconcile_presence(
        (peer,), time.monotonic())
    assert event is not None

    service._queue_repository_event(event)
    assert service._pending_repository_event is event
    service.events.get_nowait()
    service.flush_repository_events()

    assert service._pending_repository_event is None
    queued = tuple(service.events.queue)
    assert ("peer_repository", event) in queued


def test_socket_registered_after_stop_is_closed_immediately() -> None:
    service = ChatService(Hello(str(uuid4()), str(uuid4()), "Local"))
    client, server = socket.socketpair()
    try:
        service.stop()
        service._track(server, True)

        assert server.fileno() == -1
        assert server not in service._active
    finally:
        client.close()
        server.close()


def test_missing_ack_is_uncertain_after_transmission() -> None:
    sender = ChatService(Hello(str(uuid4()), str(uuid4()), "Sender"))
    receiver = Hello(str(uuid4()), str(uuid4()), "Receiver")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)

        def close_without_ack() -> None:
            conn, _ = listener.accept()
            with conn:
                assert recv_message(conn)["type"] == "CHAT"

        responder = threading.Thread(target=close_without_ack, daemon=True)
        worker = threading.Thread(target=sender._send_worker, daemon=True)
        responder.start()
        worker.start()
        peer = Peer(Hello(receiver.peer_id, receiver.session_id, receiver.name,
                          listener.getsockname()[1]), "127.0.0.1", 0)
        sender.send("hello", (peer,), direct=True)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            state = sender.message_journal.snapshot().entries[0].deliveries[0].state
            if state != "queued":
                break
            time.sleep(0.01)
        sender.stop()
        responder.join(4)
        worker.join(4)
    assert state == "uncertain"
    assert not worker.is_alive()


def test_gui_plain_text_and_queue_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    service = ChatService(Hello(str(uuid4()), str(uuid4()), "Alice"))
    window = MainWindow(service)
    window.append("<b>literal text</b>")
    assert "<b>literal text</b>" in window.log.toPlainText()
    service.events.put(("status", "ready"))
    window.drain()
    assert "ready" in window.log.toPlainText()
    window.close()
    app.processEvents()
    assert service._stop.is_set()
