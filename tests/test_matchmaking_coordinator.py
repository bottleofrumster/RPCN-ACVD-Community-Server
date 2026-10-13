"""Single match-state ownership and lifecycle regression tests; no live RPCN."""

import asyncio
import os
import time
from pathlib import Path

import pytest

from acvd_server.matchmaking import MatchmakingCoordinator
from acvd_server.protocol import decode_proto, first_field
from acvd_server.server import GameDatagramProtocol
from acvd_server.teams import TeamStore
from acvd_server.udp import GameSession


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


@pytest.fixture
def protocol(tmp_path):
    return GameDatagramProtocol(
        Events(),
        {},
        Path("native/libacvd_cwc.so"),
        tmp_path / "players",
        TeamStore(tmp_path / "teams"),
    )


def preparation(name, number, match_id=1):
    actor = GameSession(
        name.encode().ljust(8, b"0")[:8],
        bytes(16),
        name,
        number,
        established=True,
        peer=("127.0.0.1", 10000 + number),
    )
    return {
        "session": actor,
        "prepare_id": number,
        "team_id": number,
        "faction_id": 1,
        "match_id": match_id,
        "registered_monotonic": time.monotonic(),
        "criteria": {1: 3, 2: 2, 3: 1},
    }


def seed_pair(coordinator):
    host, guest = preparation("Host", 11), preparation("Guest", 22)
    match = {
        "match_id": 1,
        "members": [host, guest],
        "criteria": host["criteria"],
        "created_monotonic": time.monotonic(),
    }
    coordinator.preparations = {"Host": host, "Guest": guest}
    coordinator.matches = {1: match}
    return host, guest, match


def test_compatibility_views_have_one_state_owner(protocol):
    coordinator = protocol._matchmaking
    for field, value in (
        ("preparations", {}),
        ("matches", {}),
        ("signaling_ready_pairs", {frozenset({"A", "B"})}),
        ("match_room_log_paths", [Path("example.log")]),
    ):
        setattr(protocol, field, value)
        assert getattr(coordinator, field) is value and getattr(protocol, field) is value
    protocol.next_prepare_id = 17
    coordinator.next_prepare_id += 1
    assert protocol.next_prepare_id == 18
    coordinator.next_match_id = 29
    assert protocol.next_match_id == 29
    coordinator._clear_signaling_ready_for("A")
    assert protocol.signaling_ready_pairs == set()


def test_legacy_room_log_bridge_requires_a_second_explicit_diagnostic_opt_in(
    tmp_path, monkeypatch
):
    paths = [tmp_path / "one.log", tmp_path / "two.log"]
    monkeypatch.setenv("ACVD_MATCH_ROOM_LOG_PATHS", os.pathsep.join(map(str, paths)))
    events = Events()

    disabled = GameDatagramProtocol(
        events,
        {},
        Path("native/libacvd_cwc.so"),
        tmp_path / "players-disabled",
        TeamStore(tmp_path / "teams-disabled"),
    )
    assert disabled.match_room_log_paths == []
    assert events.rows[-1][0] == "game_match_room_log_bridge_disabled"

    monkeypatch.setenv("ACVD_ENABLE_LEGACY_ROOM_LOG_BRIDGE", "1")
    events = Events()
    enabled = GameDatagramProtocol(
        events,
        {},
        Path("native/libacvd_cwc.so"),
        tmp_path / "players-enabled",
        TeamStore(tmp_path / "teams-enabled"),
    )
    assert enabled.match_room_log_paths == paths
    assert events.rows[-1][0] == "game_match_room_log_bridge_enabled"


def test_team_directory_and_push_are_dynamic_not_constructor_snapshots(protocol, monkeypatch):
    candidate = preparation("Host", 11)
    assert protocol._matchmaking._match_team_session_membership(candidate) is None
    protocol.team_sessions = {
        11: {"session_id": 11, "members": {"Host": {"np_identity": b"H" * 37, "member_id": 1}}}
    }
    result = protocol._matchmaking._match_team_session_membership(candidate)
    assert result[1] is protocol.team_sessions[11]
    sent = []
    monkeypatch.setattr(protocol, "_send_game_push", lambda *a, **k: sent.append((a, k)))
    protocol._matchmaking._send_game_push(candidate["session"], b"payload", subtype=2)
    assert sent == [((candidate["session"], b"payload"), {"subtype": 2})]


