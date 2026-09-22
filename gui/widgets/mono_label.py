"""Label for raw technical values rendered in the mono family."""

from __future__ import annotations

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QWidget

from gui.theme.fonts import mono_family


class MonoLabel(QLabel):
    """QLabel that always uses the mono family for IPs, ports, and RTT."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        font = QFont(mono_family())
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)
