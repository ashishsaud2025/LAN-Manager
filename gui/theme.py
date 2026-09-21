"""Semantic Observatory Dark and Atlas Light themes for LAN Atlas."""

from __future__ import annotations

import sys
from typing import Final

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

SPACING: Final[dict[str, int]] = {
    "xs": 4, "sm": 8, "md": 12, "lg": 20, "xl": 28,
}
RADII: Final[dict[str, int]] = {"sm": 4, "control": 6, "card": 8}
GEOMETRY: Final[dict[str, int]] = {
    "navigation": 280,
    "navigation_compact": 176,
    "responsive_breakpoint": 1500,
    "minimum_width": 640,
    "minimum_height": 360,
}
TYPOGRAPHY: Final[dict[str, int]] = {
    "body": 10, "body_small": 11, "section": 10, "title": 24, "brand": 21,
}

THEMES: Final[dict[str, dict[str, str]]] = {
    "observatory": {
        "canvas": "#051424", "surface": "#122131", "raised": "#1C2B3C",
        "nav": "#010F1F", "ink": "#D4E4FA", "secondary": "#BACAC4",
        "border": "#273647", "accent": "#46EFCF", "signal": "#2ADEC0",
        "success": "#46EFCF", "warning": "#FFB95F", "failure": "#FFB4AB",
        "selection": "#1C2B3C", "field": "#0D1C2D", "muted": "#0D1C2D",
        "warning_bg": "#2A1D0D", "warning_border": "#653E00",
        "warning_text": "#FFCE94", "nav_text": "#BACAC4",
        "nav_selection": "#1C2B3C", "scroll_handle": "#3B4A45",
        "on_accent": "#00382E", "hover_ink": "#00382E", "brand": "#D4E4FA",
        "nav_group": "#64748B", "tooltip_ink": "#FFFFFF",
        "surface_lowest": "#010F1F", "surface_low": "#0D1C2D",
        "surface_high": "#1C2B3C", "surface_highest": "#273647",
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
        "on_accent": "#FFFFFF", "hover_ink": "#071015", "brand": "#F3F0E8",
        "nav_group": "#9FB0B5", "tooltip_ink": "#FFFFFF",
        "surface_lowest": "#FFFFFF", "surface_low": "#F7F4EC",
        "surface_high": "#ECE9E0", "surface_highest": "#E1DDD2",
    },
}


def apply_theme(app: QApplication, mode: str = "observatory") -> str:
    """Apply a named theme and return the effective mode."""
    effective = mode if mode in THEMES else "observatory"
    colors = THEMES[effective]
    families = QFontDatabase.families()
    platform_ui = "Segoe UI" if sys.platform == "win32" else app.font().family()
    family = next((name for name in ("Inter", "Segoe UI", "Noto Sans")
                   if name in families), platform_ui)
    platform_mono = "Consolas" if sys.platform == "win32" else "monospace"
    technical = next((name for name in ("JetBrains Mono", "Cascadia Mono",
                                        "Consolas", "Noto Sans Mono")
                      if name in families), platform_mono)
    app.setFont(QFont(family, TYPOGRAPHY["body"]))
    app.setStyleSheet(_stylesheet(colors, technical))
    return effective


