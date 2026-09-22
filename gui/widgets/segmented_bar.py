"""Horizontal proportion bar painted as adjacent rounded segments."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

from gui.theme.tokens import COLORS, RADIUS


class SegmentedBar(QWidget):
    """Bar of (fraction, color_token) segments for host breakdowns."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._segments: list[tuple[float, str]] = []
        self.setMinimumHeight(10)
        self.setMaximumHeight(10)
        self.setAccessibleName("Observed host breakdown")

    def set_segments(self, segments: list[tuple[float, str]]) -> None:
        """Replace segments with clamped fractions and repaint."""
        cleaned = [(min(1.0, max(0.0, fraction)), COLORS.get(token, token))
                   for fraction, token in segments]
        total = sum(fraction for fraction, _ in cleaned)
        self._segments = (cleaned if total > 0
                          else [(1.0, COLORS["surface-container-highest"])])
        self.update()

    def segments(self) -> list[tuple[float, str]]:
        """Return the current segments for tests."""
        return list(self._segments)

    def paintEvent(self, event: QPaintEvent) -> None:
        """Paint adjacent rounded segments from the token palette."""
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        width = self.width()
        x = 0.0
        for index, (fraction, color) in enumerate(self._segments):
            segment_width = width * fraction
            painter.setBrush(QColor(color))
            last = index == len(self._segments) - 1
            painter.drawRoundedRect(int(x), 0, int(segment_width) + (1 if last else 0),
                                    self.height(), RADIUS["sm"], RADIUS["sm"])
            x += segment_width
