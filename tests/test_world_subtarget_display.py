"""Subtarget details are a breakdown of existing pay, never another payment."""

from dataclasses import replace

import pytest

from acvd_server.combat_reports import decode_combat_report
from acvd_server.protocol import decode_proto
from acvd_server.rewards import (
    WorldPayPolicy,
    WorldRewardParticipant,
    world_cash_reward_plan,
    world_wallet_payment,
)
from acvd_server.teams import TeamStore
from acvd_server.udp import GameSession
from acvd_server.world_economy import WORLD_REWARD_DEFAULTS, build_world_settlement

# Public 2026-10-03 22:43:26 UTC report: matches the screenshot's 93,704 costs
# and four large targets. Target zero is valid and must not be discarded.
REPORT = bytes.fromhex(
    "08051001180220002800300038004000"
    "4a2008c6f10610001801200128c0a70b30b6a60438d2b50140004800500058006003"
    "520a080010c6f10618c0a907520a080110c6f10618c0a907"
    "520a080210c6f10618c0a907520a080310c6f10618c0a907"
)


def details(payment):
    outer = decode_proto(bytes.fromhex(payment["response_hex"]))
    assert [field.number for field in outer] == [1, 2]
    battle = decode_proto(outer[0].value)
    cash = []
    for field in battle:
        if field.number != 8:
            continue
        quest = decode_proto(field.value)
        # Generic World reward, not a fabricated Story achievement condition.
        assert {f.number: f.value for f in quest if f.wire_type == 0} == {
            1: 0,
            2: 0,
            3: 0,
            5: 0,
        }
        rewards = [f for f in quest if f.number == 6]
        assert len(rewards) == 1
        reward = {f.number: f.value for f in decode_proto(rewards[0].value)}
        assert set(reward) == {1, 2} and reward[1] == 1
        cash.append(reward[2])
    return {f.number: f.value for f in battle if f.wire_type == 0}, cash


def settle(report, *, reward=10_000):
    actor = GameSession(b"test0001", bytes(16), "Tester", 112838)
    participant = WorldRewardParticipant(
        player_id=112838,
        social_team_id=39,
        settlement_team_id=39,
        role=0,
        combat_side=1,
        won=True,
        pay_policy=WorldPayPolicy.PAY_COSTS_EQUALLY,
    )

    class Store:
        @staticmethod
        def team_by_id(team_id):
            return {"team_id": team_id, "faction_id": 1}

    match = {
        "kind": "active_single_team",
        "world_phase": 2,
        "criteria": {2: 2, 3: 1},
        "battle_type": 1,
        "squad": {112838: (actor, 39)},
        "reward_participants": (participant,),
        "result_sha256": report.digest,
        "reward_nonce": "01" * 16,
    }
    settings = {**WORLD_REWARD_DEFAULTS, "world_large_subtarget_reward": reward}
    return build_world_settlement(match, report, Store(), settings)


@pytest.mark.parametrize("reward", [0, 10_000])
def test_reported_burn_targets_populate_bonus_without_changing_net_pay(reward):
    report = decode_combat_report(REPORT)
    result = settle(report, reward=reward)
    (payment,) = result.payments
    battle, bonuses = details(payment)
    assert report.expenses_by_player == {112838: 93_704}
    assert result.audit["subtarget_count"] == 4
    assert bonuses == [reward] * 4
    assert battle[3] == 56_296 and battle[4] == 0
    assert battle[7] == 56_296 + reward * 4
    assert payment["gross_earnings"] - payment["expenses"] == battle[7]
    assert battle[3] + sum(bonuses) + battle[4] == battle[7]


def test_no_targets_keeps_the_empty_details_envelope():
    report = replace(decode_combat_report(REPORT), subtargets=())
    (payment,) = settle(report).payments
    battle, bonuses = details(payment)
    assert bonuses == [] and battle[4] == 0
    assert battle[7] == 56_296


def test_duplicate_target_still_rejects_the_entire_settlement():
    report = decode_combat_report(REPORT)
    with pytest.raises(ValueError, match="repeats a subtarget"):
        settle(replace(report, subtargets=(report.subtargets[0],) * 2))


def test_cash_details_survive_retry_and_reload_without_double_credit(tmp_path):
    result = settle(decode_combat_report(REPORT))
    store = TeamStore(tmp_path / "teams")
    store.remember_player("Tester", 112838)
    store.synchronize_balance("Tester", 1_000_000)
    receipt = store.settle_world_battle_money(
        result.result_digest,
        result.payments,
        policy_version=result.policy_version,
    )
    assert store.balance("Tester") == 1_096_296
    reloaded = TeamStore(store.root)
    replay = reloaded.settle_world_battle_money(
        result.result_digest,
        result.payments,
        policy_version=result.policy_version,
    )
    assert not replay["applied"]
    assert replay["participants"] == receipt["participants"]
    assert reloaded.balance("Tester") == 1_096_296


def test_old_reply_is_not_rewritten_or_repaid_after_format_change(tmp_path):
    result = settle(decode_combat_report(REPORT))
    old = dict(result.payments[0])
    old["response_hex"] = "0a1618e8b70320c0b8022888dc05300038a8f00548c6f106120a0801100e18e50420e807"
    store = TeamStore(tmp_path / "teams")
    store.remember_player("Tester", 112838)
    store.synchronize_balance("Tester", 1_000_000)
    receipt = store.settle_world_battle_money(
        result.result_digest,
        (old,),
        policy_version="old-display",
    )
    reloaded = TeamStore(store.root)
    with pytest.raises(ValueError, match="different reward policy"):
        reloaded.settle_world_battle_money(
            result.result_digest,
            result.payments,
            policy_version=result.policy_version,
        )
    assert (
        reloaded.world_battle_settlement(result.result_digest)["participants"]
        == (receipt["participants"])
    )
    assert TeamStore(store.root).balance("Tester") == 1_096_296


@pytest.mark.parametrize(
    "rows",
    [
        [(0, 100)],
        ((True, 100),),
        ((-1, 100),),
        ((0, True),),
        ((0, -1),),
        ((0, 1 << 32),),
        ((0, 100), (0, 100)),
        tuple((i, 0) for i in range(65)),
        ((0, 101),),
    ],
)
def test_invalid_or_unfunded_bonus_details_are_rejected(rows):
    with pytest.raises(ValueError):
        world_cash_reward_plan(
            player_id=11,
            basic_reward=0,
            special_reward=0,
            combined_group_cost_reward=0,
            personal_reward=100,
            team_rank=(1, 1, 0, 1000),
            subtarget_rewards=rows,
        )


def test_maximum_report_details_fit_and_preserve_funded_cash():
    plan = world_cash_reward_plan(
        player_id=11,
        basic_reward=0,
        special_reward=0,
        combined_group_cost_reward=0,
        personal_reward=64,
        team_rank=(1, 1, 0, 1000),
        subtarget_rewards=tuple((i, 1) for i in range(64)),
    )
    payment = world_wallet_payment("Tester", plan, expenses=0)
    assert details(payment)[1] == [1] * 64
    assert payment["gross_earnings"] == 64
