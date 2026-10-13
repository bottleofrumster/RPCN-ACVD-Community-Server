"""Native Qt operator workspace. Construction and closing never control the server."""

from __future__ import annotations

import json
import os
from collections import Counter
from html import escape

from PySide6.QtCore import QDateTime, Qt, QTimeZone, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from acvd_server.cq_map_policy import (
    CQ_APPROVED_MAP_POOL,
    CQ_AREA_MAPS,
    CQ_AREA_NAMES,
    CQ_MAP_PROFILES,
)

from .language import description_text
from .privacy import hide_network_addresses
from .traffic import protobuf_fields
from .worker import Monitor

_UTC_DISPLAY_FORMAT = "yyyy-MM-dd HH:mm:ss 'UTC'"
# Canonical retail IDs, verified by the faction selection handler's native tests.
_FACTION_NAMES = {1: "Sirius", 2: "Venide", 3: "EGF", 0: "Unaffiliated"}
# World territory IDs are not the Mock selector's row numbers. The captured
# WORLDAREA_BATTLEMAP_PARAM_ST catalog links these IDs to each region's maps.
_AREA_NAMES = {0: "Unassigned", **CQ_AREA_NAMES}


def _team_area_name(team):
    area = team.get("territory_area_id")
    if type(area) is int:
        return _AREA_NAMES.get(area, f"Unknown ({area})")
    return "Unknown"

# Explain measurements without presenting configuration as proof of reachability.
_DIAGNOSTIC_HELP = {
    "Service": (
        "The systemd service that starts and supervises the game server. "
        "The panel uses this service for Start, Stop, and Restart. "
        "Its name alone does not mean the server is running."
    ),
    "Process": (
        "The operating-system process ID (PID) of the server. "
        "It normally changes after a restart. Zero or an unavailable value means "
        "no running process was reported by this source. A PID alone does not prove the game service is healthy."
    ),
    "Control connection": (
        "The local connection between this panel and the server. "
        "A Unix socket stays on this computer. The 0600 permission allows only its owner to read and write it. "
        "This is not the connection used by remote players."
    ),
    "Advertised host": (
        "The server address sent to game clients for later connections. "
        "Players must be able to reach this address. It is not necessarily the local bind address. "
        "This row does not test DNS, router forwarding, or firewall access."
    ),
    "Listeners": (
        "The server's configured ports: login is TCP login; auth is TCP authentication; "
        "udp is the encrypted game service; cdn is HTTP regulation download over TCP. "
        "These values do not prove that the ports are reachable from the Internet."
    ),
    "Service restarts": (
        "Automatic service restarts reported by systemd (NRestarts). "
        "This is not a lifetime count of all manual starts and restarts. "
        "If it increases without operator action, inspect the service log for failures."
    ),
    "Root": (
        "The server project folder selected in this panel. "
        "The panel uses it to locate the control socket, logs, and stored records. "
        "It is not the RPCS3 game folder or the player's savedata folder."
    ),
    "Process memory / threads": (
        "VmRSS is the server process's resident memory in kB. Threads is its operating-system thread count. "
        "disk_free_bytes is the free space on the drive that holds the server project, measured in bytes. "
        "These are different measurements. They are not CPU usage or RPCS3 memory usage."
    ),
    "Debug capture": (
        "Shows whether the server's live diagnostic capture is enabled. "
        "Debug Mode provides additional records for the Debug Feed. "
        "Normal log files can continue when this capture is disabled. "
        "Enabled does not mean that every client or network packet is being captured."
    ),
    "Scope": (
        "The limits of this panel's controls. It can control the server and its operator settings. "
        "It does not install client patches. A separate confirmed control can permanently delete "
        "one offline player's personal server-side save while retaining team membership. "
        "Stop and Restart can disconnect players; they still require care and confirmation."
    ),
}


def _team_faction_name(team):
    """Show current membership, not the requested next-season affiliation."""
    faction = team.get("current_faction_id", team.get("faction_id"))
    if type(faction) is int:
        return _FACTION_NAMES.get(faction, f"Unknown ({faction})")
    return "Unknown"


def _utc_text(seconds: int) -> str:
    """Use the same UTC representation in the schedule editor and readback."""
    return QDateTime.fromSecsSinceEpoch(seconds, QTimeZone.utc()).toString(_UTC_DISPLAY_FORMAT)


