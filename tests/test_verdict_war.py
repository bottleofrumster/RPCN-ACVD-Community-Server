"""Configurable test-war transitions, distinct from claimed retail balance."""

import asyncio
import json
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from acvd_server import verdict_war as war
from acvd_server.administration import AdminEndpoint, OperatorSettings
from acvd_server.combat_reports import CombatReport
from acvd_server.config import ServerConfig
from acvd_server.crypto import generate_rsa_keypair
from acvd_server.handlers.team_updates import join_faction
from acvd_server.handlers.world import get_world_current_info
from acvd_server.matchmaking import ACTIVE_SINGLE_TEAM, INTERWAR_BOSS, MatchmakingCoordinator
from acvd_server.protocol import decode_proto, first_field, proto_varint
from acvd_server.rewards import WorldBattleRewardPlan, world_wallet_payment
from acvd_server.server import AcvdServer
from acvd_server.teams import TeamStore
from acvd_server.telemetry import Telemetry
from acvd_server.udp import GET_WORLD_BASIC_INFO, GET_WORLD_CURRENT_INFO, GameMessage, GameSession
from acvd_server.world_messages import _world_basic_info, _world_current_info


def state(**settings):
    return war.new(
        season=3, generation="01" * 16, now=1000, settings=settings, owners=[1, 1, 2, 2, 3, 3, 3]
    )


def context(current, area=3, attacker=1):
    row = current["areas"][area - 1]
    return {
        "generation": current["generation"],
        "season": current["season"],
        "area_id": area,
        "area_epoch": row["epoch"],
        "defender_faction": row["faction_id"],
        "attacker_faction": attacker,
    }


def test_default_rules_are_explicit_test_policy_and_all_regions_have_eight_bases():
    current = state()
    war.validate(current)
    assert current["policy"] == "configurable-test-war-v1"
    assert len(current["areas"]) == 7
    assert all(a["ap"] == 800_000 and war.level(a, current["rules"]) == 0 for a in current["areas"])


def test_new_team_can_join_faction_after_rollover_without_an_enemy_home_region(tmp_path):
    store = TeamStore(tmp_path)
    store.start_test_war(settings={}, owners=[1, 1, 2, 2, 3, 3, 3], now=1000)
    store.advance_test_war(2800)
    store.advance_test_war(2920)
    store.create(
        online_id="Pilot",
        player_id=11,
        name="Example",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=1,
    )
    replies = []
    peer = GameSession(b"pilot000", bytes(16), "Pilot", 11)
    for season in (3, 4):
        join_faction(
            peer,
            GameMessage(0, bytes(4), proto_varint(1, 1) + proto_varint(2, season)),
            team_store=store,
            telemetry=SimpleNamespace(emit=lambda *a, **k: None),
            reply=replies.append,
        )
        assert len(replies) == int(season == 4)
    team = store.team_for("Pilot")
    assert team["current_faction_id"] == 1 and team["territory_area_id"] == 5
    assert store.promote_faction("Pilot")["territory_area_id"] == 5


@pytest.mark.parametrize(
    "settings",
    [
        {"unknown": 1},
        {"base_ap": True},
        {"base_ap": "100"},
        {"base_ap": 0},
        {"normal_win_damage": -1},
        {"normal_win_damage": 80_000_001},
        {"verdict_day_threshold": 100_001},
        {"grace_seconds": 0},
        {"war_seconds": 59},
    ],
)
def test_invalid_rules_fail_closed(settings):
    with pytest.raises(ValueError):
        state(**settings)


def test_damage_advances_all_eight_bases_then_captures_without_mutating_input():
    current = original = state(normal_win_damage=100_000)
    before = deepcopy(original)
    for number in range(1, 9):
        frozen = context(current)
        current, receipt = war.impact(
            current, frozen, winning_faction=1, boss=False, now=1000 + number
        )
        assert receipt["applied"]
        row = current["areas"][2]
        assert war.level(row, current["rules"]) == (number if number < 8 else 0)
    assert original == before
    assert current["areas"][2]["faction_id"] == 1
    assert current["areas"][2]["ap"] == 800_000
    assert current["areas"][2]["epoch"] == 1
    assert current["phase"] == 2


@pytest.mark.parametrize(
    "change",
    [
        {"generation": "02" * 16},
        {"season": 2},
        {"area_epoch": 1},
        {"defender_faction": 3},
        {"attacker_faction": 2},
    ],
)
def test_old_or_friendly_context_cannot_damage_current_owner(change):
    current = state()
    updated, receipt = war.impact(
        current, context(current) | change, winning_faction=1, boss=False, now=1001
    )
    assert updated == current and not receipt["applied"]


