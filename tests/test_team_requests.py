"""Team mutation/query boundaries on temporary stores; no live roster changes."""

from copy import deepcopy

import pytest

from acvd_server.handlers import team_queries, team_updates
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
    first = store.create(
        online_id="Leader",
        player_id=11,
        name="First",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=2,
    )
    second = store.create(
        online_id="Destination",
        player_id=33,
        name="Second",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=2,
    )
    store.join_team(online_id="Mover", player_id=22, team_id=first["team_id"])
    return store, first, second


def session(name="Mover", player_id=22):
    return GameSession(b"caller01", bytes(16), name, player_id)


def message(payload=b""):
    return GameMessage(0, b"\0\0\0\1", payload, 1)


def test_transfer_keeps_membership_atomic_and_refreshes_old_then_new_after_reply(setup):
    store, first, second = setup
    order = []

    def reply(payload):
        assert payload == b"" and store.team_for("Mover")["team_id"] == second["team_id"]
        order.append("reply")

    def notify(team_id, *, reason, subject_online_id):
        assert order and order[0] == "reply" and subject_online_id == "Mover"
        order.append((team_id, reason))
        return ["recipient"]

    team_updates.join_team_auto(
        session(),
        message(proto_varint(1, second["team_id"])),
        team_store=store,
        telemetry=Events(),
        reply=reply,
        notify_team_profile=notify,
    )
    assert order == [
        "reply",
        (first["team_id"], "member_transferred_out"),
        (second["team_id"], "member_joined"),
    ]
    assert store.team_for("Leader")["team_id"] == first["team_id"]


def test_closed_destination_does_not_remove_existing_membership(setup):
    store, first, second = setup
    store.update_leader_settings(
        "Destination",
        pay_apportionment=0,
        leader_identity=b"Destination",
        enlistment_method=1,
        profile_text=b"",
    )
    before = deepcopy(store.state)
    notifications = []
    replies = []
    team_updates.join_team_auto(
        session(),
        message(proto_varint(1, second["team_id"])),
        team_store=store,
        telemetry=Events(),
        reply=replies.append,
        notify_team_profile=lambda *a, **k: notifications.append(a),
    )
    assert store.state == before and store.team_for("Mover")["team_id"] == first["team_id"]
    assert replies == [b""] and notifications == []


def test_leader_leave_promotes_member_before_reply_and_refresh(setup):
    store, first, second = setup
    order = []

    def reply(payload):
        assert store.team_for("Leader") is None
        assert store.team_by_id(first["team_id"])["leader_online_id"] == "Mover"
        order.append("reply")

    def notify(team_id, **kwargs):
        assert order == ["reply"] and team_id == first["team_id"]
        order.append("refresh")
        return ["Mover"]

    team_updates.leave_team(
        session("Leader", 11),
        message(),
        team_store=store,
        telemetry=Events(),
        reply=reply,
        notify_team_profile=notify,
    )
    assert (
        order == ["reply", "refresh"]
        and store.team_for("Destination")["team_id"] == second["team_id"]
    )


def test_last_member_leave_does_not_refresh_deleted_team(setup):
    store, first, second = setup
    notifications = []
    replies = []
    team_updates.leave_team(
        session("Destination", 33),
        message(),
        team_store=store,
        telemetry=Events(),
        reply=replies.append,
        notify_team_profile=lambda *a, **k: notifications.append(a),
    )
    assert store.team_by_id(second["team_id"]) is None
    assert store.team_by_id(first["team_id"]) is not None
    assert not notifications and replies == [b""]


@pytest.mark.parametrize(
    "age,established,has_peer,online",
    [
        (90, True, True, 1),
        (91, True, True, 0),
        (0, False, True, 0),
        (0, True, False, 0),
    ],
)
def test_presence_comes_from_fresh_established_session(
    setup, monkeypatch, age, established, has_peer, online
):
    store, _, _ = setup
    monkeypatch.setattr(team_queries.time, "monotonic", lambda: 1000.0)
    peer = GameSession(
        b"peer0001",
        bytes(16),
        "Mover",
        22,
        established=established,
        peer=("127.0.0.1", 12345) if has_peer else None,
        last_seen_monotonic=1000.0 - age,
    )
    replies = []
    team_queries.get_user_info(
        session("Leader", 11),
        message(proto_varint(1, 22)),
        team_store=store,
        game_sessions={peer.auth_token: peer},
        telemetry=Events(),
        reply=replies.append,
    )
    user = decode_proto(first_field(decode_proto(replies[0]), 1).value)
    assert first_field(user, 15).value == online
    assert first_field(user, 14).value == 0  # Presence must not become the penalty flag.


def test_discovery_lists_closed_teams_and_empty_list_request_returns_all(setup):
    store, first, second = setup
    store.update_leader_settings(
        "Destination",
        pay_apportionment=0,
        leader_identity=b"Destination",
        enlistment_method=1,
        profile_text=b"",
    )
    before = deepcopy(store.state)
    replies = []
    team_queries.search_team_ids(
        session(),
        message(proto_bytes(3, proto_bytes(2, b"Second"))),
        team_store=store,
        telemetry=Events(),
        reply=replies.append,
    )
    assert [f.value for f in decode_proto(replies[-1])] == [second["team_id"]]
    team_queries.get_team_info_list(
        session(), message(), team_store=store, telemetry=Events(), reply=replies.append
    )
    assert {first_field(decode_proto(f.value), 1).value for f in decode_proto(replies[-1])} == {
        first["team_id"],
        second["team_id"],
    }
    assert store.state == before


