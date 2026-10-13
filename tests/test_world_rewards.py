"""World reward wire envelope only; payout policy remains fail-closed."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from acvd_server.combat_reports import decode_combat_report
from acvd_server.protocol import decode_proto, proto_bytes, proto_varint
from acvd_server.rewards import (
    WorldBattleRewardPlan,
    WorldPayPolicy,
    WorldSubtargetSize,
    _world_battle_result_response,
    allocate_world_mercenary_reward,
    allocate_world_team_reward,
    faction_compensation_by_player,
    mercenary_expense_rate_percent,
    mercenary_team_point_reduction_percent,
    world_cash_reward_plan,
    world_interwar_boss_base_reward,
    world_pay_policy_for_team,
    world_reward_participants,
    world_subtarget_size,
    world_wallet_payment,
)
from acvd_server.teams import TeamStore
from acvd_server.udp import GameSession
from acvd_server.world_economy import (
    WORLD_REWARD_DEFAULTS,
    build_world_settlement,
    validate_world_reward_settings,
    world_result_identity,
)

WORLD_POLICY = "fixture-world-policy-v1"


def test_world_receipt_key_changes_with_the_server_match_not_report_bytes():
    raw = bytes.fromhex(Path("tests/fixtures/operator-combat-result.hex").read_text())
    report = decode_combat_report(raw)
    first = {"result_sha256": report.digest, "reward_nonce": "01" * 16}
    second = {"result_sha256": report.digest, "reward_nonce": "02" * 16}
    first_key = world_result_identity(first, report)
    assert first_key == world_result_identity(first, report)
    assert first_key != world_result_identity(second, report)
    with pytest.raises(ValueError, match="nonce"):
        world_result_identity({"result_sha256": report.digest}, report)
    with pytest.raises(ValueError, match="retained battle"):
        world_result_identity({**first, "result_sha256": "00" * 32}, report)


def plan(**changes):
    values = {
        "player_id": 50627,
        "basic_reward": 100_000,
        "special_reward": 20_000,
        "group_cost_reward_1": 4_000,
        "group_cost_reward_2": 6_000,
        "personal_reward": 60_000,
        "team_rank_tier": 1,
        "team_rank_subdivision": 2,
        "team_rank_progress": 700,
        "team_rank_threshold": 1_000,
    }
    values.update(changes)
    return WorldBattleRewardPlan(**values)


def test_world_response_has_both_required_initialized_envelopes():
    outer = decode_proto(_world_battle_result_response(plan()))
    assert [(field.number, field.wire_type) for field in outer] == [(1, 2), (2, 2)]
    battle = {field.number: field.value for field in decode_proto(outer[0].value)}
    team_rank = {field.number: field.value for field in decode_proto(outer[1].value)}
    assert battle == {3: 100_000, 4: 20_000, 5: 4_000, 6: 6_000, 7: 60_000, 9: 50627}
    assert team_rank == {1: 1, 2: 2, 3: 700, 4: 1_000}


def test_world_response_writes_zero_presence_fields_instead_of_omitting_them():
    response = _world_battle_result_response(
        plan(
            basic_reward=0,
            special_reward=0,
            group_cost_reward_1=0,
            group_cost_reward_2=0,
            personal_reward=0,
            team_rank_tier=1,
            team_rank_subdivision=1,
            team_rank_progress=0,
            team_rank_threshold=1_000,
        )
    )
    outer = decode_proto(response)
    assert {field.number for field in decode_proto(outer[0].value)} == {3, 4, 5, 6, 7, 9}
    assert {field.number for field in decode_proto(outer[1].value)} == {1, 2, 3, 4}


@pytest.mark.parametrize(
    "changes",
    [
        {"player_id": 0},
        {"player_id": True},
        {"basic_reward": -1},
        {"personal_reward": 1 << 32},
        {"team_rank_threshold": "1000"},
        {"team_rank_tier": 0},
        {"team_rank_tier": 2},
        {"team_rank_subdivision": 28},
        {"team_rank_progress": 1_000},
    ],
)
def test_world_response_rejects_unbound_or_out_of_range_values(changes):
    with pytest.raises(ValueError):
        _world_battle_result_response(plan(**changes))


def test_world_response_does_not_accept_a_story_plan_or_raw_report_values():
    with pytest.raises(ValueError):
        _world_battle_result_response(None)


def test_cash_only_world_plan_uses_canonical_reimbursement_and_team_rank():
    cash = world_cash_reward_plan(
        player_id=50627,
        basic_reward=100_000,
        special_reward=20_000,
        combined_group_cost_reward=10_000,
        personal_reward=60_000,
        team_rank=(1, 1, 600, 1_000),
    )
    outer = decode_proto(_world_battle_result_response(cash))
    battle = {field.number: field.value for field in decode_proto(outer[0].value)}
    team_rank = {field.number: field.value for field in decode_proto(outer[1].value)}
    assert battle == {3: 100_000, 4: 20_000, 5: 10_000, 6: 0, 7: 60_000, 9: 50627}
    assert team_rank == {1: 1, 2: 1, 3: 600, 4: 1_000}


@pytest.mark.parametrize(
    "changes",
    [
        {"player_id": 0},
        {"basic_reward": -1},
        {"combined_group_cost_reward": 1 << 32},
    ],
)
def test_cash_only_world_plan_rejects_unbound_or_out_of_range_values(changes):
    values = {
        "player_id": 50627,
        "basic_reward": 100_000,
        "special_reward": 0,
        "combined_group_cost_reward": 0,
        "personal_reward": 100_000,
        "team_rank": (1, 1, 0, 1_000),
    }
    values.update(changes)
    with pytest.raises(ValueError):
        world_cash_reward_plan(**values)


@pytest.mark.parametrize(
    "area_id,mission_id",
    [(1, 3790), (2, 3690), (3, 3290), (4, 3590), (5, 3490), (6, 3390), (7, 3190)],
)
def test_grace_special_area_maps_to_exact_online_boss_base_pay(area_id, mission_id):
    reward = world_interwar_boss_base_reward(
        {
            "kind": "interwar_boss",
            "world_phase": 0,
            "battle_type": 6,
            "stored_area_id": area_id,
            "criteria": {1: 3, 2: 0, 3: 2},
        }
    )
    assert reward.mission_id == mission_id
    assert reward.area_id == area_id and reward.basic_reward == 1_000_000


@pytest.mark.parametrize("area_id", range(1, 8))
@pytest.mark.parametrize("boss_setting", [1_000_000, 750_000])
def test_grace_boss_settlement_has_independent_adjustable_base(area_id, boss_setting):
    native = decode_combat_report(
        bytes.fromhex(Path("tests/fixtures/operator-combat-result.hex").read_text())
    )
    header = (native.header[0], 1, 2, *native.header[3:])
    payload = b"".join(proto_varint(number, value) for number, value in enumerate(header, 1))
    payload += b"".join(
        proto_bytes(
            9,
            b"".join(proto_varint(number, value) for number, value in enumerate(unit, 1)),
        )
        for unit in native.units
    )
    report = decode_combat_report(payload)
    fighter = GameSession(b"fighter1", bytes(16), "Fighter", 50627)
    operator = GameSession(b"operator", bytes(16), "Operator", 451473)
    squad = {50627: (fighter, 1), 451473: (operator, 1)}
    preparation = {
        "session": fighter,
        "team_id": 1,
        "pay_policy": "equal_distribution",
        "claimed_player_ids": [50627, 451473],
        "claimed_player_roles": [0, 0],
        "squad": squad,
    }
    match = {
        "match_id": area_id,
        "reward_nonce": f"{area_id:02x}" * 16,
        "kind": "interwar_boss",
        "criteria": {1: 3, 2: 0, 3: 2},
        "world_phase": 0,
        "battle_type": 6,
        "stored_area_id": area_id,
        "squad": squad,
        "reward_participants": world_reward_participants(report, [preparation]),
        "result_sha256": report.digest,
    }

    class UnaffiliatedStore:
        @staticmethod
        def team_by_id(team_id):
            return {"team_id": team_id, "faction_id": 0}

    settings = {
        **WORLD_REWARD_DEFAULTS,
        "world_special_base_reward": 2_500_000,
        "world_boss_base_reward": boss_setting,
    }
    result = build_world_settlement(match, report, UnaffiliatedStore(), settings)
    assert result.audit["mission_class"] == "interwar_boss"
    assert result.audit["configured_base_pool"] == boss_setting
    assert f":b{boss_setting}:" in result.policy_version
    assert result.operator_win_player_ids == ()  # Grace does not progress medals.
    assert result.boss_win_player_ids == ()
    assert result.team_progress == result.team_medal_progress == ()
    payments = {payment["player_id"]: payment for payment in result.payments}
    assert [payments[player_id]["gross_earnings"] for player_id in (50627, 451473)] == [
        boss_setting // 2,
        boss_setting // 2,
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "active_single_team"},
        {"world_phase": 2},
        {"battle_type": 5},
        {"stored_area_id": 0},
        {"criteria": {1: 3, 2: 0, 3: 1}},
    ],
)
def test_online_boss_base_pay_rejects_other_world_contexts(change):
    match = {
        "kind": "interwar_boss",
        "world_phase": 0,
        "battle_type": 6,
        "stored_area_id": 3,
        "criteria": {1: 3, 2: 0, 3: 2},
    }
    match.update(change)
    with pytest.raises(ValueError, match="Online boss"):
        world_interwar_boss_base_reward(match)


def test_one_team_normal_burn_run_advances_rank_and_special():
    native = decode_combat_report(
        bytes.fromhex(Path("tests/fixtures/operator-combat-result.hex").read_text())
    )
    header = (native.header[0], 1, 2, *native.header[3:])
    payload = b"".join(proto_varint(number, value) for number, value in enumerate(header, 1))
    payload += b"".join(
        proto_bytes(
            9,
            b"".join(proto_varint(number, value) for number, value in enumerate(unit, 1)),
        )
        for unit in native.units
    )
    report = decode_combat_report(payload)
    fighter = GameSession(b"fighter1", bytes(16), "Fighter", 50627)
    operator = GameSession(b"operator", bytes(16), "Operator", 451473)
    squad = {50627: (fighter, 1), 451473: (operator, 1)}
    preparation = {
        "session": fighter,
        "team_id": 1,
        "pay_policy": "equal_distribution",
        "claimed_player_ids": [50627, 451473],
        "claimed_player_roles": [0, 0],
        "squad": squad,
    }
    match = {
        "match_id": 1,
        "reward_nonce": "01" * 16,
        "kind": "active_single_team",
        "criteria": {1: 3, 2: 2, 3: 1},
        "world_phase": 2,
        "battle_type": 5,
        "is_intra_faction": None,
        "squad": squad,
        "reward_participants": world_reward_participants(report, [preparation]),
        "result_sha256": report.digest,
    }

    class Store:
        @staticmethod
        def team_by_id(team_id):
            return {
                "team_id": team_id,
                "faction_id": 1,
                "team_rank_score": 900,
                "team_rank_floor_position": 0,
                "special_sortie_progress": 2,
            }

    result = build_world_settlement(match, report, Store(), dict(WORLD_REWARD_DEFAULTS))
    assert result.team_progress == ({"team_id": 1, "rank_delta": 200, "special_delta": 1},)
    assert result.team_medal_progress == (
        {
            "team_id": 1,
            "ac_kills_delta": 0,
            "fame_delta": 200,
            "world_sorties_delta": 1,
            "pvp_wins_delta": 0,
        },
    )
    for payment in result.payments:
        rank = decode_proto(decode_proto(bytes.fromhex(payment["response_hex"]))[1].value)
        assert [field.value for field in rank] == [1, 3, 50, 1000]


@pytest.mark.parametrize(
    "reported_value,size",
    [(80_000, WorldSubtargetSize.SMALL), (120_000, WorldSubtargetSize.LARGE)],
)
def test_subtarget_report_values_are_damage_credit_sizes_not_cash(reported_value, size):
    assert world_subtarget_size(reported_value) is size


@pytest.mark.parametrize("reported_value", [0, 100_000, 150_000, True, "120000"])
def test_subtarget_size_rejects_unknown_or_noninteger_values(reported_value):
    with pytest.raises(ValueError, match="subtarget damage credit"):
        world_subtarget_size(reported_value)


def world_payment(store, online_id, player_id, **changes):
    store.remember_player(online_id, player_id)
    store.synchronize_balance(online_id, changes.pop("balance", 1_000_000))
    values = {"group_cost_reward_1": 0, "group_cost_reward_2": 0}
    values.update(changes)
    return world_wallet_payment(
        online_id, plan(player_id=player_id, **values), expenses=10_000
    )


def test_world_wallet_payment_credits_personal_and_reimbursement_not_breakdown():
    payment = world_wallet_payment(
        "Pilot",
        plan(
            player_id=11,
            basic_reward=900_000,
            special_reward=800_000,
            personal_reward=60_000,
            group_cost_reward_1=4_000,
            group_cost_reward_2=6_000,
        ),
        expenses=13_000,
    )
    assert payment["gross_earnings"] == 70_000
    assert payment["expenses"] == 13_000
    assert decode_proto(bytes.fromhex(payment["response_hex"]))


def test_world_settlement_commits_all_wallets_and_exact_replies_once(tmp_path):
    store = TeamStore(tmp_path / "teams")
    first = world_payment(store, "First", 11, personal_reward=60_000)
    second = world_payment(
        store,
        "Second",
        22,
        balance=500_000,
        personal_reward=50_000,
        group_cost_reward_1=10_000,
    )
    digest = "12" * 32
    receipt = store.settle_world_battle_money(
        digest, (second, first), policy_version=WORLD_POLICY
    )
    assert receipt["applied"] is True
    assert [row["player_id"] for row in receipt["participants"]] == [11, 22]
    assert store.balance("First") == 1_050_000
    assert store.balance("Second") == 550_000

    reloaded = TeamStore(store.root)
    replay = reloaded.settle_world_battle_money(
        digest, (first, second), policy_version=WORLD_POLICY
    )
    assert replay["applied"] is False
    assert replay["participants"] == receipt["participants"]
    assert reloaded.balance("First") == 1_050_000
    assert reloaded.balance("Second") == 550_000
    assert len(reloaded.state["world_battle_settlements"]) == 1
    assert reloaded.world_battle_settlement(digest)["participants"] == receipt["participants"]

    # The exact committed response outlives a later missing wallet/mapping.
    updated = deepcopy(reloaded.state)
    updated["player_profiles"]["First"].pop("balance")
    updated["player_profiles"]["First"].pop("player_id")
    reloaded.commit_state(updated)
    retry_after_loss = reloaded.settle_world_battle_money(
        digest, (second, first), policy_version=WORLD_POLICY
    )
    assert retry_after_loss["applied"] is False
    assert retry_after_loss["participants"] == receipt["participants"]


def test_world_reward_saturates_wallet_without_losing_atomic_reply(tmp_path):
    store = TeamStore(tmp_path / "teams")
    payment = world_payment(
        store,
        "Pilot",
        11,
        balance=999_999_900,
        personal_reward=60_000,
    )
    receipt = store.settle_world_battle_money(
        "83" * 32, (payment,), policy_version=WORLD_POLICY
    )
    (row,) = receipt["participants"]
    assert store.balance("Pilot") == row["balance"] == 999_999_999
    assert row["nominal_delta"] == 50_000
    assert row["delta"] == 99
    assert row["discarded_over_cap"] == 49_901
    replay = TeamStore(store.root).world_battle_settlement("83" * 32)
    assert replay["participants"] == receipt["participants"]


def test_world_expense_can_create_signed_debt(tmp_path):
    store = TeamStore(tmp_path / "teams")
    payment = world_payment(store, "Pilot", 11, balance=0, personal_reward=0)
    receipt = store.settle_world_battle_money(
        "84" * 32, (payment,), policy_version=WORLD_POLICY
    )
    (row,) = receipt["participants"]
    assert row["nominal_delta"] == row["delta"] == -10_000
    assert store.balance("Pilot") == row["balance"] == -10_000
    assert TeamStore(store.root).balance("Pilot") == -10_000


def test_world_expense_saturates_at_retail_debt_floor(tmp_path):
    store = TeamStore(tmp_path / "teams")
    payment = world_payment(store, "Pilot", 11, balance=-999_999_900, personal_reward=0)
    receipt = store.settle_world_battle_money(
        "85" * 32, (payment,), policy_version=WORLD_POLICY
    )
    (row,) = receipt["participants"]
    assert store.balance("Pilot") == row["balance"] == -999_999_999
    assert row["nominal_delta"] == -10_000
    assert row["delta"] == -99
    assert row["forgiven_below_debt_floor"] == 9_901


def test_world_settlement_rejects_one_unknown_wallet_without_partial_payment(tmp_path):
    store = TeamStore(tmp_path / "teams")
    first = world_payment(store, "First", 11)
    store.remember_player("Second", 22)
    second = world_wallet_payment("Second", plan(player_id=22), expenses=10_000)
    before = deepcopy(store.state)
    with pytest.raises(ValueError, match="every known wallet"):
        store.settle_world_battle_money(
            "34" * 32, (first, second), policy_version=WORLD_POLICY
        )
    assert store.state == before and store.balance("First") == 1_000_000


def test_world_settlement_failed_write_rolls_back_every_wallet(tmp_path, monkeypatch):
    store = TeamStore(tmp_path / "teams")
    first = world_payment(store, "First", 11)
    second = world_payment(store, "Second", 22)
    before, disk = deepcopy(store.state), store.state_path.read_bytes()
    original = Path.replace

    def fail(path, *args, **kwargs):
        if path == store.state_path.with_suffix(".json.tmp"):
            raise OSError("Injected World settlement failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail)
        with pytest.raises(OSError, match="World settlement failure"):
            store.settle_world_battle_money(
                "56" * 32, (first, second), policy_version=WORLD_POLICY
            )
    assert store.state == before and store.state_path.read_bytes() == disk


def test_world_operator_progress_commits_with_wallet_once_and_survives_reload(tmp_path):
    store = TeamStore(tmp_path / "teams")
    first = world_payment(store, "Fighter", 11, personal_reward=60_000)
    second = world_payment(store, "Operator", 22, personal_reward=50_000)
    digest = "78" * 32
    receipt = store.settle_world_battle_money(
        digest,
        (first, second),
        policy_version=WORLD_POLICY,
        operator_win_player_ids=(22,),
    )
    assert receipt["operator_win_player_ids"] == [22]
    assert receipt["operator_win_progress"] == [
        {"player_id": 22, "previous_wins": 0, "current_wins": 1}
    ]
    assert "observed_operator_wins" not in store.player_profile("Fighter")
    assert store.player_profile("Operator")["observed_operator_wins"] == 1

    reloaded = TeamStore(store.root)
    replay = reloaded.settle_world_battle_money(
        digest,
        (second, first),
        policy_version=WORLD_POLICY,
        operator_win_player_ids=(22,),
    )
    assert replay["applied"] is False
    assert reloaded.player_profile("Operator")["observed_operator_wins"] == 1
    assert len(reloaded.state["world_battle_settlements"]) == 1


def test_world_player_leaderboard_progress_is_atomic_and_idempotent(tmp_path):
    store = TeamStore(tmp_path / "teams")
    fighter = world_payment(store, "Fighter", 11, personal_reward=60_000)
    mercenary = world_payment(store, "Mercenary", 22, personal_reward=50_000)
    progress = (
        {
            "player_id": 11,
            "ac_destructions_delta": 2,
            "mercenary_sorties_delta": 0,
            "mercenary_wins_delta": 0,
        },
        {
            "player_id": 22,
            "ac_destructions_delta": 1,
            "mercenary_sorties_delta": 1,
            "mercenary_wins_delta": 1,
        },
    )
    digest = "79" * 32
    receipt = store.settle_world_battle_money(
        digest,
        (fighter, mercenary),
        policy_version=WORLD_POLICY,
        player_ranking_progress=progress,
    )
    assert receipt["player_ranking_progress"] == list(progress)
    assert store.player_profile("Fighter")["observed_ac_destructions"] == 2
    mercenary_profile = store.player_profile("Mercenary")
    assert mercenary_profile["observed_ac_destructions"] == 1
    assert mercenary_profile["observed_mercenary_sorties"] == 1
    assert mercenary_profile["observed_mercenary_wins"] == 1
    assert mercenary_profile["mercenary_rank"] == 0

    reloaded = TeamStore(store.root)
    replay = reloaded.settle_world_battle_money(
        digest,
        (mercenary, fighter),
        policy_version=WORLD_POLICY,
        player_ranking_progress=progress,
    )
    assert replay["applied"] is False
    assert reloaded.player_profile("Fighter")["observed_ac_destructions"] == 2
    assert reloaded.player_profile("Mercenary")["observed_mercenary_sorties"] == 1


def test_verified_operator_threshold_awards_medal_in_same_transaction(tmp_path):
    store = TeamStore(tmp_path / "teams")
    payment = world_payment(store, "Operator", 22)
    updated = deepcopy(store.state)
    updated["player_profiles"]["Operator"]["observed_operator_wins"] = 2
    store.commit_state(updated)
    receipt = store.settle_world_battle_money(
        "81" * 32,
        (payment,),
        policy_version=WORLD_POLICY,
        operator_win_player_ids=(22,),
    )
    assert receipt["operator_win_progress"] == [
        {"player_id": 22, "previous_wins": 2, "current_wins": 3}
    ]
    profile = store.player_profile("Operator")
    assert profile["observed_operator_wins"] == 3
    assert [r["medal_id"] for r in profile["player_medals"]["records"]] == [26]
    assert receipt["player_medal_awards"] == [{"player_id": 22, "medal_ids": [26]}]


def test_player_awards_wallet_and_team_credit_commit_once_and_survive_reload(tmp_path):
    store = TeamStore(tmp_path / "teams")
    store.create(online_id="Merc", player_id=22, name="Team", operation_base=0,
                 language=1, play_type=1, enlistment_method=2)
    payment = world_payment(store, "Merc", 22)
    updated = deepcopy(store.state)
    updated["player_profiles"]["Merc"].update(
        observed_mercenary_sorties=9, observed_mercenary_wins=9,
        observed_world_boss_wins=4,
    )
    store.commit_state(updated)
    progress = ({"player_id": 22, "ac_destructions_delta": 0,
                 "mercenary_sorties_delta": 1, "mercenary_wins_delta": 1},)
    receipt = store.settle_world_battle_money(
        "f1" * 32, (payment,), policy_version=WORLD_POLICY,
        boss_win_player_ids=(22,), player_ranking_progress=progress,
    )
    assert receipt["player_medal_awards"] == [{"player_id": 22, "medal_ids": [33, 34, 35, 36]}]
    assert store.team_for("Merc")["team_player_medals"] == 4
    before = deepcopy(store.state)
    reloaded = TeamStore(store.root)
    replay = reloaded.settle_world_battle_money(
        "f1" * 32, (payment,), policy_version=WORLD_POLICY,
        boss_win_player_ids=(22,), player_ranking_progress=progress,
    )
    assert not replay["applied"]
    assert reloaded.state == before
    assert reloaded.refresh_player_medal_state("Merc", 22) == ()
    assert reloaded.state == before


def test_failed_medal_settlement_write_grants_nothing(tmp_path, monkeypatch):
    store = TeamStore(tmp_path / "teams")
    payment = world_payment(store, "Pilot", 22)
    updated = deepcopy(store.state)
    updated["player_profiles"]["Pilot"]["observed_world_boss_wins"] = 2
    store.commit_state(updated)
    before, disk = deepcopy(store.state), store.state_path.read_bytes()
    def fail():
        raise OSError("test write failure")
    monkeypatch.setattr(store, "_save", fail)
    with pytest.raises(OSError):
        store.settle_world_battle_money("f2" * 32, (payment,), policy_version=WORLD_POLICY,
                                       boss_win_player_ids=(22,))
    assert store.state == before and store.state_path.read_bytes() == disk


def test_medal_derived_team_points_match_reward_response_projection(tmp_path):
    from acvd_server.team_progression import view

    store = TeamStore(tmp_path / "teams")
    team = store.create(online_id="Fighter", player_id=50627, name="Team", operation_base=0,
                        language=1, play_type=1, enlistment_method=2)
    tid = team["team_id"]
    store.join_team(online_id="Operator", player_id=451473, team_id=tid)
    for name, pid in (("Fighter", 50627), ("Operator", 451473)):
        store.remember_player(name, pid)
        store.synchronize_balance(name, 1000000)
    state = deepcopy(store.state)
    state["teams"][str(tid)]["team_player_medals"] = 99
    state["player_profiles"]["Operator"]["observed_operator_wins"] = 2
    store.commit_state(state)
    native = decode_combat_report(bytes.fromhex(Path("tests/fixtures/operator-combat-result.hex").read_text()))
    report = replace(native, header=(native.header[0], 1, 2, *native.header[3:]))
    fighter = GameSession(b"fighter1", bytes(16), "Fighter", 50627)
    operator = GameSession(b"operator", bytes(16), "Operator", 451473)
    squad = {50627: (fighter, tid), 451473: (operator, tid)}
    preparation = {"session": fighter, "team_id": tid, "pay_policy": "equal_distribution",
                   "claimed_player_ids": [50627, 451473], "claimed_player_roles": [0, 0], "squad": squad}
    match = {"match_id": 1, "reward_nonce": "f4" * 16, "kind": "active_single_team",
             "criteria": {1: 3, 2: 2, 3: 1}, "world_phase": 2, "is_intra_faction": None,
             "squad": squad, "result_sha256": report.digest,
             "reward_participants": world_reward_participants(report, [preparation])}
    before = deepcopy(store.state)
    planned = build_world_settlement(match, report, store, dict(WORLD_REWARD_DEFAULTS))
    assert store.state == before
    receipt = store.settle_world_battle_money(
        planned.result_digest, planned.payments, policy_version=planned.policy_version,
        operator_win_player_ids=planned.operator_win_player_ids,
        boss_win_player_ids=planned.boss_win_player_ids, team_progress=planned.team_progress,
        team_medal_progress=planned.team_medal_progress, player_ranking_progress=planned.player_ranking_progress,
    )
    assert store.team_by_id(tid)["team_player_medals"] == 100
    assert 22 in store.team_by_id(tid)["team_medal_ids"]
    for payment in receipt["participants"]:
        rank = next(f for f in decode_proto(bytes.fromhex(payment["response_hex"])) if f.number == 2)
        assert tuple(f.value for f in decode_proto(rank.value)) == view(store.team_by_id(tid)).wire_values


@pytest.mark.parametrize("ids", [(23,), (22, 22), (True,), [22]])
def test_invalid_boss_progress_rejects_without_any_grant(tmp_path, ids):
    store = TeamStore(tmp_path / "teams")
    payment = world_payment(store, "Pilot", 22)
    before = deepcopy(store.state)
    with pytest.raises(ValueError):
        store.settle_world_battle_money("f3" * 32, (payment,), policy_version=WORLD_POLICY,
                                       boss_win_player_ids=ids)
    assert store.state == before


def test_invalid_stored_operator_progress_rejects_whole_payment(tmp_path):
    store = TeamStore(tmp_path / "teams")
    payment = world_payment(store, "Operator", 22)
    updated = deepcopy(store.state)
    updated["player_profiles"]["Operator"]["observed_operator_wins"] = (1 << 32) - 1
    store.commit_state(updated)
    before, disk = deepcopy(store.state), store.state_path.read_bytes()
    with pytest.raises(ValueError, match="Operator progress"):
        store.settle_world_battle_money(
            "82" * 32,
            (payment,),
            policy_version=WORLD_POLICY,
            operator_win_player_ids=(22,),
        )
    assert store.state == before and store.state_path.read_bytes() == disk


@pytest.mark.parametrize("operator_ids", [(99,), (11, 11), (True,), [11]])
def test_world_operator_progress_rejects_unbound_or_repeated_id(tmp_path, operator_ids):
    store = TeamStore(tmp_path / "teams")
    first = world_payment(store, "Fighter", 11)
    before = deepcopy(store.state)
    with pytest.raises(ValueError, match="Operator progress"):
        store.settle_world_battle_money(
            "79" * 32,
            (first,),
            policy_version=WORLD_POLICY,
            operator_win_player_ids=operator_ids,
        )
    assert store.state == before


def test_world_operator_progress_failed_write_rolls_back_wallet_and_count(tmp_path, monkeypatch):
    store = TeamStore(tmp_path / "teams")
    payment = world_payment(store, "Operator", 22)
    before, disk = deepcopy(store.state), store.state_path.read_bytes()
    original = Path.replace

    def fail(path, *args, **kwargs):
        if path == store.state_path.with_suffix(".json.tmp"):
            raise OSError("Injected World Operator commit failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail)
        with pytest.raises(OSError, match="Operator commit failure"):
            store.settle_world_battle_money(
                "80" * 32,
                (payment,),
                policy_version=WORLD_POLICY,
                operator_win_player_ids=(22,),
            )
    assert store.state == before and store.state_path.read_bytes() == disk


@pytest.mark.parametrize(
    "world_phase,intra_faction,kind,expected",
    [
        (2, False, None, (451473,)),
        (2, True, None, ()),
        (0, False, None, ()),
        (2, None, None, ()),
        (2, None, "active_single_team", (451473,)),
        (2, True, "active_single_team", ()),
    ],
)
@pytest.mark.parametrize("sortie", [1, 2])
@pytest.mark.parametrize("won", [True, False])
def test_only_classified_non_practice_active_operator_win_becomes_progress_candidate(
    world_phase, intra_faction, kind, expected, sortie, won
):
    raw = bytes.fromhex(Path("tests/fixtures/operator-combat-result.hex").read_text())
    loss = decode_combat_report(raw)
    report = replace(loss, header=(loss.header[0], 1, 2, *loss.header[3:])) if won else loss
    fighter = GameSession(b"fighter1", bytes(16), "Fighter", 50627)
    operator = GameSession(b"operator", bytes(16), "Operator", 451473)
    squad = {50627: (fighter, 1), 451473: (operator, 1)}
    preparation = {
        "session": fighter,
        "team_id": 1,
        "pay_policy": "equal_distribution",
        "claimed_player_ids": [50627, 451473],
        "claimed_player_roles": [0, 0],
        "squad": squad,
    }
    match = {
        "match_id": 2,
        "reward_nonce": "02" * 16,
        "kind": kind,
        "criteria": {1: 3, 2: world_phase, 3: sortie},
        "world_phase": world_phase,
        "is_intra_faction": intra_faction,
        "squad": squad,
        "reward_participants": world_reward_participants(report, [preparation]),
        "result_sha256": report.digest,
    }

    class FactionStore:
        @staticmethod
        def team_by_id(team_id):
            return {"team_id": team_id, "faction_id": 1}

    if world_phase == 0:
        with pytest.raises(ValueError, match="active-war"):
            build_world_settlement(match, report, FactionStore(), dict(WORLD_REWARD_DEFAULTS))
        return
    result = build_world_settlement(match, report, FactionStore(), dict(WORLD_REWARD_DEFAULTS))
    assert result.operator_win_player_ids == (expected if won else ())
    assert result.audit["operator_win_progress_player_ids"] == list(expected if won else ())
    assert result.boss_win_player_ids == (
        (50627, 451473) if expected and won and sortie == 2 and kind == "active_single_team" else ()
    )
    assert bool(result.team_medal_progress) is bool(expected)


def test_equal_distribution_splits_gross_before_each_owner_pays_their_costs():
    allocation = allocate_world_team_reward(
        120_000,
        {22: (9_000, 1_000), 11: (1_000, 2_000)},
        WorldPayPolicy.EQUAL_DISTRIBUTION,
    )
    assert allocation.undistributed_reward == 0
    assert [share.player_id for share in allocation.shares] == [11, 22]
    assert [share.personal_reward for share in allocation.shares] == [60_000, 60_000]
    assert [share.net_income for share in allocation.shares] == [57_000, 50_000]
    assert [share.group_cost_reimbursement for share in allocation.shares] == [0, 0]


def test_equal_distribution_uses_three_human_heads_not_an_owned_unac_share():
    # Historical JP-server evidence shows three human pilots plus one UNAC:
    # 150,000 total became 50,000 personal pay. The UNAC's 26,761 combined
    # ammunition/repair cost belongs to its owner but adds no payout recipient.
    allocation = allocate_world_team_reward(
        150_000,
        {11: (90_227, 26_761), 22: (0, 0), 33: (0, 0)},
        WorldPayPolicy.EQUAL_DISTRIBUTION,
    )
    assert [share.player_id for share in allocation.shares] == [11, 22, 33]
    assert [share.personal_reward for share in allocation.shares] == [50_000] * 3
    assert allocation.undistributed_reward == 0


def test_pay_costs_equally_deducts_team_costs_before_equal_net_shares():
    allocation = allocate_world_team_reward(
        120_000,
        {22: (9_000, 1_000), 11: (1_000, 2_000)},
        WorldPayPolicy.PAY_COSTS_EQUALLY,
    )
    assert allocation.undistributed_reward == 0
    assert [share.personal_reward for share in allocation.shares] == [53_500, 53_500]
    assert [share.net_income for share in allocation.shares] == [53_500, 53_500]
    assert [share.group_cost_reimbursement for share in allocation.shares] == [3_000, 10_000]
    assert allocation.operator_subsidy == 0


def test_pay_costs_equally_floors_an_underfunded_winning_pool_and_records_subsidy():
    allocation = allocate_world_team_reward(
        10_000,
        {11: (8_000, 0), 22: (7_000, 0)},
        WorldPayPolicy.PAY_COSTS_EQUALLY,
    )
    assert allocation.operator_subsidy == 5_000
    assert allocation.undistributed_reward == 0
    assert [share.personal_reward for share in allocation.shares] == [0, 0]
    assert [share.net_income for share in allocation.shares] == [0, 0]


@pytest.mark.parametrize(
    "stored,expected",
    [
        (None, WorldPayPolicy.PAY_COSTS_EQUALLY),
        (0, WorldPayPolicy.PAY_COSTS_EQUALLY),
        (1, WorldPayPolicy.EQUAL_DISTRIBUTION),
    ],
)
def test_team_pay_apportionment_selects_the_original_distribution_rule(stored, expected):
    team = {} if stored is None else {"pay_apportionment": stored}
    assert world_pay_policy_for_team(team) is expected


@pytest.mark.parametrize("team", [None, {"pay_apportionment": True}, {"pay_apportionment": 2}])
def test_team_pay_apportionment_rejects_unknown_or_malformed_values(team):
    with pytest.raises(ValueError):
        world_pay_policy_for_team(team)


@pytest.mark.parametrize(
    "rank,rate",
    [(0, 10), (1, 14), (2, 16), (3, 20), (4, 24), (5, 26), (6, 30)],
)
def test_mercenary_expense_rate_uses_native_f_through_s_enum(rank, rate):
    assert mercenary_expense_rate_percent(rank) == rate


@pytest.mark.parametrize("rank", [-1, 7, True, "6"])
def test_mercenary_expense_rate_rejects_non_native_rank(rank):
    with pytest.raises(ValueError):
        mercenary_expense_rate_percent(rank)


@pytest.mark.parametrize("count,reduction", [(0, 0), (1, 10), (2, 50), (3, 85)])
def test_mercenary_team_point_reduction_uses_original_count_table(count, reduction):
    assert mercenary_team_point_reduction_percent(count) == reduction


@pytest.mark.parametrize("count", [-1, 4, True, "3"])
def test_mercenary_team_point_reduction_rejects_unsupported_count(count):
    with pytest.raises(ValueError):
        mercenary_team_point_reduction_percent(count)


def public_mercenary_context():
    report = decode_combat_report(
        bytes.fromhex(
            "080a1001180220d8e00b2800300038004000"
            "4a1b08d17710001801200128e8ed103000380a40024800500058006002"
            "4a1f08fdf60310001801200128c0ee0930a1f20638b82640004800500058006001"
            "4a2008b99b0f10001800200228d8e00b30d8e00b38004000480050fdf60358006000"
            "520a080110fdf60318c0a907"
            "5209080210d17718c0a907"
            "5209080310d1771880f104"
        )
    )
    employer = GameSession(b"employer", bytes(16), "Employer", 64381)
    mercenary = GameSession(b"merc0001", bytes(16), "Mercenary", 15313)
    opponent = GameSession(b"opponent", bytes(16), "Opponent", 249273)
    preparations = [
        {
            "session": employer,
            "team_id": 8,
            "pay_policy": "pay_costs_equally",
            "claimed_player_ids": [64381, 15313],
            "claimed_player_roles": [0, 2],
            "squad": {64381: (employer, 8), 15313: (mercenary, 1)},
        },
        {
            "session": opponent,
            "team_id": 1,
            "pay_policy": "pay_costs_equally",
            "claimed_player_ids": [249273],
            "claimed_player_roles": [0],
            "squad": {249273: (opponent, 1)},
        },
    ]
    return report, preparations


def test_public_mercenary_result_builds_role_aware_settlement_context():
    report, preparations = public_mercenary_context()
    participants = world_reward_participants(report, preparations)
    values = {participant.player_id: participant for participant in participants}
    assert values[64381].social_team_id == values[64381].settlement_team_id == 8
    assert values[64381].pay_policy is WorldPayPolicy.PAY_COSTS_EQUALLY
    assert values[64381].won and values[64381].role == 0
    assert values[15313].social_team_id == 1
    assert values[15313].settlement_team_id == 8
    assert values[15313].pay_policy is None and values[15313].won
    assert values[15313].role == 2
    assert values[249273].social_team_id == values[249273].settlement_team_id == 1
    assert values[249273].pay_policy is WorldPayPolicy.PAY_COSTS_EQUALLY
    assert not values[249273].won and values[249273].role == 0


def test_surviving_s_rank_mercenary_takes_thirty_percent_of_mission_payout():
    report, preparations = public_mercenary_context()
    allocation = allocate_world_mercenary_reward(
        1_000_000,
        report,
        world_reward_participants(report, preparations),
        settlement_team_id=8,
        ranks_by_player={15313: 6},
    )
    assert allocation.ordinary_team_reward == 700_000
    assert (
        allocation.shares[0].player_id,
        allocation.shares[0].rank,
        allocation.shares[0].rate_percent,
        allocation.shares[0].eligible,
        allocation.shares[0].personal_reward,
    ) == (
        15313,
        6,
        30,
        True,
        300_000,
    )


def test_destroyed_winning_mercenary_forfeits_rank_fee():
    report, preparations = public_mercenary_context()
    destroyed_units = tuple(
        unit[:3] + (2,) + unit[4:] if unit[0] == 15313 and unit[1] == 0 else unit
        for unit in report.units
    )
    destroyed = replace(report, units=destroyed_units)
    allocation = allocate_world_mercenary_reward(
        1_000_000,
        destroyed,
        world_reward_participants(destroyed, preparations),
        settlement_team_id=8,
        ranks_by_player={15313: 6},
    )
    assert allocation.ordinary_team_reward == 1_000_000
    assert not allocation.shares[0].eligible and allocation.shares[0].personal_reward == 0


def test_losing_employer_cannot_supply_a_mercenary_reward_pool():
    report, preparations = public_mercenary_context()
    losing = replace(report, header=(report.header[0], 2, 1, *report.header[3:]))
    participants = world_reward_participants(losing, preparations)
    with pytest.raises(ValueError, match="losing employer"):
        allocate_world_mercenary_reward(
            1,
            losing,
            participants,
            settlement_team_id=8,
            ranks_by_player={15313: 6},
        )
    zero = allocate_world_mercenary_reward(
        0,
        losing,
        participants,
        settlement_team_id=8,
        ranks_by_player={15313: 6},
    )
    assert zero.ordinary_team_reward == 0 and not zero.shares[0].eligible


@pytest.mark.parametrize("ranks", [{}, {15313: 7}, {15313: 6, 64381: 0}])
def test_mercenary_allocation_rejects_missing_invalid_or_foreign_rank(ranks):
    report, preparations = public_mercenary_context()
    with pytest.raises(ValueError):
        allocate_world_mercenary_reward(
            1_000_000,
            report,
            world_reward_participants(report, preparations),
            settlement_team_id=8,
            ranks_by_player=ranks,
        )


def test_mercenary_allocation_floors_each_percentage_fee():
    report, preparations = public_mercenary_context()
    allocation = allocate_world_mercenary_reward(
        1,
        report,
        world_reward_participants(report, preparations),
        settlement_team_id=8,
        ranks_by_player={15313: 6},
    )
    assert allocation.shares[0].personal_reward == 0
    assert allocation.ordinary_team_reward == 1


def test_mercenary_allocation_supports_multiple_hires_with_independent_floors():
    report, preparations = public_mercenary_context()
    participants = world_reward_participants(report, preparations)
    first_mercenary = next(value for value in participants if value.role == 2)
    second_id = 777_777
    second_unit = next(unit for unit in report.units if unit[0] == first_mercenary.player_id)
    expanded_report = replace(
        report,
        units=report.units + ((second_id,) + second_unit[1:],),
    )
    expanded_participants = participants + (
        replace(first_mercenary, player_id=second_id),
    )
    allocation = allocate_world_mercenary_reward(
        101,
        expanded_report,
        expanded_participants,
        settlement_team_id=8,
        ranks_by_player={first_mercenary.player_id: 0, second_id: 6},
    )
    assert [share.personal_reward for share in allocation.shares] == [10, 30]
    assert allocation.ordinary_team_reward == 61


def test_replacement_world_economy_builds_one_atomic_payment_for_every_report_owner():
    report, preparations = public_mercenary_context()
    participants = world_reward_participants(report, preparations)
    match = {
        "match_id": 9,
        "reward_nonce": "09" * 16,
        "world_phase": 2,
        "criteria": {1: 3, 2: 2, 3: 1},
        "is_intra_faction": False,
        "squad": {
            player_id: member
            for preparation in preparations
            for player_id, member in preparation["squad"].items()
        },
        "reward_participants": participants,
        "result_sha256": report.digest,
    }

    class FactionStore:
        @staticmethod
        def team_by_id(team_id):
            return {"team_id": team_id, "faction_id": 1 if team_id == 8 else 2}

    settlement = build_world_settlement(
        match, report, FactionStore(), dict(WORLD_REWARD_DEFAULTS)
    )
    payments = {payment["player_id"]: payment for payment in settlement.payments}
    assert set(payments) == set(report.player_ids)
    assert settlement.audit["mission_class"] == "normal"
    assert settlement.audit["configured_base_pool"] == 150_000
    assert len(settlement.team_progress) == 2
    winner = next(row for row in settlement.team_progress if row["rank_delta"] > 0)
    loser = next(row for row in settlement.team_progress if row["rank_delta"] < 0)
    assert winner["special_delta"] == 1
    assert loser["special_delta"] == 0
    assert settlement.player_ranking_progress == (
        {
            "player_id": 15313,
            "ac_destructions_delta": 0,
            "mercenary_sorties_delta": 1,
            "mercenary_wins_delta": 1,
        },
        {
            "player_id": 64381,
            "ac_destructions_delta": 1,
            "mercenary_sorties_delta": 0,
            "mercenary_wins_delta": 0,
        },
        {
            "player_id": 249273,
            "ac_destructions_delta": 0,
            "mercenary_sorties_delta": 0,
            "mercenary_wins_delta": 0,
        },
    )

    # Native 80k/120k classifications are not cash. Each 10k subtarget pool is
    # split between every human participant on the collector's settlement team.
    special = {player_id: 0 for player_id in report.player_ids}
    participant_by_player = {value.player_id: value for value in participants}
    for _target_id, collector_id, _reported_value in report.subtargets:
        collector = participant_by_player[collector_id]
        recipients = sorted(
            value.player_id
            for value in participants
            if value.settlement_team_id == collector.settlement_team_id
        )
        share = 10_000 // len(recipients)
        for player_id in recipients:
            special[player_id] += share
    assert special == {64381: 15_000, 15313: 15_000, 249273: 0}
    assert settlement.audit["subtarget_reward_undistributed"] == 0
    basic = {64381: 135_000, 15313: 15_000, 249273: 0}
    for player_id, payment in payments.items():
        outer = decode_proto(bytes.fromhex(payment["response_hex"]))
        battle = {field.number: field.value for field in decode_proto(outer[0].value)}
        team_rank = {
            field.number: field.value for field in decode_proto(outer[1].value)
        }
        participant = next(value for value in participants if value.player_id == player_id)
        assert battle[3] == basic[player_id]
        assert battle[4] == 0
        quests = [field for field in decode_proto(outer[0].value) if field.number == 8]
        bonuses = []
        for quest in quests:
            rewards = [field for field in decode_proto(quest.value) if field.number == 6]
            assert len(rewards) == 1
            cash = {field.number: field.value for field in decode_proto(rewards[0].value)}
            assert cash[1] == 1
            bonuses.append(cash[2])
        assert bonuses == ([5_000] * 3 if player_id in (64381, 15313) else [])
        assert sum(bonuses) == special[player_id]
        assert battle[5] == report.expenses_by_player[player_id]
        assert battle[6] == 0
        assert battle[7] == basic[player_id] + special[player_id]
        assert payment["gross_earnings"] - payment["expenses"] == (
            basic[player_id] + special[player_id]
        )
        expected_rank = (
            {1: 1, 2: 2, 3: 157, 4: 1_000}
            if participant.settlement_team_id == 8
            else {1: 1, 2: 1, 3: 900, 4: 1_000}
        )
        assert team_rank == expected_rank


def test_historical_three_human_one_unac_normal_report_rows_are_reproduced():
    def record(values):
        return proto_bytes(
            9,
            b"".join(
                proto_varint(number, value) for number, value in enumerate(values, 1)
            ),
        )

    payload = b"".join(
        proto_varint(number, value)
        for number, value in enumerate((10, 1, 2, 0, 0, 0, 0, 0), 1)
    ) + b"".join(
        record(unit)
        for unit in (
            # Three winning human recipients and one owned UNAC. Human repair
            # plus UNAC repair/ammunition equals the photographed 116,988.
            (11, 0, 1, 1, 100_000, 90_227, 0, 0, 0, 0, 0, 0),
            (11, 1_002, 1, 1, 50_000, 26_452, 309, 0, 0, 0, 0, 0),
            (22, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0),
            (33, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0),
            (44, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0),
        )
    )
    report = decode_combat_report(payload)
    sessions = {
        player_id: GameSession(
            f"p{player_id:07d}".encode(), bytes(16), f"Player{player_id}", player_id
        )
        for player_id in (11, 22, 33, 44)
    }
    winning_squad = {
        player_id: (sessions[player_id], 1) for player_id in (11, 22, 33)
    }
    losing_squad = {44: (sessions[44], 2)}
    preparations = [
        {
            "session": sessions[11],
            "team_id": 1,
            "pay_policy": "equal_distribution",
            "claimed_player_ids": [11, 22, 33],
            "claimed_player_roles": [0, 0, 0],
            "squad": winning_squad,
        },
        {
            "session": sessions[44],
            "team_id": 2,
            "pay_policy": "equal_distribution",
            "claimed_player_ids": [44],
            "claimed_player_roles": [0],
            "squad": losing_squad,
        },
    ]
    match = {
        "match_id": 10,
        "reward_nonce": "0a" * 16,
        "criteria": {1: 3, 2: 2, 3: 1},
        "world_phase": 2,
        "is_intra_faction": False,
        "squad": {**winning_squad, **losing_squad},
        "reward_participants": world_reward_participants(report, preparations),
        "result_sha256": report.digest,
    }

    class FactionStore:
        @staticmethod
        def team_by_id(team_id):
            return {"team_id": team_id, "faction_id": team_id}

    settlement = build_world_settlement(
        match, report, FactionStore(), dict(WORLD_REWARD_DEFAULTS)
    )
    payments = {payment["player_id"]: payment for payment in settlement.payments}
    for player_id in (11, 22, 33):
        battle = {
            field.number: field.value
            for field in decode_proto(
                decode_proto(bytes.fromhex(payments[player_id]["response_hex"]))[0].value
            )
        }
        assert battle[3] == battle[7] == 50_000
    owner = payments[11]
    owner_battle = {
        field.number: field.value
        for field in decode_proto(decode_proto(bytes.fromhex(owner["response_hex"]))[0].value)
    }
    assert owner["expenses"] == owner_battle[5] == 116_988
    assert owner["gross_earnings"] - owner["expenses"] == 50_000
    assert payments[44]["gross_earnings"] - payments[44]["expenses"] == 0


def test_replacement_world_economy_settings_are_bounded_and_complete():
    assert validate_world_reward_settings({}) == WORLD_REWARD_DEFAULTS
    with pytest.raises(ValueError):
        validate_world_reward_settings({"world_normal_base_reward": True})


@pytest.mark.parametrize("fault", ["missing", "role", "social_team", "policy"])
def test_world_settlement_context_rejects_unbound_or_contradictory_roster(fault):
    report, preparations = public_mercenary_context()
    if fault == "missing":
        preparations.pop()
    elif fault == "role":
        preparations[0]["claimed_player_roles"][1] = 0
    elif fault == "social_team":
        session = preparations[0]["squad"][15313][0]
        preparations[0]["squad"][15313] = (session, 8)
    else:
        preparations[0]["pay_policy"] = "invented"
    with pytest.raises(ValueError):
        world_reward_participants(report, preparations)


def test_apportionment_retains_integer_remainder_without_guessing_a_recipient():
    allocation = allocate_world_team_reward(
        120_001,
        {11: (1_000, 2_000), 22: (9_000, 1_000)},
        WorldPayPolicy.PAY_COSTS_EQUALLY,
    )
    assert [share.net_income for share in allocation.shares] == [53_500, 53_500]
    assert allocation.undistributed_reward == 1


@pytest.mark.parametrize(
    "gross,expenses,policy",
    [
        (-1, {11: (0, 0)}, WorldPayPolicy.EQUAL_DISTRIBUTION),
        (1 << 31, {11: (0, 0)}, WorldPayPolicy.EQUAL_DISTRIBUTION),
        (1, {}, WorldPayPolicy.EQUAL_DISTRIBUTION),
        (1, {0: (0, 0)}, WorldPayPolicy.EQUAL_DISTRIBUTION),
        (1, {11: [0, 0]}, WorldPayPolicy.EQUAL_DISTRIBUTION),
        (1, {11: (0, 0)}, "equal_distribution"),
    ],
)
def test_world_apportionment_rejects_unproved_or_invalid_inputs(gross, expenses, policy):
    with pytest.raises(ValueError):
        allocate_world_team_reward(gross, expenses, policy)


def test_faction_compensation_covers_both_pvp_sides_even_on_loss():
    raw = bytes.fromhex(
        "080110021801200028a0c803300038004000"
        "4a1e08c38b0310001800200128c0b60b30f5fb03380040004800500058006000"
        "4a1e0891c71b10001801200228a0c80330a0c803380040004800500058006001"
    )
    report = decode_combat_report(raw)
    assert faction_compensation_by_player(
        report, affiliated_player_ids=frozenset({50627, 451473})
    ) == {50627: 65013, 451473: 58400}


def test_faction_compensation_does_not_cover_single_side_npc_battle():
    raw = bytes.fromhex(
        Path("tests/fixtures/operator-combat-result.hex").read_text(encoding="ascii")
    )
    report = decode_combat_report(raw)
    assert faction_compensation_by_player(
        report, affiliated_player_ids=frozenset({50627, 451473})
    ) == {50627: 0, 451473: 0}


def test_faction_compensation_requires_explicit_report_bound_affiliation():
    raw = bytes.fromhex(
        "080110021801200028a0c803300038004000"
        "4a1e08c38b0310001800200128c0b60b30f5fb03380040004800500058006000"
        "4a1e0891c71b10001801200228a0c80330a0c803380040004800500058006001"
    )
    report = decode_combat_report(raw)
    with pytest.raises(ValueError, match="report-bound"):
        faction_compensation_by_player(report, affiliated_player_ids=frozenset({99}))
