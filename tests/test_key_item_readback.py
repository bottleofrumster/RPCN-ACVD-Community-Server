"""Authenticated emblem/garage KeyItem readback for Buy Decal and migration."""

from acvd_server.handlers.inventory import get_key_items
from acvd_server.inventory import key_item_list_response, key_item_pairs
from acvd_server.protocol import decode_proto, proto_bytes, proto_varint
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession


def upload(selector, row):
    return proto_bytes(3, proto_varint(1, selector) + proto_varint(2, row))


def request(selectors, *, cursor=0, limit=2048):
    return (
        b"".join(proto_varint(2, value) for value in selectors)
        + proto_varint(5, cursor)
        + proto_varint(6, limit)
    )


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


def test_key_item_wire_projection_uses_required_three_field_shape():
    payload = upload(0x20032, 1231) + upload(0x30001, 77)
    assert key_item_pairs(payload, frozenset((0x20032,))) == ((0x20032, 1231),)
    outer = decode_proto(key_item_list_response(((0x20032, 1231),)))
    assert len(outer) == 1 and outer[0].number == 1
    assert {field.number: field.value for field in decode_proto(outer[0].value)} == {
        1: 0,
        2: 0x20032,
        3: 1231,
    }


def test_native_garage_row_zero_round_trips_instead_of_blocking_step_two():
    pair = (0x30002, 0)
    payload = upload(*pair)
    assert key_item_pairs(payload, frozenset((pair[0],))) == (pair,)
    outer = decode_proto(key_item_list_response((pair,)))
    assert len(outer) == 1
    assert {field.number: field.value for field in decode_proto(outer[0].value)} == {
        1: 0,
        2: pair[0],
        3: 0,
    }


def test_handler_returns_native_zero_row_without_error_1_28(tmp_path):
    teams = TeamStore(tmp_path / "teams")
    players = tmp_path / "players"
    players.mkdir()
    actor = GameSession(b"actor001", bytes(16), "Tester", 42)
    teams.remember_player(actor.online_id, actor.player_id)
    (players / "42.offline-data.bin").write_bytes(upload(0x30002, 0))
    replies, errors, events = [], [], Events()
    get_key_items(
        actor,
        GameMessage(0x3E2, bytes(4), request((0x30002,), limit=64)),
        teams=teams,
        player_data_path=players,
        telemetry=events,
        reply=replies.append,
        reply_error=lambda *value: errors.append(value),
    )
    assert not errors and len(replies) == 1
    assert events.rows[-1] == (
        "game_key_item_list_sent",
        {
            "online_id": "Tester",
            "requested_selectors": 1,
            "owned_count": 1,
            "namespaces": [3],
            "identity_policy": "inert-zero-v1",
        },
    )


def test_handler_filters_requested_selectors_and_includes_story_receipt(tmp_path):
    teams = TeamStore(tmp_path / "teams")
    players = tmp_path / "players"
    players.mkdir()
    actor = GameSession(b"actor001", bytes(16), "Tester", 42)
    teams.remember_player(actor.online_id, actor.player_id)
    (players / "42.offline-data.bin").write_bytes(
        upload(0x20001, 10) + upload(0x20002, 20) + upload(0x30001, 30)
    )
    state = teams.state
    state["story_rewards"] = {
        "Tester:x": {
            "online_id": "Tester",
            "player_id": 42,
            "emblems": [{"selector": 0x20002, "row_id": 21}],
        }
    }
    teams.commit_state(state)
    replies, errors, events = [], [], Events()
    get_key_items(
        actor,
        GameMessage(0x3E2, bytes(4), request((0x20001, 0x20002))),
        teams=teams,
        player_data_path=players,
        telemetry=events,
        reply=replies.append,
        reply_error=lambda *value: errors.append(value),
    )
    assert not errors and len(replies) == 1
    rows = [
        {field.number: field.value for field in decode_proto(outer.value)}
        for outer in decode_proto(replies[0])
    ]
    assert rows == [
        {1: 0, 2: 0x20001, 3: 10},
        {1: 0, 2: 0x20002, 3: 20},
        {1: 0, 2: 0x20002, 3: 21},
    ]
    assert events.rows[-1][0] == "game_key_item_list_sent"


def test_empty_requested_namespace_is_initialized_empty_and_nonzero_cursor_rejects(tmp_path):
    teams = TeamStore(tmp_path / "teams")
    players = tmp_path / "players"
    players.mkdir()
    actor = GameSession(b"actor001", bytes(16), "Tester", 42)
    teams.remember_player(actor.online_id, actor.player_id)
    (players / "42.offline-data.bin").write_bytes(upload(0x20001, 10))
    replies, errors, events = [], [], Events()
    get_key_items(
        actor,
        GameMessage(0x3E2, bytes(4), request((0x90000,), limit=256)),
        teams=teams,
        player_data_path=players,
        telemetry=events,
        reply=replies.append,
        reply_error=lambda *value: errors.append(value),
    )
    assert replies == [b""] and not errors
    get_key_items(
        actor,
        GameMessage(0x3E2, bytes(4), request((0x20001,), cursor=1)),
        teams=teams,
        player_data_path=players,
        telemetry=events,
        reply=replies.append,
        reply_error=lambda *value: errors.append(value),
    )
    assert errors == [(1, 28)]
