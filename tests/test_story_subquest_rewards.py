"""Native missing-bonus report, trusted table values and transactional replay."""

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from acvd_server.handlers import economy
from acvd_server.protocol import decode_proto, proto_bytes, proto_varint
from acvd_server.rewards import (
    STORY_BASE_PAY,
    STORY_REWARD_POLICY,
    STORY_SUBQUEST_REWARD_POLICY,
    story_reward_plan,
)
from acvd_server.story_reward_data import STORY_SUBQUEST_PAY
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession

NATIVE = bytes.fromhex((Path(__file__).parent / "fixtures/story-subquest-result.hex").read_text())


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


@pytest.fixture
def state(tmp_path):
    store = TeamStore(tmp_path / "teams")
    store.synchronize_balance("Tester", 37357)
    actor = GameSession(
        b"test0001", bytes(16), "Tester", 50627, established=True, peer=("127.0.0.1", 1111)
    )
    return store, actor, Events(), []


def submit(state, payload=NATIVE, reply=None):
    store, actor, events, replies = state
    economy.register_battle_result(
        actor,
        GameMessage(0x3C2, bytes(4), payload),
        teams=store,
        telemetry=events,
        reply=reply or replies.append,
        initial_balance=0,
    )


def quest(*, namespace=1000, mission=2002, identifier=902, variant=0):
    return proto_bytes(
        4,
        b"".join(
            proto_varint(i, value)
            for i, value in enumerate((namespace, mission, identifier, variant), 1)
        ),
    )


def without_quests():
    return b"".join(
        proto_varint(f.number, f.value) if f.wire_type == 0 else proto_bytes(f.number, f.value)
        for f in decode_proto(NATIVE)
        if f.number != 4
    )


def test_native_bonus_changes_gross_not_expenses(state):
    plan = story_reward_plan(NATIVE, 50627)
    assert (plan.base_pay, plan.bonus_pay, plan.gross_pay, plan.expenses) == (
        100000,
        20000,
        120000,
        87148,
    )
    assert plan.subquest_ids == (902,) and plan.policy_version == STORY_SUBQUEST_REWARD_POLICY
    submit(state)
    store, _, events, replies = state
    assert store.balance("Tester") == 70209
    outer = decode_proto(replies[0])
    assert len(outer) == 1 and outer[0].number == 1
    assert {f.number: f.value for f in decode_proto(outer[0].value) if f.wire_type == 0} == {
        3: 100000,
        4: 0,
        5: 0,
        6: 0,
        7: 120000,
        9: 50627,
    }
    assert events.rows[-1][1]["bonus_pay"] == 20000


def test_bonus_retry_and_reload_do_not_credit_again(state):
    submit(state)
    store, actor, events, replies = state
    reloaded = TeamStore(store.root)
    submit((reloaded, actor, events, replies))
    assert reloaded.balance("Tester") == 70209
    assert len(reloaded.state["battle_settlements"]) == 1 and replies[0] == replies[1]


def test_missing_bonus_is_not_retroactively_paid_to_an_old_receipt(state):
    store, _, events, replies = state
    store.settle_battle_money(
        "Tester",
        hashlib.sha256(NATIVE).hexdigest(),
        mission_id=2002,
        gross_earnings=100000,
        expenses=87148,
        policy_version=STORY_REWARD_POLICY,
    )
    before = deepcopy(store.state)
    submit(state)
    assert store.state == before and replies == [b""]
    assert events.rows[-1][0] == "game_story_reward_rejected"


def test_base_only_receipt_keeps_its_existing_policy(state):
    raw = without_quests()
    assert story_reward_plan(raw, 50627).policy_version == STORY_REWARD_POLICY
    submit(state, raw)
    submit(state, raw)
    assert state[0].balance("Tester") == 50209 and state[3][0] == state[3][1]


@pytest.mark.parametrize(
    "extra",
    [
        quest(),
        quest(namespace=1001),
        quest(mission=110),
        quest(identifier=999),
        quest(variant=1),
        proto_varint(4, 902),
        proto_bytes(4, b""),
        proto_bytes(4, proto_varint(1, 1000)),
    ],
)
def test_unqualified_or_duplicate_subquests_do_not_change_money(state, extra):
    store, _, events, replies = state
    before = deepcopy(store.state)
    submit(state, NATIVE + extra)
    assert store.state == before and replies == [b""]
    assert events.rows[-1][0] == "game_story_reward_rejected"


