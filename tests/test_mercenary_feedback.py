"""Accepted-hire feedback on isolated stores; no real messages or players."""

import json
from pathlib import Path

import pytest

from acvd_server.notice_storage import PersonalNoticeStore
from acvd_server.protocol import proto_bytes, proto_varint
from acvd_server.server import GameDatagramProtocol
from acvd_server.teams import TeamStore
from acvd_server.udp import DAT, GameMessage, GameSession, ReliablePacket


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    teams = TeamStore(tmp_path / "teams")
    actors = []
    for name, number in (("Employer", 11), ("Mercenary", 22), ("Other", 33)):
        teams.create(
            online_id=name,
            player_id=number,
            name=name,
            operation_base=0,
            language=1,
            play_type=1,
            enlistment_method=2,
        )
        teams.remember_player(name, number)
        actors.append(
            GameSession(
                name.encode().ljust(8, b"0")[:8],
                bytes(16),
                name,
                number,
                established=True,
                peer=("127.0.0.1", number),
            )
        )
    sessions = {s.auth_token: s for s in actors}
    protocol = GameDatagramProtocol(
        Events(), sessions, Path("native/libacvd_cwc.so"), tmp_path / "players", teams
    )
    replies = []
    monkeypatch.setattr(protocol, "_send_game_reply", lambda *a, **kw: replies.append((a[-1], kw)))
    return protocol, actors, replies, tmp_path / "players"


def dispatch(protocol, actor, opcode, payload=b"", index=1):
    protocol._dispatch_game_message(
        actor, ReliablePacket(3, 2, DAT), GameMessage(opcode, index.to_bytes(4, "big"), payload, 1)
    )


def accept(protocol, employer, mercenary):
    dispatch(protocol, mercenary, 0x03CB, proto_bytes(3, b"profile"))
    dispatch(protocol, employer, 0x0419, proto_varint(1, mercenary.player_id))
    dispatch(protocol, mercenary, 0x03D3, proto_varint(1, 1))
    assert protocol._matchmaking.hires.resolve(employer, mercenary.player_id) is not None


def feedback(target=22, code=88800):
    return proto_bytes(1, proto_varint(1, target) + proto_varint(2, code))


def test_accepted_unregistered_recipient_gets_feedback_not_another_offer(setup):
    protocol, (employer, mercenary, _), replies, players = setup
    accept(protocol, employer, mercenary)
    protocol.sessions.pop(mercenary.auth_token)
    protocol._matchmaking.hires.purge()
    assert protocol._matchmaking.hires.resolve(employer, 22) is None
    dispatch(protocol, employer, 0x03D2, feedback())
    assert replies[-1] == (b"", {})
    pending = PersonalNoticeStore(players).pending(22)
    assert len(pending) == 1 and pending[0].body == "Your performance was superb!"
    assert "Employer" in pending[0].title
    assert protocol.teams.mercenary_messages_for("Mercenary") == []


def test_registered_target_without_acceptance_does_not_receive_a_fake_hire(setup):
    protocol, (employer, mercenary, _), replies, players = setup
    dispatch(protocol, mercenary, 0x03CB, proto_bytes(3, b"profile"))
    dispatch(protocol, employer, 0x03D2, feedback())
    assert replies[-1][1].get("error")
    assert protocol.teams.mercenary_messages_for("Mercenary") == []
    assert PersonalNoticeStore(players).pending(22) == []


def test_exact_feedback_retry_survives_context_loss_and_server_recreation(setup, monkeypatch):
    protocol, (employer, mercenary, _), replies, players = setup
    accept(protocol, employer, mercenary)
    dispatch(protocol, employer, 0x03D2, feedback())
    target = players / "22.personal-notices.json"
    before = target.read_bytes()
    PersonalNoticeStore(players).acknowledge(22, [1])
    acknowledged = target.read_bytes()
    replacement = GameDatagramProtocol(
        Events(), protocol.sessions, Path("native/libacvd_cwc.so"), players, protocol.teams
    )
    monkeypatch.setattr(
        replacement, "_send_game_reply", lambda *a, **kw: replies.append((a[-1], kw))
    )
    dispatch(replacement, employer, 0x03D2, feedback())
    assert replies[-1] == (b"", {}) and target.read_bytes() == acknowledged
    assert before != acknowledged and PersonalNoticeStore(players).pending(22) == []


