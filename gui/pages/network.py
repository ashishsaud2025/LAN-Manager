"""Network presentation helpers with no data fetching of their own.

The radar itself is shared with Overview; only the toolbar title and HUD
inspector rows live here so placement math is never duplicated."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from gui.pages.overview import inspector_row, state_pill_for
from gui.theme.tokens import SPACE
from gui.widgets.header_icon import header_icon
from gui.widgets.mono_label import MonoLabel
from gui.widgets.status_pill import StatusPill


def toolbar_title() -> QWidget:
    """Build the mode label row with the reference radar glyph."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(SPACE["sm"])
    mode = QLabel("OBSERVED MAP")
    mode.setObjectName("PanelTitle")
    layout.addWidget(header_icon("radar"))
    layout.addWidget(mode)
    return row


def metric_value(initial: str) -> MonoLabel:
    """Build one mono HUD number reusing the shared metric styling."""
    value = MonoLabel(initial)
    value.setObjectName("MetricValue")
    return value


def hud_selected_block() -> tuple[list[QWidget], MonoLabel, MonoLabel,
                                   MonoLabel, StatusPill]:
    """Build HUD inspector rows; main_window fills values from records."""
    endpoint_row, endpoint = inspector_row("Endpoint:")
    session_row, session = inspector_row("Session:")
    state_row, state = inspector_row("State:")
    pill = state_pill_for(None)
    pill.setText("No selection")
    return [endpoint_row, session_row, state_row], endpoint, session, state, pill
