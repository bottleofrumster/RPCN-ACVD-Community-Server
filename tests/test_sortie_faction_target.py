"""Normal faction conditions constrain assignment before any AP settlement."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from acvd_server import verdict_war
from acvd_server.combat_reports import CombatReport
from acvd_server.handlers.world import get_world_current_info
from acvd_server.matchmaking import MatchmakingCoordinator
from acvd_server.protocol import decode_proto
from acvd_server.udp import GET_WORLD_CURRENT_INFO, GameMessage, GameSession
from acvd_server.world_messages import _world_basic_info, _world_current_info


def fields(payload):
    return {field.number: field.value for field in decode_proto(payload)}


def world_rows(payload):
    return {
        fields(field.value)[1]: fields(field.value)
        for field in decode_proto(payload)
        if field.number == 4
    }


@pytest.fixture
def setup():
    # Recorded season-28 ownership, not the fixed seed-region/faction mapping.
    war = verdict_war.new(
        season=28,
        generation="01" * 16,
        now=1000,
        settings={},
        owners=[2, 2, 3, 3, 1, 1, 1],
    )
    peer = GameSession(
        b"pilot000", bytes(16), "Pilot", 11, established=True, peer=("127.0.0.1", 9999)
    )
    pushes, events = [], []
    telemetry = SimpleNamespace(emit=lambda name, **data: events.append((name, data)))
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        send_push=lambda target, payload, subtype=None: pushes.append(payload),
        telemetry=telemetry,
        room_log_paths=[],
        get_test_war=lambda: war,
    )
    prep = {
        "session": peer,
        "team_id": 39,
        "faction_id": 3,
        "territory_area_id": 3,
        "criteria": {1: 28, 2: 2, 3: 1, 4: 1, 5: 0, 8: 3, 9: 2, 10: 1, 11: 1, 12: 1, 13: 1, 14: 1},
        "world_phase": 2,
        "prepare_id": 11,
        "match_id": 0,
        "squad": {11: (peer, 39)},
        "claimed_player_ids": [11],
        "claimed_player_roles": [0],
    }
    coordinator.preparations["Pilot"] = prep
    get_world_current_info(
        peer,
        GameMessage(GET_WORLD_CURRENT_INFO, b"0001", b""),
        telemetry=telemetry,
        reply=lambda _: None,
        war=war,
    )
    return coordinator, prep, war, pushes, events


def test_reported_venide_burn_run_targets_venide_and_damages_only_published_area(
    setup, monkeypatch
):
    coordinator, prep, war, pushes, events = setup

    def choose(eligible):
        assert eligible == [1, 2]  # Previously included Sirius's New Frontier (7).
        return eligible[-1]

    monkeypatch.setattr("acvd_server.matchmaking.secrets.choice", choose)
    assert coordinator._release_active_single_team(prep)
    match = coordinator.matches[1]
    assert fields(pushes[0])[6] == match["combat_area_id"] == 2
    assert match["war_context"]["defender_faction"] == 2
    assert match["war_context"]["attacker_faction"] == 3
    report = CombatReport(
        header=(11, 2, 1, 0, 0, 0, 0, 0),
        units=((11, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0),),
        subtargets=(),
        digest="00" * 32,
    )
    intent = coordinator.war_impact(match, report)
    updated, receipt = verdict_war.impact(
        war,
        intent["context"],
        winning_faction=intent["winning_faction"],
        boss=intent["boss"],
        now=1001,
    )
    assert receipt["applied"] and receipt["area_id"] == 2 and receipt["damage"] == 75_000
    assert receipt["previous_faction"] == receipt["faction_id"] == 2
    for original, current in zip(war["areas"], updated["areas"]):
        if original["area_id"] != 2:
            assert current == original
    now = datetime.fromtimestamp(1001, timezone.utc)
    basic = world_rows(_world_basic_info(now, war=updated))
    progress = world_rows(_world_current_info(now, war=updated))
    assert basic[2][2] == 2 and progress[2][2] == 725_000
    assert basic[7][2] == 1 and progress[7][2] == 800_000
    event, data = events[-1]
    assert event == "game_single_team_fallback_released"
    assert data["requested_opponent_faction"] == data["defending_faction"] == 2


@pytest.mark.parametrize("faction", [1, 2, 3])
@pytest.mark.parametrize("target", [0, 1, 2, 3, None, -1, 4, True, False, "2"])
def test_normal_fallback_never_silently_substitutes_another_faction(
    setup, monkeypatch, faction, target
):
    coordinator, prep, war, pushes, _ = setup
    prep["faction_id"], prep["criteria"][9] = faction, target
    eligible = [
        a["area_id"]
        for a in war["areas"]
        if type(target) is int
        and target in (0, 1, 2, 3)
        and a["faction_id"] != faction
        and (target == 0 or a["faction_id"] == target)
    ]

    def choose(areas):
        assert areas == eligible
        return areas[-1]

    monkeypatch.setattr("acvd_server.matchmaking.secrets.choice", choose)
    assert coordinator._release_active_single_team(prep) is bool(eligible)
    if not eligible:
        assert not pushes and not coordinator.matches and coordinator.next_match_id == 1
        assert prep["match_id"] == 0


@pytest.mark.parametrize("reason", ["captured", "stale_world", "missing_ownership"])
def test_unavailable_selected_faction_keeps_search_without_consuming_timer(setup, reason):
    coordinator, prep, war, pushes, _ = setup
    if reason == "captured":
        for area in war["areas"][:2]:
            area["faction_id"] = 1
            area["epoch"] += 1
    elif reason == "stale_world":
        for area in war["areas"][:2]:
            area["ap"] -= war["rules"]["base_ap"]
    else:
        coordinator.get_test_war = lambda: None
        prep["criteria"][1] = 3
    # No asyncio task is needed: rejection must leave the pending handle alone.
    pending = object()
    prep["single_team_fallback_task"] = pending
    assert not coordinator._release_active_single_team(prep)
    assert prep["single_team_fallback_task"] is pending
    assert not pushes and not coordinator.matches and coordinator.next_match_id == 1
    assert prep["match_id"] == 0


def test_normal_faction_condition_does_not_change_special_boss_selection(setup, monkeypatch):
    coordinator, prep, _, pushes, _ = setup
    prep["criteria"][3] = 2
    # Special retains its existing random-enemy contract, not Normal conditions.
    monkeypatch.setattr("acvd_server.matchmaking.secrets.choice", lambda areas: max(areas))
    assert coordinator._release_active_single_team(prep)
    assert fields(pushes[0])[4] == 6 and fields(pushes[0])[6] == 7


def test_two_team_match_uses_selected_factions_current_region_not_stale_home(setup):
    coordinator, attacker, war, pushes, events = setup
    attacker["criteria"][8] = 1
    peer = GameSession(
        b"defend00", bytes(16), "Defender", 22, established=True, peer=("127.0.0.1", 9998)
    )
    defender = {
        "session": peer,
        "team_id": 40,
        "faction_id": 2,
        "territory_area_id": 7,  # Home row predates rotating ownership; currently Sirius.
        "criteria": {1: 28, 2: 2, 3: 1, 8: 2, 9: 3},
        "world_phase": 2,
        "prepare_id": 12,
        "match_id": 0,
    }
    get_world_current_info(
        peer,
        GameMessage(GET_WORLD_CURRENT_INFO, b"0002", b""),
        telemetry=coordinator.telemetry,
        reply=lambda _: None,
        war=war,
    )
    coordinator.preparations["Defender"] = defender
    coordinator._match_preparation(defender)
    match = coordinator.matches[1]
    assert match["members"] == [attacker, defender]
    area = fields(pushes[0])[6]
    assert area in (1, 2)
    assert match["war_context"]["area_id"] == match["combat_area_id"] == area
    assert match["war_context"]["defender_faction"] == 2
    assert not match["is_intra_faction"]
    event, data = events[-1]
    assert event == "game_match_result_published"
    assert data["requested_opponent_factions"] == [2, 3]
    assert data["defending_faction"] == 2