def test_search_applies_captured_free_filter_newest_order_and_pagination(tmp_path):
    store = TeamStore(tmp_path / "teams")
    methods = (2, 1, 2, 0, 3, 2)
    for index, enlistment_method in enumerate(methods, 1):
        store.create(
            online_id=f"Leader{index}",
            player_id=100 + index,
            name=f"Team{index}",
            operation_base=0,
            language=0,
            play_type=1,
            enlistment_method=enlistment_method,
        )
    captured_free_search = (
        proto_varint(1, 32)
        + proto_varint(2, 0)
        + proto_bytes(3, proto_varint(1, 7) + proto_varint(8, 2))
        + proto_varint(4, 1)
    )
    replies = []
    events = Events()
    team_queries.search_team_ids(
        session(),
        message(captured_free_search),
        team_store=store,
        telemetry=events,
        reply=replies.append,
    )
    assert [field.value for field in decode_proto(replies[-1])] == [6, 3, 1]
    assert events.rows[-1] == (
        "game_team_id_search_sent",
        {
            "peer": None,
            "online_id": "Mover",
            "requested_name": "",
            "enlistment_methods": [2],
            "limit": 32,
            "offset": 0,
            "sort_order": 1,
            "team_ids": [6, 3, 1],
            "count": 3,
        },
    )

    paged = (
        proto_varint(1, 2)
        + proto_varint(2, 1)
        + proto_bytes(3, proto_varint(1, 7) + proto_varint(8, 2))
        + proto_varint(4, 1)
    )
    team_queries.search_team_ids(
        session(), message(paged), team_store=store, telemetry=Events(), reply=replies.append
    )
    assert [field.value for field in decode_proto(replies[-1])] == [3, 1]


def test_search_name_and_all_joinable_methods_exclude_closed_and_legacy_rows(tmp_path):
    store = TeamStore(tmp_path / "teams")
    for index, enlistment_method in enumerate((0, 1, 2, 3, 13), 1):
        store.create(
            online_id=f"Leader{index}",
            player_id=200 + index,
            name=f"Candidate{index}",
            operation_base=0,
            language=0,
            play_type=1,
            enlistment_method=enlistment_method,
        )
    criteria = [
        proto_varint(1, 1) + proto_bytes(2, b"Candidate"),
        *(proto_varint(1, 7) + proto_varint(8, value) for value in (1, 2, 3)),
    ]
    payload = (
        proto_varint(1, 32)
        + proto_varint(2, 0)
        + b"".join(proto_bytes(3, criterion) for criterion in criteria)
        + proto_varint(4, 1)
    )
    replies = []
    team_queries.search_team_ids(
        session(), message(payload), team_store=store, telemetry=Events(), reply=replies.append
    )
    assert [field.value for field in decode_proto(replies[-1])] == [4, 3, 2]


def test_nonleader_cannot_update_leader_settings(setup):
    store, _, _ = setup
    before = deepcopy(store.state)
    replies = []
    events = Events()
    team_updates.update_leader_settings(
        session(),
        message(proto_varint(1, 1) + proto_bytes(4, b"changed")),
        team_store=store,
        telemetry=events,
        reply=replies.append,
    )
    assert store.state == before and not replies
    assert events.rows[-1][0] == "game_message_rejected"


@pytest.mark.parametrize("faction,context", [(0, 1), (4, 1), (1, 0), (1, 4)])
def test_invalid_faction_selection_does_not_mutate_team(setup, faction, context):
    store, _, _ = setup
    before = deepcopy(store.state)
    replies = []
    team_updates.join_faction(
        session(),
        message(proto_varint(1, faction) + proto_varint(2, context)),
        team_store=store,
        telemetry=Events(),
        reply=replies.append,
    )
    assert store.state == before and not replies


def test_faction_selection_assigns_initial_area_before_reply_and_preserves_holdings(setup):
    store, first, _ = setup
    replies = []
    events = Events()

    def reply(payload):
        team = store.team_for("Leader")
        assert team["current_faction_id"] == 1
        assert team["territory_area_id"] == 1
        replies.append(payload)

    team_updates.join_faction(
        session("Leader", 11),
        message(proto_varint(1, 1) + proto_varint(2, 3)),
        team_store=store,
        telemetry=events,
        reply=reply,
    )
    assert replies
    assert events.rows[-1] == (
        "game_faction_joined",
        {
            "peer": None,
            "online_id": "Leader",
            "team_id": first["team_id"],
            "world_id": 1,
            "faction_id": 1,
            "requested_faction_id": 1,
            "selection_context": 3,
            "territory_area_id": 1,
        },
    )

    first["territory_area_id"] = 7
    store._save()
    team_updates.join_faction(
        session("Leader", 11),
        message(proto_varint(1, 2) + proto_varint(2, 3)),
        team_store=store,
        telemetry=events,
        reply=replies.append,
    )
    assert store.team_for("Leader")["territory_area_id"] == 7
    assert events.rows[-1][1]["territory_area_id"] == 7
