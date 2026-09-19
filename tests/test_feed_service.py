from __future__ import annotations

import socket
import threading
from pathlib import Path
from uuid import uuid4

from core.chat import ChatService
from core.discovery import Hello
from core.feed import serve_query
from core.protocol import envelope, recv_message, send_message, validate_envelope
from core.roster import Peer
from core.storage import JsonLinesPostStore


def _hello(name: str, port: int = 50001) -> Hello:
    return Hello(str(uuid4()), str(uuid4()), name, port, ("posts_v1",))


def _post(author: str, text: str) -> dict[str, object]:
    return {"post_id": str(uuid4()), "author_id": author, "text": text,
            "created_ms": 1, "refs": []}


def test_listener_serves_correlated_post_page(tmp_path: Path) -> None:
    hello = _hello("Holder")
    store = JsonLinesPostStore(tmp_path / "holder.jsonl")
    store.add(_post(hello.peer_id, "cached"))
    service = ChatService(hello, post_store=store)
    worker = threading.Thread(target=service._receive_worker, daemon=True)
    worker.start()
    client, server = socket.socketpair()
    request = envelope("POST_QUERY", str(uuid4()), str(uuid4()),
                       {"cursor": None, "limit": 10, "author_id": None})
    try:
        service._incoming.put(server)
        with client:
            send_message(client, request)
            reply = recv_message(client)
        assert reply is not None
        validate_envelope(reply)
        assert reply["type"] == "POST_PAGE"
        assert reply["reply_to"] == request["message_id"]
        assert reply["body"]["posts"][0]["text"] == "cached"
    finally:
        service.stop()
        worker.join(2)
    assert not worker.is_alive()


def test_sync_peer_pages_and_deduplicates(tmp_path: Path) -> None:
    holder = _hello("Holder")
    holder_store = JsonLinesPostStore(tmp_path / "holder.jsonl")
    for index in range(3):
        holder_store.add(_post(holder.peer_id, f"post {index}"))
    reader = _hello("Reader")
    reader_store = JsonLinesPostStore(tmp_path / "reader.jsonl")
    service = ChatService(reader, post_store=reader_store)
    errors: list[Exception] = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        listener.settimeout(3)
        peer = Peer(Hello(holder.peer_id, holder.session_id, holder.name,
                          listener.getsockname()[1], holder.capabilities),
                    "127.0.0.1", 0)

        def respond() -> None:
            try:
                conn, _ = listener.accept()
                with conn:
                    request = recv_message(conn)
                    assert request is not None
                    body = serve_query(holder_store, request["body"])
                    send_message(conn, envelope("POST_PAGE", holder.peer_id,
                                                holder.session_id, body,
                                                request["message_id"]))
            except Exception as error:
                errors.append(error)

        responder = threading.Thread(target=respond, daemon=True)
        responder.start()
        query = envelope("POST_QUERY", reader.peer_id, reader.session_id,
                         {"cursor": None, "limit": 50, "author_id": None})
        service._sync_peer(peer, query)
        responder.join(4)
    assert not errors
    assert reader_store.count() == 3
    assert service.events.get(timeout=1) == (
        "feed_updated", {"added": 3, "duplicates": 0})


def test_publish_post_emits_update_and_persists(tmp_path: Path) -> None:
    hello = _hello("Author")
    path = tmp_path / "posts.jsonl"
    service = ChatService(hello, post_store=JsonLinesPostStore(path))
    identifier = service.publish_post("Hello feed")
    assert service.events.get(timeout=1) == (
        "feed_updated", {"added": 1, "duplicates": 0})
    reopened = JsonLinesPostStore(path)
    assert reopened.get(identifier)["text"] == "Hello feed"


def test_gui_publishes_plain_text_feed(tmp_path: Path,
                                      monkeypatch: object) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")  # type: ignore[union-attr]
    from PySide6.QtWidgets import QApplication
    from gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    service = ChatService(_hello("Author"),
                          post_store=JsonLinesPostStore(tmp_path / "posts.jsonl"))
    window = MainWindow(service)
    window.post_input.setText("<b>literal post</b>")
    window.publish_post()
    assert "<b>literal post</b>" in window.feed_log.toPlainText()
    assert service.post_store.count() == 1
    window.close()
    app.processEvents()
