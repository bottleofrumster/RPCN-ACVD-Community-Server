"""Personal notices use temporary player stores and authenticated dispatch."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from acvd_server.protocol import (
    _proto_datetime,
    proto_bytes,
    proto_varint,
)
from acvd_server.server import GameDatagramProtocol
from acvd_server.teams import TeamStore
from acvd_server.udp import DAT, GameMessage, GameSession, ReliablePacket

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


def seed(players, player_id=11):
    players.mkdir(exist_ok=True)
    target = players / f"{player_id}.personal-notices.json"
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "player_id": player_id,
                "next_id": 2,
                "notices": [
                    {
                        "id": 1,
                        "event_key": "test-event",
                        "title": "Test heading",
                        "body": "Test body",
                        "created_at": NOW.isoformat(),
                        "acknowledged": False,
                    }
                ],
            }
        )
    )
    return target


def request(tmp_path, monkeypatch, opcode, payload=b"", player_id=11, welcome="", actor=None):
    events = Events()
    protocol = GameDatagramProtocol(
        events,
        {},
        Path("native/libacvd_cwc.so"),
        tmp_path / "players",
        TeamStore(tmp_path / "teams"),
        get_welcome_message=lambda: welcome,
    )
    actor = actor or GameSession(b"actor001", bytes(16), "Actor", player_id)
    replies = []
    monkeypatch.setattr(
        protocol, "_send_game_reply", lambda *args, **kw: replies.append((args[-1], kw))
    )
    protocol._dispatch_game_message(
        actor, ReliablePacket(3, 2, DAT), GameMessage(opcode, bytes(4), payload)
    )
    return replies, events.rows


def test_login_welcome_repeats_on_new_login_not_on_query_retry(tmp_path, monkeypatch):
    text = "Welcome to the example community.\nThanks to the contributors."
    actor = GameSession(b"login001", bytes(16), "Actor", 11)
    replies, _ = request(tmp_path, monkeypatch, 0x0417, welcome=text, actor=actor)
    from acvd_server.notice_storage import PersonalNoticeStore

    store = PersonalNoticeStore(tmp_path / "players")
    assert replies[-1][1] == {}
    assert store.pending(11) == []
    assert actor.login_welcome.body == text
    first_reply = replies[-1]
    path = tmp_path / "players/11.personal-notices.json"
    before = path.read_bytes()
    replies, _ = request(tmp_path, monkeypatch, 0x0417, welcome=text, actor=actor)
    assert replies[-1] == first_reply
    assert path.read_bytes() == before
    request(tmp_path, monkeypatch, 0x0418, proto_varint(1, 1), welcome=text, actor=actor)
    acknowledged = path.read_bytes()
    replies, _ = request(tmp_path, monkeypatch, 0x0417, welcome=text, actor=actor)
    assert replies[-1] == (b"", {}) and path.read_bytes() == acknowledged
    next_login = GameSession(b"login002", bytes(16), "Actor", 11)
    replies, _ = request(tmp_path, monkeypatch, 0x0417, welcome=text, actor=next_login)
    assert replies[-1][0] and next_login.login_welcome.identifier == 2
    # An acknowledgement from the old login cannot consume this new welcome.
    request(tmp_path, monkeypatch, 0x0418, proto_varint(1, 1), actor=next_login)
    assert not next_login.login_welcome_read
    assert json.loads(path.read_text())["notices"] == []


def test_bad_query_does_not_create_welcome_or_override_feedback(tmp_path, monkeypatch):
    target = seed(tmp_path / "players")
    before = target.read_bytes()
    replies, _ = request(tmp_path, monkeypatch, 0x0417, b"bad", welcome="Welcome")
    assert replies[-1][1].get("error") and target.read_bytes() == before
    actor = GameSession(b"login001", bytes(16), "Actor", 11)
    replies, _ = request(tmp_path, monkeypatch, 0x0417, welcome="Welcome", actor=actor)
    from acvd_server.notice_storage import PersonalNoticeStore

    assert [n.body for n in PersonalNoticeStore(tmp_path / "players").pending(11)] == [
        "Test body",
    ]
    assert actor.login_welcome.body == "Welcome"
    assert b"Test body" in replies[-1][0] and b"Welcome" in replies[-1][0]


def test_welcome_wraps_without_changing_words_and_old_notices_are_not_duplicated(tmp_path, monkeypatch):
    from acvd_server.notice_messages import format_login_welcome
    text = "Welcome.\n\n" + "A long credit sentence with the contributor name. " * 5
    body = format_login_welcome(text)
    assert all(len(line) <= 72 for line in body.splitlines())
    assert body.split() == text.split() and "\n\n" in body
    old_store = store(tmp_path / "players")
    old_store.enqueue(11, event_key="welcome:old", title="Old welcome", body="Old text", created_at=NOW)
    old_store.enqueue(11, event_key="feedback:1", title="Feedback", body="Keep me", created_at=NOW)
    actor = GameSession(b"login001", bytes(16), "Actor", 11)
    replies, _ = request(tmp_path, monkeypatch, 0x0417, welcome=text, actor=actor)
    assert actor.login_welcome.body == body
    assert b"Old text" not in replies[-1][0] and b"Keep me" in replies[-1][0]
    assert old_store.find_event(11, "welcome:old").body == "Old text"


def test_welcome_changes_apply_to_next_login_and_other_players_are_independent(tmp_path, monkeypatch):
    actor = GameSession(b"login001", bytes(16), "Actor", 11)
    request(tmp_path, monkeypatch, 0x0417, welcome="First", actor=actor)
    request(tmp_path, monkeypatch, 0x0417, welcome="Updated", actor=actor)
    assert actor.login_welcome.body == "First"
    other = GameSession(b"login002", bytes(16), "Other", 22)
    request(tmp_path, monkeypatch, 0x0417, welcome="Updated", actor=other)
    assert other.login_welcome.body == "Updated"
    assert actor.login_welcome_read is False
    fresh = GameSession(b"login003", bytes(16), "Actor", 11)
    request(tmp_path, monkeypatch, 0x0417, welcome="", actor=fresh)
    assert fresh.login_welcome is None and fresh.login_welcome_checked


def test_welcome_identifier_reservation_failure_is_retryable(tmp_path, monkeypatch):
    import acvd_server.notice_storage as storage
    actor = GameSession(b"login001", bytes(16), "Actor", 11)
    original_save = storage.PersonalNoticeStore._save
    monkeypatch.setattr(storage.PersonalNoticeStore, "_save", lambda *args: (_ for _ in ()).throw(OSError("Disk full")))
    replies, _ = request(tmp_path, monkeypatch, 0x0417, welcome="Hello", actor=actor)
    assert replies[-1][1].get("error") and not actor.login_welcome_checked
    assert actor.login_welcome is None
    monkeypatch.setattr(storage.PersonalNoticeStore, "_save", original_save)
    request(tmp_path, monkeypatch, 0x0417, welcome="Hello", actor=actor)
    assert actor.login_welcome.identifier == 1


def test_welcome_does_not_discard_unreturned_feedback_or_grow_notice_records(tmp_path, monkeypatch):
    import acvd_server.handlers.notices as handler
    monkeypatch.setattr(handler, "MAX_PENDING_REPLY", 2)
    notices = store(tmp_path / "players")
    for index in range(3):
        notices.enqueue(11, event_key=f"feedback:{index}", title="Feedback", body=f"Body{index}", created_at=NOW)
    actor = GameSession(b"login001", bytes(16), "Actor", 11)
    replies, _ = request(tmp_path, monkeypatch, 0x0417, welcome="Hello", actor=actor)
    from acvd_server.protocol import decode_proto
    assert len(decode_proto(replies[-1][0])) == 2
    assert len(notices.pending(11)) == 3
    request(tmp_path, monkeypatch, 0x0418, proto_varint(1, 4) + proto_varint(1, 1), actor=actor)
    replies, _ = request(tmp_path, monkeypatch, 0x0417, welcome="Hello", actor=actor)
    assert b"Hello" not in replies[-1][0] and b"Body1" in replies[-1][0] and b"Body2" in replies[-1][0]
    for index in range(5):
        request(tmp_path, monkeypatch, 0x0417, welcome="Hello")
    assert len(json.loads((tmp_path / "players/11.personal-notices.json").read_text())["notices"]) == 3


def test_reserved_ids_cannot_collide_with_durable_notices_or_wrap(tmp_path):
    notices = store(tmp_path)
    assert notices.reserve_identifier(11) == 1
    assert notices.enqueue(11, event_key="real", title="Title", body="Body", created_at=NOW) == 2
    path = tmp_path / "11.personal-notices.json"
    data = json.loads(path.read_text())
    data["next_id"] = 1 << 32
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="capacity"):
        notices.reserve_identifier(11)
    assert path.read_bytes() == before


def test_whitespace_welcome_is_disabled(tmp_path, monkeypatch):
    actor = GameSession(b"login001", bytes(16), "Actor", 11)
    replies, _ = request(tmp_path, monkeypatch, 0x0417, welcome=" \n ", actor=actor)
    assert replies[-1] == (b"", {})
    assert actor.login_welcome is None
    assert not (tmp_path / "players/11.personal-notices.json").exists()


def test_dispatcher_reads_pending_notice_without_changing_store(tmp_path, monkeypatch):
    target = seed(tmp_path / "players")
    before = target.read_bytes()
    replies, _ = request(tmp_path, monkeypatch, 0x0417)
    expected = proto_bytes(
        1,
        proto_varint(1, 1)
        + proto_bytes(2, "Test heading")
        + proto_bytes(3, "Test body")
        + proto_bytes(4, _proto_datetime(NOW))
        + proto_varint(5, 1),
    )
    assert replies == [(expected, {})]
    assert target.read_bytes() == before


def test_dispatcher_acknowledges_only_the_authenticated_player(tmp_path, monkeypatch):
    own = seed(tmp_path / "players", 11)
    other = seed(tmp_path / "players", 22)
    other_before = other.read_bytes()
    replies, _ = request(tmp_path, monkeypatch, 0x0418, proto_varint(1, 1))
    assert replies == [(b"", {})]
    assert json.loads(own.read_text())["notices"][0]["acknowledged"] is True
    assert other.read_bytes() == other_before
    assert request(tmp_path, monkeypatch, 0x0417)[0] == [(b"", {})]
    assert request(tmp_path, monkeypatch, 0x0417, player_id=22)[0][0][0] != b""


def store(root):
    from acvd_server.notice_storage import PersonalNoticeStore

    return PersonalNoticeStore(root)


def enqueue(s, key="test", **overrides):
    values = {"event_key": key, "title": "Heading", "body": "Body", "created_at": NOW}
    return s.enqueue(11, **{**values, **overrides})


def test_store_survives_restart_and_exact_retry_before_and_after_read(tmp_path):
    s = store(tmp_path)
    first = enqueue(s)
    assert first == 1 and enqueue(store(tmp_path)) == first
    assert store(tmp_path).acknowledge(11, [first, first, 999]) == [first]
    before = (tmp_path / "11.personal-notices.json").read_bytes()
    assert store(tmp_path).acknowledge(11, [first]) == []
    assert enqueue(store(tmp_path)) == first
    assert store(tmp_path).pending(11) == []
    assert (tmp_path / "11.personal-notices.json").read_bytes() == before
    assert enqueue(store(tmp_path), "next") == 2
    assert list((tmp_path / ".history/11.personal-notices.json").glob("*.bin"))


def test_empty_reads_and_acknowledgements_do_not_create_data(tmp_path):
    s = store(tmp_path / "missing")
    assert s.pending(11) == [] and s.acknowledge(11, [1]) == []
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("player_id", [0, -1, True, "../22", 1 << 32])
def test_player_path_requires_uint32_identity(tmp_path, player_id):
    with pytest.raises(ValueError):
        store(tmp_path).pending(player_id)
    assert not list(tmp_path.iterdir())


def test_event_key_cannot_replace_content(tmp_path):
    s = store(tmp_path)
    enqueue(s)
    before = (tmp_path / "11.personal-notices.json").read_bytes()
    with pytest.raises(ValueError):
        enqueue(s, body="Changed")
    assert (tmp_path / "11.personal-notices.json").read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(player_id=22),
        lambda d: d.update(version=True),
        lambda d: d.update(next_id=1),
        lambda d: d["notices"].append(dict(d["notices"][0])),
        lambda d: d["notices"][0].update(acknowledged=1),
        lambda d: d["notices"][0].update(created_at="2026-09-12"),
        lambda d: d["notices"][0].update(body=None),
    ],
)
def test_corrupt_store_is_not_replaced(tmp_path, change):
    target = seed(tmp_path)
    data = json.loads(target.read_text())
    change(data)
    target.write_text(json.dumps(data))
    before = target.read_bytes()
    with pytest.raises(ValueError):
        store(tmp_path).acknowledge(11, [1])
    assert target.read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        proto_varint(2, 22),
        proto_bytes(1, b"bad"),
        b"\x08",
        proto_varint(1, 0),
        proto_varint(1, 1) + b"\x08",
    ],
)
def test_malformed_ack_does_not_partially_update(tmp_path, monkeypatch, payload):
    target = seed(tmp_path / "players")
    before = target.read_bytes()
    replies, events = request(tmp_path, monkeypatch, 0x0418, payload)
    assert replies[0][1].get("error")
    assert target.read_bytes() == before
    assert events[-1][0] == "game_personal_notice_rejected"


def test_query_cannot_select_another_recipient(tmp_path, monkeypatch):
    target = seed(tmp_path / "players", 22)
    before = target.read_bytes()
    replies, _ = request(tmp_path, monkeypatch, 0x0417, proto_varint(1, 22))
    assert replies[0][1].get("error")
    assert target.read_bytes() == before


def test_concurrent_writers_keep_every_notice(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        identifiers = list(pool.map(lambda i: enqueue(store(tmp_path), f"event-{i}"), range(16)))
    assert len(set(identifiers)) == 16
    assert len(store(tmp_path).pending(11)) == 16


def test_write_failure_does_not_acknowledge_or_erase_notice(tmp_path, monkeypatch):
    import acvd_server.notice_storage as storage

    target = seed(tmp_path / "players")
    before = target.read_bytes()

    def fail(*args):
        raise OSError("Simulated write failure")

    monkeypatch.setattr(storage, "_replace_player_blob", fail)
    replies, _ = request(tmp_path, monkeypatch, 0x0418, proto_varint(1, 1))
    assert replies == [(b"", {"error": (1, 28)})]
    assert target.read_bytes() == before


def test_duplicate_json_keys_and_symbolic_links_require_review(tmp_path):
    target = seed(tmp_path)
    original = target.read_text()
    target.write_text(original.replace('"version": 1', '"version": 1, "version": 1'))
    with pytest.raises(ValueError):
        store(tmp_path).pending(11)
    target.unlink()
    target.symlink_to(tmp_path / "absent")
    with pytest.raises(ValueError):
        store(tmp_path).acknowledge(11, [1])
    assert target.is_symlink() and not (tmp_path / "absent").exists()


def test_queue_capacity_is_explicit_and_does_not_remove_old_data(tmp_path, monkeypatch):
    import acvd_server.notice_storage as storage

    monkeypatch.setattr(storage, "MAX_RECORDS", 1)
    enqueue(store(tmp_path))
    target = tmp_path / "11.personal-notices.json"
    before = target.read_bytes()
    with pytest.raises(ValueError):
        enqueue(store(tmp_path), "next")
    assert target.read_bytes() == before


def test_reply_bound_keeps_unreturned_notices_pending(tmp_path, monkeypatch):
    import acvd_server.notice_storage as storage

    monkeypatch.setattr(storage, "MAX_PENDING_REPLY", 1)
    first, second = enqueue(store(tmp_path)), enqueue(store(tmp_path), "second")
    assert [n.identifier for n in store(tmp_path).pending(11)] == [first]
    store(tmp_path).acknowledge(11, [first])
    assert [n.identifier for n in store(tmp_path).pending(11)] == [second]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": ""},
        {"body": "x" * 2049},
        {"title": "\0bad"},
        {"created_at": datetime(2026, 9, 12)},
    ],
)
def test_invalid_notice_is_rejected_before_creating_store(tmp_path, kwargs):
    with pytest.raises(ValueError):
        enqueue(store(tmp_path), **kwargs)
    assert not list(tmp_path.iterdir())