def label(text, kind=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if kind:
        widget.setObjectName(kind)
    return widget


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.verticalHeader().hide()
    widget.setAlternatingRowColors(True)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    widget.horizontalHeader().setStretchLastSection(True)
    return widget


def populate(widget, rows, *, row_help=None):
    widget.setRowCount(len(rows))
    for r, row in enumerate(rows):
        help_text = row_help.get(row[0]) if row_help else None
        tooltip = (
            f'<table width="360"><tr><td><b>{escape(str(row[0]))}</b><br><br>'
            f"{escape(help_text)}</td></tr></table>"
            if help_text
            else ""
        )
        for c, value in enumerate(row):
            item = QTableWidgetItem(str(value if value is not None else "—"))
            item.setToolTip(tooltip)
            widget.setItem(r, c, item)


class SortableTableItem(QTableWidgetItem):
    """Sort numeric columns numerically while retaining the displayed text."""

    def __init__(self, text, sort_value=None, record=None):
        super().__init__(str(text if text is not None else "—"))
        value = text if sort_value is None else sort_value
        if isinstance(value, str):
            value = value.casefold()
        self.setData(Qt.ItemDataRole.UserRole, value)
        if record is not None:
            self.setData(Qt.ItemDataRole.UserRole + 1, record)

    def __lt__(self, other):
        left = self.data(Qt.ItemDataRole.UserRole)
        right = other.data(Qt.ItemDataRole.UserRole)
        if type(left) is type(right):
            return left < right
        return str(left) < str(right)


class PlayerDetailsDialog(QDialog):
    """One selected player's server-owned summary and bounded operator actions."""

    def __init__(self, record, *, capable, on_delete, parent=None):
        super().__init__(parent)
        self.record = record
        self.setWindowTitle(f"Player details — {record.get('online_id', 'Unknown')}")
        self.resize(620, 690)
        layout = QVBoxLayout(self)
        layout.addWidget(label("PLAYER SERVER DATA", "eyebrow"))
        layout.addWidget(
            label(
                "This view contains server-side metadata only. Network addresses and raw save "
                "blobs are never displayed.",
                "muted",
            )
        )
        form = QFormLayout()

        def yes_no(value):
            return "Yes" if value else "No"

        status = "ONLINE" if record.get("online") else "OFFLINE"
        if record.get("online"):
            status += f" — heartbeat {record.get('last_seen_seconds', 0)}s ago"
        wallet = record.get("wallet_balance")
        rows = (
            ("Online ID", record.get("online_id", "—")),
            ("Player ID", record.get("player_id", "—")),
            ("Status", status),
            ("Team", record.get("team_name") or "No team"),
            ("Team ID", record.get("team_id") or "—"),
            ("Member ID", record.get("member_id") or "—"),
            ("Wallet", f"{wallet:,} Au" if type(wallet) is int else "Not stored"),
            ("AC mirror", yes_no(record.get("ac_data"))),
            ("Story-progress mirror", yes_no(record.get("story_data"))),
            ("UNAC mirrors", record.get("unac_count", 0)),
            ("Inventory mirror", yes_no(record.get("inventory_data"))),
            ("Emblem", yes_no(record.get("emblem_data"))),
            ("Pending emblem", yes_no(record.get("pending_emblem"))),
            ("Mercenary registration", yes_no(record.get("mercenary_registered"))),
            ("Personal notices", yes_no(record.get("notice_data"))),
            ("Server files", record.get("server_file_count", 0)),
            ("Stored bytes", f"{record.get('server_bytes', 0):,}"),
            ("Reward settlements", record.get("settlement_count", 0)),
            ("Purchase / inventory records", record.get("purchase_count", 0)),
            ("Money transfers", record.get("transfer_count", 0)),
            ("Data categories", ", ".join(record.get("data_categories", [])) or "None"),
        )
        for name, value in rows:
            value_label = label(str(value))
            value_label.setWordWrap(True)
            form.addRow(name, value_label)
        layout.addLayout(form)
        action_card = QFrame()
        action_card.setObjectName("card")
        action_layout = QVBoxLayout(action_card)
        action_layout.addWidget(label("SERVER ACTIONS", "eyebrow"))
        self.delete_button = QPushButton("DELETE / REBUILD SERVER SAVE DATA…")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(lambda: on_delete(record, self))
        allowed = bool(
            capable and record.get("has_republishable_data") and not record.get("online")
        )
        self.delete_button.setEnabled(allowed)
        action_layout.addWidget(self.delete_button)
        if record.get("online"):
            note = "This player is online. They must disconnect before server save data can be deleted."
        elif not record.get("has_republishable_data"):
            note = "No client-republishable AC, Story-progress, or UNAC data is stored."
        else:
            note = (
                "Permanent for the selected server mirrors: AC, Story progress, and UNAC data "
                "are deleted. The unchanged local save republishes them on the next login. "
                "Wallet, inventory, emblem, team membership, settings, and history are retained."
            )
        action_note = label(note, "muted")
        action_note.setWordWrap(True)
        action_layout.addWidget(action_note)
        layout.addWidget(action_card)
        close = QPushButton("CLOSE")
        close.clicked.connect(self.close)
        layout.addWidget(close)


class Header(QFrame):
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(QPen(QColor("#163744"), 1))
        for x in range(self.width() // 2, self.width() + 100, 45):
            painter.drawLine(x, 0, x - 95, self.height())
        painter.setPen(QPen(QColor("#117d99"), 1))
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)


class Console(QMainWindow):
    def __init__(self, root):
        super().__init__()
        self.root, self.current, self.unknown = root, {}, {}
        self.setWindowTitle("ACVD • Server Control Center")
        self.resize(1420, 920)
        self.setMinimumSize(1080, 720)
        self.monitor = Monitor(root)
        host = QWidget()
        outer = QVBoxLayout(host)
        outer.setContentsMargins(0, 0, 0, 0)
        self.setCentralWidget(host)
        header = Header()
        header.setFixedHeight(110)
        heading = QHBoxLayout(header)
        heading.setContentsMargins(28, 16, 28, 16)
        titles = QVBoxLayout()
        titles.addWidget(label("ARMORED CORE  /  VERDICT DAY", "eyebrow"))
        titles.addWidget(label("SERVER CONTROL", "title"))
        titles.addWidget(label("LOCAL OPERATOR CONSOLE   •   SERVER / CLIENT TELEMETRY", "muted"))
        heading.addLayout(titles)
        heading.addStretch()
        self.connection = label("DISCOVERING SERVICE", "eyebrow")
        heading.addWidget(self.connection)
        self.debug = QCheckBox("DEBUG MODE")
        self.debug.setToolTip("Read server packets and client logs. Do not start game clients.")
        heading.addWidget(self.debug)
        outer.addWidget(header)
        body = QHBoxLayout()
        body.setSpacing(0)
        self.navigation = QListWidget()
        self.navigation.setFixedWidth(220)
        self.navigation.addItems(
            [
                "01   OVERVIEW",
                "02   PLAYERS & TEAMS",
                "03   WORLD & RECORDS",
                "04   DEBUG FEED",
                "05   PROTOCOL CATALOG",
                "06   LOG TOOLS",
                "07   SETTINGS",
            ]
        )
        self.pages = QStackedWidget()
        body.addWidget(self.navigation)
        body.addWidget(self.pages, 1)
        outer.addLayout(body, 1)
        self.make_overview()
        self.make_players()
        self.make_world()
        self.make_debug()
        self.make_catalog()
        self.make_tools()
        self.make_settings()
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.navigation.setCurrentRow(0)
        self.debug.toggled.connect(self.toggle_debug)
        self.monitor.snapshot.connect(self.update_snapshot)
        self.monitor.events.connect(self.add_events)
        self.monitor.result.connect(self.operation_result)
        self.statusBar().showMessage(
            "This window connects to the existing server. The server stays on when you close this window."
        )
        self.monitor.start()

    def page(self, title, subtitle):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(26, 22, 26, 22)
        layout.setSpacing(16)
        layout.addWidget(label(title, "eyebrow"))
        sub = label(subtitle, "muted")
        sub.setWordWrap(True)
        layout.addWidget(sub)
        self.pages.addWidget(page)
        return layout

    def button(self, text, callback, kind=None):
        button = QPushButton(text)
        if kind:
            button.setObjectName(kind)
        button.clicked.connect(callback)
        return button

    def make_overview(self):
        box = self.page(
            "SYSTEM OVERVIEW",
            "Live data comes from the running server. The panel marks stored data and features that are not available.",
        )
        cards = QHBoxLayout()
        self.metrics = {}
        for key, title in (
            ("state", "SERVICE"),
            ("players", "LIVE PLAYERS"),
            ("sessions", "TEAM SESSIONS"),
            ("uptime", "UPTIME"),
        ):
            card = QFrame()
            card.setObjectName("card")
            layout = QVBoxLayout(card)
            layout.addWidget(label(title, "eyebrow"))
            metric = label("—", "metric")
            self.metrics[key] = metric
            layout.addWidget(metric)
            cards.addWidget(card)
        box.addLayout(cards)
        controls = QHBoxLayout()
        self.start = self.button("START SERVER", lambda: self.service_action("start"), "primary")
        self.stop = self.button("STOP SERVER", lambda: self.service_action("stop"), "danger")
        self.restart = self.button("RESTART", lambda: self.service_action("restart"))
        self.maintenance = self.button("START MAINTENANCE", self.maintenance_action)
        for button in (self.start, self.stop, self.restart, self.maintenance):
            controls.addWidget(button)
        box.addLayout(controls)
        self.mode_note = label("Connecting…", "notice")
        self.mode_note.setWordWrap(True)
        box.addWidget(self.mode_note)
        self.overview_table = table(["DIAGNOSTIC", "CURRENT VALUE"])
        box.addWidget(self.overview_table, 1)
        self.message = label("Ready. No server changes made.", "muted")
        self.message.setWordWrap(True)
        box.addWidget(self.message)

    def make_players(self):
        box = self.page(
            "PLAYERS / TEAM DIRECTORY",
            "Search, filter, and sort the complete server directory. Double-click a player to "
            "inspect stored data and available server actions. Player IP addresses stay hidden.",
        )
        directory_tabs = QTabWidget()
        player_page = QWidget()
        player_panel = QVBoxLayout(player_page)
        player_panel.setContentsMargins(10, 12, 10, 10)
        player_panel.setSpacing(12)
        team_page = QWidget()
        team_panel = QVBoxLayout(team_page)
        team_panel.setContentsMargins(10, 12, 10, 10)
        team_panel.setSpacing(12)
        directory_tabs.addTab(player_page, "PLAYER DIRECTORY & ACTIONS")
        directory_tabs.addTab(team_page, "TEAM DIRECTORY")
        box.addWidget(directory_tabs, 1)
        filters = QFrame()
        filters.setObjectName("card")
        filter_panel = QVBoxLayout(filters)
        filter_panel.addWidget(label("PLAYER FILTERS", "eyebrow"))
        search_row = QHBoxLayout()
        self.player_search = QLineEdit()
        self.player_search.setPlaceholderText("Search Online ID, Player ID, team, or data category…")
        self.player_status_filter = QComboBox()
        for text, value in (("All statuses", "all"), ("Online", "online"), ("Offline", "offline")):
            self.player_status_filter.addItem(text, value)
        self.player_save_filter = QComboBox()
        for text, value in (
            ("All server data", "all"),
            ("Rebuildable save", "rebuildable"),
            ("Stored data only", "stored"),
            ("No stored data", "none"),
        ):
            self.player_save_filter.addItem(text, value)
        self.player_data_filter = QComboBox()
        self.player_data_filter.addItem("Any data type", "all")
        for category in (
            "AC data",
            "Story progress",
            "UNAC data",
            "Inventory",
            "Emblem",
            "Wallet",
            "History",
            "Mercenary",
        ):
            self.player_data_filter.addItem(category, category)
        self.player_team_filter = QComboBox()
        self.player_team_filter.addItem("All teams", "all")
        self.player_team_filter.addItem("No team", "none")
        self.player_columns_button = QPushButton("COLUMNS")
        self.player_columns_menu = QMenu(self.player_columns_button)
        self.player_columns_button.setMenu(self.player_columns_menu)
        self.player_clear_filters = QPushButton("CLEAR FILTERS")
        for control in (
            self.player_search,
            self.player_status_filter,
            self.player_save_filter,
            self.player_data_filter,
            self.player_team_filter,
            self.player_columns_button,
            self.player_clear_filters,
        ):
            search_row.addWidget(control)
        filter_panel.addLayout(search_row)
        self.player_filter_count = label("No player records loaded.", "muted")
        filter_panel.addWidget(self.player_filter_count)
        player_panel.addWidget(filters)

        self.player_headers = (
            "ONLINE ID",
            "PLAYER ID",
            "STATUS",
            "TEAM",
            "TEAM ID",
            "SERVER SAVE",
            "CASH",
            "AC",
            "STORY",
            "UNACS",
            "INVENTORY",
            "EMBLEM",
            "LAST HEARTBEAT",
        )
        self.players = table(self.player_headers)
        self.players.setSortingEnabled(True)
        self.players.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.players.itemSelectionChanged.connect(self._update_player_selection)
        self.players.itemDoubleClicked.connect(lambda _item: self.open_selected_player())
        self.players.horizontalHeader().setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        for column, heading in enumerate(self.player_headers):
            action = self.player_columns_menu.addAction(heading)
            action.setCheckable(True)
            action.setChecked(True)
            action.toggled.connect(
                lambda checked, index=column: self.players.setColumnHidden(index, not checked)
            )
        player_panel.addWidget(self.players, 1)
        action_notice = label(
            "PLAYER ACTIONS — Select a row below, then open its data card. Permanent save "
            "deletion is inside that card and always requires a second confirmation.",
            "notice",
        )
        action_notice.setWordWrap(True)
        player_panel.addWidget(action_notice)
        selection_row = QHBoxLayout()
        self.player_details_button = self.button(
            "VIEW PLAYER DATA / SERVER ACTIONS…", self.open_selected_player, "primary"
        )
        self.player_details_button.setEnabled(False)
        self.player_selection_note = label(
            "Select a player row, then open details to view stored data and server actions.",
            "muted",
        )
        self.player_selection_note.setWordWrap(True)
        selection_row.addWidget(self.player_details_button)
        selection_row.addWidget(self.player_selection_note, 1)
        player_panel.addLayout(selection_row)
        self.player_search.textChanged.connect(self._apply_player_filters)
        for control in (
            self.player_status_filter,
            self.player_save_filter,
            self.player_data_filter,
            self.player_team_filter,
        ):
            control.currentIndexChanged.connect(self._apply_player_filters)
        self.player_clear_filters.clicked.connect(self._clear_player_filters)
        self._player_detail_dialog = None
        self.faction_totals = label("Team counts unavailable.", "notice")
        self.faction_totals.setWordWrap(True)
        self.faction_totals.setToolTip(
            "Counts all teams in the directory by current faction, including teams with no online players."
        )
        team_panel.addWidget(self.faction_totals)
        self.teams = table(
            [
                "TEAM",
                "NAME",
                "LEADER",
                "MEMBERS",
                "RANK",
                "SPECIAL GAUGE",
                "FACTION",
                "AREA",
                "LAYOUT RECORDS",
            ]
        )
        team_panel.addWidget(self.teams, 1)

    def make_world(self):
        box = self.page(
            "WORLD / BATTLE RECORDS",
            "Control the world phase without restarting the server. Saved battle results are not official rankings.",
        )
        self.world_note = label("Live world state unavailable.", "notice")
        self.world_note.setWordWrap(True)
        box.addWidget(self.world_note)
        world_tabs = QTabWidget()
        schedule_page = QWidget()
        schedule_panel = QVBoxLayout(schedule_page)
        schedule_panel.setContentsMargins(10, 14, 10, 10)
        schedule_panel.setSpacing(14)
        payouts_page = QWidget()
        payouts_panel = QVBoxLayout(payouts_page)
        payouts_panel.setContentsMargins(10, 14, 10, 10)
        payouts_panel.setSpacing(14)
        maps_page = QWidget()
        maps_panel = QVBoxLayout(maps_page)
        maps_panel.setContentsMargins(10, 14, 10, 10)
        maps_panel.setSpacing(14)
        records_page = QWidget()
        records_panel = QVBoxLayout(records_page)
        records_panel.setContentsMargins(10, 14, 10, 10)
        records_panel.setSpacing(14)
        world_tabs.addTab(schedule_page, "WAR / GRACE SCHEDULE")
        world_tabs.addTab(payouts_page, "SORTIE PAYOUTS")
        world_tabs.addTab(maps_page, "CQ MAP POOL")
        world_tabs.addTab(records_page, "BATTLE RECORDS")
        box.addWidget(world_tabs, 1)
        controls = QFrame()
        controls.setObjectName("card")
        panel = QVBoxLayout(controls)
        panel.addWidget(label("WAR / GRACE-PERIOD SCHEDULE", "eyebrow"))
        actions = QHBoxLayout()
        self.grace_begin = self.button(
            "END WAR / START GRACE NOW", lambda: self.set_world_mode("grace"), "primary"
        )
        self.grace_end = self.button(
            "START WAR / END GRACE NOW", lambda: self.set_world_mode("active")
        )
        actions.addWidget(self.grace_begin)
        actions.addWidget(self.grace_end)
        panel.addLayout(actions)
        schedule = QFormLayout()
        now = QDateTime.currentDateTimeUtc()
        self.grace_start_time = QDateTimeEdit(now.addSecs(3600))
        self.grace_end_time = QDateTimeEdit(now.addSecs(7200))
        for editor in (self.grace_start_time, self.grace_end_time):
            editor.setTimeZone(QTimeZone.utc())
            editor.setMinimumHeight(40)
            editor.setMinimumDateTime(QDateTime.fromSecsSinceEpoch(0, QTimeZone.utc()))
            editor.setDisplayFormat(_UTC_DISPLAY_FORMAT)
            editor.setCalendarPopup(True)
        schedule.addRow("War ends / grace starts (UTC)", self.grace_start_time)
        schedule.addRow("War starts / grace ends (UTC)", self.grace_end_time)
        panel.addLayout(schedule)
        self.grace_schedule = self.button("SAVE WAR / GRACE SCHEDULE", self.apply_world_schedule)
        self.grace_cancel = self.button(
            "CANCEL SCHEDULE / USE ACTIVE WAR", self.cancel_world_schedule
        )
        self.grace_reload = self.button("LOAD SAVED TIMES", self.reload_world_schedule)
        schedule_actions = QHBoxLayout()
        for control in (self.grace_schedule, self.grace_cancel, self.grace_reload):
            schedule_actions.addWidget(control)
        panel.addLayout(schedule_actions)
        self.world_schedule_saved = label("Saved schedule unavailable.", "muted")
        self.world_schedule_saved.setWordWrap(True)
        panel.addWidget(self.world_schedule_saved)
        self.grace_limits = label(
            "One UTC window: active war before it, grace inside it, active war after it. "
            "Manual controls override the saved window. Save it again to resume timed control. "
            "The server runs confirmed schedules with this panel closed and after a restart. "
            "These controls do not reset seasons, clear territories, or award war rewards. "
            "Menus update on their next refresh; active-mission transitions remain unqualified.",
            "muted",
        )
        self.grace_limits.setWordWrap(True)
        panel.addWidget(self.grace_limits)
        schedule_panel.addWidget(controls)
        schedule_panel.addStretch()
        self._world_settings_signature = None
        self._world_schedule_dirty = False
        self._pending_world_settings = None
        for editor in (self.grace_start_time, self.grace_end_time):
            editor.dateTimeChanged.connect(
                lambda _value: setattr(self, "_world_schedule_dirty", True)
            )
        self._world_controls = (
            self.grace_begin,
            self.grace_end,
            self.grace_schedule,
            self.grace_cancel,
            self.grace_reload,
            self.grace_start_time,
            self.grace_end_time,
        )
        for control in self._world_controls:
            control.setEnabled(False)

        map_controls = QFrame()
        map_controls.setObjectName("card")
        map_panel = QVBoxLayout(map_controls)
        map_panel.addWidget(label("ACTIVE-WAR CONQUEST MAP POLICY", "eyebrow"))
        map_form = QFormLayout()
        self.cq_map_mode = QComboBox()
        self.cq_map_mode.addItem("TEAM TERRITORY / DEFAULT", "territory")
        self.cq_map_mode.addItem("LOCKED MAP POOL", "locked_pool")
        self.cq_map_mode.setMinimumHeight(40)
        map_form.addRow("CQ map mode", self.cq_map_mode)
        map_panel.addLayout(map_form)
        map_panel.addWidget(label("MAPS AVAILABLE TO THE LOCKED POOL", "eyebrow"))
        map_scroll = QScrollArea()
        map_scroll.setWidgetResizable(True)
        map_scroll.setMinimumHeight(160)
        map_scroll_body = QWidget()
        map_list = QVBoxLayout(map_scroll_body)
        map_list.setContentsMargins(8, 8, 8, 8)
        map_list.setSpacing(4)
        self.cq_map_checks = {}
        for area_id, maps in CQ_AREA_MAPS.items():
            map_list.addWidget(label(CQ_AREA_NAMES[area_id].upper(), "eyebrow"))
            for mission_id, map_name in maps:
                approved = (
                    " — APPROVED LOWER-LOAD"
                    if mission_id in CQ_APPROVED_MAP_POOL
                    else ""
                )
                checkbox = QCheckBox(f"{map_name} (Mission {mission_id}){approved}")
                checkbox.setMinimumHeight(30)
                self.cq_map_checks[mission_id] = checkbox
                map_list.addWidget(checkbox)
        map_list.addStretch()
        map_scroll.setWidget(map_scroll_body)
        map_panel.addWidget(map_scroll, 1)
        map_actions = QHBoxLayout()
        self.cq_map_apply = self.button(
            "APPLY CQ MAP POLICY", self.apply_cq_map_settings, "primary"
        )
        self.cq_map_reload = self.button(
            "LOAD SAVED MAP POLICY", self.reload_cq_map_settings
        )
        map_actions.addWidget(self.cq_map_apply)
        map_actions.addWidget(self.cq_map_reload)
        map_panel.addLayout(map_actions)
        self.cq_map_saved = label("Saved CQ map policy unavailable.", "muted")
        self.cq_map_saved.setWordWrap(True)
        map_panel.addWidget(self.cq_map_saved)
        cq_map_note = label(
            "Locked mode rotates the checked exact maps for new active-war paired Extermination "
            "sorties. The game caches one map level per region, so selecting another map in a region "
            "replaces that region's previous selection. Change it only while all players are logged "
            "out. Other mission types and team territories keep their native rules.",
            "muted",
        )
        cq_map_note.setWordWrap(True)
        map_panel.addWidget(cq_map_note)
        maps_panel.addWidget(map_controls, 1)
        self._cq_map_settings_signature = None
        self._cq_map_dirty = False
        self._pending_cq_map_settings = None
        self._cq_map_controls = (
            self.cq_map_mode,
            *self.cq_map_checks.values(),
            self.cq_map_apply,
            self.cq_map_reload,
        )
        self.cq_map_mode.currentIndexChanged.connect(self._cq_map_changed)
        for mission_id, checkbox in self.cq_map_checks.items():
            checkbox.toggled.connect(
                lambda checked, selected=mission_id: self._cq_map_changed(
                    selected, checked
                )
            )
        for control in self._cq_map_controls:
            control.setEnabled(False)

        reward_controls = QFrame()
        reward_controls.setObjectName("card")
        reward_panel = QVBoxLayout(reward_controls)
        reward_panel.addWidget(label("WORLD SORTIE REPLACEMENT ECONOMY", "eyebrow"))
        reward_form = QFormLayout()

        def cash_control(value):
            control = QSpinBox()
            control.setRange(0, 10_000_000)
            control.setSingleStep(10_000)
            control.setValue(value)
            control.setSuffix(" Au")
            control.setGroupSeparatorShown(True)
            control.setMinimumHeight(40)
            return control

        self.world_normal_base_reward = cash_control(150_000)
        self.world_special_base_reward = cash_control(1_000_000)
        self.world_boss_base_reward = cash_control(1_000_000)
        self.world_small_subtarget_reward = cash_control(10_000)
        self.world_large_subtarget_reward = cash_control(10_000)
        reward_form.addRow("Normal Sortie winning-team pool", self.world_normal_base_reward)
        reward_form.addRow("Active-war Special winning-team pool", self.world_special_base_reward)
        reward_form.addRow("Grace Online Boss base pay", self.world_boss_base_reward)
        reward_form.addRow("Small subtarget collector cash", self.world_small_subtarget_reward)
        reward_form.addRow("Large subtarget collector cash", self.world_large_subtarget_reward)
        self.world_mercenary_rank = QComboBox()
        for rank, rate in zip("FEDCBAS", (10, 14, 16, 20, 24, 26, 30)):
            self.world_mercenary_rank.addItem(f"{rank} — {rate}% of employer pool")
        self.world_mercenary_rank.setMinimumHeight(40)
        reward_form.addRow("Fallback mercenary rank", self.world_mercenary_rank)
        reward_panel.addLayout(reward_form)
        reward_actions = QHBoxLayout()
        self.world_reward_apply = self.button(
            "APPLY SORTIE PAYOUTS", self.apply_world_reward_settings, "primary"
        )
        self.world_reward_reload = self.button(
            "LOAD SAVED PAYOUTS", self.reload_world_reward_settings
        )
        reward_actions.addWidget(self.world_reward_apply)
        reward_actions.addWidget(self.world_reward_reload)
        reward_panel.addLayout(reward_actions)
        self.world_reward_saved = label("Saved payout policy unavailable.", "muted")
        self.world_reward_saved.setWordWrap(True)
        reward_panel.addWidget(self.world_reward_saved)
        reward_note = label(
            "These are explicit replacement-server values because the original active-war "
            "Normal/Special base-pay table is not recovered. Grace Online Boss defaults to "
            "the separate exact 1,000,000 Au game-table value; changing it is an explicit "
            "replacement override. Team Pay and original F–S "
            "mercenary percentages are applied automatically. Subtarget cash belongs to its "
            "recorded collector. Zero "
            "disables that category. Changes affect newly settled results only; exact saved "
            "receipts are never recalculated. No client or RPCS3 files are changed.",
            "muted",
        )
        reward_note.setWordWrap(True)
        reward_panel.addWidget(reward_note)
        payouts_panel.addWidget(reward_controls)
        payouts_panel.addStretch()
        self._world_reward_setting_names = (
            "world_normal_base_reward",
            "world_special_base_reward",
            "world_boss_base_reward",
            "world_small_subtarget_reward",
            "world_large_subtarget_reward",
            "world_mercenary_rank",
        )
        self._world_reward_settings_signature = None
        self._world_reward_dirty = False
        self._pending_world_reward_settings = None
        self._world_reward_controls = (
            self.world_normal_base_reward,
            self.world_special_base_reward,
            self.world_boss_base_reward,
            self.world_small_subtarget_reward,
            self.world_large_subtarget_reward,
            self.world_mercenary_rank,
            self.world_reward_apply,
            self.world_reward_reload,
        )
        for control in self._world_reward_controls:
            control.setEnabled(False)
        for control in self._world_reward_controls[:5]:
            control.valueChanged.connect(
                lambda _value: setattr(self, "_world_reward_dirty", True)
            )
        self.world_mercenary_rank.currentIndexChanged.connect(
            lambda _value: setattr(self, "_world_reward_dirty", True)
        )
        self.rank_kind = QComboBox()
        self.rank_kind.addItem("Recorded battle settlements")
        for entry in self.monitor.catalog.entries.values():
            if entry["layer"] == "application_service" and "RANKING" in entry["name"]:
                self.rank_kind.addItem(entry["name"].replace("_", " "))
        records_panel.addWidget(self.rank_kind)
        self.rank_note = label(
            "The amounts come from saved battle results. Some rewards still require tests.",
            "muted",
        )
        self.rank_note.setWordWrap(True)
        records_panel.addWidget(self.rank_note)
        self.results = table(["PLAYER", "MISSION", "GROSS", "EXPENSES", "NET", "BALANCE AFTER"])
        records_panel.addWidget(self.results, 1)
        self.rank_kind.currentIndexChanged.connect(
            lambda index: self.rank_note.setText(
                "Official rankings are not available. The table shows saved battle results. It does not assign official ranks."
                if index
                else "The table shows saved battle results. It is not an official ranking."
            )
        )

    def make_debug(self):
        box = self.page(
            "LIVE HANDSHAKE / DEBUG FEED",
            "Select Debug Mode to read live records. The panel removes known secrets. It marks short or incomplete packet data.",
        )
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by code, player, event or description…")
        self.unknown_only = QCheckBox("Unknown codes only")
        self.freeze = QCheckBox("Pause display")
        row.addWidget(self.search, 1)
        row.addWidget(self.unknown_only)
        row.addWidget(self.freeze)
        row.addWidget(self.button("EXPORT VIEW", self.export_view))
        row.addWidget(self.button("CLEAR", self.clear_feed))
        box.addLayout(row)
        self.feed_note = label("DEBUG OFF — no client observers or raw capture started.", "notice")
        box.addWidget(self.feed_note)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.feed = table(
            ["TIME", "SOURCE", "DIRECTION", "LAYER / CODE", "HANDSHAKE / EVENT", "CONFIDENCE"]
        )
        self.feed.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        splitter.addWidget(self.feed)
        tabs = QTabWidget()
        self.description = QPlainTextEdit()
        self.raw = QPlainTextEdit()
        self.fields = QPlainTextEdit()
        self.source_description = QPlainTextEdit()
        for widget, title in (
            (self.description, "Meaning"),
            (self.raw, "Raw Record"),
            (self.fields, "Message Fields"),
            (self.source_description, "Source Text (privacy filtered)"),
        ):
            widget.setReadOnly(True)
            tabs.addTab(widget, title)
        splitter.addWidget(tabs)
        splitter.setSizes([390, 240])
        box.addWidget(splitter, 1)
        self.feed.itemSelectionChanged.connect(self.inspect_event)
        self.search.textChanged.connect(self.filter_feed)
        self.unknown_only.toggled.connect(self.filter_feed)
        self.freeze.toggled.connect(self.update_feed_note)

    def update_feed_note(self, paused):
        self.feed_note.setText(
            "DISPLAY PAUSED. The view skips new rows. The server log continues."
            if paused
            else "DEBUG ON — bounded capture / passive readers."
            if self.debug.isChecked()
            else "DEBUG OFF — no new live diagnostic rows requested."
        )

    def make_catalog(self):
        box = self.page(
            "PROTOCOL KNOWLEDGE",
            "Each code has a source and a test status. A known code does not prove that the feature works.",
        )
        self.catalog_search = QLineEdit()
        self.catalog_search.setPlaceholderText("Search known protocol codes and descriptions…")
        box.addWidget(self.catalog_search)
        self.catalog_table = table(["LAYER", "CODE", "NAME", "CONFIDENCE"])
        entries = sorted(
            self.monitor.catalog.entries.values(), key=lambda x: (x["layer"], x["code"])
        )
        populate(
            self.catalog_table,
            [[r["layer"], r["code"], r["name"], r.get("confidence", "unknown")] for r in entries],
        )
        for i, entry in enumerate(entries):
            self.catalog_table.item(i, 0).setData(Qt.ItemDataRole.UserRole, entry)
        self.catalog_detail = QPlainTextEdit()
        self.catalog_detail.setReadOnly(True)
        self.catalog_detail.setMaximumHeight(230)
        self.catalog_source = QPlainTextEdit()
        self.catalog_source.setReadOnly(True)
        catalog_tabs = QTabWidget()
        catalog_tabs.setMaximumHeight(250)
        catalog_tabs.addTab(self.catalog_detail, "Meaning")
        catalog_tabs.addTab(self.catalog_source, "Source Text (privacy filtered)")
        box.addWidget(self.catalog_table, 1)
        box.addWidget(catalog_tabs)
        self.catalog_table.itemSelectionChanged.connect(self.inspect_catalog)
        self.catalog_search.textChanged.connect(
            lambda text: self.filter_table(self.catalog_table, text)
        )

    def make_tools(self):
        box = self.page(
            "LOG TOOLS",
            "Debug mode reads logs and message codes. It does not change or stop game clients.",
        )
        row = QHBoxLayout()
        row.addWidget(self.button("ADD LOCAL LOG / JSONL", self.add_log))
        row.addWidget(self.button("INSPECT PCAP", self.import_pcap))
        row.addWidget(
            self.button(
                "OPEN LOGS",
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.root / "logs"))),
            )
        )
        row.addWidget(
            self.button(
                "OPEN README",
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.root / "README.txt"))),
            )
        )
        box.addLayout(row)
        self.tools_table = table(["CAPABILITY", "POLICY / COVERAGE"])
        populate(
            self.tools_table,
            [
                [
                    "Server plaintext / wire events",
                    "Packet data stays in memory. Limits: 64 KiB per packet and 8 MiB total. Known secrets are removed.",
                ],
                [
                    "RPCS3 / RPCN / signaling",
                    "Read both client logs. Do not start clients or change their settings.",
                ],
                [
                    "Protobuf inspection",
                    "Field numbers/wire types; semantic names only where reviewed",
                ],
                [
                    "NXR and internal event catalog",
                    "Use names from the code catalog. Mark other codes as unknown.",
                ],
                [
                    "Captured JSONL / tool output",
                    "Read an existing local log. Do not change the source file.",
                ],
                [
                    "PCAP inspector",
                    "Read saved PCAP files. Supported formats: IPv4/UDP and Ethernet/SLL/SLL2/raw IP. The panel does not combine packet fragments.",
                ],
                [
                    "Player save reset / recovery",
                    "Not exposed; no destructive data controls or invented ownership",
                ],
            ],
        )
        box.addWidget(self.tools_table, 1)
        self.sources = QPlainTextEdit()
        self.sources.setReadOnly(True)
        self.sources.setMaximumHeight(150)
        box.addWidget(self.sources)

    def make_settings(self):
        box = self.page(
            "SERVER / OPERATOR SETTINGS",
            "Only the local server owner can use these controls. The controls do not change player files or game files.",
        )
        form = QFormLayout()
        self.root_label = QLineEdit(str(self.root))
        self.root_label.setReadOnly(True)
        form.addRow("Server repository", self.root_label)
        self.logins = QCheckBox("Accept new login / authentication connections")
        form.addRow("Admission", self.logins)
        self.notice = QPlainTextEdit()
        self.notice.setMaximumHeight(140)
        self.notice.setPlaceholderText(
            "Operator welcome / maintenance notice (up to 512 characters)"
        )
        form.addRow("Operator notice", self.notice)
        self.welcome = QPlainTextEdit()
        self.welcome.setMaximumHeight(140)
        self.welcome.setPlaceholderText("In-game login notice (up to 2048 UTF-8 bytes)")
        self.welcome.setEnabled(False)
        form.addRow("Game login welcome", self.welcome)
        box.addLayout(form)
        note = label(
            "Operator notice is panel-only. Game login welcome appears once per login. Text wraps on the game notice card. Edits and empty text apply to the next login; they do not recall a notice already sent.",
            "notice",
        )
        note.setWordWrap(True)
        box.addWidget(note)
        self.apply_settings = self.button("APPLY LIVE SETTINGS", self.save_settings, "primary")
        box.addWidget(self.apply_settings)
        box.addStretch()

    def toggle_debug(self, enabled):
        self.monitor.submit("debug", enabled=enabled)
        self.feed_note.setText(
            "DEBUG ON. The panel reads packets and logs with size limits. Old server logs contain only the first part of each packet."
            if enabled
            else "DEBUG OFF — raw capture cleared; clients are not controlled."
        )
        if enabled:
            self.navigation.setCurrentRow(3)

    def service_action(self, action):
        confirmed = False
        if action in ("stop", "restart"):
            live = self.current.get("admin")
            players = str(len(live["players"])) if live else "unknown"
            answer = QMessageBox.warning(
                self,
                "Confirm server interruption",
                f"{action.capitalize()} the ACVD server?\n\nConnected players: {players}. This command stops their connections.\nStart maintenance before this command to prevent new logins. This command does not reset player files.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            confirmed = True
        live = self.current.get("admin")
        if action == "stop" and live:
            self.monitor.submit("shutdown", confirmed=confirmed)
        else:
            self.monitor.submit("service", action=action, confirmed=confirmed)

    def maintenance_action(self):
        live = self.current.get("admin")
        if live:
            self.monitor.submit(
                "configure", settings={"maintenance": not live["settings"]["maintenance"]}
            )

    def save_settings(self):
        if len(self.notice.toPlainText()) > 512:
            QMessageBox.warning(self, "Notice too long", "Use at most 512 characters.")
            return
        settings = {
            "login_enabled": self.logins.isChecked(),
            "operator_notice": self.notice.toPlainText(),
        }
        if (self.current.get("admin") or {}).get("capabilities", {}).get("login_welcome"):
            text = self.welcome.toPlainText()
            if len(text.encode("utf-8")) > 2048 or "\0" in text:
                QMessageBox.warning(
                    self, "Welcome too long", "Use at most 2048 UTF-8 bytes and no null characters."
                )
                return
            settings["welcome_message"] = text
        self.monitor.submit("configure", settings=settings)

    def _selected_player(self):
        items = self.players.selectedItems()
        if not items:
            return None
        record = items[0].data(Qt.ItemDataRole.UserRole + 1)
        return record if isinstance(record, dict) else None

    def _update_player_selection(self):
        record = self._selected_player()
        self.player_details_button.setEnabled(record is not None)
        if record is None:
            self.player_selection_note.setText(
                "Select a player row, then open details to view stored data and server actions."
            )
            return
        status = "online" if record.get("online") else "offline"
        categories = ", ".join(record.get("data_categories", [])) or "no stored data"
        self.player_selection_note.setText(
            f"{record.get('online_id')} is {status}. Stored categories: {categories}."
        )

    def _clear_player_filters(self):
        self.player_search.clear()
        for control in (
            self.player_status_filter,
            self.player_save_filter,
            self.player_data_filter,
            self.player_team_filter,
        ):
            control.setCurrentIndex(0)
        self._apply_player_filters()

    def _apply_player_filters(self, *_args):
        query = self.player_search.text().strip().casefold()
        status = self.player_status_filter.currentData()
        save = self.player_save_filter.currentData()
        category = self.player_data_filter.currentData()
        team = self.player_team_filter.currentData()
        shown = 0
        for index in range(self.players.rowCount()):
            item = self.players.item(index, 0)
            record = item.data(Qt.ItemDataRole.UserRole + 1) if item else None
            if not isinstance(record, dict):
                self.players.setRowHidden(index, True)
                continue
            haystack = " ".join(
                (
                    str(record.get("online_id", "")),
                    str(record.get("player_id", "")),
                    str(record.get("team_name", "")),
                    " ".join(record.get("data_categories", [])),
                )
            ).casefold()
            matches = not query or query in haystack
            matches &= status == "all" or bool(record.get("online")) == (status == "online")
            if save == "rebuildable":
                matches &= bool(record.get("has_republishable_data"))
            elif save == "stored":
                matches &= bool(record.get("has_saved_data"))
            elif save == "none":
                matches &= not record.get("has_saved_data")
            matches &= category == "all" or category in record.get("data_categories", [])
            if team == "none":
                matches &= not record.get("team_id")
            elif team != "all":
                matches &= record.get("team_id") == team
            self.players.setRowHidden(index, not matches)
            shown += int(matches)
        self.player_filter_count.setText(
            f"Showing {shown} of {self.players.rowCount()} players. Click any column heading to sort."
        )
        record = self._selected_player()
        if record is not None:
            selected_row = self.players.currentRow()
            if selected_row >= 0 and self.players.isRowHidden(selected_row):
                self.players.clearSelection()
        if self._selected_player() is None:
            for row in range(self.players.rowCount()):
                if not self.players.isRowHidden(row):
                    self.players.selectRow(row)
                    break
        self._update_player_selection()

    def _refresh_player_directory(self, live):
        selected = self._selected_player()
        selected_identity = (
            (selected.get("online_id"), selected.get("player_id")) if selected else None
        )
        live_rows = {
            (row.get("online_id"), row.get("player_id")): row for row in live.get("players", [])
        }
        directory = []
        for source in live.get("player_directory", []):
            record = dict(source)
            current = live_rows.get((record.get("online_id"), record.get("player_id")))
            record["online"] = current is not None
            record["last_seen_seconds"] = (
                current.get("last_seen_seconds") if current is not None else None
            )
            directory.append(record)

        old_team = self.player_team_filter.currentData()
        blocked = self.player_team_filter.blockSignals(True)
        self.player_team_filter.clear()
        self.player_team_filter.addItem("All teams", "all")
        self.player_team_filter.addItem("No team", "none")
        teams = sorted(
            {
                (record.get("team_id"), record.get("team_name") or f"Team {record.get('team_id')}")
                for record in directory
                if record.get("team_id")
            },
            key=lambda value: (str(value[1]).casefold(), value[0]),
        )
        for team_id, team_name in teams:
            self.player_team_filter.addItem(team_name, team_id)
        old_team_index = self.player_team_filter.findData(old_team)
        self.player_team_filter.setCurrentIndex(max(0, old_team_index))
        self.player_team_filter.blockSignals(blocked)

        sort_column = self.players.horizontalHeader().sortIndicatorSection()
        sort_order = self.players.horizontalHeader().sortIndicatorOrder()
        self.players.setSortingEnabled(False)
        self.players.setRowCount(len(directory))

        def cell(row, column, text, sort_value=None):
            self.players.setItem(
                row,
                column,
                SortableTableItem(text, sort_value, directory[row]),
            )

        for row, record in enumerate(directory):
            wallet = record.get("wallet_balance")
            if record.get("has_republishable_data"):
                save_text, save_order = "Rebuildable", 2
            elif record.get("has_saved_data"):
                save_text, save_order = "Retained only", 1
            else:
                save_text, save_order = "None", 0
            heartbeat = record.get("last_seen_seconds")
            cell(row, 0, record.get("online_id"), record.get("online_id", ""))
            cell(row, 1, record.get("player_id"), record.get("player_id", 0))
            cell(row, 2, "ONLINE" if record.get("online") else "OFFLINE", int(record.get("online")))
            cell(row, 3, record.get("team_name") or "No team")
            cell(row, 4, record.get("team_id") or "—", record.get("team_id", 0))
            cell(row, 5, save_text, save_order)
            cell(row, 6, f"{wallet:,}" if type(wallet) is int else "—", wallet or -1)
            cell(row, 7, "Yes" if record.get("ac_data") else "No", int(bool(record.get("ac_data"))))
            cell(row, 8, "Yes" if record.get("story_data") else "No", int(bool(record.get("story_data"))))
            cell(row, 9, record.get("unac_count", 0), record.get("unac_count", 0))
            cell(row, 10, "Yes" if record.get("inventory_data") else "No", int(bool(record.get("inventory_data"))))
            cell(row, 11, "Yes" if record.get("emblem_data") else "No", int(bool(record.get("emblem_data"))))
            cell(row, 12, f"{heartbeat}s" if heartbeat is not None else "—", heartbeat if heartbeat is not None else 10**9)
        self.players.setSortingEnabled(True)
        self.players.sortItems(sort_column, sort_order)
        if selected_identity:
            for row in range(self.players.rowCount()):
                record = self.players.item(row, 0).data(Qt.ItemDataRole.UserRole + 1)
                if (record.get("online_id"), record.get("player_id")) == selected_identity:
                    self.players.selectRow(row)
                    break
        self._apply_player_filters()

    def open_selected_player(self):
        row = self._selected_player()
        if row is None:
            return
        live = self.current.get("admin") or {}
        dialog = PlayerDetailsDialog(
            row,
            capable=bool(live.get("capabilities", {}).get("player_reset")),
            on_delete=self.reset_selected_player,
            parent=self,
        )
        self._player_detail_dialog = dialog
        dialog.finished.connect(lambda _result: setattr(self, "_player_detail_dialog", None))
        dialog.show()

    def reset_selected_player(self, row=None, dialog=None):
        row = row if isinstance(row, dict) else self._selected_player()
        if (
            not isinstance(row, dict)
            or not row.get("has_republishable_data")
            or row.get("online")
        ):
            return
        online_id, player_id = row["online_id"], row["player_id"]
        answer = QMessageBox.question(
            self,
            "Permanently delete player server save?",
            f"Are you sure you want to permanently delete {online_id}'s server save data "
            f"(player {player_id})?\n\n"
            "This permanently deletes the server's AC, Story-progress and UNAC mirrors. The "
            "next login rebuilds those from the player's unchanged local save. Wallet, owned "
            "inventory, emblem, team membership, profile settings and transaction/reward "
            "history are retained because retail does not resend them. The player must be "
            "offline and must keep the local save they want to rebuild from.\n\n"
            "The cleared mirrors cannot be recovered if that local save is missing.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if dialog is not None:
            dialog.delete_button.setEnabled(False)
            dialog.close()
        self.monitor.submit(
            "delete_player",
            online_id=online_id,
            player_id=player_id,
            confirmed=True,
            confirmation=online_id,
        )

    def _world_controls_available(self):
        live = self.current.get("admin") or {}
        return bool(live.get("capabilities", {}).get("world_mode_controls"))

    def _world_reward_controls_available(self):
        live = self.current.get("admin") or {}
        return bool(live.get("capabilities", {}).get("world_reward_controls"))

    def _cq_map_controls_available(self):
        live = self.current.get("admin") or {}
        return bool(live.get("capabilities", {}).get("cq_map_pool_controls"))

    def _cq_map_changed(self, mission_id=None, checked=False):
        if checked and mission_id in CQ_MAP_PROFILES:
            area_id = int(CQ_MAP_PROFILES[mission_id]["area_id"])
            for other_id, checkbox in self.cq_map_checks.items():
                if (
                    other_id != mission_id
                    and int(CQ_MAP_PROFILES[other_id]["area_id"]) == area_id
                    and checkbox.isChecked()
                ):
                    blocked = checkbox.blockSignals(True)
                    checkbox.setChecked(False)
                    checkbox.blockSignals(blocked)
        self._cq_map_dirty = True
        self._update_cq_map_control_state()

    def _update_cq_map_control_state(self):
        ready = self._cq_map_controls_available()
        locked = self.cq_map_mode.currentData() == "locked_pool"
        self.cq_map_mode.setEnabled(ready)
        for checkbox in self.cq_map_checks.values():
            checkbox.setEnabled(ready and locked)
        self.cq_map_apply.setEnabled(ready)
        self.cq_map_reload.setEnabled(ready)

    def apply_cq_map_settings(self):
        if (
            not self._cq_map_controls_available()
            or self._pending_cq_map_settings is not None
        ):
            return
        mode = self.cq_map_mode.currentData()
        pool = sorted(
            mission_id
            for mission_id, checkbox in self.cq_map_checks.items()
            if checkbox.isChecked()
        )
        if mode == "locked_pool" and not pool:
            QMessageBox.warning(
                self,
                "Empty CQ map pool",
                "Select at least one map before enabling Locked Map Pool mode.",
            )
            return
        live = self.current.get("admin") or {}
        players = len(live.get("players", []))
        matches = int(live.get("matches", 0))
        if players or matches:
            QMessageBox.warning(
                self,
                "CQ map policy is session-cached",
                "Log out every player and wait for active matches to clear before changing the "
                "CQ map policy. This prevents clients from using different map-level tables.",
            )
            return
        settings = {"cq_map_mode": mode, "cq_map_pool": pool}
        selected = (
            ", ".join(
                f"{CQ_MAP_PROFILES[mission_id]['region']} — "
                f"{CQ_MAP_PROFILES[mission_id]['map_name']}"
                for mission_id in pool
            )
            or "none saved"
        )
        answer = QMessageBox.question(
            self,
            "Change CQ map policy",
            "Apply this server-side map policy?\n\n"
            f"Mode: {self.cq_map_mode.currentText()}\n"
            f"Pool: {selected}\n"
            "Connected players: 0. New paired Extermination sorties will rotate through these "
            "exact maps. Team territories, client files, burn runs, Special sorties, grace bosses, "
            "and custom-layout bindings remain unchanged.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._cq_map_dirty = True
        self._pending_cq_map_settings = settings
        self.monitor.submit("configure", settings=settings)

    def reload_cq_map_settings(self):
        """Discard the local map-policy draft without changing the server."""
        if (
            not self._cq_map_controls_available()
            or self._pending_cq_map_settings is not None
        ):
            return
        self._cq_map_dirty = False
        self._cq_map_settings_signature = None
        self.update_snapshot(self.current)

    def apply_world_reward_settings(self):
        if (
            not self._world_reward_controls_available()
            or self._pending_world_reward_settings is not None
        ):
            return
        settings = {
            "world_normal_base_reward": self.world_normal_base_reward.value(),
            "world_special_base_reward": self.world_special_base_reward.value(),
            "world_boss_base_reward": self.world_boss_base_reward.value(),
            "world_small_subtarget_reward": self.world_small_subtarget_reward.value(),
            "world_large_subtarget_reward": self.world_large_subtarget_reward.value(),
            "world_mercenary_rank": self.world_mercenary_rank.currentIndex(),
        }
        players = len((self.current.get("admin") or {}).get("players", []))
        answer = QMessageBox.question(
            self,
            "Change World sortie payouts",
            "Apply these server-side payout values?\n\n"
            f"Connected players: {players}. Battles settled after confirmation use the new "
            "values. Existing exact receipts keep their original values. This does not restart "
            "the server or change clients.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._world_reward_dirty = True
        self._pending_world_reward_settings = settings
        self.monitor.submit("configure", settings=settings)

    def reload_world_reward_settings(self):
        """Discard the local payout draft without sending a server command."""
        if (
            not self._world_reward_controls_available()
            or self._pending_world_reward_settings is not None
        ):
            return
        self._world_reward_dirty = False
        self._world_reward_settings_signature = None
        self.update_snapshot(self.current)

    def _confirm_world_change(self, description):
        if not self._world_controls_available():
            self.statusBar().showMessage(
                "This server version does not support world-mode controls."
            )
            return False
        players = len((self.current.get("admin") or {}).get("players", []))
        return (
            QMessageBox.question(
                self,
                "Change world mode",
                f"{description}\n\nConnected players: {players}. "
                "Client menus update on their next World refresh. This does not restart the server or reset the season. "
                "Active-battle phase transitions still need tests. Apply this change?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )

    def set_world_mode(self, mode):
        description = (
            "Start the grace period now."
            if mode == "grace"
            else "End the grace period and use active-war responses."
        )
        if mode not in ("active", "grace") or not self._confirm_world_change(description):
            return
        self._pending_world_settings = {"world_mode": mode}
        self.monitor.submit("configure", settings=self._pending_world_settings)

    def cancel_world_schedule(self):
        if not self._confirm_world_change("Remove the saved times and use active war now."):
            return
        self._pending_world_settings = {
            "world_mode": "active",
            "grace_start_utc": None,
            "grace_end_utc": None,
        }
        self.monitor.submit("configure", settings=self._pending_world_settings)

    def reload_world_schedule(self):
        """Discard only the local draft; never send a server command."""
        if not self._world_controls_available() or self._pending_world_settings is not None:
            return
        self._world_schedule_dirty = False
        self._world_settings_signature = None
        self.update_snapshot(self.current)

    def apply_world_schedule(self):
        start = self.grace_start_time.dateTime().toSecsSinceEpoch()
        end = self.grace_end_time.dateTime().toSecsSinceEpoch()
        if start >= end:
            QMessageBox.warning(
                self, "Invalid schedule", "The end time must follow the start time."
            )
            return
        if end <= QDateTime.currentDateTimeUtc().toSecsSinceEpoch():
            QMessageBox.warning(
                self, "Expired schedule", "The war start time must be in the future."
            )
            return
        if not self._confirm_world_change(
            "End active war at the first UTC time. Start grace then. "
            "End grace and start active war at the second UTC time. "
            "If the first time has passed, grace starts now."
        ):
            return
        self._world_schedule_dirty = True
        self._pending_world_settings = {
            "world_mode": "scheduled",
            "grace_start_utc": start,
            "grace_end_utc": end,
        }
        self.monitor.submit("configure", settings=self._pending_world_settings)

    def operation_result(self, message, ok):
        self.message.setText(message)
        self.statusBar().showMessage(message)
        if not ok:
            # Keep the draft after failure. Only matching server readback confirms it.
            self._pending_world_settings = None
            self._pending_world_reward_settings = None
            self._pending_cq_map_settings = None
            self.message.setStyleSheet("color:#f99b78")
        else:
            self.message.setStyleSheet("color:#a7ccd7")

    def update_snapshot(self, state):
        self.current = state
        service = state.get("service", {})
        live = state.get("admin")
        status = service.get("ActiveState", "unknown")
        if live:
            status = "active"
        self.connection.setText(
            "SERVER CONNECTION ACTIVE"
            if live
            else "SERVER CONNECTED / OLD VERSION"
            if status == "active"
            else "SERVICE " + status.upper()
        )
        self.metrics["state"].setText(
            "MAINTENANCE" if live and live["settings"]["maintenance"] else status.upper()
        )
        self.metrics["players"].setText(str(len(live["players"])) if live else "UNKNOWN")
        self.metrics["sessions"].setText(str(live["sessions"]) if live else "—")
        self.metrics["uptime"].setText(
            f"{live['uptime_seconds'] // 3600:02}:{live['uptime_seconds'] // 60 % 60:02}:{live['uptime_seconds'] % 60:02}"
            if live
            else "—"
        )
        matching = service.get("WorkingDirectory") == str(self.root)
        self.start.setEnabled(matching and service.get("LoadState") == "loaded" and not live)
        self.stop.setEnabled(bool(live) or matching and status == "active")
        self.restart.setEnabled(
            matching and (not live or str(live["pid"]) == service.get("MainPID"))
        )
        self.maintenance.setEnabled(bool(live))
        self.apply_settings.setEnabled(bool(live))
        self.welcome.setEnabled(bool(live and live.get("capabilities", {}).get("login_welcome")))
        if live:
            settings = live["settings"]
            self.maintenance.setText(
                "STOP MAINTENANCE" if settings["maintenance"] else "START MAINTENANCE"
            )
            self.mode_note.setText(
                (
                    "MAINTENANCE MODE. New players cannot log in. Existing players stay connected."
                    if settings["maintenance"]
                    else "The server is on. New players "
                    + ("can log in." if settings["login_enabled"] else "cannot log in.")
                )
                + ("\n" + settings["operator_notice"] if settings["operator_notice"] else "")
            )
            if not self.logins.hasFocus():
                self.logins.setChecked(settings["login_enabled"])
            if not self.notice.hasFocus() and not self.notice.document().isModified():
                self.notice.setPlainText(settings["operator_notice"])
                self.notice.document().setModified(False)
            if not self.welcome.hasFocus() and not self.welcome.document().isModified():
                self.welcome.setPlainText(settings.get("welcome_message", ""))
                self.welcome.document().setModified(False)
            self._refresh_player_directory(live)
        else:
            self.mode_note.setText(
                "The server runs an old version. Start and Stop use systemd. Other controls require the new server version. The panel will not restart it automatically."
            )
            self._refresh_player_directory({"players": [], "player_directory": []})
        teams = live["teams"] if live else state.get("stored_teams", [])
        factions = [_team_faction_name(team) for team in teams]
        counts = Counter(factions)
        unknown = sum(count for name, count in counts.items() if name not in _FACTION_NAMES.values())
        self.faction_totals.setText(
            f"Teams by current faction — Total: {len(teams)}\n"
            + "  |  ".join(f"{name}: {counts[name]}" for name in _FACTION_NAMES.values())
            + f"  |  Unknown: {unknown}"
        )
        populate(
            self.teams,
            [
                [
                    r.get("team_id"),
                    r.get("name", r.get("team_name", "—")),
                    r.get("leader_online_id"),
                    len(r.get("member_records") or []),
                    r.get("team_rank", "—"),
                    (
                        f"{r.get('special_sortie_progress', 0)}/"
                        f"{r.get('special_sortie_segments', 8)}"
                    ),
                    faction,
                    _team_area_name(r),
                    len(r.get("territory_layouts") or {})
                    + len(r.get("native_territory_layouts") or {}),
                ]
                for r, faction in zip(teams, factions)
            ],
        )
        settlements = live["settlements"] if live else state.get("stored_settlements", [])
        populate(
            self.results,
            [
                [
                    r.get(k)
                    for k in (
                        "online_id",
                        "mission_id",
                        "gross_earnings",
                        "expenses",
                        "delta",
                        "balance",
                    )
                ]
                for r in reversed(settlements)
            ],
        )
        world = live.get("world", {}) if live else {}
        ready = self._world_controls_available()
        for control in self._world_controls:
            control.setEnabled(ready)
        if ready:
            settings = live["settings"]
            pending = self._pending_world_settings
            if pending is not None and all(settings.get(k) == v for k, v in pending.items()):
                if pending.get("world_mode") == "scheduled":
                    draft_matches = (
                        self.grace_start_time.dateTime().toSecsSinceEpoch()
                        == pending["grace_start_utc"]
                        and self.grace_end_time.dateTime().toSecsSinceEpoch()
                        == pending["grace_end_utc"]
                    )
                    self._world_schedule_dirty = not draft_matches
                self._pending_world_settings = None
            signature = tuple(
                settings.get(key) for key in ("world_mode", "grace_start_utc", "grace_end_utc")
            )
            if signature != self._world_settings_signature and not self._world_schedule_dirty:
                for editor, key in (
                    (self.grace_start_time, "grace_start_utc"),
                    (self.grace_end_time, "grace_end_utc"),
                ):
                    if settings.get(key) is not None:
                        blocked = editor.blockSignals(True)
                        editor.setDateTime(
                            QDateTime.fromSecsSinceEpoch(settings[key], QTimeZone.utc())
                        )
                        editor.blockSignals(blocked)
                self._world_settings_signature = signature
            next_time = world.get("next_change_utc")
            next_text = _utc_text(next_time) if next_time is not None else "none"
            self.world_note.setText(
                f"SEASON {world.get('season')} / {world.get('label', 'Unknown mode')}. "
                f"Control: {world.get('mode')}. Schedule: {world.get('schedule_state', '').replace('_', ' ')}. "
                f"Next change: {next_text}."
            )

            def saved_time(key):
                value = settings.get(key)
                return _utc_text(value) if value is not None else "not set"

            confirmation = (
                "Waiting for server confirmation. " if self._pending_world_settings else ""
            )
            draft = "Unsaved draft. " if self._world_schedule_dirty else ""
            self.world_schedule_saved.setText(
                f"{confirmation}{draft}Server control: {settings.get('world_mode')}.\n"
                f"Saved war end / grace start: {saved_time('grace_start_utc')}.\n"
                f"Saved war start / grace end: {saved_time('grace_end_utc')}."
            )
        else:
            self.world_schedule_saved.setText(
                "No live schedule readback. A compatible server is required."
            )
            self.world_note.setText(
                f"SEASON {world.get('season')} / PHASE {world.get('phase')}. This server version has no world-mode controls."
                if live
                else "STORED RECORDS ONLY. Live world controls are unavailable."
            )

        cq_map_ready = self._cq_map_controls_available()
        if cq_map_ready:
            settings = live["settings"]
            pending = self._pending_cq_map_settings
            if pending is not None and all(
                settings.get(key) == value for key, value in pending.items()
            ):
                draft = {
                    "cq_map_mode": self.cq_map_mode.currentData(),
                    "cq_map_pool": sorted(
                        mission_id
                        for mission_id, checkbox in self.cq_map_checks.items()
                        if checkbox.isChecked()
                    ),
                }
                self._cq_map_dirty = draft != pending
                self._pending_cq_map_settings = None
            signature = (
                settings.get("cq_map_mode", "territory"),
                tuple(settings.get("cq_map_pool", [])),
            )
            if signature != self._cq_map_settings_signature and not self._cq_map_dirty:
                blocked = self.cq_map_mode.blockSignals(True)
                index = self.cq_map_mode.findData(signature[0])
                self.cq_map_mode.setCurrentIndex(max(0, index))
                self.cq_map_mode.blockSignals(blocked)
                selected = set(signature[1])
                for mission_id, checkbox in self.cq_map_checks.items():
                    blocked = checkbox.blockSignals(True)
                    checkbox.setChecked(mission_id in selected)
                    checkbox.blockSignals(blocked)
                self._cq_map_settings_signature = signature
            confirmation = (
                "Waiting for server confirmation. "
                if self._pending_cq_map_settings
                else ""
            )
            draft = "Unsaved draft. " if self._cq_map_dirty else ""
            pool_names = [
                f"{CQ_MAP_PROFILES[mission_id]['region']} — "
                f"{CQ_MAP_PROFILES[mission_id]['map_name']}"
                for mission_id in settings.get("cq_map_pool", [])
                if mission_id in CQ_MAP_PROFILES
            ]
            mode_text = (
                "Locked map pool"
                if settings.get("cq_map_mode") == "locked_pool"
                else "Team territory / default"
            )
            self.cq_map_saved.setText(
                f"{confirmation}{draft}Saved mode: {mode_text}.\n"
                f"Saved pool: {', '.join(pool_names) if pool_names else 'none'}."
            )
        else:
            self.cq_map_saved.setText(
                "No live CQ map-policy readback. A compatible server is required."
            )
        self._update_cq_map_control_state()

        reward_ready = self._world_reward_controls_available()
        for control in self._world_reward_controls:
            control.setEnabled(reward_ready)
        if reward_ready:
            settings = live["settings"]
            pending = self._pending_world_reward_settings
            if pending is not None and all(
                settings.get(key) == value for key, value in pending.items()
            ):
                self._pending_world_reward_settings = None
                self._world_reward_dirty = False
            signature = tuple(settings.get(key) for key in self._world_reward_setting_names)
            if signature != self._world_reward_settings_signature and not self._world_reward_dirty:
                for control, key in (
                    (self.world_normal_base_reward, "world_normal_base_reward"),
                    (self.world_special_base_reward, "world_special_base_reward"),
                    (self.world_boss_base_reward, "world_boss_base_reward"),
                    (self.world_small_subtarget_reward, "world_small_subtarget_reward"),
                    (self.world_large_subtarget_reward, "world_large_subtarget_reward"),
                ):
                    blocked = control.blockSignals(True)
                    control.setValue(int(settings[key]))
                    control.blockSignals(blocked)
                blocked = self.world_mercenary_rank.blockSignals(True)
                self.world_mercenary_rank.setCurrentIndex(int(settings["world_mercenary_rank"]))
                self.world_mercenary_rank.blockSignals(blocked)
                self._world_reward_settings_signature = signature
            confirmation = (
                "Waiting for server confirmation. "
                if self._pending_world_reward_settings
                else ""
            )
            draft = "Unsaved draft. " if self._world_reward_dirty else ""
            self.world_reward_saved.setText(
                f"{confirmation}{draft}Saved replacement economy — "
                f"Normal {settings['world_normal_base_reward']:,} Au; "
                f"active-war Special {settings['world_special_base_reward']:,} Au; "
                f"grace boss {settings['world_boss_base_reward']:,} Au; "
                f"small/large subtargets {settings['world_small_subtarget_reward']:,}/"
                f"{settings['world_large_subtarget_reward']:,} Au; "
                f"fallback mercenary rank {'FEDCBAS'[settings['world_mercenary_rank']]}."
            )
        else:
            self.world_reward_saved.setText(
                "No live payout readback. A compatible server is required."
            )
        populate(
            self.overview_table,
            [
                ["Service", "acvd-server.service"],
                ["Process", live["pid"] if live else service.get("MainPID")],
                [
                    "Control connection",
                    "Local Unix socket / 0600" if live else "Not available / old server version",
                ],
                [
                    "Advertised host",
                    live.get("advertised_host") if live else "Read from service configuration",
                ],
                [
                    "Listeners",
                    json.dumps(live["ports"]) if live else "Unavailable through legacy status",
                ],
                ["Service restarts", service.get("NRestarts")],
                ["Root", str(self.root)],
                [
                    "Process memory / threads",
                    json.dumps(live.get("resources", {}))
                    if live
                    else "Admin-enabled release required",
                ],
                [
                    "Debug capture",
                    "Enabled" if live and live["debug_enabled"] else "Disabled / log-only",
                ],
                ["Scope", "Server controls only • no client patches • no player-data reset"],
            ],
            row_help=_DIAGNOSTIC_HELP,
        )
        self.sources.setPlainText(
            "LIVE SOURCE PATHS\n"
            + "\n".join(f"{key}: {path}" for key, path in state.get("sources", {}).items())
        )

    def add_events(self, events):
        if self.freeze.isChecked():
            self.feed_note.setText(
                "DISPLAY PAUSED. The view skips new rows. The server log continues."
            )
            return
        self.feed.setUpdatesEnabled(False)
        for event in events:
            d = event["description"]
            code = d.get("code")
            self.feed.insertRow(0)
            values = [
                event["record"].get("timestamp_utc", "Read at " + event["received_at"]),
                event["source"],
                event["direction"],
                d["layer"] + (f" / {int(code):#06x}" if isinstance(code, int) else ""),
                d["name"],
                d["confidence"],
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if d["confidence"] == "unknown":
                    item.setForeground(QColor("#ff9d46"))
                self.feed.setItem(0, column, item)
            self.feed.item(0, 0).setData(Qt.ItemDataRole.UserRole, event)
            if self.feed.rowCount() > 1200:
                self.feed.removeRow(self.feed.rowCount() - 1)
        self.feed.setUpdatesEnabled(True)
        self.filter_feed()

    def filter_table(self, widget, text):
        for row in range(widget.rowCount()):
            widget.setRowHidden(
                row,
                text.casefold()
                not in " ".join(
                    widget.item(row, c).text() for c in range(widget.columnCount())
                ).casefold(),
            )

    def filter_feed(self):
        self.filter_table(self.feed, self.search.text())
        if self.unknown_only.isChecked():
            for row in range(self.feed.rowCount()):
                if self.feed.item(row, 5).text() != "unknown":
                    self.feed.setRowHidden(row, True)

    def inspect_event(self):
        row = self.feed.currentRow()
        if row < 0 or not self.feed.item(row, 0):
            return
        event = self.feed.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if not isinstance(event, dict):
            return
        self.description.setPlainText(description_text(event["description"]))
        self.source_description.setPlainText(
            json.dumps(event["description"], indent=2, ensure_ascii=False)
        )
        self.raw.setPlainText(json.dumps(event["record"], indent=2, ensure_ascii=False))
        payload = event["record"].get("payload")
        if event["description"]["layer"] == "application_service" and isinstance(payload, dict):
            try:
                raw_hex = payload.get("hex", payload.get("hex_prefix", ""))
                if isinstance(raw_hex, str) and raw_hex.startswith("[payload hidden:"):
                    self.fields.setPlainText("Packet bytes are hidden to protect IP addresses.")
                    return
                data = bytes.fromhex(raw_hex)
                self.fields.setPlainText(json.dumps(hide_network_addresses(protobuf_fields(data)), indent=2))
            except (ValueError, TypeError) as error:
                self.fields.setPlainText(str(error))
        else:
            self.fields.setPlainText(
                "This record has no decoded application message. The panel does not read encrypted packets as protobuf fields."
            )

    def inspect_catalog(self):
        row = self.catalog_table.currentRow()
        if row >= 0:
            entry = self.catalog_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            entry = hide_network_addresses(entry)
            self.catalog_detail.setPlainText(description_text(entry))
            self.catalog_source.setPlainText(json.dumps(entry, indent=2, ensure_ascii=False))

    def clear_feed(self):
        self.feed.setRowCount(0)
        self.description.clear()
        self.raw.clear()
        self.fields.clear()
        self.source_description.clear()

    def export_view(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export redacted visible events", "acvd-diagnostics.json", "JSON (*.json)"
        )
        if not path:
            return
        rows = [
            self.feed.item(r, 0).data(Qt.ItemDataRole.UserRole)
            for r in range(self.feed.rowCount())
            if not self.feed.isRowHidden(r)
        ]
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(
                    {
                        "note": "Known secrets and literal IP addresses are hidden. Player IDs and unknown payload encodings can remain. Keep this file private.",
                        "events": rows,
                    },
                    stream,
                    indent=2,
                )
            self.operation_result(f"Exported {len(rows)} visible records to {path}", True)
        except OSError as error:
            self.operation_result(str(error), False)

    def add_log(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Follow local diagnostic log",
            str(self.root),
            "Logs (*.log *.jsonl *.txt);;All files (*)",
        )
        if path:
            self.monitor.submit("log", path=path)

    def import_pcap(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Inspect classic PCAP", str(self.root / "logs"), "PCAP (*.pcap *.cap)"
        )
        if path:
            QMessageBox.information(
                self,
                "Private packet data",
                "Packet files can contain private data. The panel removes known game tokens. Other protocols can contain secrets. Check the file before you share it.",
            )
            self.navigation.setCurrentRow(3)
            self.monitor.submit("pcap", path=path)

    def closeEvent(self, event):
        self.monitor.requestInterruption()
        if not self.monitor.wait(7000):
            QMessageBox.information(
                self,
                "Finishing a bounded operation",
                "A server command is still active. Wait for the command to stop. Then close the window. The server will stay on.",
            )
            event.ignore()
            return
        event.accept()
