"""Observed-session topology that never infers physical network structure."""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QResizeEvent
from PySide6.QtWidgets import (
    QGraphicsItem, QGraphicsScene, QGraphicsView, QLabel, QVBoxLayout, QWidget,
)

from core.discovery import Hello
from core.roster import Peer
from gui.theme import THEMES


class NetworkTopology(QWidget):
    """Draw discovery observations as a local star, not a route or distance map."""

    device_selected = Signal(str)

    def __init__(self, local: Hello, mode: str = "observatory",
                 show_note: bool = True) -> None:
        super().__init__()
        self.local = local
        self.mode = mode if mode in THEMES else "observatory"
        self.peers: tuple[Peer, ...] = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if show_note:
            note = QLabel(
                "Observed from this device. Lines mean a recent UDP announcement was received; "
                "positions do not represent distance, route, gateway, or latency.")
            note.setObjectName("PageSubtitle")
            note.setWordWrap(True)
            layout.addWidget(note)
        self.scene = QGraphicsScene(self)
        self.scene.setSceneRect(0, 0, 900, 520)
        self.scene.selectionChanged.connect(self._selection_changed)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.view.setAccessibleName("Observed network sessions")
        self.view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.view, 1)
        self.set_peers(())

    def set_peers(self, peers: tuple[Peer, ...]) -> None:
        """Replace topology nodes from one immutable roster snapshot."""
        self.peers = peers
        names = ", ".join(peer.hello.name for peer in peers)
        self.view.setAccessibleDescription(
            f"{len(peers)} nearby unverified session(s)"
            + (f": {names}" if names else ". Searching the LAN."))
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
        edge_pen = QPen(QColor(colors["border"]), 1.5, Qt.PenStyle.DashLine)
        positions: list[tuple[Peer, float, float]] = []
        count = len(peers)
        radius = 175.0 if count <= 8 else 205.0
        for index, peer in enumerate(peers):
            angle = -math.pi / 2 + (2 * math.pi * index / max(count, 1))
            x = center_x + radius * math.cos(angle)
            y = center_y + radius * math.sin(angle)
            positions.append((peer, x, y))
            self.scene.addLine(center_x, center_y, x, y, edge_pen)
        self._add_node(center_x, center_y, self.local.name,
                       f"This device\n:{self.local.tcp_port}", "", True, colors)
        for peer, x, y in positions:
            self._add_node(x, y, peer.hello.name,
                           f"{peer.ip}:{peer.hello.tcp_port}",
                           peer.hello.session_id, False, colors)
        if not peers:
            text = self.scene.addText("Searching your LAN...")
            text.setDefaultTextColor(QColor(colors["secondary"]))
            text.setPos(center_x - 105, center_y + 72)
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def _add_node(self, x: float, y: float, title: str, detail: str,
                  session_id: str, local: bool, colors: dict[str, str]) -> None:
        diameter = 58.0 if local else 44.0
        fill = QColor(colors["accent"] if local else colors["surface_high"])
        border = QPen(QColor(colors["signal"] if local else colors["border"]), 2.0)
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
        if session_id:
            group.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
            group.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, True)
            group.setToolTip("Select this observed session in Devices")

    def select_session(self, session_id: str | None) -> bool:
        """Select a peer node by session without emitting a navigation action."""
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
        """Redraw scene colors after an application theme change."""
        self.mode = mode if mode in THEMES else "observatory"
        self.set_peers(self.peers)

    def _selection_changed(self) -> None:
        selected = self.scene.selectedItems()
        if selected:
            session_id = selected[0].data(0)
            if isinstance(session_id, str) and session_id:
                self.device_selected.emit(session_id)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep the observed network centered without introducing scrollbars."""
        super().resizeEvent(event)
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
