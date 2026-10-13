"""Story expense/base-pay repair against synthetic versions of the native form."""

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from acvd_server.handlers import economy
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.rewards import (
    STORY_REWARD_POLICY,
    _register_battle_result_response,
    story_reward_plan,
    story_reward_plans,
)
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession


def participant(unit=0, repair=6785, ammo=0, player=42, marker=1, reserved=(0, 0)):
    values = {
        1: player,
        2: unit,
        3: marker,
        4: repair,
        5: ammo,
        6: reserved[0],
        7: reserved[1],
    }
    return proto_bytes(3, b"".join(proto_varint(n, v) for n, v in values.items()))


def request(*, mission=2000, records=None):
    records = [participant(), participant(1, 37774, 1220)] if records is None else records
    return proto_varint(2, mission) + b"".join(records) + proto_varint(6, 3) + proto_varint(7, 2)


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


@pytest.fixture
def fixture(tmp_path):
    store = TeamStore(tmp_path / "teams")
    store.synchronize_balance("Tester", 100000)
    session = GameSession(
        b"test0001", bytes(16), "Tester", 42, established=True, peer=("127.0.0.1", 1000)
    )
    return store, session, Events(), []


def submit(fixture, payload):
    store, session, events, replies = fixture
    economy.register_battle_result(
        session,
        GameMessage(0x3C2, bytes(4), payload),
        teams=store,
        telemetry=events,
        reply=replies.append,
        initial_balance=1_000_000,
    )


def test_native_expense_values_are_not_earnings():
    plan = story_reward_plan(request(), 42)
    assert (plan.mission_id, plan.base_pay, plan.expenses, plan.unit_count) == (
        2000,
        100000,
        45779,
        2,
    )
    outer = decode_proto(_register_battle_result_response(plan))
    assert len(outer) == 1 and outer[0].number == 1
    assert {f.number: f.value for f in decode_proto(outer[0].value)} == {
        3: 100000,
        4: 0,
        5: 0,
        6: 0,
        7: 100000,
        9: 42,
    }


def test_destroyed_second_human_in_completed_coop_story_is_rewarded():
    # Exact structural form from the 2026-09-27 live mission: both owners use
    # unit index zero, while the destroyed guest reports participant status 2.
    payload = bytes.fromhex(
        "10e00f"
        "1a1208d71f1000180120acc50328955230003800"
        "1a1308b3a01c1000180220e8e00c28ba0430003800"
        "30033802"
    )
    plans = story_reward_plans(payload, (4055, 462899))
    assert [(plan.player_id, plan.unit_count, plan.expenses) for plan in plans] == [
        (4055, 1, 68_545),
        (462899, 1, 209_570),
    ]
    assert all(plan.mission_id == 2016 and plan.gross_pay == 150_000 for plan in plans)


def test_hired_second_human_native_story_marker_is_rewarded():
    # Exact public RUKKVI + etherdoubler J-fight result.  The hired second
    # human uses reserved fields (2, 1), unlike same-team co-op's (0, 0).
    payload = bytes.fromhex(
        "10f207"
        "1a1308aae0161000180120e4950528b44130003800"
        "1a1308a7c3161000180120ffac0728814630023801"
        "220b08e80710f2071885072000"
        "30033802"
    )
    plans = story_reward_plans(payload, (372778, 369063))
    assert [(plan.player_id, plan.unit_count, plan.expenses) for plan in plans] == [
        (372778, 1, 93_080),
        (369063, 1, 129_408),
    ]
    assert all(
        plan.mission_id == 1010
        and plan.gross_pay == 280_000
        and plan.subquest_ids == (901,)
        for plan in plans
    )


def test_hired_story_marker_survives_employer_unac_and_destroyed_human():
    records = [
        participant(player=42),
        participant(player=42, unit=1),
        participant(player=43, marker=2, reserved=(2, 1)),
    ]
    plans = story_reward_plans(request(mission=1010, records=records), (42, 43))
    assert [(plan.player_id, plan.unit_count) for plan in plans] == [(42, 2), (43, 1)]


