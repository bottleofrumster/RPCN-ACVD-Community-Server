"""World request lifecycle: preserve compatibility, do not certify placeholders."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from acvd_server.handlers import world
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


class FrozenDate(datetime):
    @classmethod
    def now(cls, tz=None):
        assert tz is timezone.utc
        return cls(2026, 9, 7, 5, 22, 0, 123000, tzinfo=timezone.utc)


@pytest.fixture
def session():
    return GameSession(b"owner001", bytes(16), "Owner", 11)


def invoke(session, function, payload=b"", **kwargs):
    replies, events = [], Events()
    function(
        session,
        GameMessage(0, b"\0\0\0\1", payload, 1),
        telemetry=events,
        reply=replies.append,
        **kwargs,
    )
    assert len(replies) == 1
    return decode_proto(replies[0]), events


def test_datetime_uses_utc_and_retains_milliseconds(session, monkeypatch):
    monkeypatch.setattr(world, "datetime", FrozenDate)
    fields, events = invoke(session, world.get_server_datetime)
    date = decode_proto(first_field(fields, 1).value)
    assert [first_field(date, n).value for n in range(1, 8)] == [2026, 9, 7, 5, 22, 0, 123]
    assert events.rows[-1][1]["timestamp_utc"] == "2026-09-07T05:22:00.123000+00:00"


@pytest.mark.parametrize("cursor,expected", [(0, 1), (2, 0), (99, 0)])
def test_world_log_cursor_terminates_the_fetch(session, monkeypatch, cursor, expected):
    monkeypatch.setattr(world, "datetime", FrozenDate)
    fields, events = invoke(session, world.get_world_log, proto_varint(2, cursor))
    assert sum(f.number == 4 for f in fields) == 0
    assert sum(f.number == 5 for f in fields) == expected
    assert events.rows[-1][1]["world_phase_count"] == expected
    assert events.rows[-1][1]["area_phase_count"] == 0


def test_faction_selection_is_read_only_and_scoped_to_requester(session, tmp_path):
    store = TeamStore(tmp_path / "teams")
    store.create(
        online_id="Owner",
        player_id=11,
        name="Example",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=2,
    )
    store.set_faction("Owner", 1, 2)
    before = deepcopy(store.state)
    fields, _ = invoke(session, world.get_faction_selection, teams=store)
    assert first_field(fields, 2).value == 2 and store.state == before


def test_faction_result_retains_promotion_before_reply(session):
    order = []

    class Store:
        def test_war_snapshot(self):
            return None

        def promote_faction(self, online_id):
            assert online_id == "Owner"
            order.append("promoted")
            return {"current_faction_id": 2, "territory_area_id": 1}

    def reply(payload):
        assert order == ["promoted"] and payload
        order.append("replied")

    events = Events()
    world.get_faction_struggle_result(
        session, GameMessage(0, b"\0\0\0\1", b"", 1), teams=Store(), telemetry=events, reply=reply
    )
    assert order == ["promoted", "replied"]
    assert events.rows[-1][1]["current_faction_id"] == 2


def test_missing_team_ranking_is_empty_not_an_invented_team(session, tmp_path):
    store = TeamStore(tmp_path / "teams")
    before = deepcopy(store.state)
    fields, events = invoke(
        session, world.get_my_team_ranking, proto_varint(1, 3), teams=store
    )
    assert fields == [] and store.state == before
    assert events.rows[-1][0] == "game_my_team_ranking_missing"


def test_empty_team_ranking_preserves_requested_season_field(session, tmp_path):
    payload = proto_varint(1, 3) + proto_bytes(
        2, proto_varint(1, 0) + proto_varint(2, 100)
    )
    fields, _ = invoke(
        session, world.get_team_ranking_list, payload, teams=TeamStore(tmp_path / "teams")
    )
    assert first_field(fields, 1).value == 3


def test_compatibility_empty_ranking_preserves_required_count_field(session):
    fields, _ = invoke(session, world.get_empty_ranking_list)
    assert first_field(fields, 1).value == 0
