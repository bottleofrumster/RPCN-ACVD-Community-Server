"""Encrypted loss/reorder controls. No sockets, game clients or real saves."""

from dataclasses import replace
from pathlib import Path

import pytest

from acvd_server.request_recovery import MAX_ATTEMPTS, MAX_RECOVERY_SESSIONS, RequestRecovery
from acvd_server.server import GameDatagramProtocol
from acvd_server.teams import TeamStore
from acvd_server.telemetry import Telemetry
from acvd_server.udp import (
    ACK,
    DAT,
    FIN,
    HBT,
    RETRANSMIT,
    RST,
    SYN,
    GameMessage,
    GameSession,
    ReliablePacket,
    decode_reliable_packet,
    decrypt_server_datagram,
    encode_game_request_fragments,
    encode_reliable_packet,
    encrypt_client_datagram,
)

LIBRARY = Path(__file__).resolve().parents[1] / "native/libacvd_cwc.so"


class Timer:
    def __init__(self, delay, callback, args):
        self.delay, self.callback, self.args = delay, callback, args
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        assert not self.cancelled
        self.cancelled = True
        self.callback(*self.args)


class Loop:
    def __init__(self):
        self.timers = []

    def call_later(self, delay, callback, *args):
        timer = Timer(delay, callback, args)
        self.timers.append(timer)
        return timer

    def fire(self):
        next(t for t in self.timers if not t.cancelled).fire()

    @property
    def active(self):
        return [t for t in self.timers if not t.cancelled]


@pytest.fixture
def stream(tmp_path, monkeypatch):
    session = GameSession(b"test0001", bytes(range(16)), "Pilot", 11,
                          established=True, peer=("127.0.0.1", 12345))
    protocol = GameDatagramProtocol(Telemetry(tmp_path/"events.jsonl"),
        {session.auth_token: session}, LIBRARY, tmp_path/"players", TeamStore(tmp_path/"teams"))
    sent, delivered = [], []

    class Transport:
        def sendto(self, payload, peer):
            sent.append(decode_reliable_packet(decrypt_server_datagram(payload, session.game_key, LIBRARY)))

    protocol.connection_made(Transport())
    protocol._dispatch_game_message = lambda _, packet, message: delivered.append(message)
    loop = Loop()
    monkeypatch.setattr("acvd_server.request_recovery.asyncio.get_running_loop", lambda: loop)
    request = GameMessage(0x03F6, b"\0\0\0\1", b"X"*80, 7)
    fragments = encode_game_request_fragments(request, max_fragment_length=32)
    assert len(fragments) == 3

    def receive(sequence, payload=b"", opcode=DAT):
        packet = ReliablePacket(sequence, 0, opcode, payload=payload)
        wire = encrypt_client_datagram(encode_reliable_packet(packet), session.game_key,
                                       session.auth_token, 0, LIBRARY)
        protocol.datagram_received(wire, session.peer)

    yield protocol, session, request, fragments, receive, sent, delivered, loop
    protocol.connection_lost(None)


@pytest.mark.parametrize("first", [100, 4095])
def test_large_request_fragment_gap_is_cached_and_cumulatively_acknowledged(stream, first):
    protocol, session, request, fragments, receive, sent, delivered, loop = stream
    receive(first, fragments[0])
    receive((first+2) % 4096, fragments[2])
    assert [p.remote_sequence for p in sent if p.opcode == ACK] == [first, first]
    assert not delivered and len(loop.active) == 1
    loop.fire()
    assert sent[-1] == ReliablePacket(0, first, RETRANSMIT)
    receive((first+1) % 4096, fragments[1])
    assert delivered == [request]
    assert session.remote_sequence == (first+2) % 4096
    assert not session.fragment_parts and session.receive_high_sequence is None
    assert not loop.active and not protocol._request_recovery.pending


def test_nonfragmented_future_command_still_waits_for_its_gap(stream):
    _, session, _, fragments, receive, sent, delivered, loop = stream
    receive(100, fragments[0])
    receive(102, encode_game_request_fragments(GameMessage(9, b"\0" * 4, b""))[0])
    assert not delivered and session.remote_sequence == 100
    assert sent[-1] == ReliablePacket(0, 100, ACK)
    loop.fire()
    assert sent[-1] == ReliablePacket(0, 100, RETRANSMIT)


def test_missing_tail_is_requested_without_another_client_packet(stream):
    _, _, request, fragments, receive, sent, delivered, loop = stream
    receive(100, fragments[0])
    receive(101, fragments[1])
    assert not delivered
    assert loop.active[0].delay == 0.5
    loop.fire()
    assert sent[-1] == ReliablePacket(0, 101, RETRANSMIT)
    receive(102, fragments[2])
    assert delivered == [request] and not loop.active


def test_in_order_stream_has_no_resend_and_duplicate_cannot_dispatch_twice(stream):
    _, session, request, fragments, receive, sent, delivered, loop = stream
    for sequence, body in enumerate(fragments, 100):
        receive(sequence, body)
    for sequence, body in enumerate(fragments, 100):
        receive(sequence, body)
    assert delivered == [request] and session.remote_sequence == 102
    assert [p.remote_sequence for p in sent if p.opcode == ACK] == [100, 101, 102, 102, 102, 102]
    assert not any(p.opcode == RETRANSMIT for p in sent) and not loop.active


