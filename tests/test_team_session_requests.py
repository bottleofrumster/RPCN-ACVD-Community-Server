"""Live-session request boundaries with explicit shared state and callbacks."""

import pytest

from acvd_server.handlers import team_sessions
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


def message(payload=b""):
    return GameMessage(0, b"\0\0\0\1", payload, 1)


@pytest.fixture
def setup(tmp_path):
    store = TeamStore(tmp_path / "teams")
    team = store.create(
        online_id="Owner",
        player_id=11,
        name="Example",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=2,
    )
    store.join_team(online_id="Peer", player_id=22, team_id=team["team_id"])
    owner = GameSession(
        b"owner001", bytes(16), "Owner", 11, established=True, peer=("127.0.0.1", 1111)
    )
    peer = GameSession(
        b"peer0001",
        bytes(16),
        "Peer",
        22,
        established=True,
        peer=("127.0.0.1", 2222),
        team_session_ready=True,
    )
    directory = {
        team["team_id"]: {
            "session_id": team["team_id"],
            "team_id": team["team_id"],
            "members": {"Peer": {"player_id": 22, "member_id": 2, "np_identity": b"P" * 37}},
        }
    }
    return store, team["team_id"], directory, owner, peer


@pytest.mark.parametrize(
    "peer_ready,identity,expected_pushes",
    [
        (True, b"O" * 37, 2),
        (False, b"O" * 37, 0),
        (True, b"", 0),
    ],
)
def test_create_replies_before_correct_directional_announcements(
    setup, peer_ready, identity, expected_pushes
):
    store, tid, directory, owner, peer = setup
    peer.team_session_ready = peer_ready
    order = []
    pushes = []

    def purge():
        order.append("purge")

    def reply(payload):
        assert owner.team_session_ready and "Owner" in directory[tid]["members"]
        assert first_field(decode_proto(payload), 1).value == tid
        order.append("reply")

    def push(target, payload, *, subtype=None):
        assert order[:2] == ["purge", "reply"] and subtype == 9
        pushes.append((target, decode_proto(payload)))

    team_sessions.create_session(
        owner,
        message(proto_bytes(1, identity)),
        team_store=store,
        session_directory=directory,
        game_sessions={owner.auth_token: owner, peer.auth_token: peer},
        purge_stale_members=purge,
        send_push=push,
        telemetry=Events(),
        reply=reply,
    )
    assert len(pushes) == expected_pushes
    if expected_pushes:
        assert pushes[0][0] is owner and first_field(pushes[0][1], 5).value == b"P" * 37
        assert pushes[1][0] is peer and first_field(pushes[1][1], 5).value == identity


def test_join_updates_only_callers_identity_and_readiness(setup):
    store, tid, directory, owner, peer = setup
    replies = []
    team_sessions.join_session(
        owner,
        message(proto_varint(1, tid) + proto_bytes(2, b"O" * 37)),
        team_store=store,
        session_directory=directory,
        purge_stale_members=lambda: None,
        telemetry=Events(),
        reply=replies.append,
    )
    assert (
        owner.team_session_ready and directory[tid]["members"]["Owner"]["np_identity"] == b"O" * 37
    )
    assert directory[tid]["members"]["Peer"]["np_identity"] == b"P" * 37
    assert len(decode_proto(replies[0])) == 2


@pytest.mark.parametrize("team_count,clients_per_team", [(1, 3), (1, 5), (2, 5)])
def test_large_team_directories_announce_every_pair_without_merging_teams(
    tmp_path, team_count, clients_per_team
):
    store = TeamStore(tmp_path / "teams")
    directory, transports, pushes, groups = {}, {}, [], []
    for team_index in range(team_count):
        group = []
        team = store.create(
            online_id=f"Leader{team_index}",
            player_id=1000 + team_index * 100,
            name=f"Team{team_index}",
            operation_base=0,
            language=1,
            play_type=1,
            enlistment_method=2,
        )
        for index in range(clients_per_team):
            name = f"Leader{team_index}" if index == 0 else f"Member{team_index}_{index}"
            player_id = 1000 + team_index * 100 + index
            if index:
                assert store.join_team(online_id=name, player_id=player_id, team_id=team["team_id"])
            peer = GameSession(
                player_id.to_bytes(8, "big"),
                bytes(16),
                name,
                player_id,
                established=True,
                peer=("127.0.0.1", 12000 + player_id),
            )
            transports[peer.auth_token] = peer
            identity = b"\x01" + name.encode().ljust(36, b"\0")
            replies = []

            def push(target, payload, *, subtype=None):
                assert replies and subtype == 9
                fields = decode_proto(payload)
                pushes.append(
                    (
                        target.player_id,
                        first_field(fields, 2).value,
                        first_field(fields, 1).value,
                        first_field(fields, 5).value,
                    )
                )

            team_sessions.create_session(
                peer,
                message(proto_bytes(1, identity)),
                team_store=store,
                session_directory=directory,
                game_sessions=transports,
                purge_stale_members=lambda: None,
                send_push=push,
                telemetry=Events(),
                reply=replies.append,
            )
            group.append(peer)
        groups.append(group)
    expected = {
        (a.player_id, b.player_id) for group in groups for a in group for b in group if a is not b
    }
    assert {(recipient, announced) for recipient, announced, _, _ in pushes} == expected
    assert len(pushes) == team_count * clients_per_team * (clients_per_team - 1)
    assert len(directory) == team_count
    for group in groups:
        tid = store.team_for(group[0].online_id)["team_id"]
        assert set(directory[tid]["members"]) == {peer.online_id for peer in group}
        for recipient, announced, session_id, identity in pushes:
            if recipient not in {peer.player_id for peer in group}:
                continue
            source = next(peer for peer in group if peer.player_id == announced)
            assert session_id == tid
            assert identity == b"\x01" + source.online_id.encode().ljust(36, b"\0")


