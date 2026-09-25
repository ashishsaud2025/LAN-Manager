from __future__ import annotations

import time
from uuid import uuid4

import pytest

from core.chat import ChatService
from core.diagnostics import Neighbor, NeighborSnapshot, ProbeResult
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
    result.show()
    app.processEvents()
    yield result
    result.close()
    app.processEvents()


def test_navigation_shell_exposes_phase_one_pages(window: object) -> None:
    from PySide6.QtWidgets import QFrame

    assert window.stack.count() == 11
    assert len(window.navigation.page_items) == 11
    assert window.page_names == (
        "Overview", "Network", "Devices", "Workbench", "Files", "Transfers",
        "Messages", "Feed", "Games", "Activity", "Settings")
    window.navigation.select(4)
    assert window.stack.currentIndex() == 4
    window.shortcuts[1].activated.emit()
    assert window.stack.currentIndex() == 1
    window.shortcuts[3].activated.emit()
    assert window.stack.currentIndex() == 3
    assert "Unverified LAN" in window.security_status.text()
    assert window.findChild(QFrame, "ApplicationHeader") is not None
    assert window.findChild(QFrame, "ApplicationFooter") is not None
    labels = [window.navigation.list.item(row).text()
              for row in range(window.navigation.list.count())]
    assert labels == [
        "CONTROL", "Overview", "Network", "Devices", "Workbench",
        "SHARE", "Files", "Transfers",
        "COMMUNITY", "Messages", "Feed", "Games",
        "SYSTEM", "Activity", "Settings",
    ]


def test_navigation_supports_mouse_and_keyboard(window: object) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    files_item = window.navigation.page_items[4]
    window.navigation.list.scrollToItem(files_item)
    QTest.qWait(10)
    files_rect = window.navigation.list.visualItemRect(files_item)
    QTest.mouseClick(window.navigation.list.viewport(),
                     Qt.MouseButton.LeftButton, pos=files_rect.center())
    selected_page = window.navigation.list.currentItem().data(256)
    assert isinstance(selected_page, int)
    assert selected_page != 0
    assert window.stack.currentIndex() == selected_page

    window.navigation.select(0)
    window.navigation.list.setFocus()
    QTest.keyClick(window.navigation.list, Qt.Key.Key_Down)
    assert window.stack.currentIndex() == 1


def test_shell_adapts_to_narrow_logical_width(window: object) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from gui.theme import GEOMETRY

    window.resize(900, 600)
    QTest.qWait(10)
    assert window.navigation.width() == GEOMETRY["navigation_compact"]
    assert window.overview_splitter.orientation() == Qt.Orientation.Vertical
    assert window.peer_splitter.orientation() == Qt.Orientation.Vertical
    assert window.workbench_splitter.orientation() == Qt.Orientation.Vertical
    assert window.overview_metrics.getItemPosition(2)[:2] == (1, 0)
    assert (window.minimumWidth(), window.minimumHeight()) == (640, 360)
    window.navigation.select(3)
    workbench_scroll = window.stack.currentWidget()
    assert workbench_scroll.verticalScrollBar().maximum() > 0
    workbench_scroll.ensureWidgetVisible(window.admin_cancel_button)
    QTest.qWait(10)
    assert workbench_scroll.verticalScrollBar().value() > 0

    window.resize(1200, 760)
    QTest.qWait(10)
    assert window.navigation.width() == GEOMETRY["navigation_compact"]
    assert window.overview_splitter.orientation() == Qt.Orientation.Vertical
    assert window.overview_metrics.getItemPosition(2)[:2] == (1, 0)
    for index in range(4):
        window.stack.setCurrentIndex(index)
        QTest.qWait(1)
        assert window.stack.widget(index).horizontalScrollBar().maximum() == 0


