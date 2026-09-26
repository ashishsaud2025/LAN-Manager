from __future__ import annotations

from uuid import uuid4

import pytest

from core.chat import ChatService
from core.discovery import Hello
from gui.models import (TerminalTransferProxy, TransferListModel, format_eta,
                        human_bytes, transfer_pace)


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


def test_pace_helpers_stay_honest_without_samples() -> None:
    assert human_bytes(512) == "512 B"
    assert human_bytes(2048) == "2.0 KiB"
    assert human_bytes(5 * 1024 * 1024) == "5.0 MiB"
    assert format_eta(None) == "unavailable"
    assert format_eta(9) == "9s"
    assert format_eta(125) == "2m 05s"
    assert transfer_pace((), 0, 100) == (None, None)
    assert transfer_pace(((1.0, 10.0),), 10.0, 100.0) == (None, None)
    assert transfer_pace(((1.0, 0.0), (3.0, 20.0)), 20.0, 60.0) == (10.0, 4.0)
    assert transfer_pace(((1.0, 0.0), (2.0, 60.0)), 60.0, 60.0) == (None, None)


def test_transfer_rate_and_eta_come_from_event_timing(
        window: object, monkeypatch: pytest.MonkeyPatch) -> None:
    import gui.main_window
    moments = iter([100.0, 101.0, 102.0, 103.0, 104.0])
    monkeypatch.setattr(gui.main_window.time, "monotonic",
                        lambda: next(moments))
    identifier = str(uuid4())
    window.update_transfer({"id": identifier, "name": "big.bin",
                            "state": "queued", "bytes": 0, "total": 2097152})
    window.update_transfer({"id": identifier, "state": "queued",
                            "bytes": 1048576, "total": 2097152})
    row = window.transfer_rows[identifier]
    assert row["_rate"] == pytest.approx(1048576.0)
    assert row["_eta"] == pytest.approx(1.0)
    window.selected_transfer_id = identifier
    window._show_transfer(identifier)
    assert "MiB/s" in window.transfer_rate.text()
    assert "ETA" in window.transfer_rate.text()


def test_terminal_records_stay_visible_with_separate_verdict(
        window: object) -> None:
    first, second = str(uuid4()), str(uuid4())
    window.update_transfer({"id": first, "name": "done.bin", "state": "saved",
                            "bytes": 10, "total": 10, "path": "/tmp/done.bin"})
    window.update_transfer({"id": second, "name": "broken.bin",
                            "state": "failed", "bytes": 3, "total": 10,
                            "message": "hash mismatch"})
    assert window.transfer_model.rowCount() == 2
    rendered = window.transfer_model.data(window.transfer_model.index(0, 0))
    assert "saved" in rendered and "10/10" in rendered
    assert "saved" != "10/10"


def test_files_page_lists_only_finished_transfers(window: object) -> None:
    active, done = str(uuid4()), str(uuid4())
    window.update_transfer({"id": active, "name": "moving.bin",
                            "state": "queued", "bytes": 1, "total": 10})
    window.update_transfer({"id": done, "name": "done.bin", "state": "saved",
                            "bytes": 10, "total": 10})
    assert window.files_proxy.rowCount() == 1
    assert "1 finished transfer kept locally" in window.files_count.text()
    shown = window.files_proxy.data(window.files_proxy.index(0, 0))
    assert "done.bin" in shown and "saved" in shown


def test_terminal_proxy_never_merges_live_state() -> None:
    model = TransferListModel()
    proxy = TerminalTransferProxy(frozenset({"saved"}))
    proxy.setSourceModel(model)
    model.upsert({"id": "a", "name": "a.bin", "state": "queued",
                  "bytes": 1, "total": 2})
    assert proxy.rowCount() == 0
    model.upsert({"id": "a", "state": "saved", "bytes": 2, "total": 2})
    assert proxy.rowCount() == 1
