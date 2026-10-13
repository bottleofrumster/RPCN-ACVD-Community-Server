"""Native world controls with a fake backend; no service or client is controlled."""

import os
from html import unescape

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from acvd_console import window
from acvd_console.traffic import Catalog
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton


class FakeMonitor(QObject):
    snapshot = Signal(dict)
    events = Signal(list)
    result = Signal(str, bool)

    def __init__(self, root):
        super().__init__()
        self.catalog = Catalog(root)
        self.calls = []

    def start(self):
        pass

    def submit(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    def requestInterruption(self):
        pass

    def wait(self, timeout):
        return True


def snapshot(capable=True):
    settings = {
        "maintenance": False,
        "login_enabled": True,
        "operator_notice": "",
        "world_mode": "active",
        "grace_start_utc": None,
        "grace_end_utc": None,
        "world_normal_base_reward": 150_000,
        "world_special_base_reward": 1_000_000,
        "world_boss_base_reward": 1_000_000,
        "world_small_subtarget_reward": 10_000,
        "world_large_subtarget_reward": 10_000,
        "world_mercenary_rank": 0,
        "cq_map_mode": "territory",
        "cq_map_pool": [],
    }
    return {
        "service": {},
        "admin": {
            "pid": 123,
            "settings": settings,
            "players": [],
            "player_directory": [
                {
                    "online_id": "Tester",
                    "player_id": 42,
                    "team_id": 1,
                    "team_name": "Test Team",
                    "has_saved_data": True,
                    "has_republishable_data": True,
                    "wallet_balance": 123_456,
                    "member_id": 2,
                    "member_status": 1,
                    "member_team_secondary": 0,
                    "ac_data": True,
                    "story_data": True,
                    "unac_count": 2,
                    "inventory_data": True,
                    "notice_data": False,
                    "emblem_data": True,
                    "pending_emblem": False,
                    "mercenary_registered": False,
                    "server_file_count": 5,
                    "server_bytes": 4096,
                    "settlement_count": 3,
                    "purchase_count": 4,
                    "transfer_count": 1,
                    "data_categories": [
                        "Rebuildable save",
                        "AC data",
                        "Story progress",
                        "UNAC data",
                        "Inventory",
                        "Emblem",
                        "Wallet",
                        "History",
                    ],
                },
                {
                    "online_id": "Empty",
                    "player_id": 7,
                    "team_id": 0,
                    "team_name": "",
                    "has_saved_data": False,
                    "has_republishable_data": False,
                    "wallet_balance": None,
                    "data_categories": [],
                },
            ],
            "teams": [],
            "settlements": [],
            "sessions": 0,
            "matches": 0,
            "uptime_seconds": 1,
            "debug_enabled": False,
            "ports": {},
            "capabilities": {
                "world_mode_controls": capable,
                "world_reward_controls": capable,
                "cq_map_pool_controls": capable,
                "player_reset": capable,
            },
            "world": {
                "season": 3,
                "phase": 2,
                "label": "Active war",
                "mode": "active",
                "schedule_state": "not_scheduled",
                "next_change_utc": None,
            },
        },
    }


@pytest.fixture
def console(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(window, "Monitor", FakeMonitor)
    widget = window.Console(tmp_path)
    widget.update_snapshot(snapshot())
    yield widget
    widget.close()
    app.processEvents()


def test_log_tools_keep_inspection_without_research_controls(console):
    buttons = {button.text() for button in console.findChildren(QPushButton)}
    assert {"ADD LOCAL LOG / JSONL", "INSPECT PCAP", "OPEN LOGS", "OPEN README"} <= buttons
    assert not {"READ MEMORY MAPPING", "OBSERVE NATIVE JOBS · 20s", "OPEN EVIDENCE"} & buttons
    assert not hasattr(console, "tool")
    assert not console.monitor.calls


def test_manual_control_requires_confirmation_and_only_submits_settings(console, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    console.set_world_mode("grace")
    assert not console.monitor.calls
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    console.set_world_mode("grace")
    assert console.monitor.calls == [(("configure",), {"settings": {"world_mode": "grace"}})]


def select_player(console, online_id):
    for row in range(console.players.rowCount()):
        if console.players.item(row, 0).text() == online_id:
            console.players.selectRow(row)
            return
    raise AssertionError(f"Missing player {online_id}")


def visible_players(console):
    return {
        console.players.item(row, 0).text()
        for row in range(console.players.rowCount())
        if not console.players.isRowHidden(row)
    }


def test_player_actions_are_above_fold_and_default_selection_is_actionable(console):
    assert console._selected_player() is not None
    assert console.player_details_button.isEnabled()
    assert "SERVER ACTIONS" in console.player_details_button.text()
    tab_sets = [
        [widget.tabText(index) for index in range(widget.count())]
        for widget in console.findChildren(window.QTabWidget)
    ]
    assert ["PLAYER DIRECTORY & ACTIONS", "TEAM DIRECTORY"] in tab_sets


def test_world_controls_are_split_into_readable_task_tabs(console):
    tab_sets = [
        [widget.tabText(index) for index in range(widget.count())]
        for widget in console.findChildren(window.QTabWidget)
    ]
    assert [
        "WAR / GRACE SCHEDULE",
        "SORTIE PAYOUTS",
        "CQ MAP POOL",
        "BATTLE RECORDS",
    ] in tab_sets


def test_player_directory_sorts_searches_filters_and_hides_columns(console):
    assert console.players.rowCount() == 2
    console.players.sortItems(1, window.Qt.SortOrder.AscendingOrder)
    assert console.players.item(0, 1).text() == "7"
    console.player_search.setText("tester")
    assert visible_players(console) == {"Tester"}
    console.player_search.clear()
    console.player_save_filter.setCurrentIndex(
        console.player_save_filter.findData("rebuildable")
    )
    assert visible_players(console) == {"Tester"}
    console.player_save_filter.setCurrentIndex(0)
    console.player_data_filter.setCurrentIndex(
        console.player_data_filter.findData("UNAC data")
    )
    assert visible_players(console) == {"Tester"}
    console.player_data_filter.setCurrentIndex(0)
    console.player_team_filter.setCurrentIndex(console.player_team_filter.findData("none"))
    assert visible_players(console) == {"Empty"}
    console.player_columns_menu.actions()[6].setChecked(False)
    assert console.players.isColumnHidden(6)
    console.player_columns_menu.actions()[6].setChecked(True)
    assert not console.players.isColumnHidden(6)


def test_player_details_require_confirmation_before_permanent_delete(console, monkeypatch):
    select_player(console, "Tester")
    console.open_selected_player()
    dialog = console._player_detail_dialog
    assert dialog is not None and dialog.isVisible()
    assert "Tester" in dialog.windowTitle()
    assert dialog.delete_button.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    console.reset_selected_player(dialog.record, dialog)
    assert not console.monitor.calls
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    console.reset_selected_player(dialog.record, dialog)
    assert console.monitor.calls == [
        (
            ("delete_player",),
            {
                "online_id": "Tester",
                "player_id": 42,
                "confirmed": True,
                "confirmation": "Tester",
            },
        )
    ]


def test_online_player_cannot_be_reset_from_the_ui(console):
    live = snapshot()
    live["admin"]["players"] = [
        {"online_id": "Tester", "player_id": 42, "last_seen_seconds": 1}
    ]
    console.update_snapshot(live)
    select_player(console, "Tester")
    console.open_selected_player()
    dialog = console._player_detail_dialog
    assert dialog is not None and not dialog.delete_button.isEnabled()
    assert any("online" in item.text().lower() for item in dialog.findChildren(QLabel))


def test_schedule_uses_utc_seconds_and_does_not_run_a_desktop_timer(console, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    start = console.grace_start_time.dateTime().toSecsSinceEpoch()
    end = console.grace_end_time.dateTime().toSecsSinceEpoch()
    console.apply_world_schedule()
    assert console.monitor.calls == [
        (
            ("configure",),
            {
                "settings": {
                    "world_mode": "scheduled",
                    "grace_start_utc": start,
                    "grace_end_utc": end,
                }
            },
        )
    ]
    assert console.grace_start_time.timeZone().id() == b"UTC"


def test_bad_schedule_does_not_submit(console, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
    console.grace_end_time.setDateTime(console.grace_start_time.dateTime())
    console.apply_world_schedule()
    assert warnings and not console.monitor.calls


def test_old_server_disables_controls_and_never_submits(console):
    console.update_snapshot(snapshot(False))
    assert not console.grace_begin.isEnabled() and not console.grace_schedule.isEnabled()
    console.set_world_mode("grace")
    assert not console.monitor.calls


def test_polling_does_not_overwrite_a_schedule_draft(console):
    edited = console.grace_start_time.dateTime().addSecs(123)
    console.grace_start_time.setDateTime(edited)
    console.update_snapshot(snapshot())
    assert console.grace_start_time.dateTime() == edited


def test_construction_and_close_do_not_control_the_server(console):
    console.close()
    assert not console.monitor.calls


def test_failed_schedule_keeps_draft_and_does_not_claim_saved(console, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    edited = console.grace_start_time.dateTime().addSecs(120)
    console.grace_start_time.setDateTime(edited)
    console.apply_world_schedule()
    console.operation_result("Server rejected the schedule.", False)
    console.update_snapshot(snapshot())
    assert console._world_schedule_dirty
    assert console.grace_start_time.dateTime() == edited
    assert "Unsaved draft" in console.world_schedule_saved.text()
    assert "not set" in console.world_schedule_saved.text()


def test_matching_readback_confirms_schedule_without_losing_a_new_edit(console, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    console.apply_world_schedule()
    pending = dict(console._pending_world_settings)
    console.update_snapshot(snapshot())
    assert console._pending_world_settings == pending
    assert "Waiting for server confirmation" in console.world_schedule_saved.text()
    changed = console.grace_start_time.dateTime().addSecs(5)
    console.grace_start_time.setDateTime(changed)
    live = snapshot()
    live["admin"]["settings"].update(pending)
    console.update_snapshot(live)
    assert console._pending_world_settings is None
    assert console._world_schedule_dirty
    assert console.grace_start_time.dateTime() == changed


def test_confirmed_schedule_and_reload_are_readback_driven(console, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    console.apply_world_schedule()
    live = snapshot()
    live["admin"]["settings"].update(console._pending_world_settings)
    console.update_snapshot(live)
    assert not console._world_schedule_dirty and console._pending_world_settings is None
    console.grace_start_time.setDateTime(console.grace_start_time.dateTime().addSecs(200))
    count = len(console.monitor.calls)
    console.reload_world_schedule()
    assert len(console.monitor.calls) == count
    assert (
        console.grace_start_time.dateTime().toSecsSinceEpoch()
        == live["admin"]["settings"]["grace_start_utc"]
    )


def test_cancel_schedule_requires_confirmation_and_removes_only_world_times(console, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    console.cancel_world_schedule()
    assert not console.monitor.calls
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    console.cancel_world_schedule()
    assert console.monitor.calls == [
        (
            ("configure",),
            {"settings": {"world_mode": "active", "grace_start_utc": None, "grace_end_utc": None}},
        )
    ]


def test_expired_schedule_is_rejected_before_submission(console, monkeypatch):
    from PySide6.QtCore import QDateTime, QTimeZone

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
    console.grace_start_time.setDateTime(QDateTime.fromSecsSinceEpoch(100, QTimeZone.utc()))
    console.grace_end_time.setDateTime(QDateTime.fromSecsSinceEpoch(200, QTimeZone.utc()))
    console.apply_world_schedule()
    assert warnings and not console.monitor.calls


def test_war_labels_and_unsupported_server_safety(console):
    assert "END WAR" in console.grace_begin.text()
    assert "START WAR" in console.grace_end.text()
    console.update_snapshot(snapshot(False))
    assert not console.grace_cancel.isEnabled() and not console.grace_reload.isEnabled()
    console.cancel_world_schedule()
    assert not console.monitor.calls


def test_all_world_controls_follow_capability_and_keep_utc_readback(console):
    names = (
        "grace_begin",
        "grace_end",
        "grace_schedule",
        "grace_cancel",
        "grace_reload",
        "grace_start_time",
        "grace_end_time",
    )
    for capable in (False, True, False, True):
        live = snapshot(capable)
        live["admin"]["settings"].update(grace_start_utc=100, grace_end_utc=200)
        live["admin"]["world"]["next_change_utc"] = 100
        console.update_snapshot(live)
        assert all(getattr(console, name).isEnabled() == capable for name in names)
        if capable:
            assert "1970-01-01 00:01:40 UTC" in console.world_note.text()
            assert "1970-01-01 00:03:20 UTC" in console.world_schedule_saved.text()
            assert console.grace_start_time.text() == "1970-01-01 00:01:40 UTC"
            assert console.grace_end_time.text() == "1970-01-01 00:03:20 UTC"
    assert not console.monitor.calls


def test_reward_defaults_are_visible_and_apply_as_one_server_setting_change(
    console, monkeypatch
):
    assert console.world_normal_base_reward.value() == 150_000
    assert console.world_special_base_reward.value() == 1_000_000
    assert console.world_boss_base_reward.value() == 1_000_000
    assert console.world_small_subtarget_reward.value() == 10_000
    assert console.world_large_subtarget_reward.value() == 10_000
    assert console.world_mercenary_rank.currentIndex() == 0
    labels = [label.text() for label in console.findChildren(QLabel)]
    assert "Active-war Special winning-team pool" in labels
    assert any("Grace Online Boss base pay" in value for value in labels)
    console.world_normal_base_reward.setValue(600_000)
    console.world_boss_base_reward.setValue(1_200_000)
    console.world_mercenary_rank.setCurrentIndex(3)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    console.apply_world_reward_settings()
    assert console.monitor.calls[-1] == (
        ("configure",),
        {
            "settings": {
                "world_normal_base_reward": 600_000,
                "world_special_base_reward": 1_000_000,
                "world_boss_base_reward": 1_200_000,
                "world_small_subtarget_reward": 10_000,
                "world_large_subtarget_reward": 10_000,
                "world_mercenary_rank": 3,
            }
        },
    )


def test_reward_draft_is_not_overwritten_and_reload_is_local_only(console):
    console.world_small_subtarget_reward.setValue(90_000)
    console.update_snapshot(snapshot())
    assert console.world_small_subtarget_reward.value() == 90_000
    count = len(console.monitor.calls)
    console.reload_world_reward_settings()
    assert len(console.monitor.calls) == count
    assert console.world_small_subtarget_reward.value() == 10_000


def test_old_server_disables_reward_controls(console):
    console.update_snapshot(snapshot(False))
    assert not console.world_reward_apply.isEnabled()
    assert not console.world_normal_base_reward.isEnabled()
    console.apply_world_reward_settings()
    assert not console.monitor.calls


def test_locked_cq_map_pool_is_visible_confirmed_and_submitted_atomically(
    console, monkeypatch
):
    assert console.cq_map_mode.currentData() == "territory"
    assert len(console.cq_map_checks) == 56
    assert not any(checkbox.isChecked() for checkbox in console.cq_map_checks.values())
    assert all(not checkbox.isEnabled() for checkbox in console.cq_map_checks.values())
    assert "Windy City" in console.cq_map_checks[3710].text()
    assert "APPROVED LOWER-LOAD" in console.cq_map_checks[3710].text()
    assert "Pask Field" in console.cq_map_checks[3410].text()
    assert "Route R1024" in console.cq_map_checks[3101].text()

    console.cq_map_mode.setCurrentIndex(console.cq_map_mode.findData("locked_pool"))
    console.cq_map_checks[3602].setChecked(True)
    console.cq_map_checks[3101].setChecked(True)
    assert all(checkbox.isEnabled() for checkbox in console.cq_map_checks.values())
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )

    console.apply_cq_map_settings()

    assert console.monitor.calls[-1] == (
        ("configure",),
        {
            "settings": {
                "cq_map_mode": "locked_pool",
                "cq_map_pool": [3101, 3602],
            }
        },
    )


def test_cq_map_ui_enforces_one_exact_map_per_region(console):
    console.cq_map_mode.setCurrentIndex(console.cq_map_mode.findData("locked_pool"))
    console.cq_map_checks[3700].setChecked(True)
    console.cq_map_checks[3710].setChecked(True)
    assert not console.cq_map_checks[3700].isChecked()
    assert console.cq_map_checks[3710].isChecked()


def test_supplied_lower_load_pool_is_selected_from_live_readback(console):
    approved = [3101, 3200, 3300, 3410, 3500, 3602, 3710]
    live = snapshot()
    live["admin"]["settings"].update(
        {"cq_map_mode": "locked_pool", "cq_map_pool": approved}
    )

    console.update_snapshot(live)

    assert console.cq_map_mode.currentData() == "locked_pool"
    assert {
        mission_id
        for mission_id, checkbox in console.cq_map_checks.items()
        if checkbox.isChecked()
    } == set(approved)


def test_cq_map_change_is_blocked_until_players_log_out(console, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
    live = snapshot()
    live["admin"]["players"] = [{"online_id": "Pilot"}]
    console.update_snapshot(live)
    console.cq_map_mode.setCurrentIndex(console.cq_map_mode.findData("locked_pool"))
    console.cq_map_checks[3200].setChecked(True)

    console.apply_cq_map_settings()

    assert warnings and "Log out" in warnings[-1][2]
    assert not console.monitor.calls


def test_empty_locked_cq_pool_is_rejected_before_submission(console, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
    console.cq_map_mode.setCurrentIndex(console.cq_map_mode.findData("locked_pool"))
    console.apply_cq_map_settings()
    assert warnings and not console.monitor.calls


def test_cq_map_readback_confirms_without_overwriting_a_new_draft(console, monkeypatch):
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    console.cq_map_mode.setCurrentIndex(console.cq_map_mode.findData("locked_pool"))
    console.cq_map_checks[3602].setChecked(True)
    console.apply_cq_map_settings()
    pending = dict(console._pending_cq_map_settings)
    console.cq_map_checks[3101].setChecked(True)
    live = snapshot()
    live["admin"]["settings"].update(pending)

    console.update_snapshot(live)

    assert console._pending_cq_map_settings is None
    assert console._cq_map_dirty
    assert console.cq_map_checks[3602].isChecked()
    assert console.cq_map_checks[3101].isChecked()
    count = len(console.monitor.calls)
    console.reload_cq_map_settings()
    assert len(console.monitor.calls) == count
    assert console.cq_map_checks[3602].isChecked()
    assert not console.cq_map_checks[3101].isChecked()


def test_old_server_disables_cq_map_controls(console):
    console.update_snapshot(snapshot(False))
    assert not console.cq_map_mode.isEnabled()
    assert not console.cq_map_apply.isEnabled()
    console.apply_cq_map_settings()
    assert not console.monitor.calls


def test_tool_guide_opens_the_single_project_readme(console, monkeypatch):
    from PySide6.QtWidgets import QPushButton

    opened = []
    monkeypatch.setattr(
        window.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile())
    )
    button = next(
        button for button in console.findChildren(QPushButton) if button.text() == "OPEN README"
    )
    button.click()
    assert opened == [str(console.root / "README.txt")]
    assert not console.monitor.calls


def test_login_welcome_editor_is_separate_and_capability_gated(console):
    assert not console.welcome.isEnabled()
    live = snapshot()
    live["admin"]["capabilities"]["login_welcome"] = True
    live["admin"]["settings"]["welcome_message"] = "Configured login text"
    console.update_snapshot(live)
    assert console.welcome.isEnabled()
    assert console.welcome.toPlainText() == "Configured login text"
    console.welcome.setPlainText("New login text")
    console.save_settings()
    assert console.monitor.calls[-1][1]["settings"]["welcome_message"] == "New login text"


def test_welcome_draft_is_not_overwritten_by_polling(console):
    live = snapshot()
    live["admin"]["capabilities"]["login_welcome"] = True
    live["admin"]["settings"]["welcome_message"] = "Old"
    console.update_snapshot(live)
    console.welcome.setPlainText("Draft")
    console.welcome.document().setModified(True)
    console.update_snapshot(live)
    assert console.welcome.toPlainText() == "Draft"


def test_team_faction_names_and_subtotals_use_current_membership(console):
    state = snapshot()
    state["admin"]["teams"] = [
        {"current_faction_id": 1, "desired_faction_id": 3},
        {"current_faction_id": 1},
        {"current_faction_id": 2},
        {"current_faction_id": 3},
        {"current_faction_id": 0, "faction_id": 1},
        {"current_faction_id": 7},
        {"current_faction_id": None, "faction_id": 1},
    ]
    console.update_snapshot(state)
    assert [console.teams.item(row, 6).text() for row in range(7)] == [
        "Sirius", "Sirius", "Venide", "EGF", "Unaffiliated", "Unknown (7)", "Unknown"
    ]
    assert console.faction_totals.text() == (
        "Teams by current faction — Total: 7\n"
        "Sirius: 2  |  Venide: 1  |  EGF: 1  |  Unaffiliated: 1  |  Unknown: 2"
    )
    assert not console.monitor.calls


def test_team_faction_counts_refresh_and_support_stored_legacy_teams(console):
    console.update_snapshot({"stored_teams": [{"faction_id": 2}, {"faction_id": 3}]})
    assert console.teams.item(0, 6).text() == "Venide"
    assert console.teams.item(1, 6).text() == "EGF"
    assert "Total: 2" in console.faction_totals.text()


def test_team_rank_and_special_gauge_are_visible_in_directory(console):
    state = snapshot()
    state["admin"]["teams"] = [
        {"team_rank": "C+", "special_sortie_progress": 5, "special_sortie_segments": 8}
    ]
    console.update_snapshot(state)
    assert console.teams.item(0, 4).text() == "C+"
    assert console.teams.item(0, 5).text() == "5/8"
    state = snapshot()
    state["admin"]["teams"] = [{"current_faction_id": 1}]
    console.update_snapshot(state)
    assert "Sirius: 1  |  Venide: 0  |  EGF: 0" in console.faction_totals.text()
    console.update_snapshot(snapshot())
    assert console.teams.rowCount() == 0
    assert console.faction_totals.text() == (
        "Teams by current faction — Total: 0\n"
        "Sirius: 0  |  Venide: 0  |  EGF: 0  |  Unaffiliated: 0  |  Unknown: 0"
    )
    assert not console.monitor.calls


def test_each_diagnostic_has_the_same_hover_card_on_both_cells(console):
    table = console.overview_table
    assert table.rowCount() == len(window._DIAGNOSTIC_HELP) == 10
    for row in range(table.rowCount()):
        name = table.item(row, 0).text()
        card = table.item(row, 0).toolTip()
        assert name in unescape(card) and window._DIAGNOSTIC_HELP[name] in unescape(card)
        assert table.item(row, 1).toolTip() == card
    assert not console.monitor.calls


def test_diagnostic_hover_cards_survive_refresh_and_missing_live_status(console):
    cards = [console.overview_table.item(row, 0).toolTip() for row in range(10)]
    console.update_snapshot({"service": {"MainPID": "0"}})
    assert console.overview_table.item(1, 1).text() == "0"
    assert [console.overview_table.item(row, 1).toolTip() for row in range(10)] == cards
    console.update_snapshot(snapshot())
    assert [console.overview_table.item(row, 1).toolTip() for row in range(10)] == cards
    assert not console.monitor.calls


def test_hover_cards_escape_text_and_do_not_spread_to_other_tables(console):
    window.populate(console.players, [["<sample>", "value"]], row_help={"<sample>": "A < B & C"})
    assert "&lt;sample&gt;" in console.players.item(0, 0).toolTip()
    assert "A &lt; B &amp; C" in console.players.item(0, 1).toolTip()
    window.populate(console.players, [["<sample>", "value"]])
    assert console.players.item(0, 0).toolTip() == ""
    assert console.players.item(0, 1).toolTip() == ""


def test_team_area_names_use_territory_ids_not_mock_selector_order(console):
    state = snapshot()
    state["admin"]["teams"] = [{"territory_area_id": value} for value in range(8)]
    state["admin"]["teams"] += [{"territory_area_id": 99}, {}, {"territory_area_id": None}]
    console.update_snapshot(state)
    assert [console.teams.item(row, 7).text() for row in range(11)] == [
        "Unassigned", "North Frontier", "Far East", "Middle East", "South Frontier",
        "Mid-Continent", "South Islands", "New Frontier", "Unknown (99)", "Unknown", "Unknown"
    ]
    console.update_snapshot({"stored_teams": [{"territory_area_id": 7}]})
    assert console.teams.item(0, 7).text() == "New Frontier"
    assert not console.monitor.calls


def test_player_peer_is_hidden_but_operator_server_address_remains(console):
    state = snapshot()
    state["admin"]["advertised_host"] = "198.51.100.10"
    state["admin"]["players"] = [{"online_id": "Tester", "player_id": 42, "peer": ["203.0.113.17", 4000], "last_seen_seconds": 1}]
    console.update_snapshot(state)
    select_player(console, "Tester")
    assert console.players.item(console.players.currentRow(), 2).text() == "ONLINE"
    assert "203.0.113.17" not in " ".join(
        console.players.item(row, column).text()
        for row in range(console.players.rowCount())
        for column in range(console.players.columnCount())
    )
    assert console.overview_table.item(3, 1).text() == "198.51.100.10"
    assert state["admin"]["players"][0]["peer"] == ["203.0.113.17", 4000]
    assert not console.monitor.calls


def test_debug_inspection_and_export_do_not_reveal_peer(console, monkeypatch, tmp_path):
    import json

    from acvd_console.traffic import decode_record
    address = "203.0.113.17"
    event = decode_record({"event": "game_message_received", "message_type": 0x0417, "peer": [address, 4000], "payload": {"hex": address.encode().hex()}}, console.monitor.catalog)
    console.add_events([event])
    console.feed.selectRow(0)
    console.inspect_event()
    assert address not in console.raw.toPlainText()
    assert console.fields.toPlainText() == "Packet bytes are hidden to protect IP addresses."
    output = tmp_path / "export.json"
    monkeypatch.setattr(window.QFileDialog, "getSaveFileName", lambda *args: (str(output), "JSON"))
    console.export_view()
    assert address not in output.read_text()
    assert json.loads(output.read_text())["events"][0]["record"]["peer"] == "[IP hidden]"