def test_two_distinct_earned_subquests_sum_table_values(state):
    plan = story_reward_plan(NATIVE + quest(identifier=901), 50627)
    assert plan.bonus_pay == 30000 and plan.subquest_ids == (901, 902)


def test_table_covers_supported_missions_and_distinguishes_paired_duels():
    assert set(STORY_SUBQUEST_PAY) == set(STORY_BASE_PAY)
    assert STORY_SUBQUEST_PAY[2002][902] == 20000
    assert STORY_SUBQUEST_PAY[2011][902] == 30000
    assert all(
        900 < key < 1000 and type(value) is int and 0 <= value < 1 << 31
        for table in STORY_SUBQUEST_PAY.values()
        for key, value in table.items()
    )


@pytest.mark.parametrize("method", ["write_text", "replace"])
def test_failed_bonus_commit_preserves_wallet_receipt_and_retry(state, monkeypatch, method):
    store, _, _, replies = state
    before, disk = deepcopy(store.state), store.state_path.read_bytes()
    original = getattr(Path, method)

    def fail(path, *args, **kwargs):
        if path == store.state_path.with_suffix(".json.tmp"):
            raise OSError("Injected bonus storage failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, method, fail)
        with pytest.raises(OSError, match="bonus storage"):
            submit(state)
    assert store.state == before and store.state_path.read_bytes() == disk and replies == []
    submit(state)
    assert store.balance("Tester") == 70209


def test_lost_bonus_reply_does_not_repeat_the_payment(state):
    def lost(_):
        assert state[0].balance("Tester") == 70209
        raise OSError("Lost bonus reply")

    with pytest.raises(OSError, match="Lost bonus"):
        submit(state, reply=lost)
    submit(state)
    assert state[0].balance("Tester") == 70209
    assert len(state[0].state["battle_settlements"]) == 1


def test_bonus_display_uses_nested_quest_cash_not_special_consideration(state):
    # Native first candidate: field4 increased Total Pay but Bonus Pay stayed0.
    # Exact converters route field8 QuestRewardInfo -> field6 RewardInfo(type1)
    # to display kind2, then add its amount to GetExtraReward's source word.
    submit(state)
    battle = decode_proto(decode_proto(state[3][0])[0].value)
    scalars = {f.number: f.value for f in battle if f.wire_type == 0}
    assert scalars[4] == 0
    quests = [decode_proto(f.value) for f in battle if f.number == 8]
    assert len(quests) == 1
    assert {f.number: f.value for f in quests[0] if f.wire_type == 0} == {
        1: 1000,
        2: 2002,
        3: 902,
        5: 0,
    }
    cash = [decode_proto(f.value) for f in quests[0] if f.number == 6]
    assert len(cash) == 1
    assert {f.number: f.value for f in cash[0]} == {1: 1, 2: 20000}
    assert scalars[7] == 120000 and state[0].balance("Tester") == 70209


def test_completed_mission_keeps_destroyed_owned_unac_costs(state):
    # Native MISSION COMPLETE with player status1 and the owned UNAC status2.
    # A dead companion must not reject the entire result or erase its expenses.
    raw = bytes.fromhex(
        (Path(__file__).parent / "fixtures/story-destroyed-unac-result.hex").read_text()
    )
    plan = story_reward_plan(raw, 50627)
    assert plan.unit_count == 2 and plan.expenses == 186550
    assert plan.base_pay == 100000 and plan.bonus_pay == 20000
    submit(state, raw)
    assert state[0].balance("Tester") == -29193
    receipt = state[0].state["battle_settlements"][0]
    assert receipt["gross_earnings"] == 120000 and receipt["expenses"] == 186550
    assert receipt["nominal_delta"] == receipt["delta"] == -66550


@pytest.mark.parametrize("unit,status", [(1, 3), (1, 0)])
def test_new_unac_form_does_not_admit_unqualified_outcomes(state, unit, status):
    rewritten = []
    for field in decode_proto(NATIVE):
        if field.number == 3:
            nested = decode_proto(field.value)
            values = {f.number: f.value for f in nested}
            if values[2] == unit:
                values[3] = status
            rewritten.append(
                proto_bytes(3, b"".join(proto_varint(n, v) for n, v in values.items()))
            )
        else:
            rewritten.append(
                proto_varint(field.number, field.value)
                if field.wire_type == 0
                else proto_bytes(field.number, field.value)
            )
    before = deepcopy(state[0].state)
    submit(state, b"".join(rewritten))
    assert state[0].state == before and state[3] == [b""]