@pytest.mark.parametrize(
    "records, owners",
    [
        ([participant(player=42, reserved=(2, 1))], (42,)),
        (
            [participant(player=42, reserved=(2, 1)), participant(player=43)],
            (42, 43),
        ),
        (
            [participant(player=42), participant(player=43, unit=1, reserved=(2, 1))],
            (42, 43),
        ),
        ([participant(player=42), participant(player=43, reserved=(1, 1))], (42, 43)),
        ([participant(player=42), participant(player=43, reserved=(2, 0))], (42, 43)),
    ],
)
def test_hired_story_marker_cannot_escape_its_exact_second_human_form(records, owners):
    with pytest.raises(ValueError, match="participant record form"):
        story_reward_plans(request(mission=1010, records=records), owners)


def test_owned_unac_costs_are_charged_but_pay_is_not_doubled(fixture):
    store, _, events, replies = fixture
    submit(fixture, request())
    assert store.balance("Tester") == 154221
    assert len(replies) == 1 and events.rows[-1][0] == "game_battle_result_registered"
    receipt = store.state["battle_settlements"][0]
    assert receipt["gross_earnings"] == 100000 and receipt["expenses"] == 45779
    assert receipt["policy_version"] == STORY_REWARD_POLICY


def test_story_reward_saturates_at_the_retail_cash_ceiling(fixture):
    store, _, events, replies = fixture
    store.synchronize_balance("Tester", 999_999_900)
    submit(fixture, request(mission=110, records=[participant(repair=18_184, ammo=6_360)]))
    assert len(replies) == 1 and events.rows[-1][0] == "game_battle_result_registered"
    assert store.balance("Tester") == 999_999_999
    receipt = store.state["battle_settlements"][-1]
    assert receipt["nominal_delta"] == 125_456
    assert receipt["delta"] == 99
    assert receipt["discarded_over_cap"] == 125_357


def test_preexisting_over_cap_wallet_requires_review_before_new_story_credit(fixture):
    store, _, events, replies = fixture
    invalid = deepcopy(store.state)
    invalid["player_profiles"]["Tester"]["balance"] = 1_000_000_001
    store.commit_state(invalid)
    before = deepcopy(store.state)
    submit(fixture, request())
    assert replies == [b""] and store.state == before
    assert events.rows[-1][0] == "game_story_reward_rejected"


def test_story_expense_saturates_at_retail_debt_floor(fixture):
    store, _, _, replies = fixture
    store.synchronize_balance("Tester", -999_999_900)
    submit(fixture, request(mission=204, records=[participant(repair=10_000, ammo=0)]))
    assert replies and store.balance("Tester") == -999_999_999
    receipt = store.state["battle_settlements"][-1]
    assert receipt["nominal_delta"] == -10_000
    assert receipt["delta"] == -99
    assert receipt["forgiven_below_debt_floor"] == 9_901


def test_exact_retry_after_reload_keeps_wallet_and_response(fixture):
    store, session, events, replies = fixture
    payload = request()
    submit(fixture, payload)
    reloaded = TeamStore(store.root)
    submit((reloaded, session, events, replies), payload)
    assert reloaded.balance("Tester") == 154221 and len(reloaded.state["battle_settlements"]) == 1
    assert replies[0] == replies[1]
    assert not events.rows[-1][1]["money_settlement"]["applied"]


def test_old_incorrect_receipt_is_not_repaid_or_rewritten(fixture):
    store, _, events, replies = fixture
    payload = request()
    store.settle_battle_money(
        "Tester",
        hashlib.sha256(payload).hexdigest(),
        mission_id=2000,
        gross_earnings=6785,
        expenses=0,
    )
    before = deepcopy(store.state)
    submit(fixture, payload)
    assert store.state == before and replies == [b""]
    assert events.rows[-1][0] == "game_story_reward_rejected"