def test_defender_win_does_not_damage_own_territory():
    current = state()
    updated, receipt = war.impact(
        current, context(current), winning_faction=2, boss=False, now=1001
    )
    assert updated == current and not receipt["applied"]


def test_deadline_loses_only_current_base_and_does_not_repeat():
    current = state(normal_win_damage=75_000)
    current, _ = war.impact(current, context(current), winning_faction=1, boss=False, now=1001)
    assert current["areas"][2]["verdict_deadline"] == 1121
    assert war.advance(current, 1120) == current
    advanced = war.advance(current, 1121)
    row = advanced["areas"][2]
    assert row["ap"] == 700_000 and row["faction_id"] == 2
    assert row["verdict_deadline"] is None
    assert war.advance(advanced, 1122) == advanced


def test_crossing_a_base_cannot_carry_its_old_deadline_forward():
    current = state(normal_win_damage=75_000, boss_win_damage=100_000)
    current, _ = war.impact(current, context(current), winning_faction=1, boss=False, now=1001)
    current, _ = war.impact(current, context(current), winning_faction=1, boss=True, now=1010)
    assert current["areas"][2]["ap"] == 625_000
    assert current["areas"][2]["verdict_deadline"] == 1130


def test_final_base_deadline_transfers_area_to_recorded_attacker():
    current = state(normal_win_damage=775_000)
    current, _ = war.impact(current, context(current), winning_faction=1, boss=False, now=1001)
    advanced = war.advance(current, 1121)
    assert advanced["areas"][2]["faction_id"] == 1
    assert advanced["areas"][2]["ap"] == 800_000


def test_complete_conquest_grace_and_next_season_cycle():
    current = state(boss_win_damage=800_000)
    for index in range(3, 8):
        current, _ = war.impact(
            current, context(current, index), winning_faction=1, boss=True, now=1010 + index
        )
    assert current["phase"] == 0
    assert current["last_result"][0] == {"faction_id": 1, "areas": 7, "ap": 5_600_000, "rank": 1}
    assert current["ends_at"] == 1137
    next_season = war.advance(current, 1137)
    assert next_season["season"] == 4 and next_season["phase"] == 2
    assert [a["faction_id"] for a in next_season["areas"]] == [2, 2, 3, 3, 1, 1, 1]
    assert next_season["last_result"] == current["last_result"]


def test_clock_expiry_is_once_only_and_offline_time_does_not_invent_wars():
    current = state()
    expired = war.advance(current, 9999)
    assert expired["phase"] == 0 and expired["season"] == 3
    assert expired["ends_at"] == 10119
    assert war.advance(expired, 9999) == expired


def test_deadline_after_war_end_cannot_change_final_standings_during_downtime():
    current = state(war_seconds=60)
    current, _ = war.impact(current, context(current), winning_faction=1, boss=False, now=1001)
    assert current["areas"][2]["verdict_deadline"] == 1121
    expired = war.advance(current, 9999)
    assert expired["phase"] == 0
    assert expired["areas"][2]["ap"] == 725_000


def test_opt_in_state_persists_without_resetting_profiles_or_wallets(tmp_path):
    store = TeamStore(tmp_path)
    before = deepcopy(store.state)
    assert store.test_war_snapshot() is None
    started = store.start_test_war(settings={}, owners=[1, 1, 2, 2, 3, 3, 3], now=1000)
    assert {k: v for k, v in store.state.items() if k != "test_war"} == before
    assert TeamStore(tmp_path).test_war_snapshot() == started
    with pytest.raises(ValueError, match="already exists"):
        store.start_test_war(settings={}, owners=[1, 1, 2, 2, 3, 3, 3], now=1001)
    store.advance_test_war(2800)
    assert TeamStore(tmp_path).test_war_snapshot()["phase"] == 0
    store.advance_test_war(2920)
    assert TeamStore(tmp_path).world_season_id() == 4


def test_world_wire_uses_current_owner_ap_and_one_based_city_thresholds():
    current = state()
    now = datetime.fromtimestamp(1000, timezone.utc)
    basic = decode_proto(_world_basic_info(now, war=current))
    areas = [decode_proto(f.value) for f in basic if f.number == 4]
    assert len(areas) == 7
    assert [first_field(a, 2).value for a in areas] == [1, 1, 2, 2, 3, 3, 3]
    for area in areas:
        assert first_field(area, 3).value == 800_000
        cities = [decode_proto(f.value) for f in area if f.number == 4]
        assert [(first_field(c, 1).value, first_field(c, 2).value) for c in cities] == [
            (i, (8 - i) * 100_000) for i in range(1, 9)
        ]
    current, _ = war.impact(current, context(current), winning_faction=1, boss=False, now=1001)
    wire = decode_proto(_world_current_info(now, war=current))
    region = decode_proto([f.value for f in wire if f.number == 4][2])
    assert first_field(region, 2).value == 725_000