def test_disconnect_removes_the_whole_match_and_cancels_its_watch(protocol):
    coordinator = protocol._matchmaking
    host, guest, match = seed_pair(coordinator)

    class Task:
        cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    task = Task()
    match["room_log_bridge_task"] = task
    coordinator.preparations["Other"] = preparation("Other", 33, 0)
    assert protocol._remove_matchmaking_member("Host", reason="fin") == [1]
    assert task.cancelled and coordinator.matches == {}
    assert set(protocol.preparations) == {"Other"} and guest["match_id"] == 0


def test_finished_old_match_does_not_remove_newer_preparation(protocol):
    coordinator = protocol._matchmaking
    _, _, _ = seed_pair(coordinator)
    newer = preparation("Guest", 22, 2)
    coordinator.preparations["Guest"] = newer
    coordinator.signaling_ready_pairs = {
        frozenset({"Host", "Guest"}),
        frozenset({"Other", "Third"}),
    }
    assert coordinator._remove_matchmaking_state(1) == ["Host", "Guest"]
    assert coordinator.preparations == {"Guest": newer}
    assert coordinator.signaling_ready_pairs == {frozenset({"Other", "Third"})}


def test_live_search_outlasts_handoff_timeout_but_stale_transport_is_removed(protocol, monkeypatch):
    coordinator = protocol._matchmaking
    monkeypatch.setattr("acvd_server.matchmaking.time.monotonic", lambda: 1000.0)
    stale, fresh = preparation("Old", 11, 0), preparation("New", 22, 0)
    stale["registered_monotonic"] = 819.0
    fresh["registered_monotonic"] = 900.0
    stale["session"].last_seen_monotonic = fresh["session"].last_seen_monotonic = 1000.0
    coordinator.preparations = {"Old": stale, "New": fresh}
    coordinator._purge_stale_matchmaking()
    assert set(coordinator.preparations) == {"Old", "New"}
    stale["session"].last_seen_monotonic = 909.0
    coordinator._purge_stale_matchmaking()
    assert set(coordinator.preparations) == {"New"}


def test_unpublished_formed_match_still_has_a_bounded_handoff(protocol, monkeypatch):
    coordinator = protocol._matchmaking
    monkeypatch.setattr("acvd_server.matchmaking.time.monotonic", lambda: 1000.0)
    host, guest, match = seed_pair(coordinator)
    match["created_monotonic"] = 819.0
    host["session"].last_seen_monotonic = guest["session"].last_seen_monotonic = 1000.0
    coordinator._purge_stale_matchmaking()
    assert not coordinator.matches and not coordinator.preparations


@pytest.mark.parametrize("released", [False, True])
def test_new_squads_form_a_separate_pair_without_changing_existing_match(
    protocol, monkeypatch, released
):
    coordinator = protocol._matchmaking
    host, guest, original = seed_pair(coordinator)
    coordinator.next_match_id = 2
    if released:
        original.update(native_room_archive=b"accepted archive", combat_area_id=3)
    else:
        original["preflight_retry_pending"] = {"Host", "Guest"}
    before = {**original, "members": list(original["members"])}
    sent = []
    monkeypatch.setattr(protocol, "_send_game_push", lambda target, *a, **k: sent.append(target))

    third = preparation("Third", 33, 0)
    coordinator.preparations["Third"] = third
    coordinator._match_preparation(third)
    assert third["match_id"] == 0
    assert original == before
    assert sent == []

    fourth = preparation("Fourth", 44, 0)
    coordinator.preparations["Fourth"] = fourth
    coordinator._match_preparation(fourth)
    assert coordinator.matches[2]["members"] == [third, fourth]
    assert third["match_id"] == fourth["match_id"] == 2
    assert original == before
    assert host["match_id"] == guest["match_id"] == 1
    assert all(target not in (host["session"], guest["session"]) for target in sent)


