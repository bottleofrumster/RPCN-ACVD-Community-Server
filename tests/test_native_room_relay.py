"""Native room-notify shape and match scope; synthetic data, no live clients."""

import copy
import time
import zlib

import pytest

from acvd_server.handlers.matching import notify_room
from acvd_server.matchmaking import MatchmakingCoordinator
from acvd_server.protocol import decode_proto, proto_bytes, proto_varint
from acvd_server.room_messages import validate_room_archive
from acvd_server.udp import NOTIFY_MATCH_ROOM_INFO, GameMessage, GameSession

RAW = b"NXRV\x03\xf2" + bytes(506)
ENCODED = b"\x01" + zlib.compress(RAW)


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **data):
        self.rows.append((event, data))


@pytest.fixture
def fixture():
    calls = []
    events = Events()
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        send_push=lambda actor, body, **kwargs: calls.append((actor.online_id, body, kwargs)),
        telemetry=events,
        room_log_paths=[],
    )
    members = []
    for name, number in (("Host", 101), ("Guest", 202)):
        actor = GameSession(
            name.encode().ljust(8, b"0"),
            bytes(16),
            name,
            number,
            established=True,
            peer=("127.0.0.1", number),
        )
        members.append({"session": actor, "prepare_id": number, "match_id": 1, "team_id": number})
    match = {
        "match_id": 1,
        "members": members,
        "criteria": {},
        "combat_area_id": 3,
        "created_monotonic": time.monotonic(),
    }
    coordinator.preparations = {m["session"].online_id: m for m in members}
    coordinator.matches = {1: match}
    return coordinator, members, match, calls, events


def send(fixture, actor=None, key=101, archive=ENCODED):
    coordinator, members, _, calls, events = fixture
    message = GameMessage(
        NOTIFY_MATCH_ROOM_INFO, bytes(4), proto_varint(1, key) + proto_bytes(2, archive), 1
    )
    notify_room(
        actor or members[0]["session"],
        message,
        matchmaking=coordinator,
        telemetry=events,
        reply=lambda body: calls.append(("ACK", body, {})),
    )


def test_forwarding_keeps_compressed_bytes_and_preparation_ids(fixture):
    fixture[2]["territory_layout_owner_id"] = 50627
    send(fixture)
    _, _, match, calls, _ = fixture
    assert NOTIFY_MATCH_ROOM_INFO == 0x03BC
    assert [c[0] for c in calls] == ["ACK", "Guest", "Guest"]
    assert calls[1][2] == {"subtype": 2}
    assert {f.number: f.value for f in decode_proto(calls[1][1])}[7] == 50627
    assert calls[2][2] == {"subtype": 5}
    assert [(f.number, f.value) for f in decode_proto(calls[2][1])] == [
        (1, 202),
        (2, 101),
        (3, ENCODED),
    ]
    assert match["native_room_archive"] == ENCODED
    send(fixture)
    assert len(calls) == 4 and calls[-1][0] == "ACK"
    send(fixture, archive=b"\0" + RAW)
    assert len(calls) == 4  # Re-encoding is not an exact replay.


@pytest.mark.parametrize("practice", [True, False])
def test_native_room_release_keeps_the_frozen_classification(fixture, practice):
    fixture[2]["is_intra_faction"] = practice
    send(fixture)
    calls = fixture[3]
    assert calls[1][2] == {"subtype": 2}
    fields = {f.number: f.value for f in decode_proto(calls[1][1])}
    assert fields[9] == int(practice) and fields[8] == 0


@pytest.mark.parametrize(
    "case",
    [
        "guest",
        "orphan",
        "stale_session",
        "wrong_key",
        "expired",
        "preflight",
        "unreleased",
        "disconnected",
        "missing_guest",
        "bridge",
    ],
)
def test_rejected_sender_or_state_has_no_reply_or_publication(fixture, case):
    coordinator, members, match, calls, events = fixture
    actor, key = members[0]["session"], 101
    if case == "guest":
        actor, key = members[1]["session"], 202
    elif case == "orphan":
        coordinator.matches.clear()
    elif case == "stale_session":
        actor = copy.copy(actor)
    elif case == "wrong_key":
        key = 202
    elif case == "expired":
        match["created_monotonic"] -= 181
    elif case == "preflight":
        match["preflight_retry_pending"] = {"Guest"}
    elif case == "unreleased":
        match.pop("combat_area_id")
    elif case == "disconnected":
        actor.established = False
    elif case == "missing_guest":
        coordinator.preparations.pop("Guest")
    elif case == "bridge":
        match["counterparts_released"] = True
    send(fixture, actor, key)
    assert not calls and "native_room_archive" not in match
    assert events.rows[-1][0] == "game_message_rejected"


@pytest.mark.parametrize(
    "encoded",
    [
        b"",
        b"\0",
        b"PRVTPSWD",
        b"\0" + RAW[:-1],
        b"\0" + RAW + b"x",
        b"\0" + bytes(512),
        b"\2" + RAW,
        ENCODED[:-1],
        ENCODED + b"junk",
        ENCODED + ENCODED[1:],
        b"\1" + zlib.compress(b"X" * 1000000),
        bytes(1025),
    ],
)
def test_invalid_archive_is_bounded_and_has_no_state_change(fixture, encoded):
    with pytest.raises(ValueError):
        validate_room_archive(encoded)
    send(fixture, archive=encoded)
    assert not fixture[3] and "native_room_archive" not in fixture[2]


def test_valid_raw_archive():
    validate_room_archive(b"\0" + RAW)
