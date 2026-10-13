"""Active-war one-squad fallback modes, without client or durable-state mutation."""

import asyncio
import time

import pytest

from acvd_server.match_messages import active_single_team_result
from acvd_server.matchmaking import ACTIVE_SINGLE_TEAM, MatchmakingCoordinator
from acvd_server.protocol import decode_proto, proto_bytes, proto_varint
from acvd_server.teams import TeamStore
from acvd_server.udp import GameSession


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


def preparation(name, player_id, *, sortie=1, team_id=1, faction_id=1, area_id=2):
    session = GameSession(
        name.encode().ljust(8, b"0"),
        bytes(16),
        name,
        player_id,
        established=True,
        peer=("127.0.0.1", 10000 + player_id),
    )
    return {
        "session": session,
        "prepare_id": player_id,
        "team_id": team_id,
        "faction_id": faction_id,
        "territory_area_id": area_id,
        "territory_layout_owner_id": 0,
        "criteria": {1: 3, 2: 2, 3: sortie, 8: 3},
        "world_phase": 2,
        "claimed_player_ids": [player_id],
        "claimed_player_roles": [0],
        "squad": {player_id: (session, team_id)},
        "match_id": 0,
        "registered_monotonic": time.monotonic(),
        "search_started_monotonic": time.monotonic(),
    }


def coordinator(*, target=None, wait=30.0, cq_maps=None):
    pushes, events = [], Events()
    instance = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        send_push=lambda *args, **kwargs: pushes.append((args, kwargs)),
        telemetry=events,
        room_log_paths=[],
        select_opposing_territory=lambda _team, _faction: target,
        get_cq_map_settings=lambda: cq_maps or {},
        single_team_fallback_seconds=wait,
    )
    return instance, pushes, events


@pytest.mark.parametrize("battle_type,owner", [(4, 99), (5, 0), (6, 0)])
def test_active_single_team_encoder_has_no_fabricated_counterpart(battle_type, owner):
    fields = {
        field.number: field.value
        for field in decode_proto(
            active_single_team_result(
                17,
                battle_type=battle_type,
                area_id=3,
                territory_layout_owner_id=owner,
            )
        )
    }
    assert fields == {1: 17, 2: 0, 3: 1, 4: battle_type, 5: 1, 6: 3, 7: owner, 8: 0, 9: 0}


def test_normal_fallback_releases_type5_and_tracks_the_whole_squad():
    instance, pushes, _ = coordinator()
    member = preparation("Normal", 11, sortie=1)
    instance.preparations["Normal"] = member

    assert instance._release_active_single_team(member)
    match = instance.matches[1]
    assert match["kind"] == ACTIVE_SINGLE_TEAM
    assert match["battle_type"] == 5 and match["combat_area_id"] == 2
    assert match["territory_layout_owner_id"] == 0
    assert set(match["squad"]) == {11}
    assert pushes[0][0][0] is member["session"] and pushes[0][1] == {"subtype": 2}
    assert next(f.value for f in decode_proto(pushes[0][0][1]) if f.number == 9) == 0


def test_special_sortie_releases_a_one_team_world_boss_not_offline_territory():
    target = ("Defender", 9, 99, 7)
    instance, pushes, _ = coordinator(target=target)
    member = preparation("Special", 11, sortie=2)
    instance.preparations["Special"] = member
    instance.hires.get_sessions = lambda: [member["session"]]
    instance.hires.get_team_id = lambda online_id: 1 if online_id == "Special" else 0

    assert instance._release_active_single_team(member)
    match = instance.matches[1]
    assert match["battle_type"] == 6
    assert match["territory_layout_owner_id"] == 0
    fields = {field.number: field.value for field in decode_proto(pushes[0][0][1])}
    assert fields[4] == 6 and fields[7] == 0


