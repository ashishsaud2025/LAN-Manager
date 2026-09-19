"""LAN Atlas desktop shell; all core events are consumed on the Qt thread."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from queue import Empty
import time
from typing import Any

from PySide6.QtCore import QModelIndex, QSettings, Qt, QTimer, Slot
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QListView, QMainWindow, QProgressBar, QPushButton,
    QSplitter, QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

from core.chat import ChatService
from core.roster import Peer
from gui.components import NavigationRail, action_button, card, page_header
from gui.models import (
    ActivityEntry, ActivityListModel, MessageEntry, MessageListModel,
    PeerListModel, PostListModel, TransferListModel, capability_label,
)
from gui.theme import apply_theme
from gui.topology import NetworkTopology

PAGE_OVERVIEW = 0
PAGE_NETWORK = 1
PAGE_DEVICES = 2
PAGE_MESSAGES = 3
PAGE_TRANSFERS = 4
PAGE_FEED = 5
PAGE_SERVICES = 6
PAGE_WORKBENCH = 7
PAGE_ACTIVITY = 8
PAGE_SETTINGS = 9
TERMINAL_TRANSFERS = {"failed", "cancelled", "declined", "saved", "verified"}


class MainWindow(QMainWindow):
    """Present peer-to-peer state without implying central trust or connectivity."""

    def __init__(self, service: ChatService) -> None:
        super().__init__()
        self.settings = QSettings("LAN Manager", "LAN Atlas")
        self.theme_mode = str(self.settings.value("appearance/theme", "observatory"))
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, self.theme_mode)
        self.service = service
        self.all_peers: tuple[Peer, ...] = ()
        self.peers: tuple[Peer, ...] = ()
        self.feed_peers: tuple[Peer, ...] = ()
        self.transfer_rows: dict[str, dict[str, Any]] = {}
        self.selected_transfer_id: str | None = None
        self.message_outcomes: dict[str, dict[str, str]] = {}
        self._next_presence_refresh = 0.0
        self.peer_model = PeerListModel()
        self.message_model = MessageListModel()
        self.transfer_model = TransferListModel()
        self.post_model = PostListModel()
        self.activity_model = ActivityListModel()
        self.page_names = ("Overview", "Network", "Devices", "Messages", "Transfers",
                           "Feed", "Services", "Workbench", "Activity", "Settings")
        self.navigation_groups = (
            ("Control", (("Overview", PAGE_OVERVIEW), ("Network", PAGE_NETWORK),
                         ("Devices", PAGE_DEVICES), ("Workbench", PAGE_WORKBENCH))),
            ("Share", (("Files", None), ("Transfers", PAGE_TRANSFERS))),
            ("Community", (("Messages", PAGE_MESSAGES), ("Feed", PAGE_FEED),
                           ("Games", None))),
            ("Services", (("Services", PAGE_SERVICES),)),
            ("System", (("Activity", PAGE_ACTIVITY), ("Settings", PAGE_SETTINGS))),
        )
        self.setWindowTitle(f"LAN Atlas · {service.hello.name}")
        self.resize(1180, 760)
        self.setMinimumSize(820, 560)
        self._build_shell()
        self._install_shortcuts()
        self.refresh_feed()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.drain)
        self.timer.start(50)

    def _build_shell(self) -> None:
        root = QWidget(self)
        root.setObjectName("AppRoot")
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.navigation = NavigationRail(self.navigation_groups)
        self.navigation.selected.connect(self.select_page)
        outer.addWidget(self.navigation)

        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(18, 14, 18, 16)
        workspace_layout.setSpacing(12)
        workspace_layout.addWidget(self._build_status_strip())
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_overview_page())
        self.stack.addWidget(self._build_network_page())
        self.stack.addWidget(self._build_peers_page())
        self.stack.addWidget(self._build_messages_page())
        self.stack.addWidget(self._build_transfers_page())
        self.stack.addWidget(self._build_feed_page())
        self.stack.addWidget(self._build_placeholder_page(
            "Services", "M8 will publish and browse explicit local projects and services.",
            "Advertised metadata, owner presence, TCP reachability, and application "
            "health will remain separate states."))
        self.stack.addWidget(self._build_placeholder_page(
            "Workbench", "Network diagnostics arrive in Phase 2 and Phase 3.",
            "The first tools will be bounded TCP connect and LAN Manager ECHO probes. "
            "ICMP ping and a guarded Raw TCP Console follow."))
        self.stack.addWidget(self._build_activity_page())
        self.stack.addWidget(self._build_settings_page())
        workspace_layout.addWidget(self.stack, 1)
        self.transfer_drawer = QFrame()
        self.transfer_drawer.setProperty("card", True)
        drawer_layout = QHBoxLayout(self.transfer_drawer)
        self.transfer_drawer_label = QLabel("Transfer activity")
        drawer_layout.addWidget(self.transfer_drawer_label)
        drawer_layout.addStretch(1)
        drawer_layout.addWidget(action_button(
            "Open transfers", lambda: self.navigation.select(PAGE_TRANSFERS)))
        self.transfer_drawer.setVisible(False)
        workspace_layout.addWidget(self.transfer_drawer)
        outer.addWidget(workspace, 1)
        self.setCentralWidget(root)

    def _build_status_strip(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("StatusStrip")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(13, 9, 13, 9)
        self.identity_status = QLabel(self.service.hello.name)
        self.identity_status.setStyleSheet("font-weight: 700;")
        self.nearby_status = QLabel("0 sessions nearby")
        self.network_status = QLabel("Network core configured")
        self.transfer_status = QLabel("0 active transfers")
        self.command_button = QPushButton("Commands  Ctrl+K")
        self.command_button.setEnabled(False)
        self.command_button.setToolTip("Command palette reserved for a later phase")
        self.security_status = QPushButton("◇  Unverified LAN")
        self.security_status.setProperty("security", True)
        self.security_status.setToolTip(
            "Peer names and IDs are self-reported. Traffic is not authenticated or encrypted.")
        self.security_status.clicked.connect(lambda: self.navigation.select(PAGE_SETTINGS))
        for widget in (self.identity_status, self.nearby_status, self.network_status,
                       self.transfer_status):
            layout.addWidget(widget)
        layout.addStretch(1)
        layout.addWidget(self.command_button)
        layout.addWidget(self.security_status)
        return frame

    def _page(self, title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(12)
        layout.addWidget(page_header(title, subtitle))
        return page, layout

    def _build_overview_page(self) -> QWidget:
        page, layout = self._page(
            "Local network overview",
            "Live observations from this LAN Manager session.")
        metrics = QGridLayout()
        nearby_card, self.nearby_value = card("Nearby sessions", "Searching...")
        capability_card, self.capability_value = card("Advertised features", "Waiting")
        transfer_card, self.transfer_value = card("Active transfers", "None")
        identity_card, self.identity_value = card("This device", self.service.hello.name)
        self.identity_detail = QLabel(f"ID {self.service.hello.peer_id[:8]}")
        self.identity_detail.setObjectName("TechnicalDetail")
        identity_card.layout().addWidget(self.identity_detail)
        metrics.addWidget(nearby_card, 0, 0)
        metrics.addWidget(capability_card, 0, 1)
        metrics.addWidget(transfer_card, 0, 2)
        metrics.addWidget(identity_card, 0, 3)
        layout.addLayout(metrics)

        live = QSplitter(Qt.Orientation.Horizontal)
        network = QFrame()
        network.setProperty("card", True)
        network_layout = QVBoxLayout(network)
        network_heading = QLabel("OBSERVED NETWORK")
        network_heading.setObjectName("SectionLabel")
        network_layout.addWidget(network_heading)
        self.overview_topology = NetworkTopology(
            self.service.hello, self.theme_mode, show_note=False)
        self.overview_topology.device_selected.connect(self._topology_device_selected)
        network_layout.addWidget(self.overview_topology, 1)
        live.addWidget(network)

        nearby = QFrame()
        nearby.setProperty("card", True)
        nearby_layout = QVBoxLayout(nearby)
        nearby_heading = QLabel("NEARBY")
        nearby_heading.setObjectName("SectionLabel")
        nearby_layout.addWidget(nearby_heading)
        self.overview_nearby_stack = QStackedWidget()
        self.overview_nearby_empty = QLabel(
            "Searching your LAN...\nNearby sessions will appear after a HELLO announcement.")
        self.overview_nearby_empty.setObjectName("PageSubtitle")
        self.overview_nearby_empty.setWordWrap(True)
        self.overview_nearby_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overview_peer_list = QListView()
        self.overview_peer_list.setModel(self.peer_model)
        self.overview_peer_list.setAccessibleName("Overview nearby sessions")
        self.overview_peer_list.setWordWrap(True)
        self.overview_peer_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.overview_peer_list.clicked.connect(self._overview_peer_selected)
        self.overview_nearby_stack.addWidget(self.overview_nearby_empty)
        self.overview_nearby_stack.addWidget(self.overview_peer_list)
        nearby_layout.addWidget(self.overview_nearby_stack, 1)
        nearby_layout.addWidget(action_button(
            "Browse all devices", lambda: self.navigation.select(PAGE_DEVICES)))
        live.addWidget(nearby)
        live.setSizes([620, 310])
        layout.addWidget(live, 1)

        activity = QFrame()
        activity.setProperty("card", True)
        activity_layout = QVBoxLayout(activity)
        activity_header = QHBoxLayout()
        activity_heading = QLabel("RECENT ACTIVITY")
        activity_heading.setObjectName("SectionLabel")
        activity_header.addWidget(activity_heading)
        activity_header.addStretch(1)
        activity_header.addWidget(action_button(
            "View activity", lambda: self.navigation.select(PAGE_ACTIVITY)))
        activity_layout.addLayout(activity_header)
        self.overview_activity_stack = QStackedWidget()
        self.overview_activity_empty = QLabel("No operational activity yet")
        self.overview_activity_empty.setObjectName("PageSubtitle")
        self.overview_activity_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overview_activity_view = QListView()
        self.overview_activity_view.setModel(self.activity_model)
        self.overview_activity_view.setAccessibleName("Recent operational activity")
        self.overview_activity_view.setWordWrap(True)
        self.overview_activity_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.overview_activity_stack.addWidget(self.overview_activity_empty)
        self.overview_activity_stack.addWidget(self.overview_activity_view)
        self.overview_activity_stack.setMaximumHeight(150)
        activity_layout.addWidget(self.overview_activity_stack)
        layout.addWidget(activity)

        self.status_help_button = QPushButton("How status works")
        self.status_help_button.setCheckable(True)
        self.status_help_button.setFlat(True)
        self.status_help_button.setMaximumWidth(150)
        self.status_help = QLabel(
            "Nearby means a UDP HELLO was observed recently. Reachable will mean a TCP "
            "probe succeeded. Compatible will mean a versioned protocol exchange worked. "
            "Authenticated is reserved for future cryptographic pairing.")
        self.status_help.setObjectName("PageSubtitle")
        self.status_help.setWordWrap(True)
        self.status_help.setVisible(False)
        self.status_help_button.toggled.connect(self.status_help.setVisible)
        help_row = QHBoxLayout()
        help_row.addWidget(self.status_help_button)
        help_row.addWidget(self.status_help, 1)
        help_row.addStretch(1)
        layout.addLayout(help_row)
        return page

    def _build_network_page(self) -> QWidget:
        page, layout = self._page(
            "Observed network",
            "Discovery observations from this instance, not inferred physical topology.")
        self.topology = NetworkTopology(self.service.hello, self.theme_mode)
        self.topology.device_selected.connect(self._topology_device_selected)
        layout.addWidget(self.topology, 1)
        return page

    def _build_peers_page(self) -> QWidget:
        page, layout = self._page(
            "Devices", "Active sessions discovered through bounded UDP announcements.")
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.peer_list = QListView()
        self.peer_list.setModel(self.peer_model)
        self.peer_list.setAccessibleName("Nearby peer sessions")
        self.peer_list.setWordWrap(True)
        self.peer_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.peer_list.selectionModel().currentChanged.connect(self._peer_selected)
        splitter.addWidget(self.peer_list)

        inspector = QFrame()
        inspector.setProperty("card", True)
        details = QVBoxLayout(inspector)
        self.peer_name = QLabel("Select a nearby session")
        self.peer_name.setObjectName("PageTitle")
        self.peer_presence = QLabel("No peer selected")
        self.peer_presence.setObjectName("PageSubtitle")
        self.peer_endpoint = QLabel("Endpoint: —")
        self.peer_endpoint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.peer_identity = QLabel("Identity: —")
        self.peer_identity.setWordWrap(True)
        self.peer_identity.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.peer_capabilities = QLabel("Presence only")
        self.peer_capabilities.setProperty("chip", True)
        self.peer_warning = QLabel("◇  Unverified identity")
        self.peer_warning.setProperty("warning", True)
        self.peer_warning.setToolTip(
            "Names and identifiers are self-reported. Nearby does not mean trusted or reachable.")
        self.peer_message_button = action_button("Message", self._message_selected_peer, True)
        self.peer_file_button = action_button("Send file", self._file_selected_peer)
        self.peer_sync_button = action_button("Sync posts", self._sync_selected_peer)
        self.peer_probe_button = QPushButton("Open Workbench")
        self.peer_probe_button.setToolTip("Diagnostics are implemented in Phase 2.")
        self.peer_probe_button.clicked.connect(
            lambda: self.navigation.select(PAGE_WORKBENCH))
        for button in (self.peer_message_button, self.peer_file_button,
                       self.peer_sync_button, self.peer_probe_button):
            button.setEnabled(False)
        details.addWidget(self.peer_name)
        details.addWidget(self.peer_presence)
        details.addSpacing(8)
        details.addWidget(self.peer_endpoint)
        details.addWidget(self.peer_identity)
        details.addWidget(self.peer_capabilities)
        details.addWidget(self.peer_warning)
        details.addStretch(1)
        detail_actions = QGridLayout()
        detail_actions.addWidget(self.peer_message_button, 0, 0)
        detail_actions.addWidget(self.peer_file_button, 0, 1)
        detail_actions.addWidget(self.peer_sync_button, 1, 0)
        detail_actions.addWidget(self.peer_probe_button, 1, 1)
        details.addLayout(detail_actions)
        splitter.addWidget(inspector)
        splitter.setSizes([430, 570])
        layout.addWidget(splitter, 1)
        return page

    def _build_messages_page(self) -> QWidget:
        page, layout = self._page(
            "Messages", "The nearby room is TCP fan-out; acknowledgements mean application acceptance.")
        controls = QFrame()
        controls.setProperty("card", True)
        control_layout = QVBoxLayout(controls)
        self.recipient = QComboBox()
        self.recipient.setAccessibleName("Message recipient")
        self.recipient.addItem("Nearby room · all discovered chat sessions", None)
        self.room_explainer = QLabel(
            "Nearby room sends one TCP message to each currently discovered chat-capable session.")
        self.room_explainer.setObjectName("PageSubtitle")
        self.room_explainer.setWordWrap(True)
        control_layout.addWidget(self.recipient)
        control_layout.addWidget(self.room_explainer)
        layout.addWidget(controls)
        self.message_view = QListView()
        self.message_view.setModel(self.message_model)
        self.message_view.setAccessibleName("Message history")
        self.message_view.setWordWrap(True)
        self.message_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.message_view, 1)
        compose = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setMaxLength(4096)
        self.input.setPlaceholderText("Write a plain-text message")
        self.input.setAccessibleName("Message text")
        self.send_button = action_button("Send", self.send, True)
        self.input.returnPressed.connect(self.send)
        compose.addWidget(self.input, 1)
        compose.addWidget(self.send_button)
        layout.addLayout(compose)
        return page

    def _build_transfers_page(self) -> QWidget:
        page, layout = self._page(
            "Transfers", "Byte progress and verification are separate phases.")
        toolbar = QHBoxLayout()
        self.file_button = action_button("Send file to selected message peer",
                                         self.send_file, True)
        toolbar.addWidget(self.file_button)
        toolbar.addStretch(1)
        self.transfer_slots = QLabel("0 of 4 transfer slots in use")
        toolbar.addWidget(self.transfer_slots)
        layout.addLayout(toolbar)
        self.transfer_view = QListView()
        self.transfer_view.setModel(self.transfer_model)
        self.transfer_view.setAccessibleName("Transfer records")
        self.transfer_view.setWordWrap(True)
        self.transfer_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.transfer_view.selectionModel().currentChanged.connect(
            self._transfer_selected)
        layout.addWidget(self.transfer_view, 1)
        self.transfer_progress = QProgressBar()
        self.transfer_progress.setRange(0, 100)
        self.transfer_progress.setValue(0)
        self.transfer_progress.setFormat("Select a transfer for phase details")
        layout.addWidget(self.transfer_progress)
        actions = QHBoxLayout()
        self.accept_file = action_button("Accept and choose location", self.accept_offer, True)
        self.decline_file = action_button("Decline offer", self.decline_offer)
        self.cancel_file = action_button("Cancel selected transfer", self.cancel_transfer)
        for button in (self.accept_file, self.decline_file, self.cancel_file):
            button.setEnabled(False)
        actions.addWidget(self.accept_file)
        actions.addWidget(self.decline_file)
        actions.addWidget(self.cancel_file)
        actions.addStretch(1)
        layout.addLayout(actions)
        # Retained as a non-primary compatibility selector for existing callers/tests.
        self.transfer_list = QComboBox()
        self.transfer_list.setVisible(False)
        layout.addWidget(self.transfer_list)
        return page

    def _build_feed_page(self) -> QWidget:
        page, layout = self._page(
            "Local commons", "Posts are local or cached; reported wall time is not global order.")
        sync = QHBoxLayout()
        self.feed_peer = QComboBox()
        self.feed_peer.setAccessibleName("Feed synchronization source")
        self.feed_peer.addItem("Select one posts-capable peer", None)
        self.sync_button = action_button("Sync selected peer", self.sync_feed)
        sync.addWidget(self.feed_peer, 1)
        sync.addWidget(self.sync_button)
        layout.addLayout(sync)
        self.feed_view = QListView()
        self.feed_view.setModel(self.post_model)
        self.feed_view.setAccessibleName("Local and cached posts")
        self.feed_view.setWordWrap(True)
        self.feed_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.feed_view, 1)
        compose = QHBoxLayout()
        self.post_input = QLineEdit()
        self.post_input.setMaxLength(4096)
        self.post_input.setPlaceholderText("Publish plain text to your local feed")
        self.post_input.setAccessibleName("Post text")
        self.publish_button = action_button("Publish locally", self.publish_post, True)
        self.post_input.returnPressed.connect(self.publish_post)
        compose.addWidget(self.post_input, 1)
        compose.addWidget(self.publish_button)
        layout.addLayout(compose)
        note = QLabel(
            "Publishing stores the post locally. Other peers receive it only when they synchronize.")
        note.setObjectName("PageSubtitle")
        note.setWordWrap(True)
        layout.addWidget(note)
        # Plain-text compatibility surface used by existing tests and accessibility fallback.
        self.feed_log = QTextEdit()
        self.feed_log.setReadOnly(True)
        self.feed_log.setVisible(False)
        layout.addWidget(self.feed_log)
        return page

    def _build_placeholder_page(self, title: str, subtitle: str, body: str) -> QWidget:
        page, layout = self._page(title, subtitle)
        frame = QFrame()
        frame.setProperty("card", True)
        frame_layout = QVBoxLayout(frame)
        label = QLabel(body)
        label.setWordWrap(True)
        label.setStyleSheet("font-size: 14px;")
        frame_layout.addWidget(label)
        frame_layout.addStretch(1)
        layout.addWidget(frame, 1)
        return page

    def _build_activity_page(self) -> QWidget:
        page, layout = self._page(
            "Activity", "Operational events stay separate from human conversations.")
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.activity_view = QListView()
        self.activity_view.setModel(self.activity_model)
        self.activity_view.setAccessibleName("Operational activity")
        self.activity_view.setWordWrap(True)
        self.activity_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        splitter.addWidget(self.activity_view)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.document().setMaximumBlockCount(1000)
        self.log.setAccessibleName("Plain-text activity transcript")
        splitter.addWidget(self.log)
        splitter.setSizes([420, 170])
        layout.addWidget(splitter, 1)
        return page

    def _build_settings_page(self) -> QWidget:
        page, layout = self._page(
            "Settings", "Phase 1 exposes local identity; network settings remain CLI-configured.")
        identity = QFrame()
        identity.setProperty("card", True)
        form = QGridLayout(identity)
        values = (
            ("Display name", self.service.hello.name),
            ("Installation ID", self.service.hello.peer_id),
            ("Session ID", self.service.hello.session_id),
            ("Application port", str(self.service.hello.tcp_port)),
            ("Capabilities", ", ".join(self.service.hello.capabilities) or "presence only"),
            ("Trust", "Unverified; no cryptographic pairing configured"),
        )
        for row, (name, value) in enumerate(values):
            label = QLabel(name)
            label.setObjectName("MetricLabel")
            content = QLabel(value)
            content.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            content.setWordWrap(True)
            form.addWidget(label, row, 0)
            form.addWidget(content, row, 1)
        appearance_row = len(values)
        appearance_label = QLabel("Appearance")
        appearance_label.setObjectName("MetricLabel")
        self.theme_selector = QComboBox()
        self.theme_selector.addItem("System", "system")
        self.theme_selector.addItem("Observatory Dark", "observatory")
        self.theme_selector.addItem("Atlas Light", "atlas")
        theme_index = self.theme_selector.findData(self.theme_mode)
        self.theme_selector.setCurrentIndex(max(0, theme_index))
        self.theme_selector.currentIndexChanged.connect(self._theme_changed)
        form.addWidget(appearance_label, appearance_row, 0)
        form.addWidget(self.theme_selector, appearance_row, 1)
        layout.addWidget(identity)
        security = QFrame()
        security.setProperty("card", True)
        security_layout = QVBoxLayout(security)
        security_heading = QLabel("Unverified LAN")
        security_heading.setStyleSheet("font-size: 16px; font-weight: 700;")
        security_detail = QLabel(
            "Peer names, installation IDs, session IDs, posts, and advertised features are "
            "self-reported. Current application traffic is not authenticated or encrypted. "
            "Nearby means only that this instance recently received a discovery announcement.")
        security_detail.setWordWrap(True)
        security_layout.addWidget(security_heading)
        security_layout.addWidget(security_detail)
        layout.addWidget(security)
        layout.addStretch(1)
        return page

    def _install_shortcuts(self) -> None:
        self.shortcuts: list[QShortcut] = []
        shortcut_pages = (PAGE_OVERVIEW, PAGE_NETWORK, PAGE_DEVICES, PAGE_WORKBENCH,
                          PAGE_TRANSFERS, PAGE_MESSAGES, PAGE_FEED, PAGE_SERVICES,
                          PAGE_ACTIVITY)
        for key, page_index in enumerate(shortcut_pages, 1):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{key}"), self)
            shortcut.activated.connect(
                lambda page=page_index: self.navigation.select(page))
            self.shortcuts.append(shortcut)
        self.settings_shortcut = QShortcut(QKeySequence("Ctrl+,"), self)
        self.settings_shortcut.activated.connect(
            lambda: self.navigation.select(PAGE_SETTINGS))
        self.command_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        self.command_shortcut.activated.connect(
            lambda: self.append("Command palette is reserved for a later phase.", "System"))

    @Slot(int)
    def select_page(self, index: int) -> None:
        """Switch one primary workspace and preserve all model selections."""
        if 0 <= index < self.stack.count():
            self.stack.setCurrentIndex(index)

    @Slot(int)
    def _theme_changed(self, index: int) -> None:
        """Apply and persist one user-selected appearance mode."""
        mode = self.theme_selector.itemData(index)
        if not isinstance(mode, str):
            return
        self.theme_mode = mode
        self.settings.setValue("appearance/theme", mode)
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, mode)
        self.topology.set_theme(mode)
        self.overview_topology.set_theme(mode)

    def append(self, text: str, category: str = "System",
               severity: str = "info") -> None:
        """Retain plain-text operational output and a structured Activity row."""
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text + "\n")
        self.log.setTextCursor(cursor)
        self.activity_model.append(ActivityEntry(datetime.now(), category, text,
                                                  severity=severity))
        self.activity_view.scrollToBottom()
        self.overview_activity_stack.setCurrentWidget(self.overview_activity_view)
        self.overview_activity_view.scrollToBottom()

    @Slot()
    def send(self) -> None:
        """Queue a room or direct message without blocking the GUI."""
        text = self.input.text()
        selected = self.recipient.currentData()
        peers = tuple(peer for peer in self.peers
                      if selected is None or peer.hello.session_id == selected)
        try:
            identifier = self.service.send(text, peers, selected is not None)
        except (ValueError, RuntimeError) as error:
            self.append(str(error), "Messages", "warning")
            return
        scope = "direct" if selected is not None else "nearby room"
        self.message_model.append(MessageEntry(identifier, self.service.hello.name,
                                               text, scope,
                                               "Queued locally", True))
        self.message_view.scrollToBottom()
        self.input.clear()

    @Slot()
    def drain(self) -> None:
        """Consume a bounded event batch so network load cannot starve Qt."""
        for _ in range(64):
            try:
                kind, value = self.service.events.get_nowait()
            except Empty:
                break
            if kind == "roster":
                self._update_roster(tuple(value))
            elif kind == "message":
                name = self._peer_name(value["peer_id"], value["session_id"])
                self.message_model.append(MessageEntry(
                    value["message_id"], name, value["body"]["text"],
                    value["body"]["scope"], "Accepted locally", False))
                self.message_view.scrollToBottom()
            elif kind == "message_outcome":
                outcomes = self.message_outcomes.setdefault(value["message_id"], {})
                outcomes[value["session_id"]] = value["state"]
                accepted = sum(state == "accepted" for state in outcomes.values())
                failed = sum(state == "failed" for state in outcomes.values())
                state = f"Accepted by {accepted}; failed for {failed}"
                self.message_model.update_state(value["message_id"], state)
            elif kind in {"transfer", "file_offer"}:
                if kind == "file_offer":
                    value = {**value, "state": "offer_pending", "total": value["size"]}
                    self.append(
                        f"Incoming file offer {value['name']!r}, {value['size']} bytes "
                        f"from unverified peer {value['peer_id'][:8]}…",
                        "Transfers", "warning")
                self.update_transfer(value)
            elif kind == "feed_updated":
                suffix = "; more pages available" if value.get("partial") else ""
                self.append(f"Feed sync: {value['added']} added, "
                            f"{value['duplicates']} duplicates{suffix}", "Feed")
                self.refresh_feed()
            elif kind == "post_published":
                self.refresh_feed()
            else:
                text = str(value)
                if text.startswith("TCP listener stopped"):
                    self.network_status.setText("Network stopped")
                elif text.startswith("Discovery stopped"):
                    self.network_status.setText("Discovery unavailable")
                self.append(text, "Network",
                            "warning" if any(word in text.lower()
                                             for word in ("failed", "stopped")) else "info")
        now = time.monotonic()
        if now >= self._next_presence_refresh:
            self._next_presence_refresh = now + 1.0
            self.peer_model.refresh_ages()
            peer = self._selected_peer()
            if peer is not None:
                self._show_peer(peer)

    def _update_roster(self, peers: tuple[Peer, ...]) -> None:
        previous_count = len(self.all_peers)
        peer_selection = self._selected_peer()
        selected_session = peer_selection.hello.session_id if peer_selection else None
        message_selected = self.recipient.currentData()
        feed_selected = self.feed_peer.currentData()
        self.all_peers = peers
        self.peers = tuple(peer for peer in peers
                           if "chat_v1" in peer.hello.capabilities)
        self.feed_peers = tuple(peer for peer in peers
                                if "posts_v1" in peer.hello.capabilities)
        self.peer_model.set_peers(peers)
        self.topology.set_peers(peers)
        self.overview_topology.set_peers(peers)
        self.overview_nearby_stack.setCurrentWidget(
            self.overview_peer_list if peers else self.overview_nearby_empty)
        self._rebuild_message_recipients(message_selected)
        self._rebuild_feed_peers(feed_selected)
        self._restore_peer_selection(selected_session)
        self.nearby_status.setText(f"{len(peers)} sessions nearby")
        self.network_status.setText("Discovery active · application listener configured")
        self.nearby_value.setText(str(len(peers)) if peers else "Searching...")
        capabilities = {item for peer in peers for item in peer.hello.capabilities}
        self.capability_value.setText(str(len(capabilities)) if peers else "Waiting")
        if len(peers) != previous_count:
            self.append(f"Nearby roster now contains {len(peers)} active session(s).",
                        "Discovery")
        names = {peer.hello.peer_id: peer.hello.name for peer in self.all_peers}
        names[self.service.hello.peer_id] = self.service.hello.name
        self.post_model.set_author_names(names)

    def _rebuild_message_recipients(self, selected: object) -> None:
        self.recipient.blockSignals(True)
        self.recipient.clear()
        self.recipient.addItem("Nearby room · all discovered chat sessions", None)
        for peer in self.peers:
            self.recipient.addItem(
                f"Direct · {peer.hello.name} · {peer.ip}", peer.hello.session_id)
        index = self.recipient.findData(selected)
        if selected is not None and index < 0:
            self.recipient.addItem("Selected session is no longer nearby", selected)
            index = self.recipient.count() - 1
        self.recipient.setCurrentIndex(max(0, index))
        self.recipient.blockSignals(False)

    def _rebuild_feed_peers(self, selected: object) -> None:
        self.feed_peer.clear()
        self.feed_peer.addItem("Select one posts-capable peer", None)
        for peer in self.feed_peers:
            self.feed_peer.addItem(
                f"{peer.hello.name} · {peer.ip}", peer.hello.session_id)
        index = self.feed_peer.findData(selected)
        self.feed_peer.setCurrentIndex(max(0, index))

    def _restore_peer_selection(self, session_id: str | None) -> None:
        row = next((index for index, peer in enumerate(self.all_peers)
                    if peer.hello.session_id == session_id), -1)
        if row < 0 and session_id is None and self.all_peers:
            row = 0
        if row >= 0:
            self.peer_list.setCurrentIndex(self.peer_model.index(row, 0))
        else:
            self.peer_list.clearSelection()
            self.peer_list.setCurrentIndex(QModelIndex())
            self._show_peer(None)

    @Slot(QModelIndex, QModelIndex)
    def _peer_selected(self, current: QModelIndex,
                       previous: QModelIndex = QModelIndex()) -> None:
        del previous
        self._show_peer(self.peer_model.peer_at(current.row()))

    def _show_peer(self, peer: Peer | None) -> None:
        if peer is None:
            self.peer_name.setText("Select a nearby session")
            self.peer_presence.setText("No peer selected")
            self.peer_endpoint.setText("Endpoint: —")
            self.peer_identity.setText("Identity: —")
            self.peer_capabilities.setText("Presence only")
            for button in (self.peer_message_button, self.peer_file_button,
                           self.peer_sync_button, self.peer_probe_button):
                button.setEnabled(False)
            return
        age = max(0.0, time.monotonic() - peer.last_seen)
        self.peer_name.setText(peer.hello.name)
        self.peer_presence.setText(f"Nearby · last announcement {age:.1f} seconds ago")
        self.peer_endpoint.setText(
            f"Observed endpoint: {peer.ip}:{peer.hello.tcp_port}")
        self.peer_identity.setText(
            f"Installation {peer.hello.peer_id[:12]}…\n"
            f"Session {peer.hello.session_id[:12]}…")
        labels = [capability_label(item) for item in peer.hello.capabilities]
        self.peer_capabilities.setText("  ·  ".join(labels) or "Presence only")
        self.peer_message_button.setEnabled("chat_v1" in peer.hello.capabilities)
        self.peer_file_button.setEnabled("file_v1" in peer.hello.capabilities)
        self.peer_sync_button.setEnabled("posts_v1" in peer.hello.capabilities)
        self.peer_probe_button.setEnabled(True)

    def _selected_peer(self) -> Peer | None:
        return self.peer_model.peer_at(self.peer_list.currentIndex().row())

    @Slot(str)
    def _topology_device_selected(self, session_id: str) -> None:
        """Open the exact observed session selected in the topology."""
        row = next((index for index, peer in enumerate(self.all_peers)
                    if peer.hello.session_id == session_id), -1)
        if row >= 0:
            self.peer_list.setCurrentIndex(self.peer_model.index(row, 0))
            self.navigation.select(PAGE_DEVICES)

    @Slot(QModelIndex)
    def _overview_peer_selected(self, index: QModelIndex) -> None:
        """Open the selected overview session in the full device inspector."""
        peer = self.peer_model.peer_at(index.row())
        if peer is not None:
            self._topology_device_selected(peer.hello.session_id)

    def _peer_name(self, peer_id: str, session_id: str) -> str:
        peer = next((item for item in self.all_peers
                     if item.hello.session_id == session_id), None)
        return peer.hello.name if peer is not None else f"Peer {peer_id[:8]}…"

    def _message_selected_peer(self) -> None:
        peer = self._selected_peer()
        if peer is None:
            return
        index = self.recipient.findData(peer.hello.session_id)
        if index >= 0:
            self.recipient.setCurrentIndex(index)
            self.navigation.select(PAGE_MESSAGES)

    def _file_selected_peer(self) -> None:
        peer = self._selected_peer()
        if peer is None:
            return
        self._send_file_to_peer(peer)

    def _sync_selected_peer(self) -> None:
        peer = self._selected_peer()
        if peer is None:
            return
        index = self.feed_peer.findData(peer.hello.session_id)
        if index >= 0:
            self.feed_peer.setCurrentIndex(index)
            self.sync_feed()

    def update_transfer(self, value: dict[str, Any]) -> None:
        """Update both the model and compatibility selector for one transfer."""
        identifier = value["id"]
        if identifier not in self.transfer_rows:
            self.transfer_rows[identifier] = {}
            self.transfer_list.addItem(identifier, identifier)
        row = self.transfer_rows[identifier]
        row.update(value)
        removed = self.transfer_model.upsert(value)
        if removed is not None:
            self.transfer_rows.pop(removed, None)
            combo_index = self.transfer_list.findData(removed)
            if combo_index >= 0:
                self.transfer_list.removeItem(combo_index)
            if self.selected_transfer_id == removed:
                self.selected_transfer_id = None
        progress = ""
        if "bytes" in row and "total" in row:
            progress = f" {row['bytes']}/{row['total']} bytes"
        text = f"{row.get('name', identifier)}: {row['state']}{progress}"
        self.transfer_list.setItemText(self.transfer_list.findData(identifier), text)
        if value["state"] in TERMINAL_TRANSFERS:
            self.append(f"Transfer {identifier[:8]}…: {value['state']} "
                        f"{value.get('message', value.get('path', ''))}", "Transfers",
                        "warning" if value["state"] == "failed" else "info")
        self._update_transfer_metrics()
        model_row = self.transfer_model.identifiers.index(identifier)
        if self.selected_transfer_id == identifier:
            self._show_transfer(identifier)
        elif self.selected_transfer_id is None:
            self.transfer_view.setCurrentIndex(self.transfer_model.index(model_row, 0))

    @Slot(QModelIndex, QModelIndex)
    def _transfer_selected(self, current: QModelIndex,
                           previous: QModelIndex = QModelIndex()) -> None:
        del previous
        if not current.isValid():
            return
        identifier = self.transfer_model.identifiers[current.row()]
        self.selected_transfer_id = identifier
        index = self.transfer_list.findData(identifier)
        self.transfer_list.setCurrentIndex(index)
        self._show_transfer(identifier)

    def _show_transfer(self, identifier: str) -> None:
        """Refresh selected transfer phase, progress, and applicable actions."""
        row = self.transfer_rows[identifier]
        count, total = row.get("bytes", 0), row.get("total", 0)
        if total:
            self.transfer_progress.setValue(min(100, int(count * 100 / total)))
        else:
            self.transfer_progress.setValue(0)
        self.transfer_progress.setFormat(f"{row.get('state', 'pending')} · %p%")
        pending_offer = row.get("state") == "offer_pending"
        terminal = row.get("state") in TERMINAL_TRANSFERS
        self.accept_file.setEnabled(pending_offer)
        self.decline_file.setEnabled(pending_offer)
        self.cancel_file.setEnabled(not terminal and not pending_offer)

    def _update_transfer_metrics(self) -> None:
        active = sum(1 for row in self.transfer_rows.values()
                     if row.get("state") not in TERMINAL_TRANSFERS)
        self.transfer_status.setText(f"{active} active transfers")
        self.transfer_value.setText(str(active) if active else "None")
        self.transfer_slots.setText(f"{active} of 4 transfer slots in use")
        self.transfer_drawer.setVisible(active > 0)
        self.transfer_drawer_label.setText(
            f"{active} active transfer{'s' if active != 1 else ''} · phases and verification")

    @Slot()
    def send_file(self) -> None:
        """Select a source file for one explicitly selected chat peer."""
        selected = self.recipient.currentData()
        peer = next((item for item in self.peers
                     if item.hello.session_id == selected), None)
        if peer is None:
            self.append("Select one direct-message peer before sending a file.",
                        "Transfers", "warning")
            return
        self._send_file_to_peer(peer)

    def _send_file_to_peer(self, peer: Peer) -> None:
        """Offer one user-selected file to an explicit file-capable peer."""
        if "file_v1" not in peer.hello.capabilities:
            self.append("Selected peer does not advertise file sharing.",
                        "Transfers", "warning")
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Send file")
        if not filename:
            return
        try:
            identifier = self.service.transfers.send(Path(filename), peer)
            self.update_transfer({"id": identifier, "name": Path(filename).name,
                                  "state": "preparing"})
            self.navigation.select(PAGE_TRANSFERS)
        except (OSError, ValueError) as error:
            self.append(str(error), "Transfers", "warning")

    @Slot()
    def accept_offer(self) -> None:
        """Choose a new destination; the remote filename never selects a path."""
        identifier = self.selected_transfer_id
        row = self.transfer_rows.get(identifier, {})
        if row.get("state") != "offer_pending":
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save received file to a new filename")
        if filename and self.service.transfers.decide(identifier, Path(filename)):
            self.update_transfer({"id": identifier, "state": "accepted"})

    @Slot()
    def decline_offer(self) -> None:
        """Decline a still-pending offer."""
        identifier = self.selected_transfer_id
        if self.transfer_rows.get(identifier, {}).get("state") == "offer_pending":
            self.service.transfers.decide(identifier, None)

    @Slot()
    def cancel_transfer(self) -> None:
        """Cancel selected work without waiting on socket I/O in the GUI."""
        identifier = self.selected_transfer_id
        if identifier is not None:
            self.service.transfers.cancel(identifier)

    @Slot()
    def publish_post(self) -> None:
        """Persist one local post and clearly describe its local publication scope."""
        try:
            identifier = self.service.publish_post(self.post_input.text())
        except (ValueError, RuntimeError, OSError) as error:
            self.append(str(error), "Feed", "warning")
            return
        self.post_input.clear()
        self.append(f"Published {identifier[:8]}… locally; peers receive it on sync.",
                    "Feed")
        self.refresh_feed()

    @Slot()
    def sync_feed(self) -> None:
        """Queue feed paging from the explicitly selected capable peer."""
        selected = self.feed_peer.currentData()
        peer = next((item for item in self.feed_peers
                     if item.hello.session_id == selected), None)
        if peer is None:
            self.append("Select one posts-capable peer before syncing.",
                        "Feed", "warning")
            return
        try:
            identifier = self.service.sync_posts(peer)
        except (ValueError, RuntimeError) as error:
            self.append(str(error), "Feed", "warning")
            return
        self.append(f"Feed sync {identifier[:8]}… queued from {peer.hello.name}.",
                    "Feed")

    def refresh_feed(self) -> None:
        """Render a bounded local snapshot through both model and plain-text fallback."""
        if self.service.post_store is None:
            self.post_model.set_posts([], self.service.hello.peer_id, {})
            self.feed_log.setPlainText("Post storage is not configured.")
            return
        posts, _, _ = self.service.post_store.page(50)
        names = {peer.hello.peer_id: peer.hello.name for peer in self.all_peers}
        names[self.service.hello.peer_id] = self.service.hello.name
        self.post_model.set_posts(posts, self.service.hello.peer_id, names)
        lines = [f"{names.get(post['author_id'], post['author_id'])} | {post['text']}"
                 for post in posts]
        self.feed_log.setPlainText("\n\n".join(lines) if lines else "No cached posts.")

    def closeEvent(self, event: QCloseEvent) -> None:
        """Cancel core work; the entry point joins after the Qt loop exits."""
        self.timer.stop()
        self.service.stop()
        event.accept()
