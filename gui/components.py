"""Small reusable widgets for the LAN Atlas shell."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)


class NavigationRail(QFrame):
    """Persistent grouped primary navigation."""

    selected = Signal(int)

    def __init__(self, groups: tuple[tuple[str, tuple[tuple[str, int | None], ...]], ...]) -> None:
        super().__init__()
        self.setObjectName("Navigation")
        self.setFixedWidth(184)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 20, 16, 16)
        brand = QLabel("LAN ATLAS")
        brand.setObjectName("Brand")
        subtitle = QLabel("your local network workspace")
        subtitle.setObjectName("BrandSubtle")
        layout.addWidget(brand)
        layout.addWidget(subtitle)
        layout.addSpacing(18)
        self.list = QListWidget()
        self.list.setObjectName("NavigationList")
        self.list.setAccessibleName("Primary navigation")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.page_items: dict[int, QListWidgetItem] = {}
        for group, pages in groups:
            heading = QListWidgetItem(group.upper())
            heading.setFlags(Qt.ItemFlag.NoItemFlags)
            heading.setData(257, "group")
            self.list.addItem(heading)
            for page, index in pages:
                if index is None:
                    continue
                label = page
                item = QListWidgetItem(label)
                item.setData(256, index)
                self.page_items[index] = item
                self.list.addItem(item)
        self.list.currentItemChanged.connect(self._emit_page)
        self.list.setCurrentItem(self.page_items[0])
        layout.addWidget(self.list, 1)

    def select(self, index: int) -> None:
        """Select a page from keyboard shortcuts or contextual actions."""
        item = self.page_items.get(index)
        if item is not None:
            self.list.setCurrentItem(item)

    def _emit_page(self, current: QListWidgetItem | None,
                   previous: QListWidgetItem | None) -> None:
        del previous
        if current is not None:
            value: Any = current.data(256)
            if isinstance(value, int):
                self.selected.emit(value)


def page_header(title: str, subtitle: str) -> QWidget:
    """Build a consistent page heading."""
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 6)
    heading = QLabel(title)
    heading.setObjectName("PageTitle")
    detail = QLabel(subtitle)
    detail.setObjectName("PageSubtitle")
    detail.setWordWrap(True)
    layout.addWidget(heading)
    layout.addWidget(detail)
    return widget


def card(title: str, value: str) -> tuple[QFrame, QLabel]:
    """Create a compact overview metric card and return its value label."""
    frame = QFrame()
    frame.setProperty("card", True)
    layout = QVBoxLayout(frame)
    label = QLabel(title)
    label.setObjectName("MetricLabel")
    metric = QLabel(value)
    metric.setObjectName("MetricValue")
    layout.addWidget(label)
    layout.addWidget(metric)
    return frame, metric


def action_button(text: str, callback: Callable[[], None],
                  primary: bool = False) -> QPushButton:
    """Create a consistently styled action button."""
    button = QPushButton(text)
    button.setProperty("primary", primary)
    button.clicked.connect(callback)
    return button
