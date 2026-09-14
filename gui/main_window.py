"""Desktop roster and chat widgets; all queue consumption occurs on the Qt thread."""

from __future__ import annotations

from queue import Empty

from PySide6.QtCore import QTimer, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox, QLineEdit, QMainWindow, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from core.chat import ChatService
from core.roster import Peer


class MainWindow(QMainWindow):
    """Show a room and direct-message selector backed by a bounded event queue."""

    def __init__(self, service: ChatService) -> None:
        super().__init__()
        self.service = service
        self.peers: tuple[Peer, ...] = ()
        self.setWindowTitle(f"LAN Manager: {service.hello.name}")
        self.resize(700, 450)
        container = QWidget(self)
        layout = QVBoxLayout(container)
        self.recipient = QComboBox()
        self.recipient.addItem("Room (all discovered chat peers)", None)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.document().setMaximumBlockCount(1000)
        self.input = QLineEdit()
        self.input.setMaxLength(4096)
        self.input.setPlaceholderText("Write a message")
        self.send_button = QPushButton("Send")
        for widget in (self.recipient, self.log, self.input, self.send_button):
            layout.addWidget(widget)
        self.setCentralWidget(container)
        self.send_button.clicked.connect(self.send)
        self.input.returnPressed.connect(self.send)
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
                self.peers = tuple(peer for peer in value
                                   if "chat_v1" in peer.hello.capabilities)
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
            elif kind == "message":
                self.append(f"{value['body']['scope']} from {value['peer_id']}: "
                            f"{value['body']['text']}")
            else:
                self.append(str(value))

    def closeEvent(self, event: QCloseEvent) -> None:
        """Cancel network work; the entry point joins after the Qt loop exits."""
        self.timer.stop()
        self.service.stop()
        event.accept()