def test_old_control_sequence_does_not_move_receive_prefix(stream):
    _, session, _, fragments, receive, _, _, _ = stream
    receive(100, fragments[0])
    receive(99, opcode=HBT)
    receive(0, opcode=ACK)
    assert session.remote_sequence == 100


@pytest.mark.parametrize("change", ["disconnect", "replace", "registry", "peer", "stale", "transport"])
def test_pending_recovery_cannot_reach_an_old_or_stale_connection(stream, change):
    protocol, session, _, fragments, receive, sent, _, loop = stream
    receive(100, fragments[0])
    if change == "disconnect":
        session.established = False
    elif change == "replace":
        protocol.sessions[session.auth_token] = replace(session)
    elif change == "registry":
        protocol.sessions = {}
    elif change == "peer":
        session.peer = ("127.0.0.1", 54321)
    elif change == "stale":
        session.last_seen_monotonic -= 91
    else:
        protocol.transport = None
    loop.fire()
    assert not any(p.opcode == RETRANSMIT for p in sent)
    assert not loop.active and not protocol._request_recovery.pending


@pytest.mark.parametrize("opcode", [FIN, RST, SYN])
def test_native_close_or_new_handshake_cancels_old_receive_timer(stream, opcode):
    protocol, session, _, fragments, receive, _, _, loop = stream
    receive(100, fragments[0])
    receive(101, opcode=opcode)
    assert not loop.active and not protocol._request_recovery.pending
    if opcode == SYN:
        assert session.remote_sequence == 101 and not session.fragment_parts


def test_transport_close_cancels_receive_timers(stream):
    protocol, _, _, fragments, receive, _, _, loop = stream
    receive(100, fragments[0])
    protocol.connection_lost(None)
    assert not loop.active and not protocol._request_recovery.pending


def test_retries_are_bounded_and_duplicate_packets_cannot_renew_them(stream):
    protocol, session, _, fragments, receive, sent, _, loop = stream
    receive(100, fragments[0])
    for attempt in range(MAX_ATTEMPTS):
        loop.fire()
        receive(102, fragments[2])
        assert protocol._request_recovery.pending[session.auth_token].attempts == attempt+1
    assert len([p for p in sent if p.opcode == RETRANSMIT]) == MAX_ATTEMPTS
    assert not loop.active
    receive(101, fragments[1])
    assert not loop.active and not protocol._request_recovery.pending


def test_future_distance_is_bounded_without_storing_payloads(stream):
    _, session, _, fragments, receive, sent, delivered, _ = stream
    receive(100, fragments[0])
    receive(613, fragments[2])
    assert session.receive_high_sequence is None
    receive(612, fragments[2])
    assert session.receive_high_sequence == 612
    assert session.remote_sequence == 100 and not delivered
    assert sent[-1] == ReliablePacket(0, 100, ACK)
    assert list(session.fragment_parts[7]) == [0]


def test_invalid_ciphertext_cannot_keep_a_dead_session_fresh(stream):
    protocol, session, _, _, _, _, _, _ = stream
    session.last_seen_monotonic -= 100
    old = (session.last_seen_monotonic, session.last_seen_unix, session.peer)
    protocol.datagram_received(session.auth_token + bytes(40), ("127.0.0.1", 54321))
    assert (session.last_seen_monotonic, session.last_seen_unix, session.peer) == old


def test_recovery_session_cap_and_close(tmp_path, monkeypatch):
    loop = Loop()
    monkeypatch.setattr("acvd_server.request_recovery.asyncio.get_running_loop", lambda: loop)
    recovery = RequestRecovery(is_current=lambda _: True, request_next=lambda _: None,
                               telemetry=Telemetry(tmp_path/"events.jsonl"))
    for i in range(MAX_RECOVERY_SESSIONS+1):
        session = GameSession(i.to_bytes(8, "big"), bytes(16), "Pilot", i,
            established=True, peer=("127.0.0.1", 12345), fragment_parts={1:{0:b"first"}})
        recovery.refresh(session)
    assert len(recovery.pending) == len(loop.active) == MAX_RECOVERY_SESSIONS
    recovery.close()
    assert not recovery.pending and not loop.active


def test_restart_snapshot_keeps_contiguous_prefix_but_not_partial_requests(tmp_path):
    from acvd_server.session_state import _load_game_sessions, _persist_game_sessions

    session = GameSession(b"test0001", bytes(16), "Pilot", 11,
        remote_sequence=4095, receive_sequence_initialized=True, established=True,
        peer=("127.0.0.1", 12345), fragment_parts={7:{0:b"partial"}},
        receive_high_sequence=1)
    path = tmp_path / "sessions.json"
    _persist_game_sessions(path, {session.auth_token:session})
    restored = _load_game_sessions(path)[session.auth_token]
    assert restored.receive_sequence_initialized and restored.remote_sequence == 4095
    assert restored.receive_high_sequence is None and not restored.fragment_parts


def test_sync_diagnostic_does_not_create_a_timer(tmp_path, monkeypatch):
    def no_loop():
        raise RuntimeError("No running loop")

    monkeypatch.setattr("acvd_server.request_recovery.asyncio.get_running_loop", no_loop)
    recovery = RequestRecovery(is_current=lambda _: True, request_next=lambda _: None,
                               telemetry=Telemetry(tmp_path/"events.jsonl"))
    session = GameSession(b"test0001", bytes(16), "Pilot", 11,
        established=True, peer=("127.0.0.1", 12345), fragment_parts={7:{0:b"partial"}})
    recovery.refresh(session)
    assert not recovery.pending