def test_new_hire_allows_new_feedback_but_old_request_cannot_consume_it(setup):
    protocol, (employer, mercenary, _), replies, players = setup
    accept(protocol, employer, mercenary)
    dispatch(protocol, employer, 0x03D2, feedback(), index=10)
    accept(protocol, employer, mercenary)
    dispatch(protocol, employer, 0x03D2, feedback(), index=10)
    assert len(PersonalNoticeStore(players).pending(22)) == 1
    dispatch(protocol, employer, 0x03D2, feedback(code=88805), index=11)
    assert replies[-1] == (b"", {})
    assert [n.body for n in PersonalNoticeStore(players).pending(22)] == [
        "Your performance was superb!",
        "Thank you.",
    ]


def test_one_request_per_hire_and_wrong_sender_are_rejected(setup):
    protocol, (employer, mercenary, other), replies, players = setup
    accept(protocol, employer, mercenary)
    dispatch(protocol, other, 0x03D2, feedback())
    assert replies[-1][1].get("error")
    dispatch(protocol, employer, 0x03D2, feedback())
    before = (players / "22.personal-notices.json").read_bytes()
    dispatch(protocol, employer, 0x03D2, feedback(code=88805), index=2)
    assert replies[-1][1].get("error")
    assert (players / "22.personal-notices.json").read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        feedback(code=0),
        feedback(code=88815),
        proto_bytes(1, proto_varint(1, 22)),
        feedback() + proto_varint(1, 22),
        feedback() + feedback(22, 88805),
    ],
)
def test_invalid_feedback_batch_does_not_write_any_notice(setup, payload):
    protocol, (employer, mercenary, _), replies, players = setup
    accept(protocol, employer, mercenary)
    dispatch(protocol, employer, 0x03D2, payload)
    assert replies[-1][1].get("error")
    assert not list(players.glob("*.personal-notices.json"))


def test_expired_context_is_not_renewed_by_feedback(setup, monkeypatch):
    import acvd_server.hire_bindings as bindings

    protocol, (employer, mercenary, _), replies, players = setup
    accept(protocol, employer, mercenary)
    now = bindings.time.monotonic()
    monkeypatch.setattr(bindings.time, "monotonic", lambda: now + 1801)
    employer.last_seen_monotonic = now + 1801
    dispatch(protocol, employer, 0x03D2, feedback())
    assert replies[-1][1].get("error")
    assert not list(players.glob("*.personal-notices.json"))


@pytest.mark.parametrize(
    "fault", ["declined", "no_offer", "replaced_sender", "identity_changed", "self"]
)
def test_feedback_requires_the_proved_relationship(setup, fault):
    protocol, (employer, mercenary, other), replies, players = setup
    if fault == "declined":
        protocol._matchmaking.hires.offer(employer, 22)
        protocol._matchmaking.hires.unregister(mercenary, False)
    elif fault == "no_offer":
        protocol._matchmaking.hires.unregister(mercenary, True)
    else:
        accept(protocol, employer, mercenary)
    if fault == "replaced_sender":
        protocol.sessions.pop(employer.auth_token)
    elif fault == "identity_changed":
        protocol.teams.remember_player("Mercenary", 44)
        protocol.teams.remember_player("Other", 22)
    dispatch(protocol, employer, 0x03D2, feedback(11 if fault == "self" else 22))
    assert replies[-1][1].get("error")
    assert not list(players.glob("*.personal-notices.json"))


def test_feedback_context_limit_does_not_change_live_hire_or_duplicate_receipts(setup, monkeypatch):
    import acvd_server.hire_bindings as bindings

    monkeypatch.setattr(bindings, "MAX_FEEDBACK_CONTEXTS", 1)
    protocol, (employer, first, second), replies, players = setup
    accept(protocol, employer, first)
    dispatch(protocol, employer, 0x03D2, feedback())
    accept(protocol, employer, second)
    assert len(protocol._matchmaking.hires.feedback_contexts) == 1
    assert protocol._matchmaking.hires.resolve(employer, 22) is not None
    dispatch(protocol, employer, 0x03D2, feedback())
    assert replies[-1] == (b"", {}) and len(PersonalNoticeStore(players).pending(22)) == 1


