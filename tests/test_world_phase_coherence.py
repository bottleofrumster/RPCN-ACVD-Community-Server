"""Both world reply paths must agree on the client's shared phase cache."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from acvd_server.protocol import decode_proto, first_field
from acvd_server.server import GameDatagramProtocol
from acvd_server.teams import TeamStore
from acvd_server.udp import (
    DAT,
    GET_WORLD_BASIC_INFO,
    GET_WORLD_CURRENT_INFO,
    GameMessage,
    GameSession,
    ReliablePacket,
    encode_game_request,
)
from acvd_server.world_messages import _world_basic_info, _world_current_info


def phase_from_reply(body, opcode):
    world = decode_proto(first_field(decode_proto(body), 1).value)
    record = decode_proto(first_field(world, 2).value) if opcode == GET_WORLD_BASIC_INFO else world
    return first_field(record, 3).value


def test_default_basic_and_current_phase_agree():
    now = datetime(2026, 9, 11, tzinfo=timezone.utc)
    basic = decode_proto(_world_basic_info(now))
    season = decode_proto(first_field(basic, 2).value)
    current = decode_proto(_world_current_info(now))
    assert first_field(season, 3).value == first_field(current, 3).value == 2


def test_season_phase_and_special_requirement_change_together():
    now = datetime(2026, 9, 11, tzinfo=timezone.utc)
    old = decode_proto(_world_basic_info(now, phase=0))
    new = decode_proto(_world_basic_info(now, phase=2))
    assert [a.number for a, b in zip(old, new, strict=True) if a != b] == [2, 3]
    assert first_field(old, 3).value == 0
    assert first_field(new, 3).value == 6
    old_season = decode_proto(first_field(old, 2).value)
    new_season = decode_proto(first_field(new, 2).value)
    assert [
        (a.number, a.value, b.value) for a, b in zip(old_season, new_season, strict=True) if a != b
    ] == [(3, 0, 2)]
    assert first_field(new, 3).value == 6
    assert first_field(new_season, 1).value == 3


@pytest.mark.parametrize("phase", [1, 3, True, "2", None])
def test_basic_info_rejects_unqualified_phase_values(phase):
    with pytest.raises(ValueError, match="world phase"):
        _world_basic_info(datetime.now(timezone.utc), phase=phase)


def test_dispatch_uses_one_policy_for_basic_current_basic_and_transitions(tmp_path, monkeypatch):
    class Events:
        def emit(self, *args, **kwargs):
            pass

    phase = [2]
    actor = GameSession(
        b"phase001", bytes(16), "PhaseTest", 101, established=True, peer=("127.0.0.1", 11001)
    )
    store = TeamStore(tmp_path / "teams")
    before = deepcopy(store.state)
    protocol = GameDatagramProtocol(
        Events(),
        {actor.auth_token: actor},
        Path("unused.so"),
        tmp_path / "players",
        store,
        get_world_state=lambda: {"phase": phase[0]},
    )
    replies = []
    monkeypatch.setattr(
        protocol,
        "_send_game_reply",
        lambda _s, _p, message, body: replies.append((message.message_type, body)),
    )
    for value in (2, 0, 2):
        phase[0] = value
        for opcode in (GET_WORLD_BASIC_INFO, GET_WORLD_CURRENT_INFO, GET_WORLD_BASIC_INFO):
            index = len(replies) + 1
            packet = ReliablePacket(
                index,
                0,
                DAT,
                payload=encode_game_request(
                    GameMessage(opcode, index.to_bytes(4, "big"), b"", index)
                ),
            )
            protocol._handle_game_payload(actor, packet)
            assert len(replies) == index
            assert phase_from_reply(replies[-1][1], opcode) == value
    assert store.state == before
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


def test_dispatch_exposes_locked_map_rows_from_the_live_operator_policy(
    tmp_path, monkeypatch
):
    class Events:
        def emit(self, *args, **kwargs):
            pass

    actor = GameSession(
        b"maps0001",
        bytes(16),
        "MapTest",
        102,
        established=True,
        peer=("127.0.0.1", 11002),
    )
    settings = {"cq_map_mode": "locked_pool", "cq_map_pool": [3200, 3410]}
    protocol = GameDatagramProtocol(
        Events(),
        {actor.auth_token: actor},
        Path("unused.so"),
        tmp_path / "players",
        TeamStore(tmp_path / "teams"),
        get_cq_map_settings=lambda: settings,
    )
    replies = []
    monkeypatch.setattr(
        protocol,
        "_send_game_reply",
        lambda _s, _p, message, body: replies.append((message.message_type, body)),
    )

    for opcode in (GET_WORLD_BASIC_INFO, GET_WORLD_CURRENT_INFO):
        index = len(replies) + 1
        packet = ReliablePacket(
            index,
            0,
            DAT,
            payload=encode_game_request(
                GameMessage(opcode, index.to_bytes(4, "big"), b"", index)
            ),
        )
        protocol._handle_game_payload(actor, packet)

    basic = decode_proto(first_field(decode_proto(replies[0][1]), 1).value)
    current = decode_proto(first_field(decode_proto(replies[1][1]), 1).value)
    assert sum(field.number == 4 for field in basic) == 7
    assert sum(field.number == 4 for field in current) == 7
