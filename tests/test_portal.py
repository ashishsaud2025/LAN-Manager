from __future__ import annotations

import http.client
import json
from pathlib import Path
import socket
from typing import Any
from uuid import uuid4

import pytest

from core.chat import ChatService
from core.discovery import Hello
from core.portal import PortalServer
from core.protocol import envelope
from core.roster import Peer
from core.services import DirectoryKind, LocalServiceDirectory
from core.storage import JsonLinesPostStore


def _hello(name: str = "Portal host") -> Hello:
    return Hello(str(uuid4()), str(uuid4()), name, 50001,
                 ("chat_v1", "file_v1", "posts_v1"))


def _request(port: int, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        return response.status, {name.lower(): value
                                 for name, value in response.getheaders()}, body
    finally:
        connection.close()


def _free_port() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


def test_portal_serves_bounded_read_only_snapshots_and_releases_port() -> None:
    service = ChatService(_hello())
    portal = PortalServer(service.hello, service.peer_repository,
                          allow_loopback=True)
    state = portal.start("127.0.0.1", 0)
    assert state.phase == "running"
    assert state.port is not None
    port = state.port
    try:
        status, headers, body = _request(port, "GET", "/api/status")
        payload = json.loads(body)
        assert status == 200
        assert headers["content-type"] == "application/json; charset=utf-8"
        assert headers["cache-control"] == "no-store"
        assert headers["x-frame-options"] == "DENY"
        assert payload["node"] == "Portal host"
        assert payload["security"] == {
            "trust": "unverified", "transport": "plaintext"}
        assert payload["peers"]["nearby"] == 0

        status, _, body = _request(port, "GET", "/api/files")
        assert status == 200
        assert json.loads(body)["available"] is False

        status, headers, body = _request(port, "POST", "/api/messages")
        assert status == 405
        assert headers["allow"] == "GET, HEAD"
        assert json.loads(body)["error"] == "Portal writes are disabled."

        status, _, body = _request(port, "GET", "/api/diagnostics")
        assert status == 404
        assert json.loads(body) == {"error": "Not found."}
    finally:
        portal.stop()
        assert portal.join(3)
    assert portal.state().phase == "stopped"
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


def test_portal_escapes_feed_html_but_preserves_json_text(tmp_path: Path) -> None:
    hello = _hello("Host <node>")
    store = JsonLinesPostStore(tmp_path / "posts.jsonl")
    post = {
        "post_id": str(uuid4()),
        "author_id": hello.peer_id,
        "text": "<script>alert('x')</script>",
        "created_ms": 1,
        "refs": [],
    }
    assert store.add(post)
    portal = PortalServer(hello, ChatService(hello).peer_repository, store,
                          allow_loopback=True)
    state = portal.start("127.0.0.1", 0)
    assert state.port is not None
    try:
        status, _, body = _request(state.port, "GET", "/feed")
        page = body.decode("utf-8")
        assert status == 200
        assert "Host &lt;node&gt;" in page
        assert "&lt;script&gt;alert" in page
        assert "<script>alert" not in page
        assert "Administrative tools are not exposed" in page

        status, _, body = _request(state.port, "GET", "/api/feed")
        payload: dict[str, Any] = json.loads(body)
        assert status == 200
        assert payload["available"] is True
        assert payload["items"][0]["text"] == post["text"]
        assert payload["items"][0]["author_name"] == hello.name
        assert payload["items"][0]["provenance"] == "published_local"
    finally:
        portal.stop()
        assert portal.join(3)


def test_portal_shares_room_history_and_local_directory_only() -> None:
    hello = _hello("Host")
    service = ChatService(hello)
    recipient = Hello(str(uuid4()), str(uuid4()), "Peer", 50001, ("chat_v1",))
    peer = Peer(recipient, "192.168.1.30", 1.0)
    room = envelope("CHAT", hello.peer_id, hello.session_id,
                    {"scope": "room", "text": "room <message>"})
    direct = envelope("CHAT", hello.peer_id, hello.session_id,
                      {"scope": "dm", "to_session": recipient.session_id,
                       "text": "private"})
    service.message_journal.record_outgoing(room, hello, (peer,))
    service.message_journal.record_outgoing(direct, hello, (peer,))
    service.message_journal.update_delivery(
        room["message_id"], recipient.session_id, "accepted",
        "accepted by receiving application")
    directory = LocalServiceDirectory(hello)
    published = directory.register(
        DirectoryKind.SERVICE, "Docs <LAN>", "Read <carefully>", "http",
        "192.168.1.40", 8080, "/docs")
    directory.register(
        DirectoryKind.GAME, "Arena", "LAN game", "https",
        "192.168.1.41", 8443, "/lobby")
    portal = PortalServer(hello, service.peer_repository, None,
                          service.message_journal, directory, allow_loopback=True)
    state = portal.start("127.0.0.1", 0)
    assert state.port is not None
    try:
        status, _, body = _request(state.port, "GET", "/api/messages")
        messages = json.loads(body)
        assert status == 200
        assert messages["retention"] == "process_lifetime_bounded"
        assert [item["text"] for item in messages["items"]] == ["room <message>"]
        assert messages["items"][0]["delivery"] == {
            "queued": 0, "accepted": 1, "failed": 0, "uncertain": 0}
        assert messages["items"][0]["sender"] == {
            "name": "Host", "identity": "unverified"}

        status, _, body = _request(state.port, "GET", "/api/services")
        services = json.loads(body)
        assert status == 200 and services["available"] is True
        assert services["items"][0]["service_id"] == published.service_id
        assert services["items"][0]["evidence"] == {
            "publication": "published_local",
            "reachability": "not_checked",
            "health": "not_defined",
        }
        status, _, body = _request(state.port, "GET", "/api/games")
        assert status == 200 and len(json.loads(body)["items"]) == 1

        status, _, body = _request(state.port, "GET", "/chat")
        page = body.decode("utf-8")
        assert status == 200
        assert "room &lt;message&gt;" in page and "private" not in page
        status, _, body = _request(state.port, "GET", "/services")
        page = body.decode("utf-8")
        assert "Docs &lt;LAN&gt;" in page
        assert "Read &lt;carefully&gt;" in page
        assert "Reachability not checked" in page
    finally:
        portal.stop()
        assert portal.join(3)


def test_portal_rejects_implicit_all_interfaces_and_duplicate_start() -> None:
    service = ChatService(_hello())
    production = PortalServer(service.hello, service.peer_repository)
    with pytest.raises(ValueError, match="concrete LAN"):
        production.start("127.0.0.1", 8080)
    portal = PortalServer(service.hello, service.peer_repository,
                          allow_loopback=True)
    with pytest.raises(ValueError, match="concrete LAN"):
        portal.start("0.0.0.0", 8080)
    portal.stop()
    assert portal.join()

    state = portal.start("127.0.0.1", 0)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            portal.start("127.0.0.1", 0)
        assert state.port is not None
        status, headers, body = _request(state.port, "HEAD", "/")
        assert status == 200
        assert int(headers["content-length"]) > 0
        assert body == b""
    finally:
        portal.stop()
        assert portal.join(3)


def test_settings_controls_own_portal_lifecycle(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import gui.main_window

    monkeypatch.setattr(gui.main_window, "local_ipv4_addresses",
                        lambda: ("127.0.0.1",))
    app = QApplication.instance() or QApplication([])
    service = ChatService(_hello())
    portal = PortalServer(service.hello, service.peer_repository,
                          service.post_store, service.message_journal,
                          allow_loopback=True)
    window = gui.main_window.MainWindow(service, portal)
    window.portal_port.setValue(_free_port())
    window.show()
    app.processEvents()
    try:
        assert window.portal_start.isEnabled()
        assert not window.portal_stop.isEnabled()
        window.portal_start.click()
        app.processEvents()
        state = portal.state()
        assert state.phase == "running"
        assert state.url is not None
        assert window.portal_status.text() == "Running"
        assert not window.portal_address.isEnabled()
        window.portal_copy.click()
        assert QApplication.clipboard().text() == state.url

        window.portal_stop.click()
        assert portal.join(3)
        window._refresh_portal_state()
        assert window.portal_status.text() == "Stopped"
        assert window.portal_start.isEnabled()
    finally:
        window.close()
        app.processEvents()
        portal.stop()
        assert portal.join(3)
