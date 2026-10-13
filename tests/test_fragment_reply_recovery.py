from pathlib import Path

from acvd_server.server import GameDatagramProtocol
from acvd_server.teams import TeamStore
from acvd_server.telemetry import Telemetry
from acvd_server.udp import DAT, DAT_ACK, GameSession, ReliablePacket


class Timer:
    def __init__(self, callback, args):
        self.callback, self.args, self.cancelled = callback, args, False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback(*self.args)


class Loop:
    def __init__(self):
        self.timers = []

    def call_later(self, delay, callback, *args):
        assert 0 < delay <= 0.5
        timer = Timer(callback, args)
        self.timers.append(timer)
        return timer


def setup(tmp_path, monkeypatch):
    session = GameSession(
        b"token123", bytes(range(16)), "Pilot", 42, established=True, peer=("127.0.0.1", 12345)
    )
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        {session.auth_token: session},
        Path("native/libacvd_cwc.so"),
        tmp_path / "players",
        TeamStore(tmp_path / "teams"),
    )
    protocol.transport = object()
    sent = []
    monkeypatch.setattr(protocol, "_send", lambda s, p, *, remember: sent.append((p, remember)))
    loop = Loop()
    monkeypatch.setattr("acvd_server.server.asyncio.get_running_loop", lambda: loop)
    packets = tuple(ReliablePacket(i, 0, DAT, payload=b"exact-fragment") for i in (4094, 4095, 0))
    session.outbound_reliable = {p.local_sequence: p for p in packets}
    return protocol, session, packets, loop, sent


def test_missing_tail_replayed_exactly_then_ack_stops_recovery(tmp_path, monkeypatch):
    protocol, session, packets, loop, sent = setup(tmp_path, monkeypatch)
    protocol._schedule_reply_recovery(session, packets)
    protocol._acknowledge_outbound(session, 4095)
    loop.timers[0].fire()
    assert sent == [(packets[-1], False)]
    protocol._acknowledge_outbound(session, 0)
    loop.timers[-1].fire()
    assert len(sent) == 1 and not protocol._reply_retries


def test_replaced_session_cannot_receive_old_reply(tmp_path, monkeypatch):
    protocol, session, packets, loop, sent = setup(tmp_path, monkeypatch)
    protocol._schedule_reply_recovery(session, packets)
    protocol.sessions[session.auth_token] = GameSession(session.auth_token, bytes(16), "Pilot", 42)
    loop.timers[0].fire()
    assert not sent and not protocol._reply_retries


def test_reused_sequence_is_not_mistaken_for_original_fragment(tmp_path, monkeypatch):
    protocol, session, packets, loop, sent = setup(tmp_path, monkeypatch)
    protocol._schedule_reply_recovery(session, packets)
    session.outbound_reliable = {0: ReliablePacket(0, 0, DAT_ACK, payload=b"new-reply")}
    loop.timers[0].fire()
    assert not sent


def test_rebound_peer_cancels_old_reply(tmp_path, monkeypatch):
    protocol, session, packets, loop, sent = setup(tmp_path, monkeypatch)
    protocol._schedule_reply_recovery(session, packets)
    session.peer = ("127.0.0.1", 54321)
    loop.timers[0].fire()
    assert not sent


def test_transport_close_cancels_pending_timer(tmp_path, monkeypatch):
    protocol, session, packets, loop, sent = setup(tmp_path, monkeypatch)
    protocol._schedule_reply_recovery(session, packets)
    protocol.connection_lost(None)
    assert loop.timers[0].cancelled and not protocol._reply_retries
    loop.timers[0].fire()
    assert not sent


def test_recovery_is_bounded_without_ack(tmp_path, monkeypatch):
    protocol, session, packets, loop, sent = setup(tmp_path, monkeypatch)
    protocol._schedule_reply_recovery(session, packets)
    index = 0
    while index < len(loop.timers):
        assert index < 33
        loop.timers[index].fire()
        index += 1
    assert index == 32 and len(sent) == 32 * len(packets)
    assert not protocol._reply_retries


def test_disconnect_stops_recovery(tmp_path, monkeypatch):
    protocol, session, packets, loop, sent = setup(tmp_path, monkeypatch)
    protocol._schedule_reply_recovery(session, packets)
    session.established = False
    loop.timers[0].fire()
    assert not sent and not protocol._reply_retries


def test_recovery_bursts_are_limited_and_advance_after_ack(tmp_path, monkeypatch):
    protocol, session, _, loop, sent = setup(tmp_path, monkeypatch)
    packets = tuple(ReliablePacket(i, 0, DAT, payload=b"fragment") for i in range(20))
    session.outbound_reliable = {p.local_sequence: p for p in packets}
    protocol._schedule_reply_recovery(session, packets)
    loop.timers[0].fire()
    assert [p for p, _ in sent] == list(packets[:8])
    protocol._acknowledge_outbound(session, 7)
    loop.timers[-1].fire()
    assert [p for p, _ in sent[8:]] == list(packets[8:16])


def test_replacing_entire_registry_invalidates_pending_recovery(tmp_path, monkeypatch):
    protocol, session, packets, loop, sent = setup(tmp_path, monkeypatch)
    protocol._schedule_reply_recovery(session, packets)
    protocol.sessions = {}
    loop.timers[0].fire()
    assert not sent and not protocol._reply_retries


def test_no_transport_does_not_schedule_a_timer(tmp_path, monkeypatch):
    protocol, session, packets, loop, sent = setup(tmp_path, monkeypatch)
    protocol.transport = None
    protocol._schedule_reply_recovery(session, packets)
    assert not loop.timers and not sent


def test_active_group_cap_and_close_cancel_every_timer(tmp_path, monkeypatch):
    protocol, session, packets, loop, sent = setup(tmp_path, monkeypatch)
    for _ in range(129):
        protocol._schedule_reply_recovery(session, packets)
    assert len(loop.timers) == len(protocol._reply_retries) == 128
    assert protocol._reply_retries is protocol._reply_recovery.timers
    protocol.connection_lost(None)
    assert all(timer.cancelled for timer in loop.timers)
    assert not protocol._reply_retries and not sent


def test_synchronous_tooling_without_running_loop_is_supported(tmp_path, monkeypatch):
    protocol, session, packets, loop, sent = setup(tmp_path, monkeypatch)

    def no_loop():
        raise RuntimeError("no running event loop")

    monkeypatch.setattr("acvd_server.server.asyncio.get_running_loop", no_loop)
    protocol._schedule_reply_recovery(session, packets)
    assert not protocol._reply_retries and not sent


def test_disconnected_session_short_circuits_registry_lookup(tmp_path):
    from acvd_server.reply_recovery import FragmentReplyRecovery

    session = GameSession(b"token123", bytes(16), "Pilot", 42, established=False)
    checked = []

    def current(_):
        raise AssertionError("Disconnected session must not query the registry")

    recovery = FragmentReplyRecovery(
        transport_available=lambda: checked.append("transport") or True,
        is_current_session=current,
        send_packet=lambda *_: None,
        telemetry=Telemetry(tmp_path / "events.jsonl"),
    )
    recovery.schedule(session, ())
    assert checked == ["transport"] and not recovery.timers
