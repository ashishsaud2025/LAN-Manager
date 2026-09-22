"""Latency distribution sparkline painted with a teal gradient fill."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import (QColor, QLinearGradient, QPainter, QPainterPath,
                           QPaintEvent, QPen)
from PySide6.QtWidgets import QWidget

from gui.theme.tokens import COLORS


class SparklineWidget(QWidget):
    """Line-plus-gradient chart over RTT samples; empty input draws a hint."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._samples: list[float] = []
        self.setMinimumHeight(44)
        self.setAccessibleName("Latency distribution")

    def set_samples(self, samples: list[float]) -> None:
        """Replace the sample list and repaint on the main thread only."""
        self._samples = [value for value in samples
                         if value is not None and value > 0]
        self.update()

    def samples(self) -> list[float]:
        """Return the current samples for tests."""
        return list(self._samples)

    def paintEvent(self, event: QPaintEvent) -> None:
        """Draw the line, gradient fill, and baseline without blocking."""
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width, height = self.width(), self.height()
        pad = 4
        if len(self._samples) < 2:
            painter.setPen(QPen(QColor(COLORS["outline-variant"]), 1,
                                Qt.PenStyle.DashLine))
            painter.drawText(pad, height // 2 + 4, "No samples yet")
            return
        top = max(self._samples)
        points = [(pad + index * (width - 2 * pad) / (len(self._samples) - 1),
                   height - pad - (value / top) * (height - 2 * pad))
                  for index, value in enumerate(self._samples)]
        baseline = float(height - pad)
        fill = QPainterPath()
        fill.moveTo(points[0][0], baseline)
        for x, y in points:
            fill.lineTo(x, y)
        fill.lineTo(points[-1][0], baseline)
        fill.closeSubpath()
        gradient = QLinearGradient(0, 0, 0, height)
        teal = QColor(COLORS["primary"])
        teal.setAlpha(89)
        clear = QColor(COLORS["primary"])
        clear.setAlpha(0)
        gradient.setColorAt(0, teal)
        gradient.setColorAt(1, clear)
        painter.fillPath(fill, gradient)
        line = QPainterPath()
        line.moveTo(*points[0])
        for x, y in points[1:]:
            line.lineTo(x, y)
        painter.setPen(QPen(QColor(COLORS["primary"]), 1.5))
        painter.drawPath(line)
        painter.setPen(QPen(QColor(COLORS["outline-variant"]), 1))
        painter.drawLine(pad, baseline, width - pad, baseline)
