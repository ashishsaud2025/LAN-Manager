"""Structured nearby-session rows painted from PeerRecord evidence.

The list model keeps returning plain strings, so Network and Devices can
reuse the same records later without inheriting a joined-text layout."""

from __future__ import annotations

import time

from PySide6.QtCore import QModelIndex, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from core.peer_repository import PeerRecord
from gui.models import PeerListModel, PeerTableModel, capability_label
from gui.theme.fonts import mono_family
from gui.theme.tokens import COLORS

ROW_HEIGHT = 68
TABLE_ROW_HEIGHT = 62

_PILL_TEXT = {"nearby": "Nearby", "reachable": "Reachable",
              "compatible": "Compatible", "offline": "Offline"}
_ROW_TEXT = {"nearby": "Nearby", "reachable": "Reachable",
             "compatible": "Compatible", "offline": "Offline / stale"}


def pill_display_text(state: str) -> str:
    """Return inspector row wording for one shared pill state key."""
    return _ROW_TEXT[state]


def peer_state(record: PeerRecord) -> str:
    """Map repository evidence to the allowed pill states for rows."""
    if not record.nearby:
        return "offline"
    if record.compatibility_state.value == "compatible":
        return "compatible"
    if record.reachability_state.value == "reachable":
        return "reachable"
    return "nearby"


def pill_colors(state: str) -> tuple[str, str, str]:
    """Return token-sourced foreground, border, and fill for a pill."""
    return {
        "nearby": (COLORS["on-surface"], COLORS["outline-variant"],
                   COLORS["surface-container-high"]),
        "reachable": (COLORS["primary"], COLORS["primary"],
                      COLORS["surface-container"]),
        "compatible": (COLORS["primary-fixed-dim"],
                       COLORS["primary-container"],
                       COLORS["surface-container"]),
        "offline": (COLORS["outline"], COLORS["outline-variant"],
                    COLORS["surface-container-lowest"]),
    }[state]


def _record_for(index: QModelIndex) -> PeerRecord | None:
    """Return the record behind a list index without parsing text."""
    model = index.model()
    if isinstance(model, PeerListModel) and index.isValid():
        return model.record_at(index.row())
    return None


def _table_record_for(index: QModelIndex) -> PeerRecord | None:
    """Return the record behind a table index without parsing text."""
    model = index.model()
    if isinstance(model, PeerTableModel) and index.isValid():
        return model.record_at(index.row())
    return None


def _paint_pill(painter: QPainter, x: int, y: int, width: int,
                state: str) -> None:
    """Paint one state pill with token colors shared by every surface."""
    foreground, border, fill = pill_colors(state)
    painter.setBrush(QColor(fill))
    pen = painter.pen()
    pen.setColor(QColor(border))
    pen.setStyle(Qt.PenStyle.DashLine
                 if state == "offline" else Qt.PenStyle.SolidLine)
    painter.setPen(pen)
    painter.drawRoundedRect(x, y, width, 18, 4, 4)
    painter.setFont(QFont(mono_family(), 9))
    painter.setPen(QColor(foreground))
    painter.drawText(x + 8, y + 14, _PILL_TEXT[state])


class PeerRowDelegate(QStyledItemDelegate):
    """Paint name, state pill, endpoint, age, and tags as separate parts."""

    def paint(self, painter: QPainter | None,
              option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """Draw one structured row from the record, never joined text."""
        record = _record_for(index)
        if painter is None or record is None:
            super().paint(painter, option, index)
            return
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect,
                             QColor(COLORS["surface-container-high"]))
        pad = 10
        left = option.rect.x() + pad
        right = option.rect.right() - pad
        state = peer_state(record)
        pill_font = QFont(mono_family(), 9)
        painter.setFont(pill_font)
        pill_width = (painter.fontMetrics().horizontalAdvance(
            _PILL_TEXT[state]) + 16)
        name_font = QFont("Inter", 11, QFont.Weight.Bold)
        painter.setFont(name_font)
        painter.setPen(QColor(COLORS["on-surface"]))
        name = painter.fontMetrics().elidedText(
            record.hello.name, Qt.TextElideMode.ElideRight,
            max(0, int(right - pill_width - 8 - left)))
        painter.drawText(int(left), option.rect.y() + 22, name)
        _paint_pill(painter, int(right - pill_width), option.rect.y() + 8,
                    int(pill_width), state)
        painter.setFont(QFont(mono_family(), 9))
        painter.setPen(QColor(COLORS["on-surface-variant"]))
        age = max(0.0, time.monotonic() - record.last_seen)
        endpoint = f"{record.ip}:{record.hello.tcp_port}"
        painter.drawText(int(left), option.rect.y() + 40, endpoint)
        painter.setPen(QColor(COLORS["outline"]))
        age_x = int(left + painter.fontMetrics().horizontalAdvance(endpoint) + 8)
        painter.drawText(age_x, option.rect.y() + 40, f"seen {age:.1f}s ago")
        tag_x = int(left)
        painter.setFont(QFont(mono_family(), 9))
        for capability in record.hello.capabilities:
            tag = capability_label(capability)
            tag_width = painter.fontMetrics().horizontalAdvance(tag) + 12
            painter.setBrush(QColor(COLORS["surface-container-low"]))
            tag_pen = painter.pen()
            tag_pen.setColor(QColor(COLORS["outline-variant"]))
            tag_pen.setStyle(Qt.PenStyle.SolidLine)
            painter.setPen(tag_pen)
            painter.drawRoundedRect(tag_x, option.rect.y() + 46,
                                    int(tag_width), 16, 4, 4)
            painter.setPen(QColor(COLORS["on-surface-variant"]))
            painter.drawText(tag_x + 6, option.rect.y() + 59, tag)
            tag_x += int(tag_width) + 4
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem,
                 index: QModelIndex) -> QSize:
        """Fix row height so wrapped text can never collapse the layout."""
        del index, option
        return QSize(280, ROW_HEIGHT)


class PeerTableDelegate(QStyledItemDelegate):
    """Paint the session cell as name plus state pill for table rows."""

    def paint(self, painter: QPainter | None,
              option: QStyleOptionViewItem, index: QModelIndex) -> None:
        """Draw the session cell from the record, never joined text."""
        record = _table_record_for(index)
        if painter is None or record is None:
            super().paint(painter, option, index)
            return
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect,
                             QColor(COLORS["surface-container-high"]))
        pad = 8
        left = option.rect.x() + pad
        right = option.rect.right() - pad
        state = peer_state(record)
        painter.setFont(QFont(mono_family(), 9))
        pill_width = (painter.fontMetrics().horizontalAdvance(
            _PILL_TEXT[state]) + 16)
        painter.setFont(QFont("Inter", 11, QFont.Weight.Bold))
        painter.setPen(QColor(COLORS["on-surface"]))
        name = painter.fontMetrics().elidedText(
            record.hello.name, Qt.TextElideMode.ElideRight,
            max(0, int(right - pill_width - 8 - left)))
        middle = option.rect.y() + option.rect.height() // 2 + 4
        painter.drawText(int(left), middle, name)
        _paint_pill(painter, int(right - pill_width), middle - 14,
                    int(pill_width), state)
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem,
                 index: QModelIndex) -> QSize:
        """Match the table row height so pills stay vertically centered."""
        del index, option
        return QSize(200, TABLE_ROW_HEIGHT)
