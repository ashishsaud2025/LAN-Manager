from __future__ import annotations

from uuid import uuid4

import pytest

from core.chat import ChatService
from core.discovery import Hello


@pytest.fixture
def window(monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import gui.main_window

    monkeypatch.setattr(gui.main_window, "local_ipv4_addresses",
                        lambda: ("192.168.1.20",))
    app = QApplication.instance() or QApplication([])
    hello = Hello(str(uuid4()), str(uuid4()), "Local", 50001,
                  ("chat_v1", "file_v1", "posts_v1"))
    result = gui.main_window.MainWindow(ChatService(hello))
    result.show()
    app.processEvents()
    yield result
    result.close()
    app.processEvents()


def _fill(window: object, kind: str, name: str) -> None:
    window.directory_kind.setCurrentIndex(window.directory_kind.findData(kind))
    window.directory_name.setText(name)
    window.directory_description.setText("Session-local publication")
    window.directory_scheme.setCurrentIndex(
        window.directory_scheme.findData("http"))
    window.directory_host.setCurrentText("192.168.1.20")
    window.directory_port.setValue(8080)
    window.directory_path.setText("/play" if kind == "game" else "/status")


def test_settings_publication_is_shared_by_desktop_and_portal(window: object) -> None:
    _fill(window, "game", "Arena")
    window.directory_publish.click()
    assert window.game_model.rowCount() == 1
    assert window.service_model.rowCount() == 0
    assert window.directory_selector.currentData() is not None
    assert window.portal.content.directory_store is window.directory
    portal_games = window.portal.content.directory(
        window.game_model.kind)
    assert portal_games["items"][0]["name"] == "Arena"
    assert portal_games["items"][0]["evidence"]["reachability"] == "not_checked"

    window.directory_description.setText("Updated locally")
    window.directory_publish.click()
    assert window.directory.snapshot().entries[0].description == "Updated locally"
    window.directory_withdraw.click()
    assert window.game_model.rowCount() == 0
    assert window.portal.content.directory(window.game_model.kind)["items"] == []


def test_games_page_keeps_services_separate_and_opens_validated_url(
        window: object, monkeypatch: pytest.MonkeyPatch) -> None:
    import gui.main_window

    _fill(window, "service", "Notebook")
    window.directory_publish.click()
    assert window.service_model.rowCount() == 1
    window.directory_tabs.setCurrentIndex(1)
    window.service_view.setCurrentIndex(window.service_model.index(0, 0))
    assert window.directory_open.isEnabled()
    opened = []
    monkeypatch.setattr(gui.main_window.QDesktopServices, "openUrl",
                        lambda url: opened.append(url.toString()) or True)
    window.directory_open.click()
    assert opened == ["http://192.168.1.20:8080/status"]
    rendered = window.service_model.data(window.service_model.index(0, 0))
    assert "Published locally" in rendered
    assert "Reachability not checked" in rendered
    assert "Health not defined" in rendered


def test_settings_rejects_loopback_publication(window: object) -> None:
    _fill(window, "service", "Private")
    window.directory_host.setCurrentText("127.0.0.1")
    before = window.activity_model.rowCount()
    window.directory_publish.click()
    assert window.service_model.rowCount() == 0
    assert window.activity_model.rowCount() == before + 1
    assert "failed" in window.activity_model.entries[-1].title.lower()