def payment(store):
    store.remember_player("Pilot", 11)
    store.synchronize_balance("Pilot", 1000)
    plan = WorldBattleRewardPlan(
        player_id=11,
        basic_reward=100,
        special_reward=0,
        group_cost_reward_1=0,
        group_cost_reward_2=0,
        personal_reward=100,
        team_rank_tier=1,
        team_rank_subdivision=1,
        team_rank_progress=0,
        team_rank_threshold=1000,
    )
    return world_wallet_payment("Pilot", plan, expenses=0)


def test_damage_and_wallet_share_one_durable_replay_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr("acvd_server.teams.time.time", lambda: 1001)
    store = TeamStore(tmp_path)
    pay = payment(store)
    current = store.start_test_war(settings={}, owners=[1, 1, 2, 2, 3, 3, 3], now=1000)
    intent = {"context": context(current), "winning_faction": 1, "boss": False}
    result = store.settle_world_battle_money(
        "ab" * 32, (pay,), policy_version="test", war_impact=intent
    )
    assert result["war_impact"]["damage"] == 75_000 and store.balance("Pilot") == 1100
    reloaded = TeamStore(tmp_path)
    duplicate = reloaded.settle_world_battle_money(
        "ab" * 32, (pay,), policy_version="test", war_impact=intent
    )
    assert not duplicate["applied"]
    assert reloaded.test_war_snapshot()["areas"][2]["ap"] == 725_000
    assert reloaded.balance("Pilot") == 1100
    with pytest.raises(ValueError, match="test-war context"):
        reloaded.settle_world_battle_money(
            "ab" * 32, (pay,), policy_version="test", war_impact=intent | {"boss": True}
        )


def test_failed_settlement_write_cannot_apply_only_ap_or_only_wallet(tmp_path, monkeypatch):
    monkeypatch.setattr("acvd_server.teams.time.time", lambda: 1001)
    store = TeamStore(tmp_path)
    pay = payment(store)
    current = store.start_test_war(settings={}, owners=[1, 1, 2, 2, 3, 3, 3], now=1000)
    before, disk = deepcopy(store.state), store.state_path.read_bytes()
    original = Path.replace

    def fail(path, *args, **kwargs):
        if path == store.state_path.with_suffix(".json.tmp"):
            raise OSError("injected war commit failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="war commit"):
        store.settle_world_battle_money(
            "ac" * 32,
            (pay,),
            policy_version="test",
            war_impact={"context": context(current), "winning_faction": 1, "boss": False},
        )
    assert store.state == before and store.state_path.read_bytes() == disk


def test_next_season_keeps_wallets_and_uses_valid_faction_and_area_preferences(tmp_path):
    store = TeamStore(tmp_path)
    team = store.create(
        online_id="Pilot",
        player_id=11,
        name="Squad",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=2,
    )
    store.set_faction("Pilot", 1, 3)
    store.synchronize_balance("Pilot", 12345)
    store.update_team_properties("Pilot", scalar_fields={26: 2, 27: 2})
    store.start_test_war(settings={}, owners=[1, 1, 2, 2, 3, 3, 3], now=1000)
    assert store.team_for("Pilot")["current_faction_id"] == 3
    store.advance_test_war(2800)
    store.advance_test_war(2920)
    selected = store.team_for("Pilot")
    assert selected["team_id"] == team["team_id"]
    assert selected["current_faction_id"] == 2 and selected["territory_area_id"] == 2
    assert store.balance("Pilot") == 12345


@pytest.mark.parametrize(
    "battle_type,kind,mission_id",
    [
        (1, None, 3500),
        (2, None, None),
        (3, None, None),
        (4, ACTIVE_SINGLE_TEAM, None),
        (5, ACTIVE_SINGLE_TEAM, None),
        (6, ACTIVE_SINGLE_TEAM, None),
        (6, INTERWAR_BOSS, None),
    ],
)
def test_test_war_exact_map_metadata_is_extermination_only(battle_type, kind, mission_id):
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        send_push=lambda *a, **k: None,
        telemetry=SimpleNamespace(emit=lambda *a, **k: None),
        room_log_paths=[],
        get_test_war=state,
    )
    area, locked, mode, profile = coordinator._select_cq_combat_area(
        4, match_id=1, battle_type=battle_type, kind=kind
    )
    assert (area, locked, mode) == (4, False, "test_war")
    if mission_id is None:
        assert profile is None
    else:
        assert profile["mission_id"] == mission_id


