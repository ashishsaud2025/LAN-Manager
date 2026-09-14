from __future__ import annotations

from queue import Empty
import socket
import threading
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
        assert service.events.get(timeout=1) == ("message", request)
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
        finally:
            sender.stop()
            responder.join(4)
            worker.join(4)
    assert not errors
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
