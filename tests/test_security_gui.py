from __future__ import annotations

from pathlib import Path
import time
from uuid import uuid4

import pytest

from core.chat import ChatService
from core.discovery import Hello
from core.identity import DeviceIdentity
from core.peer_repository import TrustState
from core.roster import Peer
from core.secure_transport import SecureTransport
from core.storage import JsonLinesPostStore
from core.trust import TrustStore


def _secure_service(tmp_path: Path) -> tuple[ChatService, TrustStore]:
    peer_id = str(uuid4())
    identity = DeviceIdentity.load_or_create(tmp_path / "local.pem", peer_id)
    hello = Hello(
        peer_id, str(uuid4()), "Local", 50001,
        ("chat_v1", "file_v1", "posts_v1", "secure_transport_v1"),
        50003, identity.fingerprint)
    trust = TrustStore(tmp_path / "trust.json")
    transport = SecureTransport(identity, trust, hello)
    service = ChatService(
        hello, post_store=JsonLinesPostStore(tmp_path / "posts.jsonl"),
        secure_transport=transport)
    return service, trust


def _remote(tmp_path: Path, suffix: str = "remote") -> tuple[Peer, DeviceIdentity]:
    peer_id = str(uuid4())
    identity = DeviceIdentity.load_or_create(
        tmp_path / f"{suffix}.pem", peer_id)
    hello = Hello(
        peer_id, str(uuid4()), "Remote", 51001,
        ("chat_v1", "file_v1", "posts_v1", "secure_transport_v1"),
        51003, identity.fingerprint)
    return Peer(hello, "192.168.1.20", time.monotonic()), identity


@pytest.fixture
def secure_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    service, trust = _secure_service(tmp_path)
    window = MainWindow(service)
    window._test_trust_store = trust
    window.show()
    app.processEvents()
    yield window
    window.close()
    app.processEvents()


def test_device_pairing_evidence_and_key_change_controls(
        secure_window: object, tmp_path: Path) -> None:
    peer, identity = _remote(tmp_path)
    event = secure_window.service.peer_repository.reconcile_presence(
        (peer,), time.monotonic())
    assert event is not None
    secure_window._apply_repository_event(event)

    assert secure_window.peer_pair_button.isEnabled()
    assert not secure_window.peer_forget_button.isEnabled()
    assert "legacy plaintext" in secure_window.peer_warning.text()

    secure_window._test_trust_store.pair(
        peer.hello.peer_id, identity.certificate_der, peer.hello.name)
    event = secure_window.service.peer_repository.refresh_trust()
    assert event is not None
    secure_window._apply_repository_event(event)

    record = secure_window._selected_record()
    assert record.trust_state is TrustState.PAIRED
    assert not secure_window.peer_pair_button.isEnabled()
    assert secure_window.peer_forget_button.isEnabled()
    assert "must still authenticate" in secure_window.peer_warning.text()
    assert secure_window.overview_trust_pill.text() == "Paired key"
    assert "Paired TLS" in secure_window.recipient.itemText(1)

    replacement = DeviceIdentity.load_or_create(
        tmp_path / "replacement.pem", peer.hello.peer_id)
    changed = Hello(
        peer.hello.peer_id, peer.hello.session_id, peer.hello.name,
        peer.hello.tcp_port, peer.hello.capabilities,
        peer.hello.secure_port, replacement.fingerprint)
    event = secure_window.service.peer_repository.reconcile_presence(
        (Peer(changed, peer.ip, time.monotonic()),), time.monotonic())
    assert event is not None
    secure_window._apply_repository_event(event)

    assert secure_window._selected_record().trust_state is TrustState.KEY_CHANGED
    assert not secure_window.peer_message_button.isEnabled()
    assert not secure_window.peer_file_button.isEnabled()
    assert "connections are blocked" in secure_window.peer_warning.text()


def test_local_secure_post_shows_signature_evidence(secure_window: object) -> None:
    identifier = secure_window.service.publish_post("Signed publication")
    secure_window.drain()

    assert secure_window.service.post_store.get(identifier) is not None
    text = secure_window.post_model.data(
        secure_window.post_model.index(0, 0))
    assert "Signed by this device" in text
    assert "Signed by this device" in secure_window.feed_log.toPlainText()
