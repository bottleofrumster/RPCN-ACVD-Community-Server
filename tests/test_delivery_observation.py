from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from acvd_server.delivery_observation import DeliveryObservation
from acvd_server.server import GameDatagramProtocol
from acvd_server.session_state import _load_game_sessions, _persist_game_sessions
from acvd_server.teams import TeamStore
from acvd_server.udp import (
    ACK,
    DAT,
    FIN,
    HBT,
    SYN,
    GameSession,
    ReliablePacket,
    decrypt_server_datagram,
    encode_reliable_packet,
    encrypt_client_datagram,
)


def test_cumulative_wrap_ack_is_passive_and_duplicate_ack_has_no_sample():
    observer = DeliveryObservation()
    packets = [ReliablePacket(i, 0, DAT) for i in (4095, 0, 1)]
    retained = {p.local_sequence: p for p in packets}
    for n, packet in enumerate(packets):
        observer.sent(packet, retained, 10 + n / 10)
    original = dict(retained)
    assert observer.acknowledge(4094, retained, 11) == {}
    result = observer.acknowledge(0, retained, 11)
    assert result == {
        "timed_packets": 2,
        "untimed_packets": 0,
        "resent_packets": 0,
        "oldest_first_send_to_ack_ms": 1000.0,
        "newest_first_send_to_ack_ms": 900.0,
        "endpoint": {"first_send_to_ack_ms": 900.0, "last_send_to_ack_ms": 900.0, "resends": 0},
    }
    assert retained == original
    assert list(observer.pending) == [1]
    assert observer.acknowledge(0, retained, 12) == {}


def test_resend_is_ambiguous_and_sequence_reuse_starts_a_new_sample():
    observer = DeliveryObservation()
    packet = ReliablePacket(5, 0, DAT)
    retained = {5: packet}
    observer.sent(packet, retained, 10)
    observer.sent(packet, retained, 11)
    sample = observer.acknowledge(5, retained, 11.2)
    assert sample["resent_packets"] == 1
    assert sample["endpoint"] == {
        "first_send_to_ack_ms": 1200.0,
        "last_send_to_ack_ms": 200.0,
        "resends": 1,
    }
    observer.sent(packet, retained, 12)
    replacement = replace(packet)  # Equal bytes are not the same send generation.
    retained[5] = replacement
    assert observer.acknowledge(5, retained, 13) == {}
    observer.sent(replacement, retained, 14)
    assert observer.acknowledge(5, retained, 14.1)["endpoint"]["resends"] == 0


def test_memory_bounded_and_unknown_restored_packets_are_not_timed():
    observer = DeliveryObservation()
    retained = {}
    for n in range(1200):
        packet = ReliablePacket(n, 0, DAT)
        retained[n] = packet
        observer.sent(packet, retained, n)
    assert len(observer.pending) == 512
    sample = observer.acknowledge(1199, retained, 1200)
    assert sample["timed_packets"] == 512
    assert sample["untimed_packets"] == 688
    assert not observer.pending
    assert DeliveryObservation().acknowledge(1199, retained, 1300) == {}


def test_observer_does_not_time_unretained_traffic_or_clock_regression():
    observer = DeliveryObservation()
    packet = ReliablePacket(7, 0, DAT)
    observer.sent(packet, {}, 10)
    assert not observer.pending
    observer.sent(packet, {7: packet}, 10)
    assert observer.acknowledge(7, {7: packet}, 9) == {}


def test_timing_is_never_persisted(tmp_path):
    session = GameSession(b"token123", bytes(16), "Pilot", 42, last_seen_unix=1000)
    packet = ReliablePacket(7, 0, DAT)
    session.outbound_reliable[7] = packet
    session.delivery_observation.sent(packet, session.outbound_reliable, 10)
    path = tmp_path / "sessions.json"
    _persist_game_sessions(path, {session.auth_token: session}, {})
    assert "delivery_observation" not in path.read_text()
    restored = _load_game_sessions(path, now_unix=1001)[session.auth_token]
    assert not restored.delivery_observation.pending
    assert restored.outbound_reliable == session.outbound_reliable


@pytest.mark.parametrize("boundary", ["ack", "rebind", "syn", "fin"])
def test_real_transport_measurement_keeps_wire_and_lifecycle_behavior(
    tmp_path, monkeypatch, boundary
):
    clock = [10.0]
    monkeypatch.setattr("acvd_server.server.time.monotonic", lambda: clock[0])
    session = GameSession(
        b"token123", bytes(16), "Pilot", 42, established=True, peer=("127.0.0.1", 1234)
    )
    sent, events = [], []
    library = Path("native/libacvd_cwc.so")
    protocol = GameDatagramProtocol(
        SimpleNamespace(emit=lambda event, **kw: events.append((event, kw))),
        {session.auth_token: session},
        library,
        tmp_path / "players",
        TeamStore(tmp_path / "teams"),
    )
    protocol.transport = SimpleNamespace(sendto=lambda data, peer: sent.append((data, peer)))
    outgoing = ReliablePacket(7, 0, DAT, payload=b"test")
    protocol._send(session, outgoing)
    assert len(sent) == 1
    clock[0] += 0.2
    if boundary == "ack":
        protocol._send(session, outgoing, remember=False)
        assert sent[0][1] == sent[1][1]
        assert decrypt_server_datagram(sent[0][0], session.game_key, library) == (
            decrypt_server_datagram(sent[1][0], session.game_key, library)
        )
    clock[0] += 0.3
    opcode = {"ack": ACK, "rebind": HBT, "syn": SYN, "fin": FIN}[boundary]
    peer = ("127.0.0.1", 4321) if boundary == "rebind" else session.peer
    incoming = ReliablePacket(10, 7, opcode)
    data = encrypt_client_datagram(
        encode_reliable_packet(incoming), session.game_key, session.auth_token, 0, library
    )
    protocol.datagram_received(data, peer)
    decoded = next(kw for event, kw in events if event == "game_packet_decoded")
    if boundary == "ack":
        assert decoded["ack_delivery"]["endpoint"] == {
            "first_send_to_ack_ms": 500.0,
            "last_send_to_ack_ms": 300.0,
            "resends": 1,
        }
        assert len(sent) == 2  # Observing an ACK sends nothing.
    elif boundary in ("rebind", "syn"):
        assert "ack_delivery" not in decoded
    if boundary == "fin":
        assert not session.delivery_observation.pending
        assert not session.established
    assert 7 not in session.outbound_reliable
