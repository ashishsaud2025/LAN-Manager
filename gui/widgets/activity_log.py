"""Inline event log bound to the existing activity model only.

Filter chips group the real categories the shell already emits. The pulse
dot stays static because no live-versus-idle signal exists to drive it."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QSize, Qt, QSortFilterProxyModel
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QListView,
                               QPushButton, QStackedWidget, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem,
                               QVBoxLayout, QWidget)

from gui.models import ActivityEntry, ActivityListModel
from gui.theme.tokens import COLORS, SPACE

_FILTERS = ("all", "discovery", "probes", "warnings")
_DISCOVERY_CATEGORIES = ("Discovery", "Network")
_PROBE_CATEGORIES = ("Workbench",)


class ActivityFilterProxy(QSortFilterProxyModel):
    """Narrow activity rows by chip without touching the source model."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._key = "all"

    def set_filter(self, key: str) -> None:
        """Select one filter chip and refresh the visible rows."""
        if key not in _FILTERS:
            raise ValueError(f"unknown activity filter: {key}")
        self._key = key
        self.invalidate()

    def filter_key(self) -> str:
        """Return the active filter for tests."""
        return self._key

    def filterAcceptsRow(self, source_row: int,
                         source_parent: QModelIndex) -> bool:
        """Accept rows using only category and severity already stored."""
        del source_parent
        model = self.sourceModel()
        if not isinstance(model, ActivityListModel):
            return True
        if not 0 <= source_row < len(model.entries):
            return False
        entry = model.entries[source_row]
        if self._key == "all":
            return True
        if self._key == "warnings":
            return entry.severity == "warning"
        if self._key == "discovery":
            return entry.category in _DISCOVERY_CATEGORIES
        return entry.category in _PROBE_CATEGORIES


class ActivityLogDelegate(QStyledItemDelegate):
    """Paint one timestamped row with an elided message, never wrapped."""

    def paint(self, painter: QPainter | None,
              option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """Draw stamp, colored type, and message from the stored entry."""
        entry = _entry_for(index)
        if painter is None or entry is None:
            super().paint(painter, option, index)
            return
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor(COLORS["surface-container-high"]))
        pad = SPACE["sm"]
        y = option.rect.y() + 17
        mono = QFont("JetBrains Mono")
        mono.setPointSize(9)
        painter.setFont(mono)
        painter.setPen(QColor(COLORS["on-surface-variant"]))
        stamp = entry.timestamp.strftime("%H:%M:%S")
        painter.drawText(option.rect.x() + pad, y, stamp)
        painter.setFont(QFont("JetBrains Mono", 9, QFont.Weight.Bold))
        painter.setPen(QColor(_color_for(entry.category, entry.severity)))
        type_x = option.rect.x() + pad + 62
        painter.drawText(type_x, y, entry.category.upper())
        painter.setPen(QColor(COLORS["on-surface"]))
        message_x = type_x + 92
        painter.setFont(QFont("Inter", 9))
        available = option.rect.right() - message_x - pad
        message = painter.fontMetrics().elidedText(
            entry.title, Qt.TextElideMode.ElideRight, max(0, available))
        painter.drawText(message_x, y, message)
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem,
                 index: QModelIndex) -> QSize:
        """Keep rows compact at one line regardless of message length."""
        del index, option
        return QSize(200, 24)


def _entry_for(index: QModelIndex) -> ActivityEntry | None:
    """Return the stored entry behind a proxy or source index."""
    model = index.model()
    if isinstance(model, QSortFilterProxyModel):
        source = model.sourceModel()
        if not isinstance(source, ActivityListModel):
            return None
        row = model.mapToSource(index).row()
        return source.entries[row] if 0 <= row < len(source.entries) else None
    if isinstance(model, ActivityListModel):
        return model.entries[index.row()] if index.isValid() else None
    return None


def _color_for(category: str, severity: str) -> str:
    """Reuse token colors for type labels without new semantics."""
    if severity == "warning":
        return COLORS["tertiary"]
    if category in (*_DISCOVERY_CATEGORIES, *_PROBE_CATEGORIES):
        return COLORS["primary"]
    return COLORS["on-surface-variant"]


class ActivityLogWidget(QWidget):
    """Header with filter chips above the shared activity rows."""

    def __init__(self, model: ActivityListModel) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE["sm"])
        header = QHBoxLayout()
        dot = QLabel()
        dot.setObjectName("PulseDot")
        dot.setFixedSize(8, 8)
        dot.setToolTip("Static marker; no liveness signal exists.")
        header.addWidget(dot)
        title = QLabel("NETWORK ACTIVITY")
        title.setObjectName("SectionLabel")
        header.addWidget(title)
        header.addStretch(1)
        self.chips = QButtonGroup(self)
        self.chips.setExclusive(True)
        for key, label in (("all", "All"), ("discovery", "Discovery"),
                           ("probes", "Probes"), ("warnings", "Warnings")):
            chip = QPushButton(label)
            chip.setCheckable(True)
            chip.setProperty("filterchip", True)
            chip.setProperty("active", "true" if key == "all" else "false")
            chip.setProperty("filter_key", key)
            chip.toggled.connect(self._chip_toggled)
            self.chips.addButton(chip)
            header.addWidget(chip)
        layout.addLayout(header)
        self.proxy = ActivityFilterProxy(self)
        self.proxy.setSourceModel(model)
        self.stack = QStackedWidget()
        self.empty = QLabel("No operational activity yet")
        self.empty.setObjectName("PageSubtitle")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view = QListView()
        self.view.setModel(self.proxy)
        self.view.setItemDelegate(ActivityLogDelegate(self.view))
        self.view.setAccessibleName("Recent operational activity")
        self.view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.view)
        self.stack.setMaximumHeight(150)
        layout.addWidget(self.stack)
        self.filter_empty = QLabel("No rows match this filter.")
        self.filter_empty.setObjectName("PageSubtitle")
        self.filter_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.filter_empty.setVisible(False)
        layout.addWidget(self.filter_empty)
        self.proxy.rowsInserted.connect(self._sync_filter_empty)
        self.proxy.rowsRemoved.connect(self._sync_filter_empty)
        self.proxy.modelReset.connect(self._sync_filter_empty)

    def _chip_toggled(self, checked: bool) -> None:
        """Apply the newly checked chip through the state QSS pattern."""
        del checked
        for button in self.chips.buttons():
            active = "true" if button.isChecked() else "false"
            button.setProperty("active", active)
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()
            if button.isChecked():
                self.proxy.set_filter(str(button.property("filter_key")))
        self._sync_filter_empty()

    def _sync_filter_empty(self) -> None:
        """Show the filter hint only when rows exist but none match."""
        source = self.proxy.sourceModel()
        total = source.rowCount() if source is not None else 0
        show = total > 0 and self.proxy.rowCount() == 0
        self.filter_empty.setVisible(show)

    def set_filter(self, key: str) -> None:
        """Select one filter chip programmatically for tests and slots."""
        for button in self.chips.buttons():
            if str(button.property("filter_key")) == key:
                button.setChecked(True)
                return
        raise ValueError(f"unknown activity filter: {key}")