def test_missing_wallet_does_not_receive_a_fallback_grant(fixture):
    store, _, _, replies = fixture
    store.state["player_profiles"]["Tester"].pop("balance")
    before = deepcopy(store.state)
    submit(fixture, request())
    assert store.state == before and replies == [b""]


@pytest.mark.parametrize(
    "payload",
    [
        request(mission=999999),
        request(records=[]),
        request(records=[participant(player=99)]),
        request(records=[participant(), participant(1, player=99)]),
        request(records=[participant(), participant()]),
        request(records=[participant(unit=1)]),
        request(records=[participant(), participant(1), participant(2)]),
        request(records=[participant(marker=0)]),
        request(records=[participant(repair=-1)]),
        request(records=[participant(ammo=1 << 40)]),
        request() + proto_varint(2, 2000),
        b"\xff",
        b"x" * 4097,
    ],
)
def test_unqualified_or_malformed_reports_do_not_change_money(fixture, payload):
    store, _, events, replies = fixture
    before = deepcopy(store.state)
    submit(fixture, payload)
    assert store.state == before and replies == [b""]
    assert events.rows[-1][0] == "game_story_reward_rejected"


def test_another_known_story_row_uses_its_base_pay(fixture):
    store, _, _, replies = fixture
    submit(fixture, request(mission=110, records=[participant(repair=18184, ammo=6360)]))
    assert store.balance("Tester") == 225456
    reward = decode_proto(first_field(decode_proto(replies[0]), 1).value)
    assert first_field(reward, 3).value == first_field(reward, 7).value == 150000


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_failed_reward_commit_preserves_memory_disk_and_retry(fixture, monkeypatch, failure):
    store, session, events, replies = fixture
    before, disk = deepcopy(store.state), store.state_path.read_bytes()
    temporary = store.state_path.with_suffix(".json.tmp")
    method = "write_text" if failure == "write" else "replace"
    original = getattr(Path, method)

    def fail(path, *args, **kwargs):
        if path == temporary:
            raise OSError("Injected settlement write failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, method, fail)
        with pytest.raises(OSError, match="Injected settlement"):
            submit(fixture, request())
    assert store.state == before and store.state_path.read_bytes() == disk
    assert not replies and not events.rows
    submit(fixture, request())
    assert store.balance("Tester") == 154221
    assert len(store.state["battle_settlements"]) == 1
    reloaded = TeamStore(store.root)
    submit((reloaded, session, events, replies), request())
    assert reloaded.balance("Tester") == 154221 and replies[0] == replies[1]


def test_reward_reply_failure_does_not_repeat_a_committed_credit(fixture):
    store, session, events, replies = fixture

    def lost_reply(_):
        assert TeamStore(store.root).balance("Tester") == 154221
        raise OSError("Injected lost reward reply")

    with pytest.raises(OSError, match="Injected lost"):
        economy.register_battle_result(
            session,
            GameMessage(0x3C2, bytes(4), request()),
            teams=store,
            telemetry=events,
            reply=lost_reply,
            initial_balance=1000000,
        )
    submit(fixture, request())
    assert store.balance("Tester") == 154221 and len(store.state["battle_settlements"]) == 1
    assert events.rows[-1][1]["money_settlement"]["applied"] is False


def test_settlement_primitive_cannot_invent_an_unknown_wallet(fixture):
    store, _, _, _ = fixture
    before, disk = deepcopy(store.state), store.state_path.read_bytes()
    with pytest.raises(ValueError, match="known wallet"):
        store.settle_battle_money(
            "Unknown",
            "new-result",
            mission_id=2000,
            gross_earnings=100000,
            expenses=1000,
            initial=1000000,
        )
    assert store.state == before and store.state_path.read_bytes() == disk
