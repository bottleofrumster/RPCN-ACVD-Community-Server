"""Media lifecycle contracts on temporary records, independent of the dispatcher."""

import pytest

from acvd_server.handlers import emblems, screenshots
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.screenshots import _proto_fixed64, _world_top_screenshot_id
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


@pytest.fixture
def setup(tmp_path):
    store = TeamStore(tmp_path / "teams")
    store.remember_player("Owner", 11)
    store.remember_player("Peer", 22)
    root = tmp_path / "players"
    root.mkdir()
    return store, root, GameSession(b"owner001", bytes(16), "Owner", 11)


def invoke(session, function, payload=b"", **dependencies):
    replies, events = [], Events()
    function(
        session,
        GameMessage(0, b"\0\0\0\1", payload, 1),
        telemetry=events,
        reply=replies.append,
        **dependencies,
    )
    return replies, events


def registration(owner=11, body=b"image"):
    return b"".join(proto_varint(n, v) for n, v in ((1, owner), (2, 1), (3, 2), (4, 3))) + b"".join(
        proto_bytes(n, v) for n, v in ((5, b"caption"), (6, body), (7, b"aux"), (8, b"thumbnail"))
    )


def test_new_draft_does_not_replace_published_screenshot(setup):
    store, root, session = setup
    first, second = registration(body=b"first"), registration(body=b"second")
    invoke(session, screenshots.register_screenshot, first, player_data_path=root)
    invoke(session, screenshots.apply_screenshot, proto_bytes(1, b"token"), player_data_path=root)
    invoke(session, screenshots.register_screenshot, second, player_data_path=root)
    assert (root / "11.world-top-screenshot.bin").read_bytes() == first
    first_id = _world_top_screenshot_id(11, first)
    viewer = GameSession(b"viewer01", bytes(16), "Peer", 22)
    replies, _ = invoke(
        viewer,
        screenshots.get_screenshot,
        _proto_fixed64(1, first_id),
        player_data_path=root,
        teams=store,
    )
    fields = decode_proto(replies[0])
    assert first_field(fields, 2).value == b"Owner"
    assert first_field(fields, 6).value == b"first"
    invoke(session, screenshots.apply_screenshot, proto_bytes(1, b"next"), player_data_path=root)
    assert (root / "11.world-top-screenshot.bin").read_bytes() == second
    assert any(
        p.read_bytes() == first for p in (root / ".history/11.world-top-screenshot.bin").iterdir()
    )


@pytest.mark.parametrize("payload", [b"", registration(owner=22)])
def test_invalid_or_foreign_registration_writes_nothing(setup, payload):
    _, root, session = setup
    replies, events = invoke(
        session, screenshots.register_screenshot, payload, player_data_path=root
    )
    assert not replies and not list(root.iterdir())
    assert events.rows[-1][0] == "game_message_rejected"


def test_apply_requires_the_authenticated_players_draft(setup):
    _, root, session = setup
    (root / "22.player-screenshot.bin").write_bytes(registration(owner=22))
    replies, events = invoke(
        session, screenshots.apply_screenshot, proto_bytes(1, b"token"), player_data_path=root
    )
    assert not replies and events.rows[-1][0] == "game_message_rejected"
    assert not (root / "11.world-top-screenshot.bin").exists()


@pytest.mark.parametrize("requested,expected", [(0, 0), (1, 1), (999, 32)])
def test_thumbnail_limit_is_bounded(setup, requested, expected):
    _, root, session = setup
    for owner in range(100, 135):
        (root / f"{owner}.world-top-screenshot.bin").write_bytes(registration(owner=owner))
    replies, events = invoke(
        session, screenshots.get_thumbnails, proto_varint(1, requested), player_data_path=root
    )
    assert len(decode_proto(replies[0])) == expected
    assert events.rows[-1][1]["applied_limit"] == expected


def test_full_screenshot_id_must_be_fixed64_not_varint(setup):
    store, root, session = setup
    replies, events = invoke(
        session,
        screenshots.get_screenshot,
        proto_varint(1, 123),
        player_data_path=root,
        teams=store,
    )
    assert not replies and events.rows[-1][0] == "game_message_rejected"


def test_emblem_assigned_id_apply_and_remote_readback(setup):
    store, _, session = setup
    replies, _ = invoke(
        session,
        emblems.register_emblem,
        proto_bytes(1, b"meta") + proto_bytes(2, b"jpeg"),
        teams=store,
    )
    fields = decode_proto(replies[0])
    assert [f.number for f in fields] == [1]
    assigned = fields[0].value
    assert assigned == store.state["pending_emblems"]["Owner"]["ugc_id"]
    application = b"\0" + b"E" * 1808
    replies, _ = invoke(
        session,
        emblems.set_player_emblem,
        proto_varint(1, assigned) + proto_bytes(2, application),
        teams=store,
    )
    assert first_field(decode_proto(replies[0]), 1).value == 1
    viewer = GameSession(b"viewer01", bytes(16), "Peer", 22)
    replies, _ = invoke(viewer, emblems.get_player_emblem, proto_varint(1, 11), teams=store)
    fields = decode_proto(replies[0])
    assert first_field(fields, 1).value == application and first_field(fields, 2).value == assigned
    assert store.player_emblem("Peer") is None


def test_explicit_team_emblem_lookup_does_not_return_requesters_team(setup):
    store, _, session = setup
    team = store.create(
        online_id="Owner",
        player_id=11,
        name="One",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=2,
    )
    peer_team = store.create(
        online_id="Peer",
        player_id=22,
        name="Two",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=2,
    )
    store.save_emblem("Owner", 101, b"\0" + b"A" * 1808)
    store.save_emblem("Peer", 202, b"\0" + b"B" * 1808)
    replies, _ = invoke(
        session, emblems.get_team_emblem, proto_varint(1, peer_team["team_id"]), teams=store
    )
    fields = decode_proto(replies[0])
    assert (
        first_field(fields, 2).value == 202 and first_field(fields, 1).value == b"\0" + b"B" * 1808
    )
    assert team["emblem_ugc_id"] == 101


def test_unknown_imported_ugc_ids_are_not_treated_as_banned(setup):
    _, _, session = setup
    replies, _ = invoke(
        session, emblems.check_ugc_ids, b"".join(proto_varint(1, n) for n in (0, 123, 999999))
    )
    rows = [decode_proto(f.value) for f in decode_proto(replies[0])]
    assert [first_field(r, 2).value for r in rows] == [0, 1, 1]