def test_special_sortie_releases_immediately_without_a_matchmaking_wait():
    instance, pushes, _ = coordinator()
    member = preparation("Special", 11, sortie=2, area_id=3)
    instance.preparations["Special"] = member
    instance.hires.get_sessions = lambda: [member["session"]]
    instance.hires.get_team_id = lambda online_id: 1 if online_id == "Special" else 0

    instance._match_preparation(member)
    assert "single_team_fallback_task" not in member
    assert instance.matches[1]["battle_type"] == 6
    assert len(pushes) == 1


def test_special_world_boss_uses_current_faction_when_team_has_no_territory():
    instance, pushes, _ = coordinator()
    member = preparation("Special", 11, sortie=2, faction_id=3, area_id=0)
    instance.preparations["Special"] = member
    instance.hires.get_sessions = lambda: [member["session"]]
    instance.hires.get_team_id = lambda online_id: 1 if online_id == "Special" else 0

    assert instance._release_active_single_team(member)
    assert instance.matches[1]["combat_area_id"] == 3
    fields = {field.number: field.value for field in decode_proto(pushes[0][0][1])}
    assert fields[4] == 6 and fields[6] == 3 and fields[7] == 0


@pytest.mark.parametrize("sortie,battle_type", [(1, 5), (2, 6)])
def test_exact_extermination_pool_does_not_relabel_burn_or_special_maps(
    sortie, battle_type
):
    instance, pushes, events = coordinator(
        cq_maps={"cq_map_mode": "locked_pool", "cq_map_pool": [3101, 3500]}
    )
    first = preparation("First", 11, sortie=sortie, area_id=3)
    second = preparation("Second", 22, sortie=sortie, area_id=3)

    assert instance._release_active_single_team(first)
    assert instance._release_active_single_team(second)

    assert first["territory_area_id"] == 3 and second["territory_area_id"] == 3
    assert instance.matches[1]["source_combat_area_id"] == 3
    assert instance.matches[1]["combat_area_id"] == 3
    assert instance.matches[2]["source_combat_area_id"] == 3
    assert instance.matches[2]["combat_area_id"] == 3
    assert instance.matches[1]["battle_type"] == battle_type
    assert instance.matches[1]["cq_map_locked"] is False
    assert [
        {field.number: field.value for field in decode_proto(args[1])}[6]
        for args, _kwargs in pushes
    ] == [3, 3]
    released = [fields for event, fields in events.rows if event.endswith("released")]
    assert [row["source_combat_area_id"] for row in released] == [3, 3]


def test_locked_pool_never_detaches_a_stored_territory_layout_from_its_area():
    instance, _pushes, _events = coordinator(
        cq_maps={"cq_map_mode": "locked_pool", "cq_map_pool": [3101]}
    )
    assert instance._select_cq_combat_area(
        3,
        match_id=1,
        battle_type=3,
        kind=None,
    ) == (3, False, "locked_pool", None)


def test_live_pool_change_does_not_relabel_non_extermination_matches():
    settings = {"cq_map_mode": "locked_pool", "cq_map_pool": [3602]}
    instance, _pushes, _events = coordinator(cq_maps=settings)
    first = preparation("First", 11, sortie=1, area_id=3)
    second = preparation("Second", 22, sortie=1, area_id=3)
    assert instance._release_active_single_team(first)

    settings["cq_map_pool"] = [3101]
    assert instance.matches[1]["combat_area_id"] == 3
    assert instance._release_active_single_team(second)
    assert instance.matches[1]["combat_area_id"] == 3
    assert instance.matches[2]["combat_area_id"] == 3


@pytest.mark.asyncio
async def test_bounded_wait_rechecks_for_a_pair_before_releasing_solo():
    instance, pushes, _ = coordinator(wait=0.01)
    first = preparation("First", 11, sortie=1, team_id=1, faction_id=1, area_id=2)
    second = preparation("Second", 22, sortie=1, team_id=2, faction_id=2, area_id=3)
    first["criteria"][8] = 1
    second["criteria"][8] = 2
    instance.preparations = {"First": first}
    instance._match_preparation(first)
    instance.preparations["Second"] = second
    instance._match_preparation(second)
    await asyncio.sleep(0.02)

    assert len(instance.matches) == 1
    match = instance.matches[1]
    assert match.get("kind") is None and len(match["members"]) == 2
    assert match["battle_type"] == 1
    assert len(pushes) == 1


