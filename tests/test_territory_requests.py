"""Opaque territory-layout and DLC-lineup contracts on temporary stores."""

from copy import deepcopy

import pytest

from acvd_server.handlers import inventory, territory
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
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
    for name, number in (("Owner", 11), ("Peer", 22)):
        store.create(
            online_id=name,
            player_id=number,
            name=name,
            operation_base=0,
            language=1,
            play_type=1,
            enlistment_method=2,
        )
    return store, GameSession(b"owner001", bytes(16), "Owner", 11)


def invoke(actor, function, payload=b"", **dependencies):
    replies, events = [], Events()
    function(
        actor,
        GameMessage(0, b"\0\0\0\1", payload, 1),
        telemetry=events,
        reply=replies.append,
        **dependencies,
    )
    return replies, events


def query(player=11):
    return proto_varint(1, 3) + proto_varint(2, player) + proto_varint(3, 9)


def test_missing_and_stored_empty_layout_have_distinct_absence_flags(setup):
    store, actor = setup
    replies, _ = invoke(actor, territory.get_layout, query(), team_store=store)
    assert first_field(decode_proto(replies[0]), 4).value == 1
    store.save_territory_layout("Owner", season_id=3, player_id=11, area_id=9, payload=b"")
    replies, _ = invoke(actor, territory.get_layout, query(), team_store=store)
    fields = decode_proto(replies[0])
    assert first_field(fields, 4).value == 0 and first_field(fields, 3).value == b""


def test_grace_rejects_own_team_download_without_changing_state(setup):
    store, actor = setup
    before = deepcopy(store.state)
    replies, events = invoke(actor, territory.get_layout, query(), team_store=store, phase=0)
    assert not replies
    assert store.state == before
    assert events.rows[-1] == (
        "game_message_rejected",
        {
            "online_id": "Owner",
            "message_type": 0,
            "reason": "Territory download is disabled during the grace period",
        },
    )


def test_grace_does_not_disclose_an_unmatched_enemy_layout(setup):
    store, actor = setup
    store.save_native_territory_layout(
        "Peer",
        player_id=22,
        season_id=3,
        area_id=3,
        layout_version=7,
        payload=b"\0\7" + bytes(1055),
    )
    before = deepcopy(store.state)
    replies, events = invoke(
        actor,
        territory.get_layout,
        proto_varint(1, 3) + proto_varint(2, 22) + proto_varint(3, 3),
        team_store=store,
        phase=0,
    )
    assert not replies and store.state == before
    assert events.rows[-1][0] == "game_message_rejected"


def test_layout_write_is_team_scoped_even_with_foreign_player_metadata(setup):
    store, actor = setup
    store.save_territory_layout(
        "Peer", season_id=3, player_id=22, area_id=9, payload=b"peer-layout"
    )
    replies, _ = invoke(
        actor,
        territory.update_layout,
        query(22) + proto_bytes(4, b"owner-layout"),
        team_store=store,
    )
    assert [f.number for f in decode_proto(replies[0])] == list(range(1, 10))
    assert store.load_territory_layout("Peer", season_id=3, area_id=9) == b"peer-layout"
    reloaded = TeamStore(store.root)
    replies, _ = invoke(actor, territory.get_layout, query(22), team_store=reloaded)
    assert first_field(decode_proto(replies[0]), 3).value == b"owner-layout"


def test_single_opaque_alternate_wrapper_is_preserved(setup):
    store, actor = setup
    replies, _ = invoke(
        actor,
        territory.update_layout,
        query() + proto_bytes(7, b"alternate-wrapper"),
        team_store=store,
    )
    assert (
        replies
        and store.load_territory_layout("Owner", season_id=3, area_id=9) == b"alternate-wrapper"
    )


def test_missing_layout_does_not_change_team_state(setup):
    store, actor = setup
    before = deepcopy(store.state)
    replies, events = invoke(actor, territory.update_layout, query(), team_store=store)
    assert not replies and store.state == before
    assert events.rows[-1][0] == "game_message_rejected"


def test_nonmember_cannot_create_territory_storage(setup):
    store, _ = setup
    before = deepcopy(store.state)
    actor = GameSession(b"other001", bytes(16), "Nobody", 99)
    replies, events = invoke(
        actor, territory.update_layout, query() + proto_bytes(4, b"layout"), team_store=store
    )
    assert not replies and store.state == before
    assert events.rows[-1][0] == "game_message_rejected"


def test_dlc_lineup_reflects_valid_requested_ids_without_claiming_entitlements(setup):
    _, actor = setup
    payload = b"".join(proto_bytes(1, proto_varint(1, n)) for n in (110, 110, 220))
    payload += (
        proto_varint(1, 99)
        + proto_bytes(1, proto_bytes(1, b"wrong-type"))
        + proto_bytes(2, proto_varint(1, 999))
    )
    replies, events = invoke(actor, inventory.synchronize_dlc_lineup, payload)
    rows = [decode_proto(f.value) for f in decode_proto(replies[0])]
    assert [first_field(row, 1).value for row in rows] == [110, 110, 220]
    assert events.rows[-1][1]["count"] == 3
