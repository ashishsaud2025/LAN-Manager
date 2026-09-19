"""Stable Qt models for the LAN Atlas Phase 1 pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import time
from typing import Any

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt

from core.roster import Peer


@dataclass(frozen=True)
class ActivityEntry:
    """One retained operational event, separate from human messages."""

    timestamp: datetime
    category: str
    title: str
    detail: str = ""
    severity: str = "info"


@dataclass(frozen=True)
class MessageEntry:
    """One locally presented message with evidence-based state."""

    identifier: str
    sender: str
    text: str
    scope: str
    state: str
    outgoing: bool


@dataclass(frozen=True)
class AdminDevice:
    """One observed endpoint with an explicit evidence source."""

    key: str
    label: str
    address: str
    source: str
    detail: str
    port: int | None = None


class AdminDeviceListModel(QAbstractListModel):
    """Present LAN observations without merging identities by address."""

    DeviceRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self) -> None:
        super().__init__()
        self.devices: tuple[AdminDevice, ...] = ()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.devices)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.devices):
            return None
        device = self.devices[index.row()]
        endpoint = (f"{device.address}:{device.port}"
                    if device.port is not None else device.address)
        if role == Qt.ItemDataRole.DisplayRole:
            return f"{device.label}\n{endpoint} · {device.source}\n{device.detail}"
        if role == self.DeviceRole:
            return device
        if role == Qt.ItemDataRole.ToolTipRole:
            return (f"Evidence source: {device.source}\n{device.detail}\n"
                    "An observation is not identity or administrative authority.")
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return (f"{device.label}, address {endpoint}, source {device.source}, "
                    f"{device.detail}")
        return None

    def set_devices(self, devices: tuple[AdminDevice, ...]) -> None:
        """Replace one complete combined observation snapshot."""
        self.beginResetModel()
        self.devices = devices
        self.endResetModel()

    def device_at(self, row: int) -> AdminDevice | None:
        """Return one observed endpoint by visible row."""
        return self.devices[row] if 0 <= row < len(self.devices) else None


class PeerListModel(QAbstractListModel):
    """Expose immutable roster snapshots while preserving session identity."""

    PeerRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self) -> None:
        super().__init__()
        self.peers: tuple[Peer, ...] = ()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.peers)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.peers):
            return None
        peer = self.peers[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            capabilities = " · ".join(capability_label(item)
                                      for item in peer.hello.capabilities) or "Presence only"
            age = max(0.0, time.monotonic() - peer.last_seen)
            return (f"{peer.hello.name} · seen {age:.1f}s ago\n"
                    f"{peer.ip}:{peer.hello.tcp_port}  ·  {capabilities}\n"
                    f"Unverified session {peer.hello.session_id[:8]}…")
        if role == self.PeerRole:
            return peer
        if role == Qt.ItemDataRole.ToolTipRole:
            return (f"Peer {peer.hello.peer_id}\nSession {peer.hello.session_id}\n"
                    "Identity is self-reported and not authenticated.")
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return (f"{peer.hello.name}, endpoint {peer.ip}:{peer.hello.tcp_port}, "
                    "unverified identity")
        return None

    def set_peers(self, peers: tuple[Peer, ...]) -> None:
        """Replace a complete discovery snapshot atomically."""
        self.beginResetModel()
        self.peers = peers
        self.endResetModel()

    def peer_at(self, row: int) -> Peer | None:
        return self.peers[row] if 0 <= row < len(self.peers) else None

    def refresh_ages(self) -> None:
        """Refresh derived last-seen labels without resetting selection."""
        if self.peers:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.peers) - 1, 0))


class MessageListModel(QAbstractListModel):
    """Bound human-message history independently of operational logs."""

    def __init__(self, limit: int = 1000) -> None:
        super().__init__()
        self.limit = limit
        self.entries: list[MessageEntry] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.entries)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.entries):
            return None
        entry = self.entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            direction = "You" if entry.outgoing else entry.sender
            return f"{direction} · {entry.scope}\n{entry.text}\n{entry.state}"
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{entry.sender}, {entry.text}, state {entry.state}"
        return None

    def append(self, entry: MessageEntry) -> None:
        """Append and evict oldest rows beyond the documented UI bound."""
        if len(self.entries) >= self.limit:
            remove = len(self.entries) - self.limit + 1
            self.beginRemoveRows(QModelIndex(), 0, remove - 1)
            del self.entries[:remove]
            self.endRemoveRows()
        row = len(self.entries)
        self.beginInsertRows(QModelIndex(), row, row)
        self.entries.append(entry)
        self.endInsertRows()

    def update_state(self, identifier: str, state: str) -> None:
        """Update one outgoing row from structured per-recipient evidence."""
        row = next((index for index, entry in enumerate(self.entries)
                    if entry.identifier == identifier and entry.outgoing), -1)
        if row < 0:
            return
        entry = self.entries[row]
        self.entries[row] = MessageEntry(entry.identifier, entry.sender, entry.text,
                                         entry.scope, state, entry.outgoing)
        index = self.index(row, 0)
        self.dataChanged.emit(index, index)


class TransferListModel(QAbstractListModel):
    """Track the latest visible state for each bounded transfer."""

    def __init__(self, limit: int = 128) -> None:
        super().__init__()
        self.limit = limit
        self.identifiers: list[str] = []
        self.rows: dict[str, dict[str, Any]] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.identifiers)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.identifiers):
            return None
        identifier = self.identifiers[index.row()]
        row = self.rows[identifier]
        if role == Qt.ItemDataRole.DisplayRole:
            progress = ""
            if "bytes" in row and "total" in row:
                progress = f" · {row['bytes']}/{row['total']} bytes"
            return (f"{row.get('name', identifier)}\n{row.get('state', 'pending')}"
                    f"{progress}\n{identifier[:8]}…")
        if role == Qt.ItemDataRole.UserRole:
            return row
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"Transfer {row.get('name', identifier)}, {row.get('state', 'pending')}"
        return None

    def upsert(self, value: dict[str, Any]) -> str | None:
        """Insert or update a transfer and return an evicted identifier, if any."""
        identifier = value["id"]
        if identifier not in self.rows:
            removed = None
            if len(self.identifiers) >= self.limit:
                self.beginRemoveRows(QModelIndex(), 0, 0)
                removed = self.identifiers.pop(0)
                self.rows.pop(removed, None)
                self.endRemoveRows()
            row = len(self.identifiers)
            self.beginInsertRows(QModelIndex(), row, row)
            self.identifiers.append(identifier)
            self.rows[identifier] = dict(value)
            self.endInsertRows()
            return removed
        row = self.identifiers.index(identifier)
        self.rows[identifier].update(value)
        index = self.index(row, 0)
        self.dataChanged.emit(index, index)
        return None


class PostListModel(QAbstractListModel):
    """Present a bounded feed snapshot without treating wall time as consensus."""

    def __init__(self) -> None:
        super().__init__()
        self.posts: list[dict[str, Any]] = []
        self.local_peer_id = ""
        self.author_names: dict[str, str] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.posts)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.posts):
            return None
        post = self.posts[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            author_id = post["author_id"]
            author = self.author_names.get(author_id, f"Peer {author_id[:8]}…")
            provenance = "Stored copy"
            try:
                stamp = datetime.fromtimestamp(
                    post["created_ms"] / 1000).strftime("%Y-%m-%d %H:%M")
            except (OSError, OverflowError, ValueError):
                stamp = "time unavailable"
            refs = f"\n{len(post['refs'])} attachment reference(s)" if post["refs"] else ""
            return (f"{author} · {provenance}\n{post['text']}\n"
                    f"Reported {stamp}; peer clocks may differ{refs}")
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"Post by {post['author_id']}, {post['text']}"
        return None

    def set_posts(self, posts: list[dict[str, Any]], local_peer_id: str,
                  author_names: dict[str, str]) -> None:
        """Replace the visible bounded page and its current author labels."""
        self.beginResetModel()
        self.posts = posts
        self.local_peer_id = local_peer_id
        self.author_names = dict(author_names)
        self.endResetModel()

    def set_author_names(self, author_names: dict[str, str]) -> None:
        """Refresh display labels without reading or replacing post storage."""
        self.author_names = dict(author_names)
        if self.posts:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.posts) - 1, 0))


class ActivityListModel(QAbstractListModel):
    """Retain bounded, structured operational events for inspection."""

    def __init__(self, limit: int = 500) -> None:
        super().__init__()
        self.limit = limit
        self.entries: list[ActivityEntry] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.entries)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.entries):
            return None
        entry = self.entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            stamp = entry.timestamp.strftime("%H:%M:%S")
            suffix = f"\n{entry.detail}" if entry.detail else ""
            return f"{stamp} · {entry.category}\n{entry.title}{suffix}"
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{entry.category}, {entry.title}, {entry.detail}"
        return None

    def append(self, entry: ActivityEntry) -> None:
        """Append one event and discard the oldest event at the bound."""
        if len(self.entries) >= self.limit:
            self.beginRemoveRows(QModelIndex(), 0, 0)
            self.entries.pop(0)
            self.endRemoveRows()
        row = len(self.entries)
        self.beginInsertRows(QModelIndex(), row, row)
        self.entries.append(entry)
        self.endInsertRows()


def capability_label(value: str) -> str:
    """Translate known wire capability names without hiding unknown values."""
    return {"chat_v1": "Chat", "file_v1": "Files", "posts_v1": "Posts",
            "echo_v1": "Echo"}.get(value, value)