def _stylesheet(c: dict[str, str], technical: str) -> str:
    return f"""
        QMainWindow, QWidget#AppRoot {{ background: {c['canvas']}; color: {c['ink']}; }}
        QWidget {{ color: {c['ink']}; }}
        QScrollArea, QScrollArea > QWidget > QWidget {{
            background: transparent; border: 0;
        }}
        QFrame#Navigation {{ background: {c['nav']}; border: 0; }}
        QLabel#Brand {{ color: {c['brand']}; font-size: {TYPOGRAPHY['brand']}px; font-weight: 700; }}
        QLabel#BrandSubtle {{ color: {c['secondary']}; font-size: {TYPOGRAPHY['body_small']}px; }}
        QLabel#NavGroup {{
            color: {c['nav_group']}; font-size: 9px; font-weight: 700;
            padding: 10px 8px 2px 8px;
        }}
        QListWidget#NavigationList {{
            background: transparent; color: {c['nav_text']}; border: 0; outline: 0;
            padding: 4px;
        }}
        QListWidget#NavigationList::item {{
            border-radius: {RADII['control']}px; margin: 0; padding: 4px 10px;
            height: 25px;
        }}
        QListWidget#NavigationList::item:selected {{
            background: {c['nav_selection']}; color: {c['signal']}; font-weight: 600;
            border-left: 3px solid {c['accent']};
        }}
        QListWidget#NavigationList::item:disabled {{ color: {c['nav_group']}; }}
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
            border: 1px solid {c['warning_border']}; border-radius: {RADII['control']}px;
            padding: 7px 9px;
        }}
        QFrame#StatusStrip, QFrame[card="true"] {{
            background: {c['surface']}; border: 1px solid {c['border']};
            border-radius: {RADII['card']}px;
        }}
        QFrame#ApplicationHeader {{
            background: {c['surface_lowest']}; border: 0;
            border-bottom: 1px solid {c['border']};
        }}
        QFrame#ApplicationFooter {{
            background: {c['surface_lowest']}; border: 0;
            border-top: 1px solid {c['border']};
        }}
        QFrame[panel="true"] {{
            background: {c['surface']}; border: 1px solid {c['border']};
            border-radius: {RADII['card']}px;
        }}
        QFrame[subpanel="true"] {{
            background: {c['surface_low']}; border: 1px solid {c['border']};
            border-radius: {RADII['control']}px;
        }}
        QFrame[well="true"] {{
            background: {c['surface_lowest']}; border: 1px solid {c['border']};
            border-radius: {RADII['control']}px;
        }}
        QFrame#OperationalToolbar {{
            background: {c['surface']}; border: 1px solid {c['border']};
            border-radius: {RADII['card']}px;
        }}
        QLabel#PageTitle {{ font-size: {TYPOGRAPHY['title']}px; font-weight: 700; color: {c['ink']}; }}
        QLabel#PageSubtitle {{ color: {c['secondary']}; font-size: {TYPOGRAPHY['body_small']}px; }}
        QLabel#StatusIdentity, QLabel#SecurityHeading {{ font-weight: 700; }}
        QLabel#BodyText {{ font-size: 14px; }}
        QLabel#MetricValue {{ font-size: 25px; font-weight: 700; color: {c['accent']}; }}
        QLabel#MetricLabel {{ color: {c['secondary']}; }}
        QLabel#TechnicalDetail, QLabel[technical="true"], QLineEdit[technical="true"],
        QSpinBox[technical="true"], QTextEdit[technical="true"] {{
            color: {c['secondary']}; font-family: "{technical}";
        }}
        QLabel#SectionLabel {{
            color: {c['secondary']}; font-size: 10px; font-weight: 700;
        }}
        QLabel#PanelTitle {{ color: {c['ink']}; font-size: 15px; font-weight: 600; }}
        QLabel#Eyebrow {{ color: {c['secondary']}; font-size: 10px; font-weight: 700; }}
        QLabel#HeaderMetric {{
            color: {c['ink']}; background: {c['surface_low']};
            border-radius: 5px; padding: 6px 9px; font-family: "{technical}";
        }}
        QLabel[chip="true"] {{
            color: {c['signal']}; background: {c['selection']};
            border: 1px solid {c['accent']}; border-radius: {RADII['control']}px;
            padding: 4px 7px;
        }}
        QLabel[warning="true"] {{
            color: {c['warning_text']}; background: {c['warning_bg']};
            border: 1px solid {c['warning_border']}; border-radius: {RADII['card']}px;
            padding: 9px;
        }}
        QPushButton {{
            background: {c['raised']}; color: {c['ink']}; border: 1px solid {c['border']};
            border-radius: {RADII['control']}px; padding: 8px 12px;
        }}
        QPushButton:hover {{ border-color: {c['accent']}; color: {c['signal']}; }}
        QPushButton:pressed {{ background: {c['selection']}; }}
        QPushButton:focus, QLineEdit:focus, QSpinBox:focus, QComboBox:focus,
        QTextEdit:focus, QPlainTextEdit:focus, QListView:focus, QTableView:focus {{
            border: 2px solid {c['signal']};
        }}
        QPushButton:disabled {{ color: {c['secondary']}; background: {c['muted']}; }}
        QPushButton[primary="true"] {{
            background: {c['accent']}; color: {c['on_accent']}; border-color: {c['accent']};
            font-weight: 600;
        }}
        QPushButton[primary="true"]:hover {{
            background: {c['signal']}; color: {c['hover_ink']};
        }}
        QPushButton[security="true"] {{
            color: {c['warning_text']}; background: {c['warning_bg']};
            border: 1px solid {c['warning_border']}; padding: 6px 9px;
        }}
        QLineEdit, QSpinBox, QComboBox, QTextEdit, QPlainTextEdit, QListView {{
            background: {c['field']}; color: {c['ink']}; border: 1px solid {c['border']};
            border-radius: {RADII['control']}px; padding: 7px;
            selection-background-color: {c['accent']};
        }}
        QSpinBox::up-button, QSpinBox::down-button {{
            background: {c['raised']}; border: 0; width: 16px;
        }}
        QComboBox QAbstractItemView {{
            background: {c['raised']}; color: {c['ink']}; selection-background-color: {c['accent']};
        }}
        QListView::item {{ padding: 9px; border-bottom: 1px solid {c['border']}; }}
        QListView::item:selected {{ background: {c['selection']}; color: {c['ink']}; }}
        QTableView {{
            background: {c['surface_lowest']}; alternate-background-color: {c['surface_low']};
            color: {c['ink']}; border: 0; gridline-color: {c['border']};
            selection-background-color: {c['selection']};
            selection-color: {c['ink']}; outline: 0;
        }}
        QTableView::item {{ padding: 8px; border-bottom: 1px solid {c['border']}; }}
        QHeaderView::section {{
            background: {c['surface_low']}; color: {c['secondary']}; border: 0;
            border-bottom: 1px solid {c['border']}; padding: 8px;
            font-size: 10px; font-weight: 700;
        }}
        QSplitter::handle {{ background: {c['border']}; width: 1px; height: 1px; }}
        QProgressBar {{
            border: 1px solid {c['border']}; border-radius: 5px; text-align: center;
            background: {c['muted']}; color: {c['ink']};
        }}
        QProgressBar::chunk {{ background: {c['signal']}; border-radius: 4px; }}
        QGraphicsView {{
            background: {c['surface']}; border: 1px solid {c['border']};
            border-radius: {RADII['card']}px;
        }}
        QToolTip {{ background: {c['nav']}; color: {c['tooltip_ink']}; border: 0; padding: 5px; }}
    """
