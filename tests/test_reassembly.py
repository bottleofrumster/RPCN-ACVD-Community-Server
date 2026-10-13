"""Application-fragment boundary checks without sockets or live player state."""

from pathlib import Path

import pytest

from acvd_server.reassembly import (
    FRAGMENT_GROUP_IDLE_SECONDS,
    FRAGMENT_GROUP_MAX_AGE_SECONDS,
    MAX_FRAGMENT_GROUPS,
    MAX_REASSEMBLED_BYTES,
    assemble_request,
)
from acvd_server.server import GameDatagramProtocol
from acvd_server.teams import TeamStore
from acvd_server.udp import (
    DAT,
    GameMessage,
    GameSession,
    ReliablePacket,
    encode_game_request_fragments,
)


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


@pytest.fixture
def session():
    return GameSession(b"owner001", bytes(16), "Owner", 11)


def packet(raw, sequence=1):
    return ReliablePacket(sequence, 0, DAT, payload=raw)


def request(body=b"data", counter=7):
    return GameMessage(0x03F6, b"\1\2\3\4", body, counter)


def test_single_fragment_keeps_identity_and_does_not_mutate_packet(session):
    expected = request()
    raw = encode_game_request_fragments(expected)[0]
    source = packet(raw)
    assert assemble_request(session, source, telemetry=Events()) == expected
    assert source.payload == raw and not session.fragment_parts and not session.fragment_totals


def test_large_reverse_order_with_duplicate_fragment_preserves_40_bit_total(session):
    expected = request(bytes(range(256)) * 781)
    fragments = encode_game_request_fragments(expected)
    assert int.from_bytes(fragments[0][3:8], "big") > 65535
    telemetry = Events()
    assert assemble_request(session, packet(fragments[-1]), telemetry=telemetry) is None
    result = None
    for index, raw in enumerate(reversed(fragments)):
        result = assemble_request(session, packet(raw), telemetry=telemetry)
        if index < len(fragments) - 1:
            assert result is None
    assert result == expected and not session.fragment_parts and not session.fragment_totals
    assert not session.fragment_started_monotonic
    assert not session.fragment_updated_monotonic


def test_voice_of_war_upload_uses_complete_16_bit_fragment_indexes(session):
    # Exact crash-time declared total: 335,696 bytes including the 12-byte
    # message header, producing native fragment indexes0 through331.
    body = (bytes(range(256)) * 1312)[:335_684]
    expected = request(body)
    fragments = encode_game_request_fragments(expected)
    assert len(fragments) == 332
    assert int.from_bytes(fragments[0][3:8], "big") == 335_696
    assert [int.from_bytes(fragments[index][8:10], "big") for index in (0, 255, 256)] == [
        0,
        255,
        256,
    ]
    result = None
    for raw in fragments:
        result = assemble_request(session, packet(raw), telemetry=Events())
    assert result == expected
    assert not session.fragment_parts and not session.fragment_updated_monotonic


def test_interleaved_request_counters_do_not_mix_bodies(session):
    expected = [request(b"A" * 80, 7), request(b"B" * 80, 8)]
    groups = [encode_game_request_fragments(item, max_fragment_length=23) for item in expected]
    received = []
    for pair in zip(*groups):
        for raw in pair:
            value = assemble_request(session, packet(raw), telemetry=Events())
            if value is not None:
                received.append(value)
    assert received == expected and not session.fragment_parts


@pytest.mark.parametrize("raw", [b"", bytes(11), bytes(12)])
def test_bad_headers_or_inner_messages_are_rejected(session, raw):
    events = Events()
    assert assemble_request(session, packet(raw), telemetry=events) is None
    assert events.rows[-1][0] == "game_message_rejected"


@pytest.mark.parametrize("change", ["compressed", "length"])
def test_unsupported_compression_and_bad_fragment_length_are_rejected(session, change):
    raw = bytearray(encode_game_request_fragments(request())[0])
    if change == "compressed":
        raw[2] = 1
    else:
        raw.pop()
    events = Events()
    assert assemble_request(session, packet(bytes(raw)), telemetry=events) is None
    assert events.rows[-1][0] == "game_message_rejected"
    assert not session.fragment_parts


def test_changed_total_does_not_replace_valid_partial_state(session):
    expected = request(b"A" * 80)
    fragments = encode_game_request_fragments(expected, max_fragment_length=23)
    assemble_request(session, packet(fragments[0]), telemetry=Events())
    changed = bytearray(fragments[1])
    changed[3:8] = (93).to_bytes(5, "big")
    events = Events()
    assert assemble_request(session, packet(bytes(changed)), telemetry=events) is None
    assert events.rows[-1][1]["reason"] == "fragment total changed"
    for raw in fragments[1:]:
        result = assemble_request(session, packet(raw), telemetry=Events())
    assert result == expected


