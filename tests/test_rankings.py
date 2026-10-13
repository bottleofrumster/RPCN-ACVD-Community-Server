"""Dynamic leaderboard projections use only durable, attributable facts."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from acvd_server.handlers import world
from acvd_server.mercenary_messages import _mercenary_ranking_list_response
from acvd_server.protocol import ProtocolError, decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.rankings import (
    decode_page,
    defeat_rows,
    mercenary_rows,
    page,
    player_rows,
    team_rows,
)
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession
from acvd_server.world_messages import (
    _defeat_ranking_list_response,
    _player_ranking_list_response,
)


def create_team(store, name, player_id, faction, fame, sorties):
    team = store.create(
        online_id=name,
        player_id=player_id,
        name=name,
        operation_base=0,
        language=0,
        play_type=0,
        enlistment_method=1,
    )
    updated = deepcopy(store.state)
    row = updated["teams"][str(team["team_id"])]
    row["faction_id"] = row["current_faction_id"] = faction
    row["team_fame_points"] = fame
    row["team_world_sorties"] = sorties
    updated["player_profiles"][name]["player_id"] = player_id
    store.commit_state(updated)
    return team["team_id"]


def ranking_page(season=3, offset=0, limit=100, *, mercenary=False):
    nested = proto_varint(1, offset) + proto_varint(2, limit)
    if mercenary:
        return proto_bytes(1, nested) + proto_varint(2, season)
    return proto_varint(1, season) + proto_bytes(2, nested)


def test_team_board_sorts_global_current_season_scores_and_pages(tmp_path):
    store = TeamStore(tmp_path / "teams")
    first = create_team(store, "First", 11, 3, 500, 2)
    second = create_team(store, "Second", 22, 3, 500, 1)
    other = create_team(store, "Other", 33, 2, 999, 5)
    create_team(store, "Inactive", 44, 3, 1000, 0)

    rows = team_rows(store)
    assert [(row["team_id"], row["rank"], row["score"]) for row in rows] == [
        (other, 1, 999),
        (first, 2, 500),
        (second, 3, 500),
    ]
    request = decode_page(ranking_page(offset=1, limit=1))
    assert page(rows, request) == (rows[1],)


def test_player_destroy_and_mercenary_boards_reject_unknown_as_zero(tmp_path):
    store = TeamStore(tmp_path / "teams")
    team_id = create_team(store, "Pilot", 11, 3, 100, 1)
    assert player_rows(store) == defeat_rows(store) == mercenary_rows(store) == ()

    now = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    updated = deepcopy(store.state)
    profile = updated["player_profiles"]["Pilot"]
    profile["player_medals"] = {
        "player_id": 11,
        "records": [
            {
                "medal_id": 1,
                "award_count": 1,
                "progress_value": 0,
                "first_date": now,
                "second_date": now,
            }
        ],
    }
    profile["observed_ac_destructions"] = 4
    profile["observed_mercenary_sorties"] = 3
    profile["observed_mercenary_wins"] = 2
    profile["observed_mercenary_earnings"] = 250_000
    profile["mercenary_rank"] = 0
    store.commit_state(updated)

    assert player_rows(store) == (
        {"player_id": 11, "team_id": team_id, "score": 100, "rank": 1,
         "online_id": "Pilot", "team_name": "Pilot"},
    )
    assert defeat_rows(store)[0] | {} == {
        "player_id": 11,
        "online_id": "Pilot",
        "team_id": team_id,
        "team_name": "Pilot",
        "score": 4,
        "rank": 1,
    }
    assert mercenary_rows(store)[0]["score"] == 250_000
    assert mercenary_rows(store)[0]["sorties"] == 3


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        ranking_page(season=0),
        ranking_page(limit=0),
        ranking_page(limit=101),
        ranking_page() + proto_varint(3, 1),
    ],
)
def test_ranking_page_rejects_malformed_or_unbounded_requests(payload):
    with pytest.raises(ProtocolError):
        decode_page(payload)


def test_team_handler_returns_season_and_page_entries(tmp_path):
    store = TeamStore(tmp_path / "teams")
    create_team(store, "Ranked", 11, 3, 321, 1)
    session = GameSession(b"rank0001", bytes(16), "Ranked", 11)
    replies, events = [], []

    class Events:
        def emit(self, event, **fields):
            events.append((event, fields))

    world.get_team_ranking_list(
        session,
        GameMessage(0x406, bytes(4), ranking_page(), 1),
        teams=store,
        telemetry=Events(),
        reply=replies.append,
    )
    fields = decode_proto(replies[0])
    assert first_field(fields, 1).value == 3  # Requested season, not the one-row count.
    info = decode_proto(first_field(fields, 2).value)
    assert [first_field(info, number).value for number in (1, 2, 3, 4)] == [
        1,
        b"Ranked",
        321,
        1,
    ]
    assert events[-1][1]["total"] == events[-1][1]["count"] == 1


def test_populated_player_defeat_and_mercenary_wire_records_are_initialized():
    player = {"player_id": 11, "team_id": 7, "rank": 2, "score": 500,
              "online_id": "Pilot", "team_name": "Team"}
    fields = decode_proto(_player_ranking_list_response((player,), 1))
    assert first_field(fields, 1).value == 1
    info = decode_proto(first_field(fields, 2).value)
    assert [first_field(info, number).value for number in (1, 4, 5)] == [11, 500, 2]
    assert [first_field(info, number).value for number in (2, 3)] == [b"Pilot", b"Team"]

    identity = {
        "player_id": 11,
        "online_id": "Pilot",
        "team_name": "Team",
        "team_id": 7,
        "rank": 2,
        "score": 4,
        "grade": 0,
    }
    fields = decode_proto(_defeat_ranking_list_response((identity,), 1))
    defeat = decode_proto(first_field(fields, 2).value)
    assert [first_field(defeat, key).value for key in range(1, 7)] == [
        11,
        0,
        b"Pilot",
        b"Team",
        4,
        2,
    ]
    fields = decode_proto(_mercenary_ranking_list_response((identity,), 1))
    mercenary = decode_proto(first_field(fields, 1).value)
    assert [first_field(mercenary, key).value for key in (1, 2, 4, 5, 6)] == [
        11,
        b"Pilot",
        b"Team",
        4,
        2,
    ]
    assert first_field(fields, 2).value == 1
