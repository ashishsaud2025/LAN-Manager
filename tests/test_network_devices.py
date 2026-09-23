from __future__ import annotations

import time
from uuid import uuid4

import pytest

from core.chat import ChatService
from core.diagnostics import ProbeResult
from core.discovery import Hello
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
    result.show()
    app.processEvents()
    yield result
    result.close()
    app.processEvents()


def test_network_topology_reuses_shared_radar(window: object) -> None:
    from gui.widgets.radar_map import RadarMapWidget
    first, second = _peer(), _peer()
    window.service.events.put(("roster", (first, second)))
    window.drain()
    assert isinstance(window.topology, RadarMapWidget)
    assert {record.session_id for record in window.topology.records} == {
        first.hello.session_id, second.hello.session_id}
    assert window.topology.select_session(first.hello.session_id)
    assert window.peer_selection.session_id == first.hello.session_id


def test_radar_header_icon_renders(window: object) -> None:
    from gui.widgets.header_icon import header_icon
    assert header_icon("radar").pixmap() is not None


def test_peer_state_agrees_across_all_pages(window: object) -> None:
    from gui.widgets.peer_row import peer_state, pill_display_text
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    window.peer_list.setCurrentIndex(window.peer_table_model.index(0, 0))
    window.service.peer_repository.register_probe(
        "cross-page", peer.hello.session_id, peer.ip, peer.hello.tcp_port,
        "tcp")
    window.service.events.put(("diagnostic_result", ProbeResult(
        "cross-page", "tcp", peer.ip, peer.hello.tcp_port,
        "reachable", 4.0, "", 2.0)))
    window.drain()
    record = window.service.peer_repository.get(peer.hello.session_id)
    assert record is not None
    assert peer_state(record) == "reachable"
    assert window.overview_state_pill.state() == "reachable"
    assert window.network_state_pill.state() == "reachable"
    assert window.peer_state_pill.state() == "reachable"
    assert window.overview_peer_state.text() == "Reachable · Unverified"
    assert window.network_selected_state.text() == "Reachable · Unverified"
    assert pill_display_text("reachable") in window.network_selected_state.text()


def test_devices_inspector_tracks_transitions_without_reselection(
        window: object) -> None:
    from gui.widgets.peer_row import peer_state
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    window.peer_list.setCurrentIndex(window.peer_table_model.index(0, 0))
    session_id = peer.hello.session_id

    def check(expected_key: str, presence: str) -> None:
        record = window.service.peer_repository.get(session_id)
        assert record is not None
        assert peer_state(record) == expected_key
        assert window.peer_state_pill.state() == expected_key
        assert presence in window.peer_presence.text()

    check("nearby", "Nearby")
    assert window.peer_installation.toolTip() == peer.hello.peer_id
    assert window.peer_session.toolTip() == session_id
    window.service.peer_repository.register_probe(
        "device-probe", session_id, peer.ip, peer.hello.tcp_port, "tcp")
    window.service.events.put(("diagnostic_result", ProbeResult(
        "device-probe", "tcp", peer.ip, peer.hello.tcp_port,
        "reachable", 4.0, "", 2.0)))
    window.drain()
    check("reachable", "Nearby")
    assert window.peer_message_button.isEnabled()
    window.service.events.put(("roster", ()))
    window.drain()
    check("offline", "Offline / stale")
    assert window.peer_selection.session_id == session_id
    assert not window.peer_message_button.isEnabled()


def test_device_tabs_count_live_states_and_filter(window: object) -> None:
    first, second = _peer(), _peer()
    window.service.events.put(("roster", (first, second)))
    window.drain()
    assert window.device_tabs["all"].text() == "All (2)"
    assert window.device_tabs["nearby"].text() == "Nearby (2)"
    assert window.device_tabs["stale"].text() == "Offline (0)"
    window.service.peer_repository.register_probe(
        "tab-probe", first.hello.session_id, first.ip,
        first.hello.tcp_port, "tcp")
    window.service.events.put(("diagnostic_result", ProbeResult(
        "tab-probe", "tcp", first.ip, first.hello.tcp_port,
        "reachable", 4.0, "", 2.0)))
    window.drain()
    assert window.device_tabs["reachable"].text() == "Reachable (1)"
    window.device_tabs["reachable"].setChecked(True)
    visible = [row for row in range(2)
               if not window.peer_list.isRowHidden(row)]
    assert len(visible) == 1
    window.device_tabs["all"].setChecked(True)
    assert not window.peer_list.isRowHidden(0)
    assert not window.peer_list.isRowHidden(1)


def test_devices_inspector_hides_template_without_selection(
        window: object) -> None:
    assert window.peer_warning.isHidden()
    assert window.device_identity_panel.isHidden()
    assert window.device_evidence_panel.isHidden()
    assert window.peer_name.text() == "Select a nearby session"
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    assert not window.peer_warning.isHidden()
    assert not window.device_identity_panel.isHidden()
    assert window.peer_installation.toolTip() == peer.hello.peer_id


def test_devices_inspector_tracks_transitions_without_reselection(
        window: object) -> None:
    from gui.widgets.peer_row import peer_state
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    window.peer_list.setCurrentIndex(window.peer_table_model.index(0, 0))
    session_id = peer.hello.session_id

    def check(expected_key: str, presence: str) -> None:
        record = window.service.peer_repository.get(session_id)
        assert record is not None
        assert peer_state(record) == expected_key
        assert window.peer_state_pill.state() == expected_key
        assert presence in window.peer_presence.text()

    check("nearby", "Nearby")
    assert window.peer_warning.isHidden() is False
    window.service.peer_repository.register_probe(
        "device-echo", session_id, peer.ip, peer.hello.tcp_port, "echo")
    window.service.events.put(("diagnostic_result", ProbeResult(
        "device-echo", "echo", peer.ip, peer.hello.tcp_port,
        "compatible", 5.0, "", 3.0)))
    window.drain()
    check("compatible", "Nearby")
    assert "Compatible" in window.peer_compatible_evidence.text()
    window.service.events.put(("roster", ()))
    window.drain()
    check("offline", "Offline / stale")
    assert window.peer_selection.session_id == session_id
    assert not window.peer_message_button.isEnabled()


def test_table_session_cell_uses_shared_delegate(window: object) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QStyleOptionViewItem
    from gui.widgets.peer_row import TABLE_ROW_HEIGHT, PeerTableDelegate
    first, second = _peer(), _peer()
    window.service.events.put(("roster", (first, second)))
    window.drain()
    delegate = window.peer_list.itemDelegateForColumn(0)
    assert isinstance(delegate, PeerTableDelegate)
    index = window.peer_table_model.index(0, 1)
    font = window.peer_table_model.data(index, Qt.ItemDataRole.FontRole)
    assert isinstance(font, QFont) and bool(font.family())
    option = QStyleOptionViewItem()
    option.initFrom(window.peer_list)
    assert delegate.sizeHint(
        option, window.peer_table_model.index(0, 0)).height() == TABLE_ROW_HEIGHT