@pytest.mark.parametrize("change", ["outsider", "replaced_session", "changed_team", "boss_context"])
def test_retry_cannot_replace_another_match_member(protocol, monkeypatch, change):
    coordinator = protocol._matchmaking
    host, guest, original = seed_pair(coordinator)
    retry = {**host}
    if change == "outsider":
        retry = preparation("Third", 33, 1)
    elif change == "replaced_session":
        retry = preparation("Host", 11, 1)
    elif change == "changed_team":
        retry["team_id"] = 99
    else:
        original["kind"] = "interwar_boss"
        original["members"] = [host]
    before = {**original, "members": list(original["members"])}
    sent = []
    monkeypatch.setattr(protocol, "_send_game_push", lambda *a, **k: sent.append(a))
    coordinator.preparations[retry["session"].online_id] = retry
    coordinator._match_preparation(retry)
    assert original == before
    assert sent == []
    assert coordinator.telemetry.rows[-1][0] == "game_match_retry_rejected"


def test_missing_match_reference_returns_preparation_to_waiting_queue(protocol, monkeypatch):
    coordinator = protocol._matchmaking
    candidate = preparation("Host", 11, 999)
    coordinator.preparations["Host"] = candidate
    monkeypatch.setattr(protocol, "_send_game_push", lambda *a, **k: pytest.fail("Unexpected push"))
    coordinator._match_preparation(candidate)
    assert candidate["match_id"] == 0
    assert coordinator.matches == {}
    assert coordinator.telemetry.rows[-1][0] == "game_match_waiting"


async def test_room_watch_uses_current_task_safely_and_releases_guest_once(tmp_path):
    sent = []
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        send_push=lambda target, payload, subtype=None: sent.append((target, payload, subtype)),
        telemetry=Events(),
        room_log_paths=[],
    )
    host, guest, match = seed_pair(coordinator)
    current = asyncio.current_task()
    match["room_log_bridge_task"] = current
    log = tmp_path / "room.log"
    log.write_text(
        "SceNpMatching2CreateJoinRoomResponse:\nserverId: 1\nworldId: 65537\nroomId: 77\n"
    )
    await coordinator._watch_match_room_logs(1, "Host", {str(log): 0})
    assert current.cancelling() == 0 and "room_log_bridge_task" not in match
    assert match["counterparts_released"] and match["room_published"]
    assert [entry[2] for entry in sent] == [2, 5]
    assert all(entry[0] is guest["session"] for entry in sent)
    assert first_field(decode_proto(sent[1][1]), 2).value == 77
    coordinator._publish_match_room(
        host["session"], room_id=77, room_password=b"PRVTPSWD", source_values={}
    )
    assert len(sent) == 2


def test_match_freezes_defender_region_for_both_peers_and_late_room_push():
    sent = []
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        send_push=lambda target, payload, subtype=None: sent.append((target, payload, subtype)),
        telemetry=Events(),
        room_log_paths=[],
    )
    host = preparation("Host", 11, 0)
    guest = preparation("Guest", 22, 0)
    host["territory_area_id"] = 7
    guest["territory_area_id"] = 3
    host["territory_layout_owner_id"] = 111
    guest["territory_layout_owner_id"] = 222
    coordinator.preparations = {"Host": host, "Guest": guest}
    coordinator._get_team_sessions = lambda: {
        item["team_id"]: {
            "session_id": item["team_id"],
            "members": {item["session"].online_id: {
                "player_id": item["session"].player_id, "member_id": 1,
                "np_identity": b"\x01" + item["session"].online_id.encode().ljust(36, b"\0"),
            }},
        } for item in (host, guest)
    }
    coordinator.signaling_ready_pairs.add(frozenset({"Host", "Guest"}))
    coordinator._match_preparation(guest)
    match = coordinator.matches[1]
    assert match["combat_area_id"] == 3
    assert match["territory_layout_owner_id"] == 222
    assert [target.online_id for target, _, subtype in sent if subtype == 2] == ["Host"]
    assert [
        first_field(decode_proto(payload), 6).value for _, payload, subtype in sent if subtype == 2
    ] == [3]
    guest["territory_area_id"] = 2
    guest["territory_layout_owner_id"] = 999
    coordinator._publish_match_room(
        host["session"],
        room_id=77,
        room_password=b"PRVTPSWD",
        source_values={},
    )
    assert [
        first_field(decode_proto(payload), 6).value for _, payload, subtype in sent if subtype == 2
    ] == [3, 3]
    assert match["combat_area_id"] == 3
    assert [
        first_field(decode_proto(payload), 7).value for _, payload, subtype in sent if subtype == 2
    ] == [222, 222]