def test_all_validation_precedes_any_delivery(setup):
    protocol, (employer, first, _), replies, players = setup
    accept(protocol, employer, first)
    dispatch(protocol, employer, 0x03D2, feedback(22) + feedback(33))
    assert replies[-1][1].get("error")
    assert not list(players.glob("*.personal-notices.json"))


def test_empty_feedback_is_a_noop_and_does_not_consume_permission(setup):
    protocol, (employer, mercenary, _), replies, players = setup
    accept(protocol, employer, mercenary)
    dispatch(protocol, employer, 0x03D2)
    assert replies[-1] == (b"", {}) and not list(players.glob("*.personal-notices.json"))
    dispatch(protocol, employer, 0x03D2, feedback())
    assert len(PersonalNoticeStore(players).pending(22)) == 1


def test_partial_io_failure_has_no_success_ack_and_exact_retry_finishes_once(setup, monkeypatch):
    protocol, (employer, first, second), replies, players = setup
    accept(protocol, employer, first)
    accept(protocol, employer, second)
    original = PersonalNoticeStore.enqueue

    def fail_second(self, recipient, **kwargs):
        if recipient == 33:
            raise OSError("Simulated second-recipient failure")
        return original(self, recipient, **kwargs)

    payload = feedback(22) + feedback(33, 88805)
    with monkeypatch.context() as patcher:
        patcher.setattr(PersonalNoticeStore, "enqueue", fail_second)
        dispatch(protocol, employer, 0x03D2, payload)
    assert replies[-1][1].get("error")
    assert len(PersonalNoticeStore(players).pending(22)) == 1
    dispatch(protocol, employer, 0x03D2, payload)
    assert replies[-1] == (b"", {})
    assert (
        len(PersonalNoticeStore(players).pending(22))
        == len(PersonalNoticeStore(players).pending(33))
        == 1
    )
    assert all(
        len(json.loads(p.read_text())["notices"]) == 1
        for p in players.glob("*.personal-notices.json")
    )


def test_all_permissions_are_reserved_before_any_durable_write(setup, monkeypatch):
    protocol, (employer, first, second), replies, players = setup
    accept(protocol, employer, first)
    accept(protocol, employer, second)
    calls = []
    hires = protocol._matchmaking.hires
    reserve = hires.reserve_feedback
    enqueue = PersonalNoticeStore.enqueue

    def observed_reserve(context, request_key):
        calls.append(("reserve", context.mercenary_id))
        return reserve(context, request_key)

    def observed_enqueue(store, recipient, **values):
        calls.append(("write", recipient))
        return enqueue(store, recipient, **values)

    monkeypatch.setattr(hires, "reserve_feedback", observed_reserve)
    monkeypatch.setattr(PersonalNoticeStore, "enqueue", observed_enqueue)
    dispatch(protocol, employer, 0x03D2, feedback(22) + feedback(33, 88805))
    assert calls == [("reserve", 22), ("reserve", 33), ("write", 22), ("write", 33)]
    assert replies[-1] == (b"", {})
    assert len(PersonalNoticeStore(players).pending(22)) == 1
    assert len(PersonalNoticeStore(players).pending(33)) == 1


def test_reservation_failure_prevents_all_notice_writes(setup, monkeypatch):
    protocol, (employer, first, second), replies, players = setup
    accept(protocol, employer, first)
    accept(protocol, employer, second)
    hires = protocol._matchmaking.hires
    reserve = hires.reserve_feedback

    def reject_second(context, request_key):
        if context.mercenary_id == 33:
            raise ValueError("Accepted context expired before reservation")
        return reserve(context, request_key)

    monkeypatch.setattr(hires, "reserve_feedback", reject_second)
    dispatch(protocol, employer, 0x03D2, feedback(22) + feedback(33))
    assert replies[-1][1].get("error")
    assert not list(players.glob("*.personal-notices.json"))
