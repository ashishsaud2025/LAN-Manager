"""Desktop roster and chat widgets; all queue consumption occurs on the Qt thread."""

from __future__ import annotations

from queue import Empty
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QLineEdit, QMainWindow, QPushButton, QTextEdit,
    QTabWidget, QVBoxLayout, QWidget,
)

from core.chat import ChatService
from core.roster import Peer


class MainWindow(QMainWindow):
    """Show a room and direct-message selector backed by a bounded event queue."""

    def __init__(self, service: ChatService) -> None:
        super().__init__()
        self.service = service
        self.peers: tuple[Peer, ...] = ()
        self.feed_peers: tuple[Peer, ...] = ()
        self.transfer_rows: dict[str, dict[str, Any]] = {}
        self.setWindowTitle(f"LAN Manager: {service.hello.name}")
        self.resize(700, 450)
        container = QWidget(self)
        layout = QVBoxLayout(container)
        tabs = QTabWidget()
        chat_tab = QWidget()
        chat_layout = QVBoxLayout(chat_tab)
        self.recipient = QComboBox()
        self.recipient.addItem("Room (all discovered chat peers)", None)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.document().setMaximumBlockCount(1000)
        self.input = QLineEdit()
        self.input.setMaxLength(4096)
        self.input.setPlaceholderText("Write a message")
        self.send_button = QPushButton("Send")
        self.file_button = QPushButton("Send file to selected peer")
        self.transfer_list = QComboBox()
        self.accept_file = QPushButton("Accept selected file offer")
        self.decline_file = QPushButton("Decline selected offer")
        self.cancel_file = QPushButton("Cancel selected transfer")
        for widget in (self.recipient, self.log, self.input, self.send_button,
                       self.file_button, self.transfer_list, self.accept_file,
                       self.decline_file, self.cancel_file):
            chat_layout.addWidget(widget)
        feed_tab = QWidget()
        feed_layout = QVBoxLayout(feed_tab)
        self.feed_peer = QComboBox()
        self.feed_log = QTextEdit()
        self.feed_log.setReadOnly(True)
        self.feed_log.document().setMaximumBlockCount(500)
        self.post_input = QLineEdit()
        self.post_input.setMaxLength(4096)
        self.post_input.setPlaceholderText("Publish a local post")
        self.publish_button = QPushButton("Publish post")
        self.sync_button = QPushButton("Sync selected peer")
        for widget in (self.feed_peer, self.feed_log, self.post_input,
                       self.publish_button, self.sync_button):
            feed_layout.addWidget(widget)
        tabs.addTab(chat_tab, "Chat and files")
        tabs.addTab(feed_tab, "Feed")
        layout.addWidget(tabs)
        self.setCentralWidget(container)
        self.send_button.clicked.connect(self.send)
        self.input.returnPressed.connect(self.send)
        self.file_button.clicked.connect(self.send_file)
        self.accept_file.clicked.connect(self.accept_offer)
        self.decline_file.clicked.connect(self.decline_offer)
        self.cancel_file.clicked.connect(self.cancel_transfer)
        self.publish_button.clicked.connect(self.publish_post)
        self.post_input.returnPressed.connect(self.publish_post)
        self.sync_button.clicked.connect(self.sync_feed)
        self.refresh_feed()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.drain)
        self.timer.start(50)

    def append(self, text: str) -> None:
        """Append plain text so received messages cannot inject rich markup."""
        cursor = self.log.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text + "\n")
        self.log.setTextCursor(cursor)

    @Slot()
    def send(self) -> None:
        """Queue a room or direct message without blocking the GUI."""
        selected = self.recipient.currentData()
        peers = tuple(peer for peer in self.peers
                      if selected is None or peer.hello.session_id == selected)
        try:
            identifier = self.service.send(self.input.text(), peers, selected is not None)
        except (ValueError, RuntimeError) as error:
            self.append(str(error))
            return
        self.append(f"Pending {identifier}: {self.input.text()}")
        self.input.clear()

    @Slot()
    def drain(self) -> None:
        """Consume a bounded batch per tick to preserve GUI responsiveness."""
        for _ in range(64):
            try:
                kind, value = self.service.events.get_nowait()
            except Empty:
                break
            if kind == "roster":
                selected = self.recipient.currentData()
                feed_selected = self.feed_peer.currentData()
                self.peers = tuple(peer for peer in value
                                   if "chat_v1" in peer.hello.capabilities)
                self.feed_peers = tuple(peer for peer in value
                                        if "posts_v1" in peer.hello.capabilities)
                self.recipient.clear()
                self.recipient.addItem("Room (all discovered chat peers)", None)
                for peer in self.peers:
                    self.recipient.addItem(f"DM: {peer.hello.name} ({peer.ip})",
                                           peer.hello.session_id)
                index = self.recipient.findData(selected)
                if selected is not None and index < 0:
                    self.recipient.addItem("Selected peer is unavailable", selected)
                    index = self.recipient.count() - 1
                self.recipient.setCurrentIndex(max(0, index))
                self.feed_peer.clear()
                for peer in self.feed_peers:
                    self.feed_peer.addItem(f"{peer.hello.name} ({peer.ip})",
                                           peer.hello.session_id)
                feed_index = self.feed_peer.findData(feed_selected)
                self.feed_peer.setCurrentIndex(max(0, feed_index))
            elif kind == "message":
                self.append(f"{value['body']['scope']} from {value['peer_id']}: "
                            f"{value['body']['text']}")
            elif kind in {"transfer", "file_offer"}:
                if kind == "file_offer":
                    value = {**value, "state": "offer_pending", "total": value["size"]}
                    self.append(f"File offer: {value['name']!r}, {value['size']} bytes "
                                f"from {value['peer_id']}. Select it below to accept or decline.")
                self.update_transfer(value)
            elif kind == "feed_updated":
                suffix = " (more pages available)" if value.get("partial") else ""
                self.append(f"Feed sync: {value['added']} added, "
                            f"{value['duplicates']} duplicates{suffix}")
                self.refresh_feed()
            else:
                self.append(str(value))

    @Slot()
    def publish_post(self) -> None:
        """Persist a local text post without doing network I/O on the Qt thread."""
        try:
            identifier = self.service.publish_post(self.post_input.text())
        except (ValueError, RuntimeError, OSError) as error:
            self.append(str(error))
            return
        self.post_input.clear()
        self.append(f"Published post {identifier}")
        self.refresh_feed()

    @Slot()
    def sync_feed(self) -> None:
        """Queue feed paging from the explicitly selected capable peer."""
        selected = self.feed_peer.currentData()
        peer = next((item for item in self.feed_peers
                     if item.hello.session_id == selected), None)
        if peer is None:
            self.append("Select one feed peer before syncing.")
            return
        try:
            identifier = self.service.sync_posts(peer)
        except (ValueError, RuntimeError) as error:
            self.append(str(error))
            return
        self.append(f"Feed sync queued {identifier}")

    def refresh_feed(self) -> None:
        """Render a bounded local feed snapshot as plain text."""
        if self.service.post_store is None:
            self.feed_log.setPlainText("Post storage is not configured.")
            return
        posts, _, _ = self.service.post_store.page(50)
        lines = [f"{post['author_id']} | {post['text']}" for post in posts]
        self.feed_log.setPlainText("\n\n".join(lines) if lines else "No cached posts.")

    def update_transfer(self, value: dict[str, Any]) -> None:
        """Display bounded transfer history with explicit progress semantics."""
        identifier = value["id"]
        if identifier not in self.transfer_rows:
            if len(self.transfer_rows) >= 128:
                for old_id, old in tuple(self.transfer_rows.items()):
                    if old.get("state") in {"failed", "cancelled", "declined", "saved", "verified"}:
                        self.transfer_rows.pop(old_id)
                        self.transfer_list.removeItem(self.transfer_list.findData(old_id))
                        break
            self.transfer_rows[identifier] = {}
            self.transfer_list.addItem(identifier, identifier)
        row = self.transfer_rows[identifier]
        row.update(value)
        progress = ""
        if "bytes" in row and "total" in row:
            progress = f" {row['bytes']}/{row['total']} bytes"
        text = f"{row.get('name', identifier)}: {row['state']}{progress}"
        self.transfer_list.setItemText(self.transfer_list.findData(identifier), text)
        if value["state"] in {"failed", "cancelled", "declined", "saved", "verified"}:
            self.append(f"Transfer {identifier}: {value['state']} "
                        f"{value.get('message', value.get('path', ''))}")

    @Slot()
    def send_file(self) -> None:
        """Select a source file for one explicitly selected peer."""
        selected = self.recipient.currentData()
        peer = next((peer for peer in self.peers
                     if peer.hello.session_id == selected), None)
        if peer is None:
            self.append("Select one peer before sending a file.")
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Send file")
        if not filename:
            return
        try:
            identifier = self.service.transfers.send(Path(filename), peer)
            self.update_transfer({"id": identifier, "name": Path(filename).name,
                                  "state": "preparing"})
        except (OSError, ValueError) as error:
            self.append(str(error))

    @Slot()
    def accept_offer(self) -> None:
        """Choose a new destination; the remote filename never selects a path."""
        identifier = self.transfer_list.currentData()
        row = self.transfer_rows.get(identifier, {})
        if row.get("state") != "offer_pending":
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Save received file to a new filename")
        if filename:
            if self.service.transfers.decide(identifier, Path(filename)):
                self.update_transfer({"id": identifier, "state": "accepted"})
            else:
                self.append("Offer is no longer available.")

    @Slot()
    def decline_offer(self) -> None:
        """Decline a still-pending offer."""
        identifier = self.transfer_list.currentData()
        if self.transfer_rows.get(identifier, {}).get("state") == "offer_pending":
            self.service.transfers.decide(identifier, None)

    @Slot()
    def cancel_transfer(self) -> None:
        """Cancel selected work without waiting on socket I/O in the GUI."""
        self.service.transfers.cancel(self.transfer_list.currentData())

    def closeEvent(self, event: QCloseEvent) -> None:
        """Cancel network work; the entry point joins after the Qt loop exits."""
        self.timer.stop()
        self.service.stop()
        event.accept()