def test_unknown_join_does_not_create_session_or_mark_ready(setup):
    store, _, directory, owner, _ = setup
    replies = []
    team_sessions.join_session(
        owner,
        message(proto_varint(1, 999)),
        team_store=store,
        session_directory=directory,
        purge_stale_members=lambda: None,
        telemetry=Events(),
        reply=replies.append,
    )
    assert 999 not in directory and not owner.team_session_ready and replies == [b""]


def test_validity_queries_after_purge_not_before(setup):
    _, tid, directory, _, peer = setup
    replies = []

    def purge():
        directory[tid]["members"].clear()

    team_sessions.check_valid_player(
        peer,
        message(),
        session_directory=directory,
        purge_stale_members=purge,
        telemetry=Events(),
        reply=replies.append,
    )
    assert [f.value for f in decode_proto(replies[0])] == [0, 0]


@pytest.mark.parametrize("function", [team_sessions.leave_session, team_sessions.delete_session])
def test_departure_uses_authenticated_identity_and_notifies_before_reply(setup, function):
    _, tid, directory, owner, _ = setup
    directory[tid]["members"]["Owner"] = {"player_id": 11, "member_id": 1, "np_identity": b"O" * 37}
    order = []

    def remove(who, requested=0):
        assert who == "Owner" and requested == tid
        removed, deleted = team_sessions._remove_team_session_member_from_state(
            directory, who, requested
        )
        order.append("removed-and-notified")
        return removed, deleted, ["Peer"]

    def reply(payload):
        assert order == ["removed-and-notified"] and payload == b""
        assert set(directory[tid]["members"]) == {"Peer"}
        order.append("reply")

    function(
        owner,
        message(proto_varint(1, tid) + proto_varint(2, 2)),
        remove_member=remove,
        telemetry=Events(),
        reply=reply,
    )
    assert order == ["removed-and-notified", "reply"]


def test_bootstrap_success_does_not_imply_team_signaling_readiness(setup):
    _, _, _, owner, _ = setup
    replies = []
    team_sessions.check_bootstrap_player(owner, message(), telemetry=Events(), reply=replies.append)
    assert first_field(decode_proto(replies[0]), 1).value == 1
    assert not owner.team_session_ready


def test_disconnect_report_does_not_directly_remove_reported_peer(setup):
    _, tid, directory, owner, _ = setup
    replies = []
    events = Events()
    team_sessions.report_disconnected_member(
        owner,
        message(proto_varint(1, tid) + proto_varint(2, 22)),
        telemetry=events,
        reply=replies.append,
    )
    assert "Peer" in directory[tid]["members"] and replies == [b""]
    assert events.rows[-1][1]["reported_player_id"] == 22


def test_disconnect_report_delegates_only_unambiguous_wire_ids(setup):
    _, tid, directory, owner, _ = setup
    calls, replies = [], []

    def retire(actor, sid, pid):
        calls.append((actor, sid, pid))
        return True

    payload = proto_varint(1, tid) + proto_varint(2, 22)
    events = Events()
    team_sessions.report_disconnected_member(
        owner, message(payload), telemetry=events, reply=replies.append, retire_overlay=retire
    )
    assert calls == [(owner, tid, 22)] and replies == [b""]
    assert events.rows[-1][1]["foreign_overlay_retired"] is True
    calls.clear()
    team_sessions.report_disconnected_member(
        owner,
        message(payload + proto_varint(2, 999)),
        telemetry=events,
        reply=replies.append,
        retire_overlay=retire,
    )
    assert calls == [] and "Peer" in directory[tid]["members"]