def test_shell_keeps_wide_layout_at_target_desktop_sizes(window: object) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from gui.theme import GEOMETRY

    for width, height in ((1600, 900), (1920, 1080)):
        window.resize(width, height)
        QTest.qWait(10)
        assert window.navigation.width() == GEOMETRY["navigation"]
        assert window.overview_splitter.orientation() == Qt.Orientation.Horizontal
        assert window.identity_status.isVisible()
        assert window.network_status.isVisible()
        for index in range(4):
            window.stack.setCurrentIndex(index)
            QTest.qWait(1)
            assert window.stack.widget(index).horizontalScrollBar().maximum() == 0


def test_theme_selector_lists_only_implemented_modes(window: object) -> None:
    assert [window.theme_selector.itemData(index)
            for index in range(window.theme_selector.count())] == [
                "observatory", "atlas"]


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
    assert [record.session_id for record in window.overview_topology.records] == [
        peer.hello.session_id]
    assert window.capability_value.text() == "4"
    window.overview_peer_list.clicked.emit(window.peer_model.index(0, 0))
    assert window.stack.currentIndex() == 0
    assert window.overview_peer_name.text() == peer.hello.name
    window.overview_peer_list.activated.emit(window.peer_model.index(0, 0))
    assert window.stack.currentIndex() == 2
    assert window._selected_peer() == peer

    window.service.events.put(("status", "Discovery sample received"))
    window.drain()
    assert window.overview_activity_stack.currentWidget() is window.overview_activity_view
    assert window.activity_model.rowCount() >= 1


def test_security_chip_opens_security_details(window: object) -> None:
    window.security_status.click()
    assert window.stack.currentIndex() == 10


def test_admin_inventory_keeps_neighbor_and_peer_evidence_separate(window: object) -> None:
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.service.events.put(("neighbor_snapshot", NeighborSnapshot(
        "refresh-one", (Neighbor(peer.ip, "00:11:22:33:44:55", "Wi-Fi", "stale"),),
        "OS neighbor cache")))
    window.drain()
    assert window.admin_device_model.rowCount() == 2
    assert {device.source for device in window.admin_device_model.devices} == {
        "LAN Atlas HELLO", "OS neighbor cache"}


def test_device_inspector_prefills_admin_target(window: object) -> None:
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    window._open_peer_admin()
    assert window.stack.currentIndex() == 3
    assert window.admin_address.text() == peer.ip
    assert window.admin_port.value() == peer.hello.tcp_port
    assert "not authenticated" in window.admin_selection.text()
    assert window.admin_echo_button.isEnabled()


def test_admin_echo_uses_selected_advertised_session(
        window: object, monkeypatch: pytest.MonkeyPatch) -> None:
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    window._open_peer_admin()
    identifier = str(uuid4())
    called = []

    def echo(address: str, port: int, peer_id: str, session_id: str) -> str:
        called.append((address, port, peer_id, session_id))
        return identifier

    monkeypatch.setattr(window.service.diagnostics, "echo", echo)
    window._echo_admin_target()
    assert called == [(peer.ip, peer.hello.tcp_port,
                       peer.hello.peer_id, peer.hello.session_id)]
    assert not window.admin_echo_button.isEnabled()
    window.service.events.put(("diagnostic_result", ProbeResult(
        identifier, "echo", peer.ip, peer.hello.tcp_port, "compatible", 2.0,
        "correlated reply")))
    window.drain()
    assert "compatible" in window.admin_result.text()
    assert window.admin_echo_button.isEnabled()
    window.admin_address.setText("192.168.1.99")
    assert not window.admin_echo_button.isEnabled()


