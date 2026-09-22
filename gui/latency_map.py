"""Radial latency map where distance encodes measured latency only."""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QResizeEvent
from PySide6.QtWidgets import (
    QGraphicsItem, QGraphicsScene, QGraphicsView, QLabel, QVBoxLayout, QWidget,
)

from core.discovery import Hello
from core.peer_repository import PeerRecord
from gui.theme import THEMES

INNER_RADIUS = 70.0
OUTER_RADIUS = 205.0
UNMEASURED_RADIUS = 232.0
MAX_RTT_REFERENCE = 100.0
RING_RTSS = (1.0, 10.0, 100.0)


def radius_for_latency(latency_ms: float | None) -> float:
    """Map one latency sample to a stable radial distance."""
    if latency_ms is None or not math.isfinite(latency_ms) or latency_ms <= 0:
        return UNMEASURED_RADIUS
    clamped = min(latency_ms, MAX_RTT_REFERENCE)
    fraction = math.log1p(clamped) / math.log1p(MAX_RTT_REFERENCE)
    return INNER_RADIUS + (OUTER_RADIUS - INNER_RADIUS) * fraction


class LatencyMap(QWidget):
    """Show measured latency as radial distance without implying physical layout."""

    device_selected = Signal(str)

    def __init__(self, local: Hello, mode: str = "observatory") -> None:
        super().__init__()
        self.local = local
        self.mode = mode if mode in THEMES else "observatory"
        self.records: tuple[PeerRecord, ...] = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel(
            "Radial distance = measured latency from this device "
            "(ping RTT, TCP handshake, or ECHO round-trip). "
            "Grey nodes have no measurement yet. "
            "Positions do not represent physical topology, route, or distance.")
        note.setObjectName("PageSubtitle")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.scene = QGraphicsScene(self)
        self.scene.setSceneRect(0, 0, 900, 520)
        self.scene.selectionChanged.connect(self._selection_changed)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.view.setAccessibleName("Latency map by measured latency")
        self.view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.view, 1)
        self.set_records(())

    def set_records(self, records: tuple[PeerRecord, ...]) -> None:
        """Replace latency nodes from one immutable repository snapshot."""
        self.records = records
        measured = [item.latency_ms for item in records
                    if (isinstance(item.latency_ms, (float, int))
                        and not isinstance(item.latency_ms, bool)
                        and math.isfinite(float(item.latency_ms))
                        and 0 < float(item.latency_ms) < 60000)]
        summary = (f"{len(records)} nearby session(s), "
                   f"{len(measured)} with measured latency"
                   + (", no measurement yet. Run Ping, TCP, or ECHO."
                      if not measured else "."))
        self.view.setAccessibleDescription(summary)
        self.scene.clear()
        colors = THEMES[self.mode]
        self.scene.setBackgroundBrush(QBrush(QColor(colors["surface_lowest"])))
        center_x, center_y = 450.0, 260.0
        grid_color = QColor(colors["border"])
        grid_color.setAlpha(85)
        grid_pen = QPen(grid_color, 1.0, Qt.PenStyle.DotLine)
        for x in range(30, 900, 30):
            self.scene.addLine(x, 0, x, 520, grid_pen)
        for y in range(20, 520, 30):
            self.scene.addLine(0, y, 900, y, grid_pen)
        for ring_rtt in RING_RTSS:
            radius = radius_for_latency(ring_rtt)
            ring_pen = QPen(QColor(colors["border"]), 1.0,
                            Qt.PenStyle.DashLine if ring_rtt >= 100 else Qt.PenStyle.SolidLine)
            self.scene.addEllipse(center_x - radius, center_y - radius,
                                  radius * 2, radius * 2, ring_pen)
            label = self.scene.addText(f"{ring_rtt:.0f} ms")
            label.setDefaultTextColor(QColor(colors["secondary"]))
            label.setFont(QFont(label.font().family(), 8))
            label.setPos(center_x + 6, center_y - radius - 8)
        if not measured:
            hint = self.scene.addText("No measurement yet — run Ping, TCP, or ECHO")
            hint.setDefaultTextColor(QColor(colors["secondary"]))
            hint.setPos(center_x - 165, center_y + OUTER_RADIUS + 18)
        edge_pen = QPen(QColor(colors["border"]), 1.2, Qt.PenStyle.DashLine)
        count = len(records)
        for index, record in enumerate(records):
            angle = -math.pi / 2 + (2 * math.pi * index / max(count, 1))
            radius = radius_for_latency(record.latency_ms)
            x = center_x + radius * math.cos(angle)
            y = center_y + radius * math.sin(angle)
            self.scene.addLine(center_x, center_y, x, y, edge_pen)
        self._add_node(center_x, center_y, self.local.name,
                       f"This device\n:{self.local.tcp_port}", "", True, colors, None)
        for index, record in enumerate(records):
            angle = -math.pi / 2 + (2 * math.pi * index / max(count, 1))
            radius = radius_for_latency(record.latency_ms)
            x = center_x + radius * math.cos(angle)
            y = center_y + radius * math.sin(angle)
            detail = self._detail_for(record)
            self._add_node(x, y, record.hello.name, detail,
                           record.session_id, False, colors, record)
        if not records:
            text = self.scene.addText("Searching your LAN...")
            text.setDefaultTextColor(QColor(colors["secondary"]))
            text.setPos(center_x - 105, center_y + 72)
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def _detail_for(self, record: PeerRecord) -> str:
        endpoint = f"{record.ip}:{record.hello.tcp_port}"
        if record.latency_ms is None:
            return f"{endpoint}\nNo measurement yet"
        source = record.latency_source or "measured"
        return f"{endpoint}\n{record.latency_ms:.1f} ms · {source}"

    def _add_node(self, x: float, y: float, title: str, detail: str,
                  session_id: str, local: bool, colors: dict[str, str],
                  record: PeerRecord | None) -> None:
        diameter = 52.0 if local else 38.0
        if local:
            fill = QColor(colors["accent"])
            border = QPen(QColor(colors["signal"]), 2.0)
        elif record is not None and record.latency_ms is None:
            fill = QColor(colors["surface_high"])
            border = QPen(QColor(colors["border"]), 1.5, Qt.PenStyle.DashLine)
        else:
            fill = QColor(colors["surface_high"])
            border = QPen(QColor(colors["accent"]), 2.0)
        ellipse = self.scene.addEllipse(-diameter / 2, -diameter / 2,
                                        diameter, diameter, border, fill)
        title_item = self.scene.addText(title)
        title_item.setDefaultTextColor(QColor(colors["ink"]))
        title_item.setFont(QFont(title_item.font().family(), 9, QFont.Weight.Bold))
        title_bounds = title_item.boundingRect()
        title_item.setPos(-title_bounds.width() / 2, diameter / 2 + 8)
        detail_item = self.scene.addText(detail)
        detail_item.setDefaultTextColor(QColor(colors["secondary"]))
        detail_item.setFont(QFont(detail_item.font().family(), 8))
        detail_bounds = detail_item.boundingRect()
        detail_item.setPos(-detail_bounds.width() / 2, diameter / 2 + 25)
        label_width = max(title_bounds.width(), detail_bounds.width()) + 16
        label_height = title_bounds.height() + detail_bounds.height() + 2
        label = self.scene.addRect(
            -label_width / 2, diameter / 2 + 7, label_width, label_height,
            QPen(QColor(colors["border"]), 1), QBrush(QColor(colors["raised"])))
        label.setZValue(-1)
        group = self.scene.createItemGroup([ellipse, label, title_item, detail_item])
        group.setPos(x, y)
        group.setData(0, session_id)
        if session_id and record is not None:
            group.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
            group.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, True)
            if record.latency_ms is None:
                tip = (f"{record.hello.name} · {record.ip}:{record.hello.tcp_port} · "
                       "No latency measured yet")
            else:
                tip = (f"{record.hello.name} · {record.ip}:{record.hello.tcp_port} · "
                       f"{record.latency_ms:.1f} ms via {record.latency_source}")
            group.setToolTip(tip)

    def select_session(self, session_id: str | None) -> bool:
        """Select a latency node by session without emitting navigation."""
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
        """Redraw latency colors after an application theme change."""
        self.mode = mode if mode in THEMES else "observatory"
        self.set_records(self.records)

    def _selection_changed(self) -> None:
        selected = self.scene.selectedItems()
        if selected:
            session_id = selected[0].data(0)
            if isinstance(session_id, str) and session_id:
                self.device_selected.emit(session_id)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep the latency map centered without introducing scrollbars."""
        super().resizeEvent(event)
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