def test_missing_index_never_delivers_a_message(session):
    fragments = encode_game_request_fragments(request(b"A" * 30), max_fragment_length=21)
    assert len(fragments) == 2
    changed = bytearray(fragments[1])
    changed[9] = 3
    assert assemble_request(session, packet(fragments[0]), telemetry=Events()) is None
    assert assemble_request(session, packet(bytes(changed)), telemetry=Events()) is None


def test_oversized_group_is_rejected_before_cache_allocation(session):
    raw = bytearray(encode_game_request_fragments(request(b"A" * 80), max_fragment_length=23)[0])
    raw[3:8] = (MAX_REASSEMBLED_BYTES + 1).to_bytes(5, "big")
    events = Events()
    assert assemble_request(session, packet(bytes(raw)), telemetry=events) is None
    assert events.rows[-1][1]["reason"] == "fragment total is outside the supported bound"
    assert not session.fragment_parts and not session.fragment_totals


def test_fragment_group_count_is_bounded(session):
    events = Events()
    for counter in range(MAX_FRAGMENT_GROUPS + 1):
        raw = encode_game_request_fragments(
            request(b"A" * 80, counter), max_fragment_length=23
        )[0]
        assert assemble_request(session, packet(raw), telemetry=events) is None
    assert len(session.fragment_parts) == MAX_FRAGMENT_GROUPS
    assert events.rows[-1][1]["reason"] == "fragment cache capacity reached"


def test_changed_duplicate_cannot_overwrite_valid_fragment(session):
    first = encode_game_request_fragments(request(b"A" * 80), max_fragment_length=23)[0]
    assert assemble_request(session, packet(first), telemetry=Events()) is None
    changed = bytearray(first)
    changed[-1] ^= 0xFF
    events = Events()
    assert assemble_request(session, packet(bytes(changed)), telemetry=events) is None
    assert events.rows[-1][1]["reason"] == "fragment duplicate changed"
    assert session.fragment_parts[7][0] == first[12:]


def test_expired_group_is_purged_before_accepting_new_work(session, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("acvd_server.reassembly.time.monotonic", lambda: clock[0])
    first = encode_game_request_fragments(request(b"A" * 80, 7), max_fragment_length=23)[0]
    second = encode_game_request_fragments(request(b"B" * 80, 8), max_fragment_length=23)[0]
    assert assemble_request(session, packet(first), telemetry=Events()) is None
    clock[0] += FRAGMENT_GROUP_MAX_AGE_SECONDS + 0.1
    events = Events()
    assert assemble_request(session, packet(second), telemetry=events) is None
    assert 7 not in session.fragment_parts and 8 in session.fragment_parts
    assert events.rows[0][0] == "game_fragment_group_expired"


def test_actively_progressing_upload_survives_beyond_idle_window(session, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("acvd_server.reassembly.time.monotonic", lambda: clock[0])
    expected = request(b"A" * 160, 7)
    fragments = encode_game_request_fragments(expected, max_fragment_length=23)
    assert len(fragments) >= 7
    result = None
    for raw in fragments:
        result = assemble_request(session, packet(raw), telemetry=Events())
        clock[0] += FRAGMENT_GROUP_IDLE_SECONDS / 2
    assert clock[0] - 100.0 > FRAGMENT_GROUP_IDLE_SECONDS
    assert result == expected


def test_duplicate_fragments_do_not_extend_idle_lifetime(session, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("acvd_server.reassembly.time.monotonic", lambda: clock[0])
    first = encode_game_request_fragments(request(b"A" * 80, 7), max_fragment_length=23)[0]
    other = encode_game_request_fragments(request(b"B" * 80, 8), max_fragment_length=23)[0]
    assert assemble_request(session, packet(first), telemetry=Events()) is None
    clock[0] += FRAGMENT_GROUP_IDLE_SECONDS / 2
    assert assemble_request(session, packet(first), telemetry=Events()) is None
    clock[0] += FRAGMENT_GROUP_IDLE_SECONDS / 2 + 0.1
    events = Events()
    assert assemble_request(session, packet(other), telemetry=events) is None
    assert 7 not in session.fragment_parts and 8 in session.fragment_parts
    assert events.rows[0][0] == "game_fragment_group_expired"


def test_dispatch_occurs_once_only_after_complete_request(tmp_path, monkeypatch, session):
    protocol = GameDatagramProtocol(
        Events(),
        {},
        Path("native/libacvd_cwc.so"),
        tmp_path / "players",
        TeamStore(tmp_path / "teams"),
    )
    calls = []
    monkeypatch.setattr(protocol, "_dispatch_game_message", lambda *args: calls.append(args))
    expected = request(b"A" * 80)
    fragments = encode_game_request_fragments(expected, max_fragment_length=23)
    for raw in fragments[:-1]:
        protocol._handle_game_payload(session, packet(raw))
    assert calls == []
    last = packet(fragments[-1], 99)
    protocol._handle_game_payload(session, last)
    assert calls == [(session, last, expected)]
