"""LAN Atlas desktop shell; all core events are consumed on the Qt thread."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from queue import Empty
import time
from typing import Any

from PySide6.QtCore import QModelIndex, QSettings, Qt, QTimer, Slot
from PySide6.QtGui import QCloseEvent, QKeySequence, QResizeEvent, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListView, QMainWindow, QProgressBar,
    QPushButton, QScrollArea, QSpinBox, QSplitter, QStackedWidget, QTableView,
    QTextEdit, QVBoxLayout, QWidget,
)

from core.chat import ChatService
from core.diagnostics import Neighbor, NeighborSnapshot, ProbeResult
from core.peer_repository import (
    CompatibilityState, DiscoveryState, PeerRecord, PeerRepositoryEvent,
    ReachabilityState,
)
from core.roster import Peer
from gui.components import NavigationRail, action_button, card, page_header
from gui.models import (
    ActivityEntry, ActivityListModel, AdminDevice, AdminDeviceListModel,
    MessageEntry, MessageListModel, PeerListModel, PeerTableModel, PostListModel,
    TransferListModel, capability_label,
)
from gui.latency_map import LatencyMap
from gui.peer_selection import PeerSelection
from gui.theme import GEOMETRY, SPACING, apply_theme
from gui.topology import NetworkTopology

PAGE_OVERVIEW = 0
PAGE_NETWORK = 1
PAGE_DEVICES = 2
PAGE_WORKBENCH = 3
PAGE_FILES = 4
PAGE_TRANSFERS = 5
PAGE_MESSAGES = 6
PAGE_FEED = 7
PAGE_GAMES = 8
PAGE_ACTIVITY = 9
PAGE_SETTINGS = 10
TERMINAL_TRANSFERS = {"failed", "cancelled", "declined", "saved", "verified"}


class MainWindow(QMainWindow):
    """Present peer-to-peer state without implying central trust or connectivity."""

    def __init__(self, service: ChatService) -> None:
        super().__init__()
        self.settings = QSettings("LAN Manager", "LAN Atlas")
        self.theme_mode = str(self.settings.value("appearance/theme", "observatory"))
        app = QApplication.instance()
        if app is not None:
            self.theme_mode = apply_theme(app, self.theme_mode)
        self.service = service
        self.peer_records: tuple[PeerRecord, ...] = service.peer_repository.snapshot()
        self._peer_revision = 0
        self.peer_selection = PeerSelection()
        self.neighbors: tuple[Neighbor, ...] = ()
        self.transfer_rows: dict[str, dict[str, Any]] = {}
        self.selected_transfer_id: str | None = None
        self.message_outcomes: dict[str, dict[str, str]] = {}
        self.active_probe_id: str | None = None
        self.inventory_request_id: str | None = None
        self._next_presence_refresh = 0.0
        self.peer_model = PeerListModel()
        self.peer_table_model = PeerTableModel()
        self.message_model = MessageListModel()
        self.transfer_model = TransferListModel()
        self.post_model = PostListModel()
        self.activity_model = ActivityListModel()
        self.admin_device_model = AdminDeviceListModel()
        self.page_names = ("Overview", "Network", "Devices", "Workbench", "Files",
                           "Transfers", "Messages", "Feed", "Games", "Activity",
                           "Settings")
        self.navigation_groups = (
            ("Control", (("Overview", PAGE_OVERVIEW), ("Network", PAGE_NETWORK),
                         ("Devices", PAGE_DEVICES), ("Workbench", PAGE_WORKBENCH))),
            ("Share", (("Files", PAGE_FILES), ("Transfers", PAGE_TRANSFERS))),
            ("Community", (("Messages", PAGE_MESSAGES), ("Feed", PAGE_FEED),
                           ("Games", PAGE_GAMES))),
            ("System", (("Activity", PAGE_ACTIVITY), ("Settings", PAGE_SETTINGS))),
        )
        self.setWindowTitle(f"LAN Atlas · {service.hello.name}")
        self.setMinimumSize(GEOMETRY["minimum_width"], GEOMETRY["minimum_height"])
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        width = min(1440, available.width()) if available is not None else 1440
        height = min(900, available.height()) if available is not None else 900
        self.resize(width, height)
        self._build_shell()
        self.peer_selection.changed.connect(self._sync_peer_selection)
        self._apply_responsive_layout(self.width())
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

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self._build_status_strip())
        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(SPACING["md"], SPACING["md"],
                                            SPACING["md"], SPACING["md"])
        workspace_layout.setSpacing(SPACING["md"])
        self.stack = QStackedWidget()
        self.responsive_pages: list[tuple[QWidget, int]] = []
        self._add_page(self._build_overview_page(), 820)
        self._add_page(self._build_network_page(), 560)
        self._add_page(self._build_peers_page(), 680)
        self._add_page(self._build_admin_page(), 800)
        self._add_page(self._build_placeholder_page(
            "Files", "Shared-file catalog planned for the file-service phase.",
            "Current file transfer remains available from Devices, Messages, and Transfers. "
            "This page will list only explicitly published files."), 420)
        self._add_page(self._build_transfers_page(), 560)
        self._add_page(self._build_messages_page(), 560)
        self._add_page(self._build_feed_page(), 560)
        self._add_page(self._build_placeholder_page(
            "Games", "LAN game and session discovery is planned for a later phase.",
            "LAN Atlas will show only explicitly advertised game sessions and known "
            "reachability evidence. It will not add multiplayer to unsupported games."), 420)
        self._add_page(self._build_activity_page(), 560)
        self._add_page(self._build_settings_page(), 640)
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
        right_layout.addWidget(workspace, 1)
        right_layout.addWidget(self._build_footer())
        outer.addWidget(right, 1)
        self.setCentralWidget(root)

    def _add_page(self, page: QWidget, compact_height: int) -> None:
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(page)
        self.responsive_pages.append((page, compact_height))
        self.stack.addWidget(scroll)

    def _build_status_strip(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("ApplicationHeader")
        frame.setFixedHeight(58)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 7, 12, 7)
        version = QLabel("v1\nlocal")
        version.setObjectName("HeaderMetric")
        self.identity_status = QLabel(
            f"NODE · {self.service.hello.name}\n{self.service.hello.peer_id[:8]}")
        self.identity_status.setObjectName("HeaderMetric")
        self.nearby_status = QLabel("0 sessions nearby")
        self.nearby_status.setObjectName("HeaderMetric")
        self.network_status = QLabel("Network core configured")
        self.network_status.setObjectName("HeaderMetric")
        self.transfer_status = QLabel("0 active transfers")
        self.transfer_status.setObjectName("HeaderMetric")
        self.command_button = QPushButton("Commands  Ctrl+K")
        self.command_button.setEnabled(False)
        self.command_button.setToolTip("Command palette reserved for a later phase")
        self.security_status = QPushButton("◇  Unverified LAN")
        self.security_status.setProperty("security", True)
        self.security_status.setToolTip(
            "Peer names and IDs are self-reported. Traffic is not authenticated or encrypted.")
        self.security_status.clicked.connect(lambda: self.navigation.select(PAGE_SETTINGS))
        for widget in (version, self.identity_status, self.nearby_status,
                       self.network_status, self.transfer_status):
            layout.addWidget(widget)
        layout.addStretch(1)
        layout.addWidget(self.command_button)
        layout.addWidget(self.security_status)
        return frame

    def _build_footer(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("ApplicationFooter")
        frame.setFixedHeight(29)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 3, 10, 3)
        self.footer_left = QLabel(
            f"● Discovery UDP 50000  ·  Application TCP {self.service.hello.tcp_port}")
        self.footer_left.setProperty("technical", True)
        self.footer_right = QLabel("0 observed sessions  ·  Unverified LAN")
        self.footer_right.setProperty("technical", True)
        layout.addWidget(self.footer_left)
        layout.addStretch(1)
        layout.addWidget(self.footer_right)
        return frame

    def _page(self, title: str, subtitle: str,
              dense: bool = False) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(SPACING["md"])
        if dense:
            toolbar = QFrame()
            toolbar.setObjectName("OperationalToolbar")
            toolbar_layout = QHBoxLayout(toolbar)
            toolbar_layout.setContentsMargins(14, 9, 14, 9)
            heading = QLabel(title)
            heading.setObjectName("PanelTitle")
            detail = QLabel(subtitle)
            detail.setObjectName("PageSubtitle")
            detail.setWordWrap(True)
            toolbar_layout.addWidget(heading)
            toolbar_layout.addSpacing(SPACING["md"])
            toolbar_layout.addWidget(detail, 1)
            layout.addWidget(toolbar)
        else:
            layout.addWidget(page_header(title, subtitle))
        return page, layout

    def _build_overview_page(self) -> QWidget:
        page, layout = self._page(
            "Local network overview",
            "Live observations from this LAN Manager session.", True)
        self.overview_metrics = QGridLayout()
        nearby_card, self.nearby_value = card("Nearby sessions", "Searching...")
        capability_card, self.capability_value = card("Advertised features", "Waiting")
        transfer_card, self.transfer_value = card("Active transfers", "None")
        identity_card, self.identity_value = card("This device", self.service.hello.name)
        self.overview_metric_cards = (
            nearby_card, capability_card, transfer_card, identity_card)
        self.identity_detail = QLabel(f"ID {self.service.hello.peer_id[:8]}")
        self.identity_detail.setObjectName("TechnicalDetail")
        identity_card.layout().addWidget(self.identity_detail)
        for index, metric_card in enumerate(self.overview_metric_cards):
            self.overview_metrics.addWidget(metric_card, index // 2, index % 2)

        self.overview_splitter = QSplitter(Qt.Orientation.Horizontal)
        network = QFrame()
        network.setProperty("panel", True)
        network_layout = QVBoxLayout(network)
        network_heading = QLabel("LATENCY MAP (OBSERVED RTT)")
        network_heading.setObjectName("SectionLabel")
        network_layout.addWidget(network_heading)
        self.overview_topology = LatencyMap(self.service.hello, self.theme_mode)
        self.overview_topology.device_selected.connect(self._overview_map_selected)
        network_layout.addWidget(self.overview_topology, 1)
        self.overview_splitter.addWidget(network)

        metrics_panel = QFrame()
        metrics_panel.setProperty("panel", True)
        metrics_layout = QVBoxLayout(metrics_panel)
        metrics_heading = QLabel("LATENCY & METRICS")
        metrics_heading.setObjectName("PanelTitle")
        metrics_layout.addWidget(metrics_heading)
        metrics_layout.addLayout(self.overview_metrics)
        self.overview_summary = QLabel("Searching your LAN for observed hosts...")
        self.overview_summary.setObjectName("TechnicalDetail")
        self.overview_summary.setWordWrap(True)
        metrics_layout.addWidget(self.overview_summary)
        self.overview_rtt = QLabel("Measured latency: none yet")
        self.overview_rtt.setObjectName("TechnicalDetail")
        self.overview_rtt.setWordWrap(True)
        metrics_layout.addWidget(self.overview_rtt)
        metrics_note = QLabel(
            "Counts come from the canonical repository. "
            "Radial distance uses measured latency only "
            "(ping RTT, TCP handshake, or ECHO round-trip); "
            "unmeasured peers use a grey ring. "
            "No physical topology is inferred.")
        metrics_note.setObjectName("PageSubtitle")
        metrics_note.setWordWrap(True)
        metrics_layout.addWidget(metrics_note)
        metrics_layout.addStretch(1)
        self.overview_splitter.addWidget(metrics_panel)
        self.overview_splitter.setSizes([820, 360])
        layout.addWidget(self.overview_splitter, 3)

        self.overview_detail_splitter = QSplitter(Qt.Orientation.Horizontal)
        nearby = QFrame()
        nearby.setProperty("panel", True)
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
        self.overview_peer_list.clicked.connect(self._overview_peer_previewed)
        self.overview_peer_list.activated.connect(self._overview_peer_selected)
        self.overview_nearby_stack.addWidget(self.overview_nearby_empty)
        self.overview_nearby_stack.addWidget(self.overview_peer_list)
        nearby_layout.addWidget(self.overview_nearby_stack, 1)
        self.overview_detail_splitter.addWidget(nearby)

        overview_inspector = QFrame()
        overview_inspector.setProperty("panel", True)
        inspector_layout = QVBoxLayout(overview_inspector)
        inspector_heading = QLabel("SELECTED SESSION")
        inspector_heading.setObjectName("SectionLabel")
        self.overview_peer_name = QLabel("No session selected")
        self.overview_peer_name.setObjectName("PanelTitle")
        self.overview_peer_detail = QLabel(
            "Select an observed session to inspect its reported identity and capabilities.")
        self.overview_peer_detail.setObjectName("TechnicalDetail")
        self.overview_peer_detail.setWordWrap(True)
        self.overview_peer_caps = QLabel("Presence only")
        self.overview_peer_caps.setProperty("chip", True)
        self.overview_peer_rtt = QLabel("Measured latency: none yet")
        self.overview_peer_rtt.setObjectName("TechnicalDetail")
        self.overview_peer_rtt.setWordWrap(True)
        overview_trust = QLabel("◇  Cryptographic trust: Unverified")
        overview_trust.setProperty("warning", True)
        overview_trust.setWordWrap(True)
        inspector_layout.addWidget(inspector_heading)
        inspector_layout.addWidget(self.overview_peer_name)
        inspector_layout.addWidget(self.overview_peer_detail)
        inspector_layout.addWidget(self.overview_peer_caps)
        inspector_layout.addWidget(self.overview_peer_rtt)
        inspector_layout.addWidget(overview_trust)
        inspector_layout.addStretch(1)
        inspector_layout.addWidget(action_button(
            "Open full device inspector", lambda: self.navigation.select(PAGE_DEVICES), True))
        self.overview_detail_splitter.addWidget(overview_inspector)
        self.overview_detail_splitter.setSizes([700, 480])
        self.overview_detail_splitter.setMaximumHeight(300)
        layout.addWidget(self.overview_detail_splitter, 2)

        activity = QFrame()
        activity.setProperty("panel", True)
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
            "Discovery observations from this instance, not inferred physical topology.",
            True)
        controls = QFrame()
        controls.setObjectName("OperationalToolbar")
        control_layout = QHBoxLayout(controls)
        mode = QLabel("◎  OBSERVED MAP")
        mode.setObjectName("PanelTitle")
        self.network_toolbar_evidence = QLabel(
            "HELLO edges  ·  Visual positions")
        self.network_toolbar_evidence.setObjectName("TechnicalDetail")
        fit_button = action_button("Fit", self._fit_topology)
        devices_button = action_button(
            "Devices", lambda: self.navigation.select(PAGE_DEVICES), True)
        control_layout.addWidget(mode)
        control_layout.addSpacing(SPACING["md"])
        control_layout.addWidget(self.network_toolbar_evidence, 1)
        control_layout.addWidget(fit_button)
        control_layout.addWidget(devices_button)
        layout.addWidget(controls)

        self.network_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.topology = NetworkTopology(self.service.hello, self.theme_mode)
        self.topology.device_selected.connect(self._network_peer_selected)
        self.network_splitter.addWidget(self.topology)

        hud = QFrame()
        hud.setProperty("panel", True)
        hud_layout = QVBoxLayout(hud)
        hud_title = QLabel("OBSERVATION HUD")
        hud_title.setObjectName("PanelTitle")
        hud_layout.addWidget(hud_title)
        summary = QFrame()
        summary.setProperty("subpanel", True)
        summary_layout = QGridLayout(summary)
        self.network_observed_value = QLabel("0")
        self.network_observed_value.setObjectName("MetricValue")
        self.network_capability_value = QLabel("0")
        self.network_capability_value.setObjectName("MetricValue")
        summary_layout.addWidget(QLabel("Observed sessions"), 0, 0)
        summary_layout.addWidget(QLabel("Advertised features"), 0, 1)
        summary_layout.addWidget(self.network_observed_value, 1, 0)
        summary_layout.addWidget(self.network_capability_value, 1, 1)
        hud_layout.addWidget(summary)
        selected_title = QLabel("SELECTED NODE")
        selected_title.setObjectName("SectionLabel")
        self.network_selected_name = QLabel("No session selected")
        self.network_selected_name.setObjectName("PanelTitle")
        self.network_selected_detail = QLabel(
            "Choose a node to inspect its observed endpoint and HELLO capabilities.")
        self.network_selected_detail.setObjectName("TechnicalDetail")
        self.network_selected_detail.setWordWrap(True)
        self.network_selected_caps = QLabel("Presence only")
        self.network_selected_caps.setProperty("chip", True)
        hud_layout.addWidget(selected_title)
        hud_layout.addWidget(self.network_selected_name)
        hud_layout.addWidget(self.network_selected_detail)
        hud_layout.addWidget(self.network_selected_caps)
        legend_title = QLabel("EVIDENCE LEGEND")
        legend_title.setObjectName("SectionLabel")
        legend = QLabel(
            "● Nearby\n  Recent UDP HELLO observed\n\n"
            "○ Reachable\n  Requires an explicit successful check\n\n"
            "◇ Trust\n  No authenticated identity established")
        legend.setObjectName("TechnicalDetail")
        legend.setWordWrap(True)
        hud_layout.addWidget(legend_title)
        hud_layout.addWidget(legend)
        hud_layout.addStretch(1)
        hud_layout.addWidget(action_button(
            "Inspect in Devices", self._open_network_peer, True))
        self.network_splitter.addWidget(hud)
        self.network_splitter.setSizes([900, 310])
        layout.addWidget(self.network_splitter, 1)
        return page

    def _build_peers_page(self) -> QWidget:
        page, layout = self._page(
            "Devices", "Active sessions discovered through bounded UDP announcements.",
            True)
        toolbar = QFrame()
        toolbar.setObjectName("OperationalToolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        self.device_search = QLineEdit()
        self.device_search.setPlaceholderText("Filter by name, endpoint, session, capability...")
        self.device_search.setAccessibleName("Filter observed sessions")
        self.device_search.textChanged.connect(self._filter_devices)
        self.device_filter = QComboBox()
        self.device_filter.setAccessibleName("Device evidence filter")
        for label, value in (
                ("All", "all"), ("Nearby", "nearby"),
                ("Reachable", "reachable"), ("Compatible", "compatible"),
                ("Offline / stale", "stale")):
            self.device_filter.addItem(label, value)
        self.device_filter.currentIndexChanged.connect(
            lambda _index: self._filter_devices(self.device_search.text()))
        self.device_scope = QLabel("No subnet scan · no inferred offline devices")
        self.device_scope.setObjectName("TechnicalDetail")
        toolbar_layout.addWidget(self.device_search, 1)
        toolbar_layout.addWidget(self.device_filter)
        toolbar_layout.addWidget(self.device_scope)
        layout.addWidget(toolbar)

        self.peer_splitter = QSplitter(Qt.Orientation.Horizontal)
        table_panel = QFrame()
        table_panel.setProperty("panel", True)
        table_layout = QVBoxLayout(table_panel)
        table_heading = QLabel("OBSERVED SESSIONS")
        table_heading.setObjectName("PanelTitle")
        table_layout.addWidget(table_heading)
        self.peer_list = QTableView()
        self.peer_list.setModel(self.peer_table_model)
        self.peer_list.setAccessibleName("Nearby peer sessions")
        self.peer_list.setAlternatingRowColors(True)
        self.peer_list.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.peer_list.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.peer_list.setShowGrid(False)
        self.peer_list.verticalHeader().setVisible(False)
        self.peer_list.verticalHeader().setDefaultSectionSize(62)
        header = self.peer_list.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.peer_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.peer_list.selectionModel().currentChanged.connect(self._peer_selected)
        table_layout.addWidget(self.peer_list, 1)
        table_footer = QLabel(
            "● HELLO active  ·  Sessions expire on timeout")
        table_footer.setObjectName("TechnicalDetail")
        table_layout.addWidget(table_footer)
        self.peer_splitter.addWidget(table_panel)

        inspector = QFrame()
        inspector.setProperty("panel", True)
        details = QVBoxLayout(inspector)
        self.peer_name = QLabel("Select a nearby session")
        self.peer_name.setObjectName("PanelTitle")
        self.peer_presence = QLabel("No peer selected")
        self.peer_presence.setObjectName("PageSubtitle")
        self.peer_endpoint = QLabel("Endpoint: —")
        self.peer_endpoint.setProperty("technical", True)
        self.peer_endpoint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.peer_identity = QLabel("Identity: —")
        self.peer_identity.setProperty("technical", True)
        self.peer_identity.setWordWrap(True)
        self.peer_identity.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.peer_host = QLabel("Hostname: Not advertised")
        self.peer_platform = QLabel("Platform / architecture: Not advertised")
        self.peer_mac = QLabel("MAC address: Not observed")
        self.peer_latency = QLabel("Latency: Not measured")
        for item in (self.peer_host, self.peer_platform, self.peer_mac,
                     self.peer_latency):
            item.setProperty("technical", True)
            item.setWordWrap(True)
        self.peer_capabilities = QLabel("Presence only")
        self.peer_capabilities.setProperty("chip", True)
        self.peer_services = QLabel("Advertised / detected services: None observed")
        self.peer_services.setProperty("technical", True)
        self.peer_services.setWordWrap(True)
        self.peer_warning = QLabel(
            "Cryptographic Trust: Unverified\n"
            "No authenticated device identity has been established.")
        self.peer_warning.setProperty("warning", True)
        self.peer_warning.setWordWrap(True)
        self.peer_warning.setToolTip(
            "Names and identifiers are self-reported. Nearby does not mean trusted or reachable.")
        self.peer_message_button = action_button("Message", self._message_selected_peer, True)
        self.peer_file_button = action_button("Send file", self._file_selected_peer)
        self.peer_sync_button = action_button("Sync posts", self._sync_selected_peer)
        self.peer_ping_button = action_button(
            "Ping", lambda: self._probe_selected_peer("ping"))
        self.peer_tcp_button = action_button(
            "TCP test", lambda: self._probe_selected_peer("tcp"))
        self.peer_copy_button = action_button("Copy address", self._copy_peer_address)
        self.peer_probe_button = QPushButton("Open in Workbench")
        self.peer_probe_button.setToolTip(
            "Use this observed endpoint for an explicit ping or TCP check.")
        self.peer_probe_button.clicked.connect(self._open_peer_admin)
        for button in (self.peer_message_button, self.peer_file_button,
                       self.peer_sync_button, self.peer_ping_button,
                       self.peer_tcp_button, self.peer_copy_button,
                       self.peer_probe_button):
            button.setEnabled(False)
        details.addWidget(self.peer_name)
        details.addWidget(self.peer_presence)
        details.addSpacing(8)
        identity_panel = QFrame()
        identity_panel.setProperty("subpanel", True)
        identity_layout = QVBoxLayout(identity_panel)
        identity_title = QLabel("NETWORK & REPORTED IDENTITY")
        identity_title.setObjectName("SectionLabel")
        identity_layout.addWidget(identity_title)
        identity_layout.addWidget(self.peer_endpoint)
        identity_layout.addWidget(self.peer_identity)
        identity_layout.addWidget(self.peer_host)
        identity_layout.addWidget(self.peer_platform)
        identity_layout.addWidget(self.peer_mac)
        identity_layout.addWidget(self.peer_latency)
        details.addWidget(identity_panel)
        details.addWidget(self.peer_capabilities)
        details.addWidget(self.peer_services)
        evidence_panel = QFrame()
        evidence_panel.setProperty("subpanel", True)
        evidence_layout = QVBoxLayout(evidence_panel)
        evidence_title = QLabel("CONNECTION EVIDENCE")
        evidence_title.setObjectName("SectionLabel")
        self.peer_nearby_evidence = QLabel("● Nearby        Recent HELLO observed")
        self.peer_reachable_evidence = QLabel("○ Reachable     Not tested")
        self.peer_compatible_evidence = QLabel("○ Compatible    Not tested")
        for item in (self.peer_nearby_evidence, self.peer_reachable_evidence,
                     self.peer_compatible_evidence):
            item.setObjectName("TechnicalDetail")
            evidence_layout.addWidget(item)
        details.addWidget(evidence_panel)
        details.addWidget(self.peer_warning)
        details.addStretch(1)
        detail_actions = QGridLayout()
        detail_actions.addWidget(self.peer_message_button, 0, 0)
        detail_actions.addWidget(self.peer_file_button, 0, 1)
        detail_actions.addWidget(self.peer_sync_button, 1, 0)
        detail_actions.addWidget(self.peer_probe_button, 1, 1)
        detail_actions.addWidget(self.peer_ping_button, 2, 0)
        detail_actions.addWidget(self.peer_tcp_button, 2, 1)
        detail_actions.addWidget(self.peer_copy_button, 3, 0, 1, 2)
        details.addLayout(detail_actions)
        self.peer_splitter.addWidget(inspector)
        self.peer_splitter.setSizes([720, 500])
        layout.addWidget(self.peer_splitter, 1)
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
        label.setObjectName("BodyText")
        frame_layout.addWidget(label)
        frame_layout.addStretch(1)
        layout.addWidget(frame, 1)
        return page

    def _build_admin_page(self) -> QWidget:
        page, layout = self._page(
            "Workbench",
            "Observe local evidence and run bounded checks against one selected endpoint.",
            True)
        scope = QLabel(
            "Local operator view · no elevated authority. Neighbor-cache rows can be stale "
            "or incomplete; checks do not establish identity or trust.")
        scope.setProperty("warning", True)
        scope.setWordWrap(True)
        layout.addWidget(scope)

        tools = QFrame()
        tools.setObjectName("OperationalToolbar")
        tools_layout = QHBoxLayout(tools)
        tools_title = QLabel("IMPLEMENTED TOOLS")
        tools_title.setObjectName("SectionLabel")
        for text in ("PING / ICMP", "TCP HANDSHAKE", "LAN ATLAS ECHO"):
            chip = QLabel(text)
            chip.setProperty("chip", True)
            tools_layout.addWidget(chip)
        tools_layout.addStretch(1)
        self.workbench_limit = QLabel("One bounded worker · finite timeout · cancellable")
        self.workbench_limit.setObjectName("TechnicalDetail")
        tools_layout.addWidget(self.workbench_limit)
        layout.addWidget(tools)

        self.workbench_splitter = QSplitter(Qt.Orientation.Horizontal)
        inventory = QFrame()
        inventory.setProperty("panel", True)
        inventory_layout = QVBoxLayout(inventory)
        inventory_header = QHBoxLayout()
        inventory_title = QLabel("OBSERVED DEVICES")
        inventory_title.setObjectName("SectionLabel")
        self.refresh_neighbors_button = action_button(
            "Refresh neighbor cache", self._refresh_neighbors)
        inventory_header.addWidget(inventory_title)
        inventory_header.addStretch(1)
        inventory_header.addWidget(self.refresh_neighbors_button)
        inventory_layout.addLayout(inventory_header)
        self.inventory_status = QLabel(
            "LAN Atlas sessions appear automatically. Refresh to read the OS neighbor cache.")
        self.inventory_status.setObjectName("PageSubtitle")
        self.inventory_status.setWordWrap(True)
        inventory_layout.addWidget(self.inventory_status)
        self.admin_device_list = QListView()
        self.admin_device_list.setModel(self.admin_device_model)
        self.admin_device_list.setAccessibleName("Observed LAN endpoints")
        self.admin_device_list.setWordWrap(True)
        self.admin_device_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.admin_device_list.selectionModel().currentChanged.connect(
            self._admin_device_selected)
        inventory_layout.addWidget(self.admin_device_list, 1)
        inventory_note = QLabel(
            "This is not a complete device census. Sleeping, isolated, and previously unseen "
            "hosts may be absent. LAN Atlas sessions and neighbor rows remain separate evidence.")
        inventory_note.setObjectName("PageSubtitle")
        inventory_note.setWordWrap(True)
        inventory_layout.addWidget(inventory_note)
        self.workbench_splitter.addWidget(inventory)

        controls = QFrame()
        controls.setProperty("panel", True)
        control_layout = QVBoxLayout(controls)
        control_title = QLabel("PROBE PARAMETERS & RESULT")
        control_title.setObjectName("PanelTitle")
        control_layout.addWidget(control_title)
        self.admin_selection = QLabel("Enter an IPv4 address or select an observation")
        self.admin_selection.setObjectName("PageSubtitle")
        self.admin_selection.setWordWrap(True)
        control_layout.addWidget(self.admin_selection)
        address_label = QLabel("Numeric IPv4 address")
        address_label.setObjectName("MetricLabel")
        self.admin_address = QLineEdit()
        self.admin_address.setProperty("technical", True)
        self.admin_address.setPlaceholderText("192.168.1.20")
        self.admin_address.setAccessibleName("Diagnostic IPv4 address")
        port_label = QLabel("TCP port")
        port_label.setObjectName("MetricLabel")
        self.admin_port = QSpinBox()
        self.admin_port.setProperty("technical", True)
        self.admin_port.setRange(1, 65535)
        self.admin_port.setValue(80)
        self.admin_port.setAccessibleName("Diagnostic TCP port")
        control_layout.addWidget(address_label)
        control_layout.addWidget(self.admin_address)
        control_layout.addWidget(port_label)
        control_layout.addWidget(self.admin_port)
        actions = QGridLayout()
        self.admin_ping_button = action_button("Ping selected", self._ping_admin_target, True)
        self.admin_tcp_button = action_button("Check TCP port", self._tcp_admin_target)
        self.admin_echo_button = action_button(
            "Check LAN Manager ECHO", self._echo_admin_target)
        self.admin_echo_button.setToolTip(
            "Available only for the selected session advertising echo_v1.")
        self.admin_echo_button.setEnabled(False)
        self.admin_cancel_button = action_button("Cancel current check", self._cancel_admin_probe)
        self.admin_cancel_button.setEnabled(False)
        actions.addWidget(self.admin_ping_button, 0, 0)
        actions.addWidget(self.admin_tcp_button, 0, 1)
        actions.addWidget(self.admin_echo_button, 1, 0, 1, 2)
        actions.addWidget(self.admin_cancel_button, 2, 0, 1, 2)
        control_layout.addLayout(actions)
        self.admin_result = QLabel(
            "No check has run. Ping and TCP are separate evidence; failed ping does not prove "
            "that a TCP service is unavailable.")
        self.admin_result.setWordWrap(True)
        self.admin_result.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.admin_result.setProperty("chip", True)
        control_layout.addWidget(self.admin_result)
        self.admin_address.textChanged.connect(self._update_admin_probe_buttons)
        self.admin_port.valueChanged.connect(self._update_admin_probe_buttons)
        control_layout.addStretch(1)
        self.workbench_splitter.addWidget(controls)
        self.workbench_splitter.setSizes([560, 420])
        layout.addWidget(self.workbench_splitter, 1)

        console = QFrame()
        console.setProperty("well", True)
        console_layout = QVBoxLayout(console)
        console_title = QLabel("WORKBENCH ACTIVITY")
        console_title.setObjectName("SectionLabel")
        self.workbench_log = QTextEdit()
        self.workbench_log.setReadOnly(True)
        self.workbench_log.setProperty("technical", True)
        self.workbench_log.document().setMaximumBlockCount(200)
        self.workbench_log.setPlaceholderText(
            "Bounded diagnostic events will appear here. No packet details are inferred.")
        console_layout.addWidget(console_title)
        console_layout.addWidget(self.workbench_log)
        console.setMaximumHeight(220)
        layout.addWidget(console)
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
        self.log.setProperty("technical", True)
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
            if name in {"Installation ID", "Session ID", "Application port"}:
                content.setProperty("technical", True)
            content.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            content.setWordWrap(True)
            form.addWidget(label, row, 0)
            form.addWidget(content, row, 1)
        appearance_row = len(values)
        appearance_label = QLabel("Appearance")
        appearance_label.setObjectName("MetricLabel")
        self.theme_selector = QComboBox()
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
        security_heading.setObjectName("SecurityHeading")
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
                          PAGE_FILES, PAGE_TRANSFERS, PAGE_MESSAGES, PAGE_FEED,
                          PAGE_GAMES)
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

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Adapt dense shell regions to the available logical width."""
        super().resizeEvent(event)
        if hasattr(self, "workbench_splitter"):
            self._apply_responsive_layout(event.size().width())

    def _apply_responsive_layout(self, width: int) -> None:
        compact = width < GEOMETRY["responsive_breakpoint"]
        self.navigation.set_compact(compact)
        self.identity_status.setVisible(not compact)
        self.network_status.setVisible(not compact)
        self.command_button.setVisible(not compact)
        self.network_toolbar_evidence.setVisible(not compact)
        self.device_scope.setVisible(not compact)
        self.workbench_limit.setVisible(not compact)
        orientation = (Qt.Orientation.Vertical if compact
                       else Qt.Orientation.Horizontal)
        for splitter in (self.overview_splitter, self.overview_detail_splitter,
                         self.network_splitter, self.peer_splitter,
                         self.workbench_splitter):
            splitter.setOrientation(orientation)
        for page, compact_height in self.responsive_pages:
            page.setMinimumHeight(compact_height if compact else 0)
            if page.layout() is not None:
                page.layout().invalidate()
            page.updateGeometry()

    @Slot(int)
    def _theme_changed(self, index: int) -> None:
        """Apply and persist one user-selected appearance mode."""
        mode = self.theme_selector.itemData(index)
        if not isinstance(mode, str):
            return
        self.theme_mode = mode
        app = QApplication.instance()
        if app is not None:
            self.theme_mode = apply_theme(app, mode)
        self.settings.setValue("appearance/theme", self.theme_mode)
        self.topology.set_theme(self.theme_mode)
        self.overview_topology.set_theme(self.theme_mode)
        self.topology.select_session(self.peer_selection.session_id)
        self.overview_topology.select_session(self.peer_selection.session_id)

    def append(self, text: str, category: str = "System",
               severity: str = "info") -> None:
        """Retain plain-text operational output and a structured Activity row."""
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text + "\n")
        self.log.setTextCursor(cursor)
        self.activity_model.append(ActivityEntry(datetime.now(), category, text,
                                                  severity=severity))
        if category == "Workbench" and hasattr(self, "workbench_log"):
            self.workbench_log.append(text)
        self.activity_view.scrollToBottom()
        self.overview_activity_stack.setCurrentWidget(self.overview_activity_view)
        self.overview_activity_view.scrollToBottom()

    @Slot()
    def _refresh_neighbors(self) -> None:
        try:
            self.inventory_request_id = self.service.diagnostics.refresh_neighbors()
        except RuntimeError as error:
            self.append(str(error), "Workbench", "warning")
            return
        self.refresh_neighbors_button.setEnabled(False)
        self.inventory_status.setText("Reading the operating system neighbor cache...")

    @Slot()
    def _ping_admin_target(self) -> None:
        self._queue_admin_probe("ping")

    @Slot()
    def _tcp_admin_target(self) -> None:
        self._queue_admin_probe("tcp")

    @Slot()
    def _echo_admin_target(self) -> None:
        self._queue_admin_probe("echo")

    def _queue_admin_probe(self, kind: str) -> None:
        address = self.admin_address.text().strip()
        try:
            if kind == "ping":
                identifier = self.service.diagnostics.ping(address)
            elif kind == "tcp":
                identifier = self.service.diagnostics.tcp_connect(
                    address, self.admin_port.value())
            else:
                device = self._selected_admin_device()
                record = self.service.peer_repository.get(
                    device.session_id if device is not None else None)
                if (device is None or "echo_v1" not in device.capabilities
                        or device.peer_id is None or device.session_id is None
                        or device.address != address
                        or device.port != self.admin_port.value()
                        or record is None or not record.nearby
                        or record.ip != address
                        or record.hello.tcp_port != self.admin_port.value()
                        or record.hello.peer_id != device.peer_id
                        or record.session_id != device.session_id
                        or "echo_v1" not in record.hello.capabilities):
                    raise ValueError(
                        "select an unchanged LAN Atlas session advertising echo_v1")
                identifier = self.service.diagnostics.echo(
                    address, self.admin_port.value(),
                    device.peer_id, device.session_id)
        except (RuntimeError, ValueError) as error:
            self.admin_address.setFocus()
            self.admin_result.setText(str(error))
            self.append(str(error), "Workbench", "warning")
            return
        device = self._selected_admin_device()
        if (device is not None and device.session_id is not None
                and device.address == address
                and (kind == "ping" or device.port == self.admin_port.value())):
            self.service.peer_repository.register_probe(
                identifier, device.session_id, address,
                None if kind == "ping" else self.admin_port.value(), kind)
        self.active_probe_id = identifier
        self.admin_ping_button.setEnabled(False)
        self.admin_tcp_button.setEnabled(False)
        self.admin_echo_button.setEnabled(False)
        self.admin_result.setText(f"{kind.upper()} check queued for {address}")

    @Slot()
    def _cancel_admin_probe(self) -> None:
        self.service.diagnostics.cancel()
        self.admin_cancel_button.setEnabled(False)
        self.admin_result.setText("Cancellation requested")

    @Slot(QModelIndex, QModelIndex)
    def _admin_device_selected(self, current: QModelIndex,
                               previous: QModelIndex = QModelIndex()) -> None:
        del previous
        device = self.admin_device_model.device_at(current.row())
        if device is None:
            return
        self.admin_address.setText(device.address)
        if device.port is not None:
            self.admin_port.setValue(device.port)
        self.admin_selection.setText(
            f"{device.label} · {device.source}\n{device.detail}")
        self._update_admin_probe_buttons()

    def _selected_admin_device(self) -> AdminDevice | None:
        return self.admin_device_model.device_at(
            self.admin_device_list.currentIndex().row())

    @Slot()
    def _update_admin_probe_buttons(self) -> None:
        idle = self.active_probe_id is None
        self.admin_ping_button.setEnabled(idle)
        self.admin_tcp_button.setEnabled(idle)
        device = self._selected_admin_device()
        self.admin_echo_button.setEnabled(
            idle and device is not None
            and "echo_v1" in device.capabilities
            and device.peer_id is not None and device.session_id is not None
            and device.address == self.admin_address.text().strip()
            and device.port == self.admin_port.value())

    def _rebuild_admin_devices(self) -> None:
        selected = self.admin_device_model.device_at(
            self.admin_device_list.currentIndex().row())
        selected_key = selected.key if selected is not None else None
        devices = [AdminDevice(
            key=f"peer:{record.session_id}", label=record.hello.name,
            address=record.ip, source="LAN Atlas HELLO",
            detail=(f"Recent unverified session; not authenticated · "
                    f"{', '.join(record.hello.capabilities) or 'presence only'}"),
            port=record.hello.tcp_port, capabilities=record.hello.capabilities,
            peer_id=record.hello.peer_id, session_id=record.session_id)
            for record in self.peer_records if record.nearby]
        devices.extend(AdminDevice(
            f"neighbor:{neighbor.interface or ''}:{neighbor.address}",
            f"Neighbor {neighbor.address}", neighbor.address, "OS neighbor cache",
            " · ".join(part for part in (
                f"MAC {neighbor.mac_address}" if neighbor.mac_address else "MAC unavailable",
                f"interface {neighbor.interface}" if neighbor.interface else "interface unavailable",
                f"state {neighbor.state}" if neighbor.state else "state unavailable") if part))
            for neighbor in self.neighbors)
        devices.sort(key=lambda item: (item.source != "LAN Atlas HELLO", item.label.lower(),
                                       item.address))
        self.admin_device_model.set_devices(tuple(devices))
        row = next((index for index, device in enumerate(devices)
                    if device.key == selected_key), -1)
        if row >= 0:
            self.admin_device_list.setCurrentIndex(
                self.admin_device_model.index(row, 0))
        elif selected_key is not None:
            self.admin_device_list.setCurrentIndex(QModelIndex())
        self._update_admin_probe_buttons()

    def _show_neighbor_snapshot(self, snapshot: NeighborSnapshot) -> None:
        if (self.inventory_request_id is not None
                and snapshot.request_id != self.inventory_request_id):
            return
        self.inventory_request_id = None
        self.refresh_neighbors_button.setEnabled(True)
        if snapshot.error:
            self.inventory_status.setText(f"Neighbor refresh unavailable: {snapshot.error}")
            self.append(f"Neighbor refresh unavailable: {snapshot.error}",
                        "Workbench", "warning")
            return
        self.neighbors = snapshot.entries
        event = self.service.peer_repository.apply_neighbor_snapshot(snapshot)
        if event is not None:
            self._apply_repository_event(event)
        self.inventory_status.setText(
            f"{len(snapshot.entries)} neighbor-cache entr"
            f"{'y' if len(snapshot.entries) == 1 else 'ies'} observed. "
            "Rows may be stale or incomplete.")
        self._rebuild_admin_devices()
        self.append(f"Observed {len(snapshot.entries)} OS neighbor-cache entries.",
                    "Workbench")

    def _show_probe_started(self, result: ProbeResult) -> None:
        if result.request_id != self.active_probe_id:
            return
        endpoint = (f"{result.address}:{result.port}"
                    if result.port is not None else result.address)
        self.admin_result.setText(f"Running {result.kind.upper()} check for {endpoint}...")
        self.admin_cancel_button.setEnabled(True)

    def _show_probe_result(self, result: ProbeResult) -> None:
        endpoint = (f"{result.address}:{result.port}"
                    if result.port is not None else result.address)
        duration = (f" · {result.duration_ms:.1f} ms local operation time"
                    if result.duration_ms is not None else "")
        rtt = (f" · measured latency {result.rtt_ms:.1f} ms via {result.kind}"
               if result.rtt_ms is not None else "")
        detail = f"\n{result.detail}" if result.detail else ""
        text = f"{result.kind.upper()} {endpoint}: {result.state}{duration}{rtt}{detail}"
        self.append(text.replace("\n", " · "), "Workbench",
                    "warning" if result.state not in {"reachable", "compatible"} else "info")
        if result.request_id != self.active_probe_id:
            return
        self.active_probe_id = None
        self.admin_result.setText(text)
        self.admin_cancel_button.setEnabled(False)
        self._update_admin_probe_buttons()

    @Slot()
    def send(self) -> None:
        """Queue a room or direct message without blocking the GUI."""
        text = self.input.text()
        selected = self.recipient.currentData()
        peers = tuple(record.as_peer()
                      for record in self.service.peer_repository.supporting("chat_v1")
                      if selected is None or record.session_id == selected)
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
            if kind == "peer_repository":
                self._apply_repository_event(value)
            elif kind == "roster":
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
            elif kind == "neighbor_snapshot":
                self._show_neighbor_snapshot(value)
            elif kind == "diagnostic_started":
                self._show_probe_started(value)
            elif kind == "diagnostic_result":
                event = self.service.peer_repository.apply_probe_result(value)
                if event is not None:
                    self._apply_repository_event(event)
                self._show_probe_result(value)
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
            self.peer_table_model.refresh_ages()
            record = self._selected_record()
            if record is not None:
                self._show_peer(record)

    def _update_roster(self, peers: tuple[Peer, ...]) -> None:
        """Adapt legacy active-roster events into the canonical repository."""
        event = self.service.peer_repository.reconcile_presence(peers, time.monotonic())
        if event is not None:
            self._apply_repository_event(event)

    def _apply_repository_event(self, event: PeerRepositoryEvent) -> None:
        """Ignore delayed revisions so older evidence cannot replace newer state."""
        if event.revision <= self._peer_revision:
            return
        self._peer_revision = event.revision
        self._update_peer_records(event.snapshot)

    def _update_peer_records(self, records: tuple[PeerRecord, ...]) -> None:
        """Render one immutable repository snapshot across all peer surfaces."""
        previous_count = sum(record.nearby for record in self.peer_records)
        message_selected = self.recipient.currentData()
        feed_selected = self.feed_peer.currentData()
        self.peer_records = records
        nearby = tuple(record for record in records if record.nearby)
        nearby_peers = tuple(record.as_peer() for record in nearby)
        self.peer_model.set_records(nearby)
        self.peer_table_model.set_records(records)
        self._rebuild_admin_devices()
        self.topology.set_peers(nearby_peers)
        self.overview_topology.set_records(nearby)
        self.overview_nearby_stack.setCurrentWidget(
            self.overview_peer_list if nearby else self.overview_nearby_empty)
        self._rebuild_message_recipients(message_selected)
        self._rebuild_feed_peers(feed_selected)
        self.peer_selection.reconcile({record.session_id for record in records})
        if self.peer_selection.session_id is None and records:
            self.peer_selection.select(records[0].session_id)
        else:
            self._sync_peer_selection(self.peer_selection.session_id)
        self._filter_devices(self.device_search.text())
        self.nearby_status.setText(f"{len(nearby)} sessions nearby")
        self.network_status.setText("Discovery active · application listener configured")
        stale_count = len(records) - len(nearby)
        self.footer_right.setText(
            f"{len(nearby)} nearby · {stale_count} stale  ·  Unverified LAN")
        self.nearby_value.setText(str(len(nearby)) if nearby else "Searching...")
        capabilities = {item for record in nearby for item in record.hello.capabilities}
        self.capability_value.setText(str(len(capabilities)) if nearby else "Waiting")
        self.network_observed_value.setText(str(len(nearby)))
        self.network_capability_value.setText(str(len(capabilities)))
        summary = self.service.peer_repository.overview_summary()
        if not records:
            self.overview_summary.setText("Searching your LAN for observed hosts...")
            self.overview_rtt.setText("Measured latency: none yet")
        else:
            self.overview_summary.setText(
                f"{summary.observed} observed hosts · "
                f"{summary.responsive} responsive · "
                f"{summary.stale} offline")
            if summary.measured and summary.min_ms is not None:
                self.overview_rtt.setText(
                    f"Measured latency: min {summary.min_ms:.1f} ms · "
                    f"avg {summary.avg_ms:.1f} ms · "
                    f"max {summary.max_ms:.1f} ms "
                    f"({summary.measured} of {summary.nearby} nearby)")
            else:
                self.overview_rtt.setText(
                    "Measured latency: none yet — run Ping, TCP, or ECHO")
        if len(nearby) != previous_count:
            self.append(f"Nearby roster now contains {len(nearby)} active session(s).",
                        "Discovery")
        names = {record.hello.peer_id: record.hello.name for record in records}
        names[self.service.hello.peer_id] = self.service.hello.name
        self.post_model.set_author_names(names)

    def _rebuild_message_recipients(self, selected: object) -> None:
        self.recipient.blockSignals(True)
        self.recipient.clear()
        self.recipient.addItem("Nearby room · all discovered chat sessions", None)
        for record in self.peer_records:
            if not record.nearby or "chat_v1" not in record.hello.capabilities:
                continue
            self.recipient.addItem(
                f"Direct · {record.hello.name} · {record.ip}", record.session_id)
        index = self.recipient.findData(selected)
        if selected is not None and index < 0:
            self.recipient.addItem("Selected session is no longer nearby", selected)
            index = self.recipient.count() - 1
        self.recipient.setCurrentIndex(max(0, index))
        self.recipient.blockSignals(False)

    def _rebuild_feed_peers(self, selected: object) -> None:
        self.feed_peer.clear()
        self.feed_peer.addItem("Select one posts-capable peer", None)
        for record in self.peer_records:
            if not record.nearby or "posts_v1" not in record.hello.capabilities:
                continue
            self.feed_peer.addItem(
                f"{record.hello.name} · {record.ip}", record.session_id)
        index = self.feed_peer.findData(selected)
        self.feed_peer.setCurrentIndex(max(0, index))

    @Slot(object)
    def _sync_peer_selection(self, session_id: object) -> None:
        """Render one shared selected session across every peer surface."""
        selected = session_id if isinstance(session_id, str) else None
        record = self._record_in_snapshot(selected)
        row = next((index for index, item in enumerate(self.peer_records)
                    if item.session_id == selected), -1)
        index = self.peer_table_model.index(row, 0) if row >= 0 else QModelIndex()
        self.peer_list.setCurrentIndex(index)
        overview_row = next(
            (index for index, item in enumerate(self.peer_model.records)
             if item.session_id == selected), -1)
        overview_index = (self.peer_model.index(overview_row, 0)
                          if overview_row >= 0 else QModelIndex())
        self.overview_peer_list.setCurrentIndex(overview_index)
        self._show_peer(record)
        self.topology.select_session(selected)
        self.overview_topology.select_session(selected)
        if record is None:
            self.overview_peer_name.setText("No session selected")
            self.overview_peer_detail.setText(
                "Select a retained session to inspect known endpoint evidence.")
            self.overview_peer_caps.setText("Presence only")
            self.overview_peer_rtt.setText("Measured latency: none yet")
            self.network_selected_name.setText("No session selected")
            self.network_selected_detail.setText(
                "Choose a node to inspect its observed endpoint and HELLO capabilities.")
            self.network_selected_caps.setText("Presence only")
            return
        state = "Nearby" if record.nearby else "Offline / stale"
        labels = [capability_label(item) for item in record.hello.capabilities]
        self.overview_peer_name.setText(record.hello.name)
        self.overview_peer_detail.setText(
            f"{record.ip}:{record.hello.tcp_port}\n"
            f"Installation {record.hello.peer_id[:12]}…\n"
            f"Session {record.session_id[:12]}…\n{state} · Unverified")
        self.overview_peer_caps.setText("  ·  ".join(labels) or "Presence only")
        if record.latency_ms is None:
            self.overview_peer_rtt.setText("Measured latency: none yet")
        else:
            source = f" via {record.latency_source}" if record.latency_source else ""
            self.overview_peer_rtt.setText(
                f"Measured latency: {record.latency_ms:.1f} ms{source}")
        self.network_selected_name.setText(record.hello.name)
        self.network_selected_detail.setText(
            f"Observed endpoint  {record.ip}:{record.hello.tcp_port}\n"
            f"Session  {record.session_id[:12]}…\n"
            f"State  {state} · Unverified")
        self.network_selected_caps.setText("  ·  ".join(labels) or "Presence only")

    @Slot(QModelIndex, QModelIndex)
    def _peer_selected(self, current: QModelIndex,
                       previous: QModelIndex = QModelIndex()) -> None:
        del previous
        record = self.peer_table_model.record_at(current.row())
        if record is not None:
            self.peer_selection.select(record.session_id)

    def _show_peer(self, record: PeerRecord | None) -> None:
        if record is None:
            self.peer_name.setText("Select a nearby session")
            self.peer_presence.setText("No peer selected")
            self.peer_endpoint.setText("Endpoint: —")
            self.peer_identity.setText("Identity: —")
            self.peer_host.setText("Hostname: Not advertised")
            self.peer_platform.setText("Platform / architecture: Not advertised")
            self.peer_mac.setText("MAC address: Not observed")
            self.peer_latency.setText("Latency: Not measured")
            self.peer_capabilities.setText("Presence only")
            self.peer_services.setText("Advertised / detected services: None observed")
            self.peer_nearby_evidence.setText("○ Nearby        No session selected")
            self.peer_reachable_evidence.setText("○ Reachable     Not tested")
            self.peer_compatible_evidence.setText("○ Compatible    Not tested")
            for button in (self.peer_message_button, self.peer_file_button,
                           self.peer_sync_button, self.peer_ping_button,
                           self.peer_tcp_button, self.peer_copy_button,
                           self.peer_probe_button):
                button.setEnabled(False)
            return
        age = max(0.0, time.monotonic() - record.last_seen)
        state = "Nearby" if record.nearby else "Offline / stale"
        self.peer_name.setText(record.hello.name)
        self.peer_presence.setText(f"{state} · last announcement {age:.1f} seconds ago")
        self.peer_endpoint.setText(
            f"Observed endpoint: {record.ip}:{record.hello.tcp_port}")
        self.peer_identity.setText(
            f"Installation {record.hello.peer_id[:12]}…\n"
            f"Session {record.session_id[:12]}…")
        self.peer_host.setText(f"Hostname: {record.hostname or 'Not advertised'}")
        platform = " / ".join(part for part in (record.platform, record.architecture)
                              if part)
        self.peer_platform.setText(
            f"Platform / architecture: {platform or 'Not advertised'}")
        mac = record.mac_address or "Not observed"
        source = f" · {record.mac_source}" if record.mac_source else ""
        self.peer_mac.setText(f"MAC address: {mac}{source}")
        if record.latency_ms is None:
            latency = "Not measured"
        else:
            source = f" via {record.latency_source}" if record.latency_source else ""
            latency = f"{record.latency_ms:.1f} ms{source}"
        self.peer_latency.setText(f"Latency: {latency}")
        labels = [capability_label(item) for item in record.hello.capabilities]
        self.peer_capabilities.setText("  ·  ".join(labels) or "Presence only")
        self.peer_services.setText(
            "Advertised / detected services: "
            + (", ".join(record.services) if record.services else "None observed"))
        self.peer_nearby_evidence.setText(
            "● Nearby        Recent HELLO observed" if record.nearby
            else "○ Nearby        HELLO expired; retained as stale")
        reachable = record.reachability_state.value.replace("_", " ").title()
        compatible = record.compatibility_state.value.replace("_", " ").title()
        self.peer_reachable_evidence.setText(f"○ Reachable     {reachable}")
        self.peer_compatible_evidence.setText(f"○ Compatible    {compatible}")
        live = record.nearby
        self.peer_message_button.setEnabled(
            live and "chat_v1" in record.hello.capabilities)
        self.peer_file_button.setEnabled(live and "file_v1" in record.hello.capabilities)
        self.peer_sync_button.setEnabled(live and "posts_v1" in record.hello.capabilities)
        self.peer_ping_button.setEnabled(live)
        self.peer_tcp_button.setEnabled(live)
        self.peer_copy_button.setEnabled(True)
        self.peer_probe_button.setEnabled(live)

    def _selected_record(self) -> PeerRecord | None:
        return self._record_in_snapshot(self.peer_selection.session_id)

    def _record_in_snapshot(self, session_id: str | None) -> PeerRecord | None:
        """Return one record from the latest revision accepted by the UI."""
        return next((record for record in self.peer_records
                     if record.session_id == session_id), None)

    def _selected_peer(self) -> Peer | None:
        record = self.service.peer_repository.get(self.peer_selection.session_id)
        return record.as_peer() if record is not None and record.nearby else None

    @Slot(str)
    def _filter_devices(self, text: str) -> None:
        """Filter only fields present in the HELLO observation."""
        query = text.strip().casefold()
        evidence_filter = self.device_filter.currentData()
        for row, peer in enumerate(self.peer_table_model.records):
            labels = tuple(capability_label(item) for item in peer.hello.capabilities)
            values = " ".join((peer.hello.name, peer.ip, str(peer.hello.tcp_port),
                               f"{peer.ip}:{peer.hello.tcp_port}", peer.session_id,
                               peer.hello.peer_id, peer.mac_address or "",
                               *peer.hello.capabilities, *labels)).casefold()
            matches_state = (
                evidence_filter == "all"
                or evidence_filter == "nearby" and peer.nearby
                or evidence_filter == "reachable"
                and peer.reachability_state is ReachabilityState.REACHABLE
                or evidence_filter == "compatible"
                and peer.compatibility_state is CompatibilityState.COMPATIBLE
                or evidence_filter == "stale"
                and peer.discovery_state is DiscoveryState.STALE)
            hidden = not matches_state or bool(query and query not in values)
            self.peer_list.setRowHidden(row, hidden)

    @Slot(str)
    def _topology_device_selected(self, session_id: str) -> None:
        """Open the exact observed session selected in the topology."""
        if self._record_in_snapshot(session_id) is not None:
            self.peer_selection.select(session_id)
            self.navigation.select(PAGE_DEVICES)

    @Slot(str)
    def _network_peer_selected(self, session_id: str) -> None:
        """Update the Network HUD without claiming probe or trust evidence."""
        if self._record_in_snapshot(session_id) is not None:
            self.peer_selection.select(session_id)

    def _open_network_peer(self) -> None:
        if self.peer_selection.session_id is not None:
            self._topology_device_selected(self.peer_selection.session_id)

    def _fit_topology(self) -> None:
        self.topology.view.fitInView(
            self.topology.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    @Slot(QModelIndex)
    def _overview_peer_previewed(self, index: QModelIndex) -> None:
        """Show one observed session without implying additional evidence."""
        record = self.peer_model.record_at(index.row())
        if record is not None:
            self.peer_selection.select(record.session_id)

    @Slot(str)
    def _overview_map_selected(self, session_id: str) -> None:
        """Preview one latency-map session without leaving Overview."""
        if self._record_in_snapshot(session_id) is not None:
            self.peer_selection.select(session_id)

    @Slot(QModelIndex)
    def _overview_peer_selected(self, index: QModelIndex) -> None:
        """Open the selected overview session in the full device inspector."""
        record = self.peer_model.record_at(index.row())
        if record is not None:
            self._topology_device_selected(record.session_id)

    def _peer_name(self, peer_id: str, session_id: str) -> str:
        record = self._record_in_snapshot(session_id)
        return record.hello.name if record is not None else f"Peer {peer_id[:8]}…"

    def _message_selected_peer(self) -> None:
        peer = self._selected_peer()
        if peer is None:
            return
        index = self.recipient.findData(peer.hello.session_id)
        if index >= 0:
            self.recipient.setCurrentIndex(index)
            self.navigation.select(PAGE_MESSAGES)

    def _probe_selected_peer(self, kind: str) -> None:
        """Queue an explicit diagnostic for the canonical live selection."""
        if self._selected_peer() is None:
            return
        self._open_peer_admin()
        self._queue_admin_probe(kind)

    def _copy_peer_address(self) -> None:
        """Copy the selected observed IPv4 address to the system clipboard."""
        record = self._selected_record()
        if record is not None:
            QApplication.clipboard().setText(record.ip)

    def _open_peer_admin(self) -> None:
        peer = self._selected_peer()
        if peer is None:
            return
        self.admin_address.setText(peer.ip)
        self.admin_port.setValue(peer.hello.tcp_port)
        self.admin_selection.setText(
            f"{peer.hello.name} · LAN Atlas HELLO\n"
            "Endpoint is observed and advertised, not authenticated.")
        key = f"peer:{peer.hello.session_id}"
        row = next((index for index, device in enumerate(self.admin_device_model.devices)
                    if device.key == key), -1)
        if row >= 0:
            self.admin_device_list.setCurrentIndex(
                self.admin_device_model.index(row, 0))
        self.navigation.select(PAGE_WORKBENCH)

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
        record = self.service.peer_repository.get(
            selected if isinstance(selected, str) else None)
        if record is None or not record.nearby:
            self.append("Select one direct-message peer before sending a file.",
                        "Transfers", "warning")
            return
        self._send_file_to_peer(record.as_peer())

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
        record = self.service.peer_repository.get(
            selected if isinstance(selected, str) else None)
        if (record is None or not record.nearby
                or "posts_v1" not in record.hello.capabilities):
            self.append("Select one posts-capable peer before syncing.",
                        "Feed", "warning")
            return
        peer = record.as_peer()
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
        names = {record.hello.peer_id: record.hello.name
                 for record in self.service.peer_repository.snapshot()}
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
