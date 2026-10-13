"""Visible Team Data fields use settled facts, not blank compatibility replies."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from acvd_server.handlers.world import get_team_record
from acvd_server.protocol import decode_proto, first_field, proto_varint
from acvd_server.team_messages import _team_record
from acvd_server.team_records import advance_team_records, team_record
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession


def state():
    return {
        "teams": {
            "1": {"team_id": 1, "name": "One", "team_world_sorties": 3, "team_pvp_wins": 1},
            "2": {"team_id": 2, "name": "Two", "team_world_sorties": 2, "team_pvp_wins": 1},
        },
        "world_battle_settlements": [
            {"team_progress": [{"team_id": 1, "rank_delta": 200}]},
            {
                "team_progress": [
                    {"team_id": 1, "rank_delta": 200},
                    {"team_id": 2, "rank_delta": -100},
                ]
            },
            {
                "team_progress": [
                    {"team_id": 1, "rank_delta": -100},
                    {"team_id": 2, "rank_delta": 200},
                ]
            },
        ],
    }


def test_visible_counts_history_and_native_reverse_order_without_mutation():
    data = state()
    before = deepcopy(data)
    record = team_record(data, 1)
    assert (record.sorties, record.wins, record.pvp_wins) == (3, 2, 1)
    assert record.history == ((True, "Two"), (False, "Two"))
    assert record.wins_complete
    fields = decode_proto(_team_record(record))
    assert [first_field(fields, n).value for n in (1, 2, 6)] == [3, 2, 1]
    assert first_field(fields, 3).value == bytes(4)
    rows = [decode_proto(f.value) for f in fields if f.number == 4]
    assert [first_field(r, 1).value for r in rows] == [0, 1]  # native zero = win
    assert all(first_field(r, 2).value == b"Two" and first_field(r, 3).value == 0 for r in rows)
    assert data == before


def test_missing_historical_outcomes_are_not_invented():
    data = state()
    data["teams"]["1"]["team_world_sorties"] = 39
    record = team_record(data, 1)
    assert record.sorties == 39 and record.wins == 2
    assert not record.wins_complete


def test_saved_wins_and_names_survive_receipt_retention_and_team_rename():
    previous = state()
    updated = deepcopy(previous)
    rows = [{"team_id": 1, "rank_delta": 200}, {"team_id": 2, "rank_delta": -100}]
    advance_team_records(previous, updated, rows)
    updated["teams"]["1"]["team_world_sorties"] = 4
    updated["teams"]["1"]["team_pvp_wins"] = 2
    updated["world_battle_settlements"] = []
    updated["teams"]["2"]["name"] = "Renamed"
    record = team_record(updated, 1)
    assert (record.sorties, record.wins, record.pvp_wins) == (4, 3, 2)
    assert record.history[-1] == (True, "Two")


def test_five_row_limit_and_no_fake_opponent_for_burn():
    previous = state()
    for i in range(9):
        updated = deepcopy(previous)
        updated["teams"]["2"]["name"] = f"Opponent{i}"
        advance_team_records(
            previous,
            updated,
            [{"team_id": 1, "rank_delta": 200}, {"team_id": 2, "rank_delta": -100}],
        )
        previous = updated
    assert [name for _, name in team_record(previous, 1).history] == [
        f"Opponent{i}" for i in range(4, 9)
    ]
    updated = deepcopy(previous)
    advance_team_records(previous, updated, [{"team_id": 1, "rank_delta": 200}])
    assert team_record(updated, 1).history == team_record(previous, 1).history


def test_team_query_honors_requested_public_team_and_is_read_only():
    data = state()
    store = SimpleNamespace(state=data, team_for=lambda _: data["teams"]["1"])
    replies = []
    before = deepcopy(data)
    get_team_record(
        GameSession(b"token123", bytes(16), "Pilot", 11),
        GameMessage(0x3C0, bytes(4), proto_varint(1, 2)),
        teams=store,
        telemetry=SimpleNamespace(emit=lambda *a, **k: None),
        reply=replies.append,
    )
    assert first_field(decode_proto(replies[0]), 1).value == 2
    assert data == before
    assert team_record(data, 999).sorties == 0


def test_real_settlement_replay_reload_and_failure_are_atomic(tmp_path, monkeypatch):
    store = TeamStore(tmp_path / "teams")
    team = store.create(
        online_id="Pilot",
        player_id=11,
        name="One",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=2,
    )
    store.remember_player("Pilot", 11)
    store.synchronize_balance("Pilot", 100000)
    payment = (
        {
            "online_id": "Pilot",
            "player_id": 11,
            "gross_earnings": 100,
            "expenses": 0,
            "response_hex": "0800",
        },
    )
    kwargs = {
        "policy_version": "test",
        "team_progress": ({"team_id": team["team_id"], "rank_delta": 200, "special_delta": 1},),
    }
    store.settle_world_battle_money("aa" * 32, payment, **kwargs)
    before = deepcopy(store.state)
    store = TeamStore(store.root)
    store.settle_world_battle_money("aa" * 32, payment, **kwargs)
    assert store.state == before
    assert team_record(store.state, team["team_id"]).wins == 1

    def fail():
        raise OSError("test failure")

    monkeypatch.setattr(store, "_save", fail)
    with pytest.raises(OSError):
        store.settle_world_battle_money("bb" * 32, payment, **kwargs)
    assert store.state == before


@pytest.mark.parametrize("value", [-1, True, "3", 1 << 32])
def test_invalid_stored_totals_rejected(value):
    data = state()
    data["teams"]["1"]["team_world_sorties"] = value
    with pytest.raises(ValueError):
        team_record(data, 1)


@pytest.mark.parametrize("payload", [proto_varint(1, 0), proto_varint(1, 1) + proto_varint(1, 2), b"\xff"])
def test_bad_query_gets_correlated_error_without_state_change(payload):
    data = state()
    before = deepcopy(data)
    replies, errors = [], []
    get_team_record(
        GameSession(b"token123", bytes(16), "Pilot", 11),
        GameMessage(0x3c0, bytes(4), payload),
        teams=SimpleNamespace(state=data, team_for=lambda _: data["teams"]["1"]),
        telemetry=SimpleNamespace(emit=lambda *a, **k: None), reply=replies.append,
        reply_error=lambda *args: errors.append(args),
    )
    assert errors == [(1, 28)] and not replies and data == before
