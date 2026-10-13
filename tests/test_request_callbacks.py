"""Routing keeps callbacks scoped to the exact request and preserves error evidence."""

import json
from pathlib import Path

import pytest

from acvd_server.handlers import shop, world
from acvd_server.server import GameDatagramProtocol
from acvd_server.teams import TeamStore
from acvd_server.telemetry import Telemetry
from acvd_server.udp import (
    DAT,
    GET_NOTICE_LIST,
    GET_TRASH_PARTS_LIST,
    GameMessage,
    GameSession,
    ReliablePacket,
)


@pytest.fixture
def protocol(tmp_path):
    return GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        {},
        Path(__file__).resolve().parents[1] / "native/libacvd_cwc.so",
        tmp_path / "players",
        TeamStore(tmp_path / "teams"),
    )


def test_replies_keep_request_identity_across_dispatches(protocol, monkeypatch):
    callbacks, sent = [], []
    monkeypatch.setattr(shop, "get_trash_parts", lambda *a, **kw: callbacks.append(kw["reply"]))
    monkeypatch.setattr(protocol, "_send_game_reply", lambda *a, **kw: sent.append((a, kw)))
    contexts = []
    for index in (7, 8):
        session = GameSession(bytes([index]) * 8, bytes(range(16)), f"Pilot{index}", index)
        packet = ReliablePacket(index, 0, DAT)
        message = GameMessage(GET_TRASH_PARTS_LIST, index, b"")
        contexts.append((session, packet, message))
        protocol._dispatch_game_message(session, packet, message)
    monkeypatch.setattr(
        protocol, "_send_game_reply", lambda *a, **kw: pytest.fail("success sender rebound")
    )
    callbacks[1](b"second")
    callbacks[0](b"first")
    assert sent == [(contexts[1] + (b"second",), {}), (contexts[0] + (b"first",), {})]


@pytest.mark.parametrize(
    "opcode,module,handler,observed",
    [
        (GET_TRASH_PARTS_LIST, shop, "get_trash_parts", False),
        (GET_NOTICE_LIST, world, "get_notice_list", True),
    ],
)
def test_error_callbacks_preserve_correlation_and_event_policy(
    protocol,
    monkeypatch,
    opcode,
    module,
    handler,
    observed,
):
    callbacks, sent = [], []
    monkeypatch.setattr(module, handler, lambda *a, **kw: callbacks.append(kw["reply_error"]))
    session = GameSession(b"token123", bytes(range(16)), "Pilot", 42)
    packet = ReliablePacket(5, 0, DAT)
    message = GameMessage(opcode, 17, b"")
    protocol._dispatch_game_message(session, packet, message)
    # These error closures resolve the current sender when invoked, unlike partial replies.
    monkeypatch.setattr(protocol, "_send_game_reply", lambda *a, **kw: sent.append((a, kw)))
    callbacks[0](2, 3)
    assert sent == [((session, packet, message, b""), {"error": (2, 3)})]
    rows = [json.loads(line) for line in protocol.telemetry.path.read_text().splitlines()]
    errors = [row for row in rows if row["event"] == "game_reply_error_sent"]
    assert len(errors) == int(observed)
    if observed:
        assert errors[0]["message_index"] == 17
        assert (errors[0]["result_code"], errors[0]["detail_code"]) == (2, 3)


def test_observed_error_resolves_current_error_sender(protocol, monkeypatch):
    callbacks, sent = [], []
    monkeypatch.setattr(
        world, "get_notice_list", lambda *a, **kw: callbacks.append(kw["reply_error"])
    )
    session = GameSession(b"token123", bytes(range(16)), "Pilot", 42)
    packet = ReliablePacket(5, 0, DAT)
    message = GameMessage(GET_NOTICE_LIST, 17, b"")
    protocol._dispatch_game_message(session, packet, message)
    monkeypatch.setattr(protocol, "_send_game_error", lambda *a: sent.append(a))
    callbacks[0](2, 3)
    assert sent == [(session, packet, message, 2, 3)]
