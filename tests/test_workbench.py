from __future__ import annotations

from uuid import uuid4

import pytest

from core.chat import ChatService
from core.diagnostics import ProbeResult
from core.discovery import Hello


def _service() -> ChatService:
    hello = Hello(str(uuid4()), str(uuid4()), "Local", 50001,
                  ("chat_v1", "file_v1", "posts_v1"))
    return ChatService(hello)


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


def test_tool_tabs_cover_planned_tools(window: object) -> None:
    names = [window.admin_tabs.tabText(index)
             for index in range(window.admin_tabs.count())]
    assert names == ["Ping", "TCP", "ECHO", "Raw TCP", "Traceroute",
                     "Port Scan"]
    for index in range(3, 6):
        assert not window.admin_tabs.isTabEnabled(index)
        assert "not implemented" in window.admin_tabs.tabToolTip(index).lower()
    assert window.ping_params["count"].maximum() == 10
    assert window.tcp_params["attempts"].maximum() == 5


def test_ping_stats_and_sparkline_render_samples(
        window: object, monkeypatch: pytest.MonkeyPatch) -> None:
    identifier = str(uuid4())
    seen: dict[str, object] = {}

    def fake_ping(address: str, **kwargs: object) -> str:
        seen.update(kwargs)
        seen["address"] = address
        return identifier

    monkeypatch.setattr(window.service.diagnostics, "ping", fake_ping)
    window.admin_address.setText("192.168.1.20")
    window.ping_params["count"].setValue(2)
    window._ping_admin_target()
    assert seen == {"count": 2, "timeout": 3.0, "payload_size": 32,
                    "address": "192.168.1.20"}
    window.service.events.put(("diagnostic_started", ProbeResult(
        identifier, "ping", "192.168.1.20", None, "running")))
    window.service.events.put(("diagnostic_result", ProbeResult(
        identifier, "ping", "192.168.1.20", None, "reachable", 9.0,
        "reply", 3.0, (2.0, 4.0), 0.0, 2.0, 64)))
    window.drain()
    assert window.ping_stats["Replies:"].text() == "2"
    assert window.ping_stats["Loss:"].text() == "0.0 %"
    assert window.ping_stats["Min RTT:"].text() == "2.0 ms"
    assert window.ping_stats["Avg RTT:"].text() == "3.0 ms"
    assert window.ping_stats["Max RTT:"].text() == "4.0 ms"
    assert window.ping_stats["Jitter:"].text() == "2.0 ms"
    assert window.ping_stats["TTL:"].text() == "64"
    assert window.ping_sparkline.samples() == [2.0, 4.0]
    assert window.ping_pill.state() == "reachable"
    assert window.admin_ping_button.isEnabled()


def test_ping_loss_without_samples_stays_honest(
        window: object, monkeypatch: pytest.MonkeyPatch) -> None:
    identifier = str(uuid4())
    monkeypatch.setattr(window.service.diagnostics, "ping",
                        lambda address, **kwargs: identifier)
    window.admin_address.setText("192.168.1.20")
    window._ping_admin_target()
    window.service.events.put(("diagnostic_result", ProbeResult(
        identifier, "ping", "192.168.1.20", None, "no_reply", 120.0,
        "ICMP did not reply")))
    window.drain()
    assert window.ping_stats["Replies:"].text() == "0"
    assert window.ping_stats["Loss:"].text() == "—"
    assert window.ping_pill.state() == "offline"


def test_tcp_attempt_parameters_reach_worker(
        window: object, monkeypatch: pytest.MonkeyPatch) -> None:
    identifier = str(uuid4())
    seen: dict[str, object] = {}

    def fake_tcp(address: str, port: int, **kwargs: object) -> str:
        seen.update(kwargs)
        seen["address"] = address
        seen["port"] = port
        return identifier

    monkeypatch.setattr(window.service.diagnostics, "tcp_connect", fake_tcp)
    window.admin_address.setText("192.168.1.20")
    window.admin_port.setValue(8080)
    window.tcp_params["attempts"].setValue(3)
    window._tcp_admin_target()
    assert seen["attempts"] == 3
    assert seen["port"] == 8080
    window.service.events.put(("diagnostic_result", ProbeResult(
        identifier, "tcp", "192.168.1.20", 8080, "refused", 6.0,
        "attempt 1: refused")))
    window.drain()
    assert "refused" in window.tcp_stats["Attempts:"].text()
    assert window.tcp_pill.state() == "reachable"


def test_echo_tab_shows_correlation_identity(
        window: object, monkeypatch: pytest.MonkeyPatch) -> None:
    import time
    from core.roster import Peer
    identifier = str(uuid4())
    monkeypatch.setattr(
        window.service.diagnostics, "echo",
        lambda *args: identifier)
    window.admin_address.setText("192.168.1.20")
    window._echo_admin_target()
    assert "unchanged LAN Atlas session" in window.admin_result.text()
    peer_id, session_id = str(uuid4()), str(uuid4())
    peer = Peer(Hello(peer_id, session_id, "Echo box", 50002, ("echo_v1",)),
                "192.168.1.21", time.monotonic())
    window.service.events.put(("roster", (peer,)))
    window.drain()
    window._open_peer_admin()
    window._echo_admin_target()
    window.service.events.put(("diagnostic_result", ProbeResult(
        identifier, "echo", "192.168.1.21", 50002, "compatible", 12.0,
        "Correlated LAN Manager ECHO_REPLY matched the advertised session",
        4.0)))
    window.drain()
    assert window.echo_stats["Correlation ID:"].toolTip() == identifier
    assert "4.0 ms round-trip" in window.echo_stats["Timing:"].text()
    assert window.echo_pill.state() == "compatible"


def test_invalid_target_shows_error_without_probe(window: object) -> None:
    window.admin_address.setText("not-an-address")
    window._ping_admin_target()
    assert "numeric IPv4" in window.admin_result.text()
    assert window.active_probe_id is None