@pytest.mark.asyncio
async def test_bounded_wait_releases_normal_weapons_when_no_pair_arrives():
    instance, pushes, _ = coordinator(wait=0)
    member = preparation("Solo", 11, sortie=1)
    instance.hires.get_sessions = lambda: [member["session"]]
    instance.hires.get_team_id = lambda online_id: 1 if online_id == "Solo" else 0
    instance.preparations = {"Solo": member}
    instance._match_preparation(member)
    await asyncio.sleep(0.01)

    assert instance.matches[1]["kind"] == ACTIVE_SINGLE_TEAM
    assert instance.matches[1]["battle_type"] == 5
    fields = {field.number: field.value for field in decode_proto(pushes[0][0][1])}
    assert fields[2] == 0 and fields[4] == 5


@pytest.mark.asyncio
async def test_phase_change_invalidates_waiting_active_preparation():
    phase, pushes, events = [2], [], Events()
    instance = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        send_push=lambda *args, **kwargs: pushes.append((args, kwargs)),
        telemetry=events,
        room_log_paths=[],
        get_world_phase=lambda: phase[0],
        single_team_fallback_seconds=0.01,
    )
    member = preparation("Normal", 11, sortie=1)
    instance.hires.get_sessions = lambda: [member["session"]]
    instance.hires.get_team_id = lambda online_id: 1 if online_id == "Normal" else 0
    instance.preparations["Normal"] = member
    instance._match_preparation(member)

    phase[0] = 0
    await asyncio.sleep(0.02)

    assert not pushes and not instance.preparations and not instance.matches
    invalidated = [row for row in events.rows if row[0] == "game_active_preparation_invalidated"]
    assert len(invalidated) == 1
    assert invalidated[0][1]["prepare_id"] == 11


def test_store_selects_only_an_intact_opposing_current_season_layout(tmp_path):
    store = TeamStore(tmp_path / "teams")
    attacker = store.create(
        online_id="Attacker",
        player_id=11,
        name="A",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=1,
    )
    defender = store.create(
        online_id="Defender",
        player_id=99,
        name="D",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=1,
    )
    attacker.update(current_faction_id=1, territory_area_id=2)
    defender.update(current_faction_id=2, territory_area_id=7)
    store._save()
    store.save_native_territory_layout(
        "Defender",
        player_id=99,
        season_id=3,
        area_id=7,
        layout_version=7,
        payload=b"intact-layout",
    )

    assert store.select_opposing_native_territory(
        attacker_team_id=attacker["team_id"], attacker_faction_id=1, season_id=3
    ) == ("Defender", defender["team_id"], 99, 7)
    assert (
        store.select_opposing_native_territory(
            attacker_team_id=attacker["team_id"], attacker_faction_id=1, season_id=4
        )
        is None
    )


def test_single_team_result_is_accepted_without_a_paired_room_handoff():
    instance, _, _ = coordinator()
    member = preparation("Normal", 11, sortie=1)
    instance.preparations["Normal"] = member
    assert instance._release_active_single_team(member)
    participant = b"".join(
        proto_varint(number, 11 if number == 1 else 1 if number == 4 else 0)
        for number in range(1, 13)
    )
    report = b"".join(
        proto_varint(number, value)
        for number, value in {1: 11, 2: 2, 3: 1, 4: 0, 5: 0, 6: 0, 7: 0, 8: 0}.items()
    ) + proto_bytes(9, participant)
    assert instance.observe_combat_result(member["session"], report, team_id=1)