def test_boss_assignment_uses_enemy_area_and_requires_current_world_view():
    current = state()
    player = GameSession(
        b"pilot000", bytes(16), "Pilot", 11, established=True, peer=("127.0.0.1", 9999)
    )
    events, pushes = [], []
    telemetry = SimpleNamespace(emit=lambda name, **data: events.append((name, data)))
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        send_push=lambda *a, **k: pushes.append((a, k)),
        telemetry=telemetry,
        room_log_paths=[],
        get_test_war=lambda: current,
    )
    prep = {
        "session": player,
        "team_id": 1,
        "faction_id": 1,
        "territory_area_id": 1,
        "criteria": {1: 3, 2: 2, 3: 2, 8: 3},
        "world_phase": 2,
        "prepare_id": 1,
        "squad": {11: (player, 1)},
        "claimed_player_ids": [11],
        "claimed_player_roles": [0],
    }
    coordinator.preparations["Pilot"] = prep
    assert not coordinator._release_active_single_team(prep)
    assert not pushes and not coordinator.matches
    get_world_current_info(
        player,
        GameMessage(GET_WORLD_CURRENT_INFO, b"0001", b""),
        telemetry=telemetry,
        reply=lambda _: None,
        war=current,
    )
    assert coordinator._release_active_single_team(prep)
    match = coordinator.matches[1]
    assert match["battle_type"] == 6
    assert match["cq_map_mission_id"] == 0 and match["cq_map_name"] == ""
    assert match["combat_area_id"] in (3, 4, 5, 6, 7)
    assert match["war_context"]["defender_faction"] in (2, 3)
    assert match["war_context"]["attacker_faction"] == 1


@pytest.mark.asyncio
async def test_stale_world_pair_does_not_retire_search_timers_or_allocate_match():
    current = state()
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        send_push=lambda *a, **k: pytest.fail("Stale pair must not be released"),
        telemetry=SimpleNamespace(emit=lambda *a, **k: None),
        room_log_paths=[],
        get_test_war=lambda: current,
    )
    preparations = []
    for number, name in enumerate(("Attacker", "Defender"), 1):
        peer = GameSession(
            name.encode().ljust(8, b"0"),
            bytes(16),
            name,
            number,
            established=True,
            peer=("127.0.0.1", 9990 + number),
        )
        prep = {
            "session": peer,
            "prepare_id": number,
            "team_id": number,
            "faction_id": number,
            "territory_area_id": number,
            "criteria": {1: 3, 2: 2, 3: 1, 8: number},
        }
        coordinator.preparations[name] = prep
        coordinator._schedule_single_team_fallback(prep)
        preparations.append(prep)
    tasks = [p["single_team_fallback_task"] for p in preparations]
    try:
        coordinator._match_preparation(preparations[1])
        assert not coordinator.matches and coordinator.next_match_id == 1
        assert [p["single_team_fallback_task"] for p in preparations] == tasks
        assert all(not task.cancelling() for task in tasks)
    finally:
        for prep in preparations:
            coordinator._cancel_single_team_fallback(prep)
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_admin_activation_needs_confirmation_and_empty_live_server(tmp_path, monkeypatch):
    monkeypatch.setattr("acvd_server.server.time.time", lambda: 1000)
    store = TeamStore(tmp_path / "teams")
    player = GameSession(
        b"pilot000", bytes(16), "Pilot", 11, established=True, peer=("127.0.0.1", 9999)
    )
    instance = SimpleNamespace(
        teams=store,
        game_sessions={b"pilot000": player},
        game_protocol=None,
        telemetry=Telemetry(tmp_path / "audit.jsonl"),
        _tick_test_war=lambda: None,
    )
    endpoint = AdminEndpoint(
        tmp_path / "operator.sock",
        lambda: {},
        OperatorSettings(tmp_path / "settings.json"),
        instance.telemetry,
        start_test_war=lambda settings, owners: AcvdServer._start_test_war(
            instance, settings, owners
        ),
    )

    async def request(**fields):
        reader, writer = await asyncio.open_unix_connection(tmp_path / "operator.sock")
        writer.write(json.dumps({"command": "start_test_war", **fields}).encode() + b"\n")
        await writer.drain()
        result = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return result

    await endpoint.start()
    try:
        assert not (await request())["ok"]
        assert not (await request(confirmed=True))["ok"]
        assert store.test_war_snapshot() is None
        instance.game_sessions.clear()
        result = await request(confirmed=True, rules={"war_seconds": 60})
        assert result["ok"] and result["data"]["rules"]["war_seconds"] == 60
        assert not (await request(confirmed=True))["ok"]
    finally:
        await endpoint.close()


