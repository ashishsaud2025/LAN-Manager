from __future__ import annotations

import time
from uuid import uuid4

import pytest

from core.chat import ChatService
from core.discovery import Hello
from core.protocol import envelope
from core.roster import Peer


def _service() -> ChatService:
    hello = Hello(str(uuid4()), str(uuid4()), "Local", 50001,
                  ("chat_v1", "file_v1", "posts_v1"))
    return ChatService(hello)


def _peer() -> Peer:
    hello = Hello(str(uuid4()), str(uuid4()), "Nearby laptop", 50101,
                  ("chat_v1", "file_v1", "posts_v1", "echo_v1"))
    return Peer(hello, "192.168.1.20", time.monotonic())


@pytest.fixture
def window(monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    result = MainWindow(_service())
    yield result
    result.close()
    app.processEvents()


def test_navigation_shell_exposes_phase_one_pages(window: object) -> None:
    assert window.stack.count() == 10
    assert len(window.navigation.page_items) == 10
    window.navigation.select(4)
    assert window.stack.currentIndex() == 4
    window.shortcuts[1].activated.emit()
    assert window.stack.currentIndex() == 1
    window.shortcuts[3].activated.emit()
    assert window.stack.currentIndex() == 7
    assert "Unverified LAN" in window.security_status.text()
    labels = [window.navigation.list.item(row).text()
              for row in range(window.navigation.list.count())]
    assert not any("planned" in label.lower() for label in labels)


def test_overview_starts_with_intentional_empty_states(window: object) -> None:
    assert window.nearby_value.text() == "Searching..."
    assert window.capability_value.text() == "Waiting"
    assert window.transfer_value.text() == "None"
    assert window.identity_value.text() == "Local"
    assert window.identity_detail.text().startswith("ID ")
    assert window.overview_nearby_stack.currentWidget() is window.overview_nearby_empty
    assert window.overview_activity_stack.currentWidget() is window.overview_activity_empty
    assert window.status_help.isHidden()


def test_overview_surfaces_live_network_and_activity(window: object) -> None:
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    assert window.overview_nearby_stack.currentWidget() is window.overview_peer_list
    assert window.overview_topology.peers == (peer,)
    assert window.capability_value.text() == "4"
    window.overview_peer_list.clicked.emit(window.peer_model.index(0, 0))
    assert window.stack.currentIndex() == 2
    assert window._selected_peer() == peer

    window.service.events.put(("status", "Discovery sample received"))
    window.drain()
    assert window.overview_activity_stack.currentWidget() is window.overview_activity_view
    assert window.activity_model.rowCount() >= 1


def test_security_chip_opens_security_details(window: object) -> None:
    window.security_status.click()
    assert window.stack.currentIndex() == 9


def test_topology_tracks_observed_sessions(window: object) -> None:
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    assert len(window.topology.scene.items()) > 3
    window._topology_device_selected(peer.hello.session_id)
    assert window.stack.currentIndex() == 2
    assert window._selected_peer().hello.session_id == peer.hello.session_id


def test_roster_updates_peer_model_and_capability_actions(window: object) -> None:
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    assert window.peer_model.rowCount() == 1
    assert window.nearby_value.text() == "1"
    assert "192.168.1.20:50101" in window.peer_endpoint.text()
    assert window.peer_message_button.isEnabled()
    assert window.peer_file_button.isEnabled()
    assert window.peer_sync_button.isEnabled()
    assert "Unverified" in window.peer_model.data(window.peer_model.index(0, 0))


def test_peer_selection_survives_roster_refresh(window: object) -> None:
    first, second = _peer(), _peer()
    window.service.events.put(("roster", (first, second)))
    window.drain()
    window.peer_list.setCurrentIndex(window.peer_model.index(1, 0))
    assert window._selected_peer().hello.session_id == second.hello.session_id
    refreshed_second = Peer(second.hello, second.ip, time.monotonic())
    window.service.events.put(("roster", (refreshed_second, first)))
    window.drain()
    assert window._selected_peer().hello.session_id == second.hello.session_id
    window.service.events.put(("roster", (first,)))
    window.drain()
    assert window._selected_peer() is None
    assert not window.peer_message_button.isEnabled()
    assert not window.peer_file_button.isEnabled()


def test_human_messages_do_not_enter_activity_transcript(window: object) -> None:
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    before = window.activity_model.rowCount()
    message = envelope("CHAT", peer.hello.peer_id, peer.hello.session_id,
                       {"scope": "room", "text": "human-only text"})
    window.service.events.put(("message", message))
    window.drain()
    assert window.message_model.rowCount() == 1
    assert window.activity_model.rowCount() == before
    assert "human-only text" not in window.log.toPlainText()


def test_status_event_is_retained_in_activity(window: object) -> None:
    window.service.events.put(("status", "TCP probe unavailable"))
    window.drain()
    assert window.activity_model.rowCount() == 1
    assert "TCP probe unavailable" in window.log.toPlainText()


def test_message_outcome_updates_message_row(window: object) -> None:
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    window.recipient.setCurrentIndex(1)
    window.input.setText("hello")
    window.send()
    identifier = window.message_model.entries[0].identifier
    window.service.events.put(("message_outcome", {
        "message_id": identifier, "session_id": peer.hello.session_id,
        "peer_name": peer.hello.name, "state": "accepted",
        "detail": "accepted by receiving application"}))
    window.drain()
    assert "Accepted by 1" in window.message_model.data(
        window.message_model.index(0, 0))


def test_transfer_model_preserves_phase_and_progress(window: object) -> None:
    window.update_transfer({"id": "transfer-one", "name": "map.bin",
                            "state": "written", "bytes": 25, "total": 100})
    assert window.transfer_model.rowCount() == 1
    window.transfer_view.setCurrentIndex(window.transfer_model.index(0, 0))
    assert window.transfer_progress.value() == 25
    assert "written" in window.transfer_progress.format()
    assert "1 of 4" in window.transfer_slots.text()
    window.update_transfer({"id": "transfer-one", "state": "written",
                            "bytes": 75, "total": 100})
    assert window.transfer_progress.value() == 75


def test_feed_model_tolerates_remote_timestamp_range(window: object) -> None:
    post = {"post_id": str(uuid4()), "author_id": str(uuid4()), "text": "future",
            "created_ms": 2 ** 63 - 1, "refs": []}
    window.post_model.set_posts([post], window.service.hello.peer_id, {})
    rendered = window.post_model.data(window.post_model.index(0, 0))
    assert "time unavailable" in rendered


def test_listener_failure_updates_prominent_status(window: object) -> None:
    window.service.events.put(("status", "TCP listener stopped: address in use"))
    window.drain()
    assert window.network_status.text() == "Network stopped"
