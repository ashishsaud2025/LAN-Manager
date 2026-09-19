"""Semantic Observatory Dark and Atlas Light themes for LAN Atlas."""

from __future__ import annotations

from typing import Final

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

THEMES: Final[dict[str, dict[str, str]]] = {
    "observatory": {
        "canvas": "#0B0F14", "surface": "#111820", "raised": "#17212B",
        "nav": "#080C10", "ink": "#E7EDF3", "secondary": "#8A9BA8",
        "border": "#263442", "accent": "#25A9B8", "signal": "#5BC0EB",
        "success": "#53A56B", "warning": "#D29A3A", "failure": "#D86050",
        "selection": "#193A45", "field": "#0E151C", "muted": "#1B252F",
        "warning_bg": "#322819", "warning_border": "#725323",
        "warning_text": "#F2C978", "nav_text": "#C9D5DC",
        "nav_selection": "#12303A", "scroll_handle": "#344957",
        "on_accent": "#071015",
    },
    "atlas": {
        "canvas": "#F3F0E8", "surface": "#FFFDF7", "raised": "#FFFFFF",
        "nav": "#17242B", "ink": "#17242B", "secondary": "#526067",
        "border": "#D7D2C6", "accent": "#167C80", "signal": "#2997A3",
        "success": "#397A52", "warning": "#B87718", "failure": "#A84232",
        "selection": "#DCEEEE", "field": "#FFFFFF", "muted": "#ECE9E0",
        "warning_bg": "#F6E7CA", "warning_border": "#DFC08A",
        "warning_text": "#71460D", "nav_text": "#D9E1DE",
        "nav_selection": "#243A42", "scroll_handle": "#50636A",
        "on_accent": "#FFFFFF",
    },
}


def apply_theme(app: QApplication, mode: str = "observatory") -> str:
    """Apply a named theme and return the effective mode."""
    effective = mode if mode in THEMES else "observatory"
    colors = THEMES[effective]
    families = QFontDatabase.families()
    family = next((name for name in ("Inter", "Segoe UI", "Noto Sans")
                   if name in families), app.font().family())
    app.setFont(QFont(family, 10))
    app.setStyleSheet(_stylesheet(colors))
    return effective


def _stylesheet(c: dict[str, str]) -> str:
    return f"""
        QMainWindow, QWidget#AppRoot {{ background: {c['canvas']}; color: {c['ink']}; }}
        QWidget {{ color: {c['ink']}; }}
        QFrame#Navigation {{ background: {c['nav']}; border: 0; }}
        QLabel#Brand {{ color: #E7EDF3; font-size: 21px; font-weight: 700; }}
        QLabel#BrandSubtle {{ color: #8A9BA8; font-size: 11px; }}
        QLabel#NavGroup {{
            color: #728591; font-size: 9px; font-weight: 700; padding: 10px 8px 2px 8px;
        }}
        QListWidget#NavigationList {{
            background: transparent; color: {c['nav_text']}; border: 0; outline: 0;
            padding: 4px;
        }}
        QListWidget#NavigationList::item {{
            border-radius: 6px; margin: 1px 0; padding: 8px 12px;
        }}
        QListWidget#NavigationList::item:selected {{
            background: {c['nav_selection']}; color: {c['signal']}; font-weight: 600;
            border-left: 3px solid {c['accent']};
        }}
        QListWidget#NavigationList::item:disabled {{ color: #60717B; }}
        QListWidget#NavigationList QScrollBar:vertical {{
            background: transparent; width: 6px; margin: 2px 0;
        }}
        QListWidget#NavigationList QScrollBar::handle:vertical {{
            background: {c['scroll_handle']}; border-radius: 3px; min-height: 28px;
        }}
        QListWidget#NavigationList QScrollBar::add-line:vertical,
        QListWidget#NavigationList QScrollBar::sub-line:vertical {{ height: 0; }}
        QListWidget#NavigationList QScrollBar::add-page:vertical,
        QListWidget#NavigationList QScrollBar::sub-page:vertical {{ background: transparent; }}
        QLabel#TrustBadge {{
            color: {c['warning_text']}; background: {c['warning_bg']};
            border: 1px solid {c['warning_border']}; border-radius: 6px; padding: 7px 9px;
        }}
        QFrame#StatusStrip, QFrame[card="true"] {{
            background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 8px;
        }}
        QLabel#PageTitle {{ font-size: 24px; font-weight: 700; color: {c['ink']}; }}
        QLabel#PageSubtitle {{ color: {c['secondary']}; font-size: 11px; }}
        QLabel#MetricValue {{ font-size: 25px; font-weight: 700; color: {c['accent']}; }}
        QLabel#MetricLabel {{ color: {c['secondary']}; }}
        QLabel#TechnicalDetail {{ color: {c['secondary']}; font-family: monospace; }}
        QLabel#SectionLabel {{
            color: {c['secondary']}; font-size: 10px; font-weight: 700;
        }}
        QLabel[chip="true"] {{
            color: {c['signal']}; background: {c['selection']};
            border: 1px solid {c['accent']}; border-radius: 6px; padding: 4px 7px;
        }}
        QLabel[warning="true"] {{
            color: {c['warning_text']}; background: {c['warning_bg']};
            border: 1px solid {c['warning_border']}; border-radius: 7px; padding: 9px;
        }}
        QPushButton {{
            background: {c['raised']}; color: {c['ink']}; border: 1px solid {c['border']};
            border-radius: 6px; padding: 8px 12px;
        }}
        QPushButton:hover {{ border-color: {c['accent']}; color: {c['signal']}; }}
        QPushButton:pressed {{ background: {c['selection']}; }}
        QPushButton:focus, QLineEdit:focus, QComboBox:focus,
        QTextEdit:focus, QPlainTextEdit:focus, QListView:focus {{
            border: 2px solid {c['signal']};
        }}
        QPushButton:disabled {{ color: {c['secondary']}; background: {c['muted']}; }}
        QPushButton[primary="true"] {{
            background: {c['accent']}; color: {c['on_accent']}; border-color: {c['accent']};
            font-weight: 600;
        }}
        QPushButton[primary="true"]:hover {{ background: {c['signal']}; color: #071015; }}
        QPushButton[security="true"] {{
            color: {c['warning_text']}; background: {c['warning_bg']};
            border: 1px solid {c['warning_border']}; padding: 6px 9px;
        }}
        QLineEdit, QComboBox, QTextEdit, QPlainTextEdit, QListView {{
            background: {c['field']}; color: {c['ink']}; border: 1px solid {c['border']};
            border-radius: 6px; padding: 7px; selection-background-color: {c['accent']};
        }}
        QComboBox QAbstractItemView {{
            background: {c['raised']}; color: {c['ink']}; selection-background-color: {c['accent']};
        }}
        QListView::item {{ padding: 9px; border-bottom: 1px solid {c['border']}; }}
        QListView::item:selected {{ background: {c['selection']}; color: {c['ink']}; }}
        QSplitter::handle {{ background: {c['border']}; width: 1px; height: 1px; }}
        QProgressBar {{
            border: 1px solid {c['border']}; border-radius: 5px; text-align: center;
            background: {c['muted']}; color: {c['ink']};
        }}
        QProgressBar::chunk {{ background: {c['signal']}; border-radius: 4px; }}
        QGraphicsView {{
            background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 8px;
        }}
        QToolTip {{ background: {c['nav']}; color: white; border: 0; padding: 5px; }}
    """