def test_war_damage_credits_mercenary_employer_not_their_opposing_social_team():
    employer = GameSession(b"employ00", bytes(16), "Employer", 11)
    mercenary = GameSession(b"merc0000", bytes(16), "Mercenary", 22)
    defender = GameSession(b"defend00", bytes(16), "Defender", 33)
    members = [
        {
            "session": employer,
            "team_id": 1,
            "faction_id": 1,
            "pay_policy": "equal_distribution",
            "squad": {11: (employer, 1), 22: (mercenary, 2)},
            "claimed_player_ids": [11, 22],
            "claimed_player_roles": [0, 2],
        },
        {
            "session": defender,
            "team_id": 2,
            "faction_id": 2,
            "pay_policy": "equal_distribution",
            "squad": {33: (defender, 2)},
            "claimed_player_ids": [33],
            "claimed_player_roles": [0],
        },
    ]
    report = CombatReport(
        header=(1, 2, 1, 0, 0, 0, 0, 0),
        units=(
            (11, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0),
            (22, 0, 0, 1, 0, 0, 0, 2, 0, 0, 0, 0),
            (33, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0),
        ),
        subtargets=(),
        digest="00" * 32,
    )
    match = {"members": members, "criteria": {3: 1}, "war_context": context(state())}
    intent = MatchmakingCoordinator.war_impact(match, report)
    assert intent["winning_faction"] == 1 and not intent["boss"]
    updated, audit = war.impact(
        state(), intent["context"], winning_faction=intent["winning_faction"], boss=False, now=1001
    )
    assert audit["applied"] and updated["areas"][2]["ap"] == 725_000


@pytest.mark.asyncio
async def test_real_isolated_service_runs_war_grace_rollover_and_stops_timer(tmp_path, monkeypatch):
    clock = [1_800_000_000]
    monkeypatch.setattr("acvd_server.server.time.time", lambda: clock[0])
    (tmp_path / "keys").mkdir()
    (tmp_path / "native").mkdir()
    generate_rsa_keypair(tmp_path / "keys/server-private.pem", tmp_path / "keys/publickey.pem")
    shutil.copy2(
        Path(__file__).resolve().parents[1] / "native/libacvd_cwc.so",
        tmp_path / "native/libacvd_cwc.so",
    )
    server = AcvdServer(
        ServerConfig(
            project_root=tmp_path,
            bind_host="127.0.0.1",
            advertised_host="127.0.0.1",
            login_ports=[0],
            auth_port=0,
            cdn_port=0,
            game_port=0,
        )
    )
    server.operator_settings.update({"cq_map_mode": "locked_pool", "cq_map_pool": [3200]})
    saved_settings = server.operator_settings.path.read_bytes()
    await server.start()

    async def request(value):
        reader, writer = await asyncio.open_unix_connection(server.admin_endpoint.path)
        writer.write(json.dumps(value).encode() + b"\n")
        await writer.drain()
        result = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return result

    try:
        started = await request(
            {
                "command": "start_test_war",
                "confirmed": True,
                "rules": {"war_seconds": 60, "grace_seconds": 30},
            }
        )
        assert started["ok"] and server._war_tick_handle is not None
        assert server.game_protocol.world_phase() == 2
        assert not (await request({"command": "configure", "settings": {"world_mode": "grace"}}))[
            "ok"
        ]
        clock[0] += 60
        server._tick_test_war()
        assert server.game_protocol.world_phase() == 0
        clock[0] += 30
        server._tick_test_war()
        snapshot = (await request({"command": "status"}))["data"]
        assert snapshot["world"]["season"] == 4 and snapshot["world"]["phase"] == 2
        assert snapshot["world"]["mode"] == "test_war"
        protocol, replies = server.game_protocol, []
        protocol._send_game_reply = lambda *args: replies.append(args[-1])
        session = GameSession(b"fixture0", bytes(16), "Fixture", 11)
        protocol._dispatch_game_message(
            session, None, GameMessage(GET_WORLD_BASIC_INFO, b"0001", b"")
        )
        basic = decode_proto(first_field(decode_proto(replies[-1]), 1).value)
        assert first_field(decode_proto(first_field(basic, 2).value), 1).value == 4
        assert sum(f.number == 4 for f in basic) == 7
        assert server.operator_settings.path.read_bytes() == saved_settings
    finally:
        await server.close()
    assert server._war_tick_handle is None