def test_admin_probe_result_is_operational_activity(window: object,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    identifier = str(uuid4())
    monkeypatch.setattr(
        window.service.diagnostics, "ping",
        lambda address, **kwargs: identifier)
    window.admin_address.setText("192.168.1.20")
    window._ping_admin_target()
    window.service.events.put(("diagnostic_started", ProbeResult(
        identifier, "ping", "192.168.1.20", None, "running")))
    window.service.events.put(("diagnostic_result", ProbeResult(
        identifier, "ping", "192.168.1.20", None, "no_reply", 120.0,
        "ICMP did not reply")))
    before_messages = window.message_model.rowCount()
    window.drain()
    assert "no_reply" in window.admin_result.text()
    assert window.activity_model.rowCount() >= 1
    assert window.message_model.rowCount() == before_messages
    assert window.admin_ping_button.isEnabled()


def test_topology_tracks_observed_sessions(window: object) -> None:
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    assert len(window.topology.scene.items()) > 3
    window._topology_device_selected(peer.hello.session_id)
    assert window.stack.currentIndex() == 2
    assert window._selected_peer().hello.session_id == peer.hello.session_id


def test_network_selection_updates_evidence_hud(window: object) -> None:
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    window._network_peer_selected(peer.hello.session_id)
    assert window.network_selected_name.text() == peer.hello.name
    assert peer.ip in window.network_selected_endpoint.text()
    assert window.network_selected_session.toolTip() == peer.hello.session_id
    assert "Unverified" in window.network_selected_state.text()
    assert window.network_observed_value.text() == "1"


def test_device_table_filters_observed_fields(window: object) -> None:
    first, second = _peer(), _peer()
    second = Peer(second.hello, "10.20.30.40", second.last_seen)
    window.service.events.put(("roster", (first, second)))
    window.drain()
    assert window.peer_table_model.rowCount() == 2
    window.device_search.setText("10.20.30.40")
    assert window.peer_list.isRowHidden(0)
    assert not window.peer_list.isRowHidden(1)
    assert window.peer_selection.session_id == first.hello.session_id
    window.device_search.setText("50101")
    assert not window.peer_list.isRowHidden(0)
    window.device_search.setText("Files")
    assert not window.peer_list.isRowHidden(0)
    window.device_search.clear()
    assert not window.peer_list.isRowHidden(0)


def test_device_filters_use_canonical_evidence_states(window: object) -> None:
    from core.diagnostics import ProbeResult

    first, second = _peer(), _peer()
    window.service.events.put(("roster", (first, second)))
    window.drain()
    window.service.peer_repository.register_probe(
        "echo", first.hello.session_id, first.ip, first.hello.tcp_port, "echo")
    event = window.service.peer_repository.apply_probe_result(ProbeResult(
        "echo", "echo", first.ip, first.hello.tcp_port, "compatible", 2.0))
    window._update_peer_records(event.snapshot)
    window.service.events.put(("roster", (first,)))
    window.drain()

    assert set(window.device_tabs) == {
        "all", "nearby", "reachable", "compatible", "stale"}
    assert window.device_tabs["all"].text() == "All (2)"
    assert window.device_tabs["nearby"].text() == "Nearby (1)"
    assert window.device_tabs["compatible"].text() == "Compatible (1)"
    assert window.device_tabs["stale"].text() == "Offline (1)"
    window.device_tabs["compatible"].setChecked(True)
    assert not window.peer_list.isRowHidden(0)
    assert window.peer_list.isRowHidden(1)
    window.device_tabs["stale"].setChecked(True)
    assert window.peer_list.isRowHidden(0)
    assert not window.peer_list.isRowHidden(1)


def test_device_inspector_uses_known_and_unknown_evidence(window: object) -> None:
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    assert window.peer_host.text() == "Hostname: Not advertised"
    assert window.peer_platform.text() == "Platform / architecture: Not advertised"
    assert window.peer_mac.text() == "MAC address: Not observed"
    assert window.peer_latency.text() == "Latency: Not measured"
    assert window.peer_services.text().endswith("None observed")
    assert "Cryptographic Trust: Unverified" in window.peer_warning.text()
    assert "No authenticated device identity" in window.peer_warning.text()


def test_shared_peer_selection_updates_all_surfaces(window: object) -> None:
    first, second = _peer(), _peer()
    window.service.events.put(("roster", (first, second)))
    window.drain()
    window.peer_list.setCurrentIndex(window.peer_table_model.index(1, 0))
    assert window.peer_selection.session_id == second.hello.session_id
    assert window.overview_peer_list.currentIndex().row() == 1
    assert window.overview_peer_name.text() == second.hello.name
    assert window.network_selected_name.text() == second.hello.name
    assert {item.data(0) for item in window.topology.scene.selectedItems()} == {
        second.hello.session_id}
    window._copy_peer_address()
    from PySide6.QtWidgets import QApplication
    assert QApplication.clipboard().text() == second.ip


def test_older_repository_revision_cannot_replace_newer_evidence(window: object) -> None:
    from core.diagnostics import ProbeResult

    peer = _peer()
    presence = window.service.peer_repository.reconcile_presence((peer,), 10)
    window.service.peer_repository.register_probe(
        "echo", peer.hello.session_id, peer.ip, peer.hello.tcp_port, "echo")
    verified = window.service.peer_repository.apply_probe_result(ProbeResult(
        "echo", "echo", peer.ip, peer.hello.tcp_port, "compatible"))
    window._apply_repository_event(verified)
    window._apply_repository_event(presence)
    assert "Compatible" in window.peer_compatible_evidence.text()
    assert window.peer_records[0].compatibility_state.value == "compatible"


def test_inspector_waits_for_repository_revision_before_rendering(window: object) -> None:
    peer = _peer()
    repository = window.service.peer_repository
    nearby = repository.reconcile_presence((peer,), 10)
    window._apply_repository_event(nearby)
    stale = repository.reconcile_presence((), 11)

    window._show_peer(window._selected_record())
    assert window.peer_presence.text().startswith("Nearby")
    window._apply_repository_event(stale)
    assert window.peer_presence.text().startswith("Offline / stale")


def test_overview_nearby_list_excludes_retained_stale_records(window: object) -> None:
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    window.service.events.put(("roster", ()))
    window.drain()
    assert window.peer_table_model.rowCount() == 1
    assert window.peer_model.rowCount() == 0
    assert window.overview_nearby_stack.currentWidget() is window.overview_nearby_empty
    assert window.peer_selection.session_id == peer.hello.session_id
    assert "Offline / stale" in window.peer_presence.text()


def test_selection_clears_when_retained_stale_record_is_purged(window: object) -> None:
    peer = _peer()
    repository = window.service.peer_repository
    repository.stale_retention = 1.0
    window._apply_repository_event(repository.reconcile_presence((peer,), 10))
    window._apply_repository_event(repository.reconcile_presence((), 11))
    assert window.peer_selection.session_id == peer.hello.session_id
    window._apply_repository_event(repository.reconcile_presence((), 12))
    assert window.peer_selection.session_id is None
    assert window.peer_table_model.rowCount() == 0
    assert window.peer_name.text() == "Select a nearby session"


def test_stale_repository_session_cannot_launch_cached_echo(
        window: object, monkeypatch: pytest.MonkeyPatch) -> None:
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    window._open_peer_admin()
    called = []
    monkeypatch.setattr(
        window.service.diagnostics, "echo",
        lambda *args: called.append(args) or str(uuid4()))
    window.service.peer_repository.reconcile_presence((), time.monotonic())
    window._queue_admin_probe("echo")
    assert called == []
    assert "unchanged LAN Atlas session" in window.admin_result.text()


def test_network_and_overview_selection_survive_roster_refresh(window: object) -> None:
    first, second = _peer(), _peer()
    window.service.events.put(("roster", (first, second)))
    window.drain()
    window._network_peer_selected(second.hello.session_id)
    window._overview_peer_previewed(window.peer_model.index(1, 0))

    refreshed_second = Peer(second.hello, second.ip, time.monotonic())
    window.service.events.put(("roster", (refreshed_second, first)))
    window.drain()

    assert window.peer_selection.session_id == second.hello.session_id
    assert {item.data(0) for item in window.topology.scene.selectedItems()} == {
        second.hello.session_id}
    assert window.overview_peer_name.text() == second.hello.name

    window.service.events.put(("roster", (first,)))
    window.drain()
    assert window.peer_selection.session_id == second.hello.session_id
    assert "Offline / stale" in window.overview_peer_state.text()
    assert not window.peer_message_button.isEnabled()
    assert not window.peer_file_button.isEnabled()


def test_selected_session_renders_structured_rows_not_joined_text(
        window: object) -> None:
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    assert window.overview_peer_endpoint.text() == (
        f"{peer.ip}:{peer.hello.tcp_port}")
    assert window.overview_peer_installation.text() == (
        f"{peer.hello.peer_id[:12]}…")
    assert window.overview_peer_installation.toolTip() == peer.hello.peer_id
    assert window.overview_peer_session.text() == (
        f"{peer.hello.session_id[:12]}…")
    assert window.overview_peer_session.toolTip() == peer.hello.session_id
    assert "Nearby" in window.overview_peer_state.text()
    assert "Unverified" in window.overview_peer_state.text()
    assert "\n" not in window.overview_peer_endpoint.text()


def test_inspector_state_matches_list_pill_across_transitions(
        window: object) -> None:
    from gui.widgets.peer_row import peer_state, pill_display_text
    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    window.peer_list.setCurrentIndex(window.peer_table_model.index(0, 0))
    session_id = peer.hello.session_id

    def check(expected_key: str) -> None:
        record = window.service.peer_repository.get(session_id)
        assert record is not None
        assert peer_state(record) == expected_key
        nearby_rows = [item for item in window.peer_model.records
                       if item.session_id == session_id]
        if record.nearby:
            assert len(nearby_rows) == 1
            assert peer_state(nearby_rows[0]) == expected_key
        assert window.overview_state_pill.state() == expected_key
        assert window.overview_peer_state.text() == (
            f"{pill_display_text(expected_key)} · Unverified")

    check("nearby")
    window.service.peer_repository.register_probe(
        "phase-tcp", session_id, peer.ip, peer.hello.tcp_port, "tcp")
    window.service.events.put(("diagnostic_result", ProbeResult(
        "phase-tcp", "tcp", peer.ip, peer.hello.tcp_port,
        "reachable", 4.0, "", 2.0)))
    window.drain()
    check("reachable")
    window.service.peer_repository.register_probe(
        "phase-echo", session_id, peer.ip, peer.hello.tcp_port, "echo")
    window.service.events.put(("diagnostic_result", ProbeResult(
        "phase-echo", "echo", peer.ip, peer.hello.tcp_port,
        "compatible", 5.0, "", 3.0)))
    window.drain()
    check("compatible")
    window.service.events.put(("roster", ()))
    window.drain()
    check("offline")
    assert window.peer_selection.session_id == session_id


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
    window.peer_list.setCurrentIndex(window.peer_table_model.index(1, 0))
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


def test_overview_summary_reports_observed_responsive_offline(window: object) -> None:
    first, second = _peer(), _peer()
    window.service.events.put(("roster", (first, second)))
    window.drain()
    window.service.peer_repository.register_probe(
        "tcp", first.hello.session_id, first.ip, first.hello.tcp_port, "tcp")
    event = window.service.peer_repository.apply_probe_result(ProbeResult(
        "tcp", "tcp", first.ip, first.hello.tcp_port, "reachable", 4.0, "", 2.0))
    window._apply_repository_event(event)
    assert "2 observed hosts" in window.overview_summary.text()
    assert "1 responsive" in window.overview_summary.text()
    assert "0 offline" in window.overview_summary.text()
    assert "min 2.0 ms" in window.overview_rtt.text()
    window.service.events.put(("roster", (first,)))
    window.drain()
    assert "2 observed hosts" in window.overview_summary.text()
    assert "1 offline" in window.overview_summary.text()


def test_overview_latency_map_encodes_measured_latency_by_radius(window: object) -> None:
    from gui.latency_map import (
        INNER_RADIUS, OUTER_RADIUS, UNMEASURED_RADIUS, radius_for_latency,
    )

    first, second, third = _peer(), _peer(), _peer()
    window.service.events.put(("roster", (first, second, third)))
    window.drain()
    for identifier, peer, rtt in (("one", first, 1.0), ("two", second, 9.0)):
        window.service.peer_repository.register_probe(
            identifier, peer.hello.session_id, peer.ip, peer.hello.tcp_port,
            "tcp")
        event = window.service.peer_repository.apply_probe_result(ProbeResult(
            identifier, "tcp", peer.ip, peer.hello.tcp_port,
            "reachable", rtt + 0.5, "", rtt))
        window._apply_repository_event(event)
    positions = {}
    for item in window.overview_topology.scene.items():
        session_id = item.data(0)
        if isinstance(session_id, str) and session_id:
            positions[session_id] = item.pos()
    assert set(positions) == {
        first.hello.session_id, second.hello.session_id, third.hello.session_id}

    def radius(session_id: str) -> float:
        pos = positions[session_id]
        return (((pos.x() - 450.0) ** 2 + (pos.y() - 260.0) ** 2) ** 0.5)

    assert radius(first.hello.session_id) < radius(second.hello.session_id)
    assert abs(radius(third.hello.session_id) - UNMEASURED_RADIUS) < 1.0
    assert INNER_RADIUS <= radius(first.hello.session_id) <= OUTER_RADIUS
    assert abs(radius(first.hello.session_id) - radius_for_latency(1.0)) < 1.0
    assert abs(radius(second.hello.session_id) - radius_for_latency(9.0)) < 1.0
    assert "2 of 3 nearby" in window.overview_rtt.text()
    window.peer_selection.select(third.hello.session_id)
    assert window.overview_peer_rtt.text() == "Measured latency: none yet"
    window.peer_selection.select(second.hello.session_id)
    assert "9.0 ms via tcp" in window.overview_peer_rtt.text()
    assert "9.0 ms via tcp" in window.peer_latency.text()


def test_latency_map_uses_stable_scale_for_single_peer(window: object) -> None:
    from gui.latency_map import radius_for_latency

    peer = _peer()
    window.service.events.put(("roster", (peer,)))
    window.drain()
    window.service.peer_repository.register_probe(
        "single", peer.hello.session_id, peer.ip, peer.hello.tcp_port, "tcp")
    event = window.service.peer_repository.apply_probe_result(ProbeResult(
        "single", "tcp", peer.ip, peer.hello.tcp_port, "reachable", 2.0, "", 1.0))
    window._apply_repository_event(event)
    positions = {item.data(0): item.pos()
                 for item in window.overview_topology.scene.items()
                 if isinstance(item.data(0), str) and item.data(0)}
    pos = positions[peer.hello.session_id]
    radius = (((pos.x() - 450.0) ** 2 + (pos.y() - 260.0) ** 2) ** 0.5)
    assert abs(radius - radius_for_latency(1.0)) < 1.0


def test_overview_map_preview_does_not_leave_overview(window: object) -> None:
    first, second = _peer(), _peer()
    window.service.events.put(("roster", (first, second)))
    window.drain()
    window.navigation.select(0)
    assert window.stack.currentIndex() == 0
    window.overview_topology.device_selected.emit(second.hello.session_id)
    assert window.stack.currentIndex() == 0
    assert window.peer_selection.session_id == second.hello.session_id
    assert window.overview_peer_name.text() == second.hello.name


def test_latency_map_selection_flows_to_shared_devices_selection(window: object) -> None:
    first, second = _peer(), _peer()
    window.service.events.put(("roster", (first, second)))
    window.drain()
    window.overview_topology.device_selected.emit(second.hello.session_id)
    assert window.peer_selection.session_id == second.hello.session_id
    assert window.peer_name.text() == second.hello.name


def test_latency_map_handles_dozens_of_peers_without_animation(window: object) -> None:
    from PySide6.QtTest import QTest

    peers = tuple(_peer() for _ in range(40))
    window.service.events.put(("roster", peers))
    window.drain()
    assert len(window.overview_topology.records) == 40
    assert window.overview_topology.select_session(peers[20].hello.session_id)
    window.resize(1600, 900)
    QTest.qWait(10)
    assert window.stack.widget(0).horizontalScrollBar().maximum() == 0
    assert "40 observed hosts" in window.overview_summary.text()
