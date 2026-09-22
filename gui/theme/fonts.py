"""Application font setup with graceful fallback when binaries are absent.

Bundled Inter and JetBrains Mono files are preferred so telemetry aligns
across machines, while system families keep headless tests working."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from gui.theme.tokens import FONTS

_FONT_DIR = Path(__file__).resolve().parent / "fonts"
_BUNDLED = (
    "Inter-Regular.ttf", "Inter-Medium.ttf", "Inter-SemiBold.ttf",
    "Inter-Bold.ttf", "JetBrainsMono-Regular.ttf", "JetBrainsMono-Medium.ttf",
    "JetBrainsMono-SemiBold.ttf", "JetBrainsMono-Bold.ttf",
)
_FALLBACK_UI = ("Inter", "Segoe UI", "Noto Sans")
_FALLBACK_MONO = ("JetBrains Mono", "Cascadia Mono", "Consolas",
                  "Noto Sans Mono")


def ensure_application_fonts(app: QApplication) -> None:
    """Register bundled fonts when present and apply Inter as default."""
    for name in _BUNDLED:
        path = _FONT_DIR / name
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
    families = set(QFontDatabase.families())
    ui_family = next((name for name in (str(FONTS["ui"]), *_FALLBACK_UI)
                      if name in families), app.font().family())
    app.setFont(QFont(ui_family, 10))


def mono_family() -> str:
    """Return the best available monospace family for technical values."""
    families = set(QFontDatabase.families())
    return next((name for name in (str(FONTS["mono"]), *_FALLBACK_MONO)
                 if name in families), "monospace")
