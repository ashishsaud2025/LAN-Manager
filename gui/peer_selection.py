"""Shared selected-session state for desktop peer surfaces."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class PeerSelection(QObject):
    """Publish one canonical selected session ID."""

    changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._session_id: str | None = None

    @property
    def session_id(self) -> str | None:
        """Return the selected session ID, if any."""
        return self._session_id

    def select(self, session_id: str | None) -> None:
        """Select one session and emit only when the identity changes."""
        if session_id == self._session_id:
            return
        self._session_id = session_id
        self.changed.emit(session_id)

    def reconcile(self, session_ids: set[str]) -> None:
        """Clear selection only after its retained repository record is removed."""
        if self._session_id is not None and self._session_id not in session_ids:
            self.select(None)
