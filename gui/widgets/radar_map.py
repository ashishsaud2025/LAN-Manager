"""Radar latency map with stable hashed angles and state colors."""

from __future__ import annotations

import hashlib
import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QResizeEvent
from PySide6.QtWidgets import (
    QGraphicsItem, QGraphicsScene, QGraphicsView, QLabel, QVBoxLayout, QWidget,
)

from core.discovery import Hello
from core.peer_repository import PeerRecord
from gui.latency_map import RING_RTSS, radius_for_latency
from gui.theme.tokens import COLORS

HIGH_LATENCY_MS = 25.0


def _angle_for(session_id: str) -> float:
    """Derive a stable angle from the session id to stop layout jumps."""
    digest = hashlib.sha256(session_id.encode("utf-8")).digest()
    return 2 * math.pi * int.from_bytes(digest[:8], "big") / 2 ** 64


class RadarMapWidget(QWidget):
    """Latency radar reusing the Overview selection state for clicks."""

    device_selected = Signal(str)

    def __init__(self, local: Hello, mode: str = "observatory") -> None:
        super().__init__()
        self.local = local
        self.mode = mode
        self.records: tuple[PeerRecord, ...] = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel(
            "Radial distance = measured latency from this device. "
            "Grey nodes have no measurement yet. "
            "Positions never represent physical topology or routes.")
        note.setObjectName("PageSubtitle")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.scene = QGraphicsScene(self)
        self.scene.setSceneRect(0, 0, 900, 520)
        self.scene.selectionChanged.connect(self._selection_changed)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.view.setAccessibleName("Latency radar by measured latency")
        self.view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.view, 1)
        self.set_records(())

    def set_records(self, records: tuple[PeerRecord, ...]) -> None:
        """Replace radar nodes from one immutable repository snapshot."""
        self.records = records
        measured = sum(1 for item in records if item.latency_ms is not None)
        self.view.setAccessibleDescription(
            f"{len(records)} nearby session(s), "
            f"{measured} with measured latency.")
        self.scene.clear()
        center_x, center_y = 450.0, 260.0
        grid = QColor(COLORS["surface-container-highest"])
        grid.setAlpha(85)
        grid_pen = QPen(grid, 1.0, Qt.PenStyle.DotLine)
        for x in range(30, 900, 30):
            self.scene.addLine(x, 0, x, 520, grid_pen)
        for y in range(20, 520, 30):
            self.scene.addLine(0, y, 900, y, grid_pen)
        for ring_rtt in RING_RTSS:
            radius = radius_for_latency(ring_rtt)
            ring_pen = QPen(QColor(COLORS["surface-container-highest"]), 1.0)
            self.scene.addEllipse(center_x - radius, center_y - radius,
                                  radius * 2, radius * 2, ring_pen)
            label = self.scene.addText(f"{ring_rtt:.0f} ms")
            label.setDefaultTextColor(QColor(COLORS["on-surface-variant"]))
            label.setFont(QFont(label.font().family(), 8))
            label.setPos(center_x + 6, center_y - radius - 8)
        edge_pen = QPen(QColor(COLORS["surface-container-highest"]), 1.2,
                        Qt.PenStyle.DashLine)
        for record in records:
            angle = _angle_for(record.session_id)
            radius = radius_for_latency(record.latency_ms)
            x = center_x + radius * math.cos(angle)
            y = center_y + radius * math.sin(angle)
            self.scene.addLine(center_x, center_y, x, y, edge_pen)
        self._add_node(center_x, center_y, self.local.name,
                       f"This device\n:{self.local.tcp_port}", "", True, None)
        for record in records:
            angle = _angle_for(record.session_id)
            radius = radius_for_latency(record.latency_ms)
            x = center_x + radius * math.cos(angle)
            y = center_y + radius * math.sin(angle)
            endpoint = f"{record.ip}:{record.hello.tcp_port}"
            if record.latency_ms is None:
                detail = f"{endpoint}\nNo measurement yet"
            else:
                source = record.latency_source or "measured"
                detail = f"{endpoint}\n{record.latency_ms:.1f} ms via {source}"
            self._add_node(x, y, record.hello.name, detail,
                           record.session_id, False, record)
        if not records:
            text = self.scene.addText("Searching your LAN...")
            text.setDefaultTextColor(QColor(COLORS["on-surface-variant"]))
            text.setPos(center_x - 105, center_y + 72)
            text.setData(1, "empty-hint")
            hint = self.scene.addText("No measurement yet, run Ping, TCP, or ECHO")
            hint.setDefaultTextColor(QColor(COLORS["on-surface-variant"]))
            hint.setPos(center_x - 170, center_y + 205 + 18)
            hint.setData(1, "empty-hint")
        self.view.fitInView(self.scene.sceneRect(),
                            Qt.AspectRatioMode.KeepAspectRatio)

    def empty_hints(self) -> list[QGraphicsItem]:
        """Return empty-state items still in the scene for tests."""
        return [item for item in self.scene.items()
                if item.data(1) == "empty-hint"]

    def _pen_for(self, record: PeerRecord) -> QPen:
        """Map repository evidence to border color without new states."""
        if not record.nearby or record.latency_ms is None:
            return QPen(QColor(COLORS["outline-variant"]), 1.5,
                        Qt.PenStyle.DashLine)
        if record.latency_ms >= HIGH_LATENCY_MS:
            return QPen(QColor(COLORS["tertiary"]), 2.0)
        return QPen(QColor(COLORS["primary"]), 2.0)

    def _add_node(self, x: float, y: float, title: str, detail: str,
                  session_id: str, local: bool,
                  record: PeerRecord | None) -> None:
        diameter = 52.0 if local else 38.0
        if local:
            fill = QColor(COLORS["primary"])
            border = QPen(QColor(COLORS["surface-tint"]), 2.0)
        elif record is None:
            fill = QColor(COLORS["surface-container-high"])
            border = QPen(QColor(COLORS["surface-container-highest"]), 2.0)
        else:
            fill = QColor(COLORS["surface-container-high"])
            border = self._pen_for(record)
        ellipse = self.scene.addEllipse(-diameter / 2, -diameter / 2,
                                        diameter, diameter, border, fill)
        title_item = self.scene.addText(title)
        title_item.setDefaultTextColor(QColor(COLORS["on-surface"]))
        title_item.setFont(QFont(title_item.font().family(), 9, QFont.Weight.Bold))
        title_bounds = title_item.boundingRect()
        title_item.setPos(-title_bounds.width() / 2, diameter / 2 + 8)
        detail_item = self.scene.addText(detail)
        detail_item.setDefaultTextColor(QColor(COLORS["on-surface-variant"]))
        detail_item.setFont(QFont(detail_item.font().family(), 8))
        detail_bounds = detail_item.boundingRect()
        detail_item.setPos(-detail_bounds.width() / 2, diameter / 2 + 25)
        label_width = max(title_bounds.width(), detail_bounds.width()) + 16
        label_height = title_bounds.height() + detail_bounds.height() + 2
        label = self.scene.addRect(
            -label_width / 2, diameter / 2 + 7, label_width, label_height,
            QPen(QColor(COLORS["surface-container-highest"]), 1),
            QBrush(QColor(COLORS["surface-container"])))
        label.setZValue(-1)
        group = self.scene.createItemGroup([ellipse, label, title_item, detail_item])
        group.setPos(x, y)
        group.setData(0, session_id)
        if session_id and record is not None:
            group.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
            group.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, True)
            group.setToolTip(f"{record.hello.name} at {record.ip}")
        elif session_id:
            group.setToolTip(title)

    def select_session(self, session_id: str | None) -> bool:
        """Select a radar node by session without emitting navigation."""
        self.scene.blockSignals(True)
        try:
            self.scene.clearSelection()
            for item in self.scene.items():
                if item.data(0) == session_id:
                    item.setSelected(True)
                    item.setFocus()
                    return True
        finally:
            self.scene.blockSignals(False)
        return False

    def set_theme(self, mode: str) -> None:
        """Redraw radar colors after an application theme change."""
        self.mode = mode
        self.set_records(self.records)

    def _selection_changed(self) -> None:
        selected = self.scene.selectedItems()
        if selected:
            session_id = selected[0].data(0)
            if isinstance(session_id, str) and session_id:
                self.device_selected.emit(session_id)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep the radar centered without introducing scrollbars."""
        super().resizeEvent(event)
        self.view.fitInView(self.scene.sceneRect(),
                            Qt.AspectRatioMode.KeepAspectRatio)
