"""Sparse inventory evidence delivers earned parts in the original mission reply."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_story_items import record

from acvd_server.handlers.economy import register_battle_result
from acvd_server.protocol import decode_proto, proto_varint
from acvd_server.rewards import StoryRewardPlan, _register_battle_result_response
from acvd_server.story_items import resolve_pending, settle, settle_group
from acvd_server.teams import TeamStore
from acvd_server.udp import REGISTER_BATTLE_RESULT, GameMessage, GameSession


@pytest.fixture
def sparse(tmp_path):
    teams = TeamStore(tmp_path / "teams")
    actor = GameSession(b"drops001", bytes(16), "Pilot", 449624)
    teams.remember_player(actor.online_id, actor.player_id)
    teams.synchronize_balance(actor.online_id, 1_000_000)
    players = tmp_path / "players"
    players.mkdir()
    (players / f"{actor.player_id}.offline-data.bin").write_bytes(record())
    # Public 2026-10-03 login: 884 locally owned instances, different XOR from
    # the server snapshot, but a valid same-login maximum and claim serial.
    actor.owned_parts_claim = proto_varint(1, 884) + proto_varint(2, 2940726550748401178)
    actor.owned_parts_max_instance_id = 2895102076882583821
    actor.owned_parts_evidence_serial = actor.owned_parts_claim_serial = 1
    return teams, actor, players


def item_ids(payload):
    result = []
    for battle in decode_proto(payload):
        for quest in decode_proto(battle.value):
            if quest.number != 8:
                continue
            for detail in decode_proto(quest.value):
                if detail.number != 6:
                    continue
                values = {f.number: f.value for f in decode_proto(detail.value)}
                if values[1] == 2:
                    result.append({f.number: f.value for f in decode_proto(values[4])})
    return result


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "10e80f1a1108d8b81b1000180120c414280030003800"
            "220b08e80710e80f1885072000220b08e80710e80f188607200030033802",
            {3110, 4310},
        ),
        (
            "10ec0f1a1308d8b81b100018012092930128d06930003800"
            "220b08e80710ec0f1885072000220b08e80710ec0f188607200030033802",
            {3310, 2930, 2120, 2410},
        ),
    ],
)
def test_exact_public_results_contain_immediate_drops_and_retry_once(sparse, raw, expected):
    teams, actor, players = sparse
    replies = []
    message = GameMessage(REGISTER_BATTLE_RESULT, b"\0\0\0\x01", bytes.fromhex(raw), 1)
    kwargs = dict(
        teams=teams,
        player_data_path=players,
        initial_balance=0,
        telemetry=SimpleNamespace(emit=lambda *a, **kw: None),
        reply=replies.append,
    )
    register_battle_result(actor, message, **kwargs)
    items = item_ids(replies[0])
    assert {item[2] for item in items} == expected
    assert len({item[4] for item in items}) == len(expected)
    assert all(item[4] > actor.owned_parts_max_instance_id for item in items)
    receipt = next(iter(teams.state["story_rewards"].values()))
    assert not receipt["pending_specs"]
    before = deepcopy(teams.state)
    register_battle_result(actor, message, **kwargs)
    assert replies[-1] == replies[0] and teams.state == before


@pytest.mark.parametrize("fault", ["missing_claim", "missing_cursor", "stale", "full", "overflow"])
def test_unsafe_sparse_evidence_keeps_rewards_pending(sparse, fault):
    teams, actor, players = sparse
    if fault == "missing_claim":
        actor.owned_parts_claim = None
    elif fault == "missing_cursor":
        actor.owned_parts_max_instance_id = None
    elif fault == "stale":
        actor.owned_parts_claim_serial = 0
    elif fault == "full":
        actor.owned_parts_claim = proto_varint(1, 2980) + proto_varint(2, 0)
    elif fault == "overflow":
        actor.owned_parts_max_instance_id = (1 << 64) - 1
    _, settlement, count = settle(
        teams,
        actor,
        StoryRewardPlan(2024, actor.player_id, 150_000, 0, 1),
        b"mission",
        player_data_path=players,
    )
    assert settlement["applied"] and count == 0
    receipt = next(iter(teams.state["story_rewards"].values()))
    assert len(receipt["pending_specs"]) == 2


def test_coop_awards_each_sparse_owner_parts_in_same_response(sparse):
    teams, actor, players = sparse
    guest = GameSession(b"drops002", bytes(16), "Guest", 22)
    teams.remember_player(guest.online_id, guest.player_id)
    teams.synchronize_balance(guest.online_id, 1_000_000)
    (players / "22.offline-data.bin").write_bytes(record(20001))
    guest.owned_parts_claim = proto_varint(1, 3) + proto_varint(2, 1)
    guest.owned_parts_max_instance_id = 30000
    guest.owned_parts_claim_serial = guest.owned_parts_evidence_serial = 1
    plans = tuple(StoryRewardPlan(2024, p.player_id, 150_000, 0, 1) for p in (actor, guest))
    reply, result = settle_group(
        teams, (actor, guest), plans, b"co-op", player_data_path=players, request_digest="ab" * 32
    )
    assert result["reply_item_count"] == 4 and result["pending_item_count"] == 0
    assert len(item_ids(reply)) == 4
    assert {r["online_id"] for r in teams.state["story_rewards"].values()} == {"Pilot", "Guest"}


def test_deferred_repeat_rewards_preserve_every_completion_and_prefix(sparse):
    teams, actor, _players = sparse
    # First completion delivered A; later completions earned A and B. Each
    # receipt must recover all its own parts, independent of prior ownership.
    first = StoryRewardPlan(2001, actor.player_id, 100_000, 0, 1)
    settle(
        teams, actor, first, b"first",
        inventory=b"".join(record(10000 + i) for i in range(2979)),
        reserved_instance_ids=frozenset(range(10000, 12979)),
    )
    later = StoryRewardPlan(2001, actor.player_id, 100_000, 0, 1)
    settle(teams, actor, later, b"second", inventory=None)
    settle(teams, actor, later, b"third", inventory=None)
    before_cash = teams.balance(actor.online_id)
    original_replies = [r["reply_hex"] for r in teams.state["story_rewards"].values()]
    assert (
        resolve_pending(teams, actor, inventory=record(), reserved_instance_ids=frozenset({10001}))
        == 5
    )
    assert all(not r["pending_specs"] for r in teams.state["story_rewards"].values())
    assert all(
        [i["part_id"] for i in r["items"]] == [2520, 2220]
        for r in teams.state["story_rewards"].values()
    )
    assert [r["reply_hex"] for r in teams.state["story_rewards"].values()] == original_replies
    assert teams.balance(actor.online_id) == before_cash
    assert (
        resolve_pending(teams, actor, inventory=record(), reserved_instance_ids=frozenset({10001}))
        == 0
    )


def test_no_part_reward_does_not_report_invalid_allocation(sparse):
    teams, actor, players = sparse
    plan = StoryRewardPlan(201, actor.player_id, 120_000, 0, 1)
    reply, _, count = settle(teams, actor, plan, b"cash", player_data_path=players)
    assert count == 0 and reply == _register_battle_result_response(plan)
    assert next(iter(teams.state["story_rewards"].values()))["deferred_reason"] == ""
