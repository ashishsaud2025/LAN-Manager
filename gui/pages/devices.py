"""Devices presentation helpers with no data fetching of their own.

Table cells paint through the shared PeerTableDelegate and mono font role,
so a peer renders identically whether seen here or on Overview."""

from __future__ import annotations

from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QPushButton, QWidget)

from core.peer_repository import PeerRecord
from gui.pages.overview import inspector_row
from gui.widgets.mono_label import MonoLabel
from gui.widgets.peer_row import peer_state

FILTER_TABS = (("all", "All"), ("nearby", "Nearby"),
               ("reachable", "Reachable"), ("compatible", "Compatible"),
               ("stale", "Offline"))


def identity_block() -> tuple[list[QWidget], MonoLabel, MonoLabel]:
    """Build installation/session rows; main_window fills values live."""
    installation_row, installation = inspector_row("Installation:")
    session_row, session = inspector_row("Session:")
    return [installation_row, session_row], installation, session


def filter_tabs() -> tuple[QWidget, dict[str, QPushButton], QButtonGroup]:
    """Build status tabs reusing the activity chip toggle pattern."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    group = QButtonGroup(row)
    group.setExclusive(True)
    buttons: dict[str, QPushButton] = {}
    for key, label in FILTER_TABS:
        button = QPushButton(label)
        button.setCheckable(True)
        button.setProperty("filterchip", True)
        button.setProperty("active", "true" if key == "all" else "false")
        button.setProperty("tab_key", key)
        button.setAccessibleName(f"Filter {label}")
        if key == "all":
            button.setChecked(True)
        group.addButton(button)
        layout.addWidget(button)
        buttons[key] = button
    layout.addStretch(1)
    return row, buttons, group


def tab_counts(records: tuple[PeerRecord, ...]) -> dict[str, int]:
    """Count records per tab with the same rules the filter applies."""
    counts = {key: 0 for key, _ in FILTER_TABS}
    for record in records:
        counts["all"] += 1
        if record.nearby:
            counts["nearby"] += 1
        state = peer_state(record)
        if state == "reachable":
            counts["reachable"] += 1
        elif state == "compatible":
            counts["compatible"] += 1
        elif state == "offline":
            counts["stale"] += 1
    return counts
