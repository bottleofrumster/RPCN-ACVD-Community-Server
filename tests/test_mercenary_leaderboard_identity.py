"""Regression for October 4: rank was used as the AC lookup identity."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from acvd_server.handlers import mercenaries, snapshots
from acvd_server.mercenary_messages import (
    _mercenary_ranking_list_response,
    _mercenary_self_ranking_response,
)
from acvd_server.protocol import decode_proto, first_field, proto_varint
from acvd_server.teams import TeamStore
from acvd_server.udp import GET_AC_DATA, GameMessage, GameSession
from acvd_server.world_messages import CURRENT_SEASON_ID


def record(payload):
    return decode_proto(first_field(decode_proto(payload), 1).value)


def test_identity_score_and_position_are_distinct_for_list_and_self():
    # These intentionally differ: the old mapping displayed position 5,
    # score 0, and made AC lookup request player 1 instead of 167745.
    row = {
        "online_id": "CinnamonLyn",
        "player_id": 167745,
        "team_name": "Team",
        "rank": 1,
        "score": 5,
        "grade": 0,
    }
    for payload in (
        _mercenary_ranking_list_response((row,), 1),
        _mercenary_self_ranking_response(row["online_id"], "Team", row),
    ):
        fields = record(payload)
        assert [first_field(fields, n).value for n in (1, 5, 6)] == [167745, 5, 1]
        assert first_field(fields, 2).value == b"CinnamonLyn"


def test_selection_requests_the_ranked_players_actual_snapshot(tmp_path):
    row = {
        "online_id": "Ranked",
        "player_id": 167745,
        "team_name": "Team",
        "rank": 1,
        "score": 5,
        "grade": 0,
    }
    selected_id = first_field(record(_mercenary_ranking_list_response((row,), 1)), 1).value
    correct = b"\x00ranked-player-assembly"
    (tmp_path / "167745.ac-data.bin").write_bytes(correct)
    (tmp_path / "1.ac-data.bin").write_bytes(b"\x00wrong-player-assembly")
    replies = []
    snapshots.get_ac_data(
        GameSession(b"viewer00", bytes(16), "Viewer", 42),
        GameMessage(GET_AC_DATA, bytes(4), proto_varint(1, selected_id)),
        player_data_path=tmp_path,
        telemetry=SimpleNamespace(emit=lambda *args, **kwargs: None),
        reply=replies.append,
    )
    assert first_field(decode_proto(replies[0]), 1).value == correct


@pytest.mark.parametrize(
    "payload", [b"", proto_varint(1, CURRENT_SEASON_ID), proto_varint(1, CURRENT_SEASON_ID + 1)]
)
def test_empty_historical_or_invalid_self_keeps_authenticated_identity(tmp_path, payload):
    store = TeamStore(tmp_path / "teams")
    session = GameSession(b"viewer00", bytes(16), "Unranked", 443322)
    replies = []
    before = deepcopy(store.state)
    mercenaries.get_self_ranking(
        session,
        GameMessage(0x3D1, bytes(4), payload),
        team_store=store,
        telemetry=SimpleNamespace(emit=lambda *args, **kwargs: None),
        reply=replies.append,
    )
    fields = record(replies[0])
    assert [first_field(fields, n).value for n in (1, 5, 6)] == [443322, 0, 0]
    assert store.state == before


def test_score_projection_respects_signed_wire_bound_without_mutating_history():
    row = {
        "online_id": "Ranked",
        "player_id": 167745,
        "team_name": "Team",
        "rank": 2,
        "score": (1 << 32) - 1,
        "grade": 6,
    }
    before = dict(row)
    fields = record(_mercenary_ranking_list_response((row,), 1))
    assert first_field(fields, 5).value == (1 << 31) - 1
    assert first_field(fields, 6).value == 2
    assert row == before
