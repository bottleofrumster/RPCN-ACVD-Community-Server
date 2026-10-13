"""Live session state ownership, freshness and notification regression checks."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from acvd_server.protocol import decode_proto, first_field
from acvd_server.server import GameDatagramProtocol
from acvd_server.teams import TeamStore
from acvd_server.udp import FIN, FIN_ACK, RST, GameSession, ReliablePacket


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("acvd_server.session_lifecycle.time.monotonic", lambda: 1000.0)
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
    protocol = GameDatagramProtocol(
        Events(), {}, Path("native/libacvd_cwc.so"), tmp_path / "players", store
    )
    sent = []
    monkeypatch.setattr(
        protocol, "_send_game_push", lambda *args, **kwargs: sent.append((args, kwargs))
    )
    return protocol, store, team["team_id"], sent


def actor(name, number, age=0, established=True, has_peer=True):
    return GameSession(
        name.encode().ljust(8, b"0")[:8],
        bytes(16),
        name,
        number,
        established=established,
        peer=("127.0.0.1", 10000 + number) if has_peer else None,
        team_session_ready=True,
        last_seen_monotonic=1000.0 - age,
    )


def directory(tid):
    return {
        tid: {
            "session_id": tid,
            "team_id": tid,
            "members": {
                "Owner": {"player_id": 11, "member_id": 1, "np_identity": b"O" * 37},
                "Peer": {"player_id": 22, "member_id": 2, "np_identity": b"P" * 37},
            },
        }
    }


def test_one_directory_is_shared_with_handlers_and_matchmaking(setup):
    protocol, _, tid, _ = setup
    state = directory(tid)
    protocol.team_sessions = state
    assert protocol._session_lifecycle.team_sessions is state
    assert protocol._matchmaking.team_sessions is state
    active = {"Peer"}
    protocol.active_mercenaries = active
    assert protocol._session_lifecycle.active_mercenaries is active
    protocol._session_lifecycle.active_mercenaries = set()
    assert protocol.active_mercenaries == set()


def test_auth_registry_and_store_are_borrowed_dynamically(setup, tmp_path):
    protocol, _, _, _ = setup
    sessions = {b"peer0001": actor("Peer", 22)}
    protocol.sessions = sessions
    assert protocol._session_lifecycle.sessions is sessions
    store = TeamStore(tmp_path / "other-teams")
    protocol.teams = store
    assert protocol._session_lifecycle.teams is store


@pytest.mark.parametrize(
    "age,established,has_peer,registered,expected",
    [
        (90, True, True, True, {"Peer"}),
        (91, True, True, True, set()),
        (0, False, True, True, set()),
        (0, True, False, True, set()),
        (0, True, True, False, set()),
    ],
)
def test_mercenary_availability_requires_registration_and_fresh_transport(
    setup, age, established, has_peer, registered, expected
):
    protocol, _, _, _ = setup
    peer = actor("Peer", 22, age, established, has_peer)
    protocol.sessions = {peer.auth_token: peer}
    protocol.active_mercenaries = {"Peer"} if registered else set()
    assert protocol._live_mercenary_online_ids() == expected


def test_profile_refresh_targets_only_fresh_durable_team_members(setup):
    protocol, _, tid, sent = setup
    people = [actor("Owner", 11), actor("Peer", 22, 91), actor("Other", 33)]
    protocol.sessions = {p.auth_token: p for p in people}
    recipients = protocol._notify_team_profile_updated(tid, reason="test", subject_online_id="Peer")
    assert recipients == ["Owner"]
    assert sent == [((people[0], b""), {"subtype": 4})]


def test_roster_changes_coalesce_until_recipient_returns_to_world(setup):
    protocol, _, tid, sent = setup
    owner = actor("Owner", 11)
    protocol.sessions = {owner.auth_token: owner}
    protocol.team_sessions = directory(tid)
    coordinator = protocol._session_lifecycle
    assert coordinator.defer_team_session_push(
        owner, b"join", subtype=9, session_id=tid, player_id=22
    )
    assert coordinator.defer_team_session_push(
        owner, b"leave", subtype=10, session_id=tid, player_id=22
    )
    assert sent == []
    assert coordinator.flush_deferred_team_session_pushes(owner, trigger="world_basic_info") == 1
    assert sent == [((owner, b"leave"), {"subtype": 10})]
    assert coordinator.flush_deferred_team_session_pushes(owner, trigger="world_current_info") == 0


def test_post_battle_peer_refresh_waits_for_world_and_preserves_leave_join_order(setup):
    protocol, _, tid, sent = setup
    owner, peer = actor("Owner", 11), actor("Peer", 22)
    protocol.sessions = {p.auth_token: p for p in (owner, peer)}
    protocol.team_sessions = directory(tid)

    assert protocol._session_lifecycle.defer_post_battle_team_peer_refreshes(owner) == 1
    assert sent == []
    assert (
        protocol._session_lifecycle.flush_deferred_team_session_pushes(
            owner, trigger="world_current_info"
        )
        == 1
    )
    assert [kwargs["subtype"] for _args, kwargs in sent] == [10, 9]
    leave = decode_proto(sent[0][0][1])
    join = decode_proto(sent[1][0][1])
    assert [first_field(leave, n).value for n in (1, 2)] == [tid, 22]
    assert [first_field(join, n).value for n in (1, 2, 3)] == [tid, 22, b"Peer"]


def test_stale_recipient_callback_does_not_consume_current_queue(setup):
    protocol, _, tid, sent = setup
    current = actor("Owner", 11)
    stale = replace(current)
    protocol.sessions = {current.auth_token: current}
    protocol.team_sessions = directory(tid)
    coordinator = protocol._session_lifecycle
    assert coordinator.defer_team_session_push(
        current, b"leave", subtype=10, session_id=tid, player_id=22
    )
    assert coordinator.flush_deferred_team_session_pushes(stale, trigger="world_current_info") == 0
    assert (
        coordinator.flush_deferred_team_session_pushes(current, trigger="world_current_info") == 1
    )
    assert len(sent) == 1


def test_refresh_does_not_reannounce_departed_peer_from_saved_payload(setup):
    protocol, _, tid, sent = setup
    owner, peer = actor("Owner", 11), actor("Peer", 22)
    protocol.sessions = {p.auth_token: p for p in (owner, peer)}
    protocol.team_sessions = directory(tid)
    coordinator = protocol._session_lifecycle
    assert coordinator.defer_post_battle_team_peer_refreshes(owner) == 1
    del protocol.team_sessions[tid]["members"]["Peer"]
    coordinator.flush_deferred_team_session_pushes(owner, trigger="world_current_info")
    assert [kw["subtype"] for _, kw in sent] == [10]


def test_refresh_rebuilds_changed_identity_instead_of_old_queued_bytes(setup):
    protocol, _, tid, sent = setup
    owner, peer = actor("Owner", 11), actor("Peer", 22)
    protocol.sessions = {p.auth_token: p for p in (owner, peer)}
    protocol.team_sessions = directory(tid)
    coordinator = protocol._session_lifecycle
    assert coordinator.defer_post_battle_team_peer_refreshes(owner) == 1
    current_identity = b"N" * 37
    protocol.team_sessions[tid]["members"]["Peer"]["np_identity"] = current_identity
    coordinator.flush_deferred_team_session_pushes(owner, trigger="world_current_info")
    assert first_field(decode_proto(sent[-1][0][1]), 5).value == current_identity


def test_post_battle_team_refresh_is_reciprocal_for_nonparticipant_members(setup):
    protocol, _, tid, sent = setup
    owner, peer = actor("Owner", 11), actor("Peer", 22)
    protocol.sessions = {p.auth_token: p for p in (owner, peer)}
    protocol.team_sessions = directory(tid)

    assert protocol._session_lifecycle.defer_post_battle_team_refreshes(tid) == [
        "Owner",
        "Peer",
    ]
    assert (
        protocol._session_lifecycle.flush_deferred_team_session_pushes(
            owner, trigger="world_current_info"
        )
        == 1
    )
    assert (
        protocol._session_lifecycle.flush_deferred_team_session_pushes(
            peer, trigger="world_current_info"
        )
        == 1
    )
    assert [kwargs["subtype"] for _args, kwargs in sent] == [10, 9, 10, 9]


@pytest.mark.parametrize("fault", ["not_ready", "disconnected", "no_peer"])
def test_temporarily_unready_recipient_retains_queue_until_safe_read(setup, fault):
    protocol, _, tid, sent = setup
    owner, peer = actor("Owner", 11), actor("Peer", 22)
    protocol.sessions = {p.auth_token: p for p in (owner, peer)}
    protocol.team_sessions = directory(tid)
    coordinator = protocol._session_lifecycle
    coordinator.defer_post_battle_team_peer_refreshes(owner)
    if fault == "not_ready":
        owner.team_session_ready = False
    elif fault == "disconnected":
        owner.established = False
    else:
        owner.peer = None
    assert coordinator.flush_deferred_team_session_pushes(owner, trigger="world_current_info") == 0
    assert not sent
    owner.team_session_ready = owner.established = True
    owner.peer = ("127.0.0.1", 10011)
    assert (
        coordinator.flush_deferred_team_session_pushes(owner, trigger="team_session_validated") == 1
    )
    assert [kw["subtype"] for _, kw in sent] == [10, 9]


def test_old_queue_and_old_discard_cannot_cross_same_token_session_replacement(setup):
    protocol, _, tid, sent = setup
    owner, peer = actor("Owner", 11), actor("Peer", 22)
    protocol.sessions = {p.auth_token: p for p in (owner, peer)}
    protocol.team_sessions = directory(tid)
    coordinator = protocol._session_lifecycle
    coordinator.defer_post_battle_team_peer_refreshes(owner)
    replacement = replace(owner)
    protocol.sessions[owner.auth_token] = replacement
    assert (
        coordinator.flush_deferred_team_session_pushes(replacement, trigger="world_current_info")
        == 0
    )
    assert not sent
    coordinator.defer_post_battle_team_peer_refreshes(replacement)
    assert coordinator.discard_deferred_team_session_pushes(owner) == 0
    assert (
        coordinator.flush_deferred_team_session_pushes(replacement, trigger="world_current_info")
        == 1
    )


@pytest.mark.parametrize(
    "fault", ["recipient_left", "duplicate_peer", "invalid_identity", "rejoined_leave"]
)
def test_stale_or_ambiguous_queued_changes_do_not_reset_healthy_peers(setup, fault):
    protocol, _, tid, sent = setup
    owner, peer = actor("Owner", 11), actor("Peer", 22)
    protocol.sessions = {p.auth_token: p for p in (owner, peer)}
    protocol.team_sessions = directory(tid)
    coordinator = protocol._session_lifecycle
    coordinator.defer_post_battle_team_peer_refreshes(owner)
    if fault == "recipient_left":
        del protocol.team_sessions[tid]["members"]["Owner"]
    elif fault == "duplicate_peer":
        duplicate = replace(peer, auth_token=b"dupe0001")
        protocol.sessions[duplicate.auth_token] = duplicate
    elif fault == "invalid_identity":
        protocol.team_sessions[tid]["members"]["Peer"]["np_identity"] = b"bad"
    else:
        coordinator.defer_team_session_push(
            owner, b"old leave", subtype=10, session_id=tid, player_id=22
        )
    assert coordinator.flush_deferred_team_session_pushes(owner, trigger="world_current_info") == 0
    assert not sent


def test_queue_age_records_observed_wait_not_a_claim_of_peer_connectivity(setup, monkeypatch):
    protocol, _, tid, sent = setup
    owner, peer = actor("Owner", 11), actor("Peer", 22)
    protocol.sessions = {p.auth_token: p for p in (owner, peer)}
    protocol.team_sessions = directory(tid)
    coordinator = protocol._session_lifecycle
    coordinator.defer_post_battle_team_peer_refreshes(owner)
    monkeypatch.setattr("acvd_server.session_lifecycle.time.monotonic", lambda: 1017.0)
    coordinator.flush_deferred_team_session_pushes(owner, trigger="world_current_info")
    event = protocol.telemetry.rows[-1][1]
    assert event["delivery_decisions"][0]["queue_age_seconds"] == 17.0
    assert event["delivery_decisions"][0]["reason"] == "current_roster"
    assert event["peer_connection_verified"] is False


def test_live_removal_preserves_durable_membership_and_notifies_survivor(setup):
    protocol, store, tid, sent = setup
    owner, peer = actor("Owner", 11), actor("Peer", 22)
    protocol.sessions = {p.auth_token: p for p in (owner, peer)}
    protocol.team_sessions = directory(tid)
    before = deepcopy(store.state)
    removed, deleted, notified = protocol._remove_team_session_member_and_notify("Peer", tid)
    assert (removed, deleted, notified) == ([tid], [], ["Owner"])
    assert not peer.team_session_ready and owner.team_session_ready
    assert store.state == before and store.team_for("Peer")["team_id"] == tid
    assert sent == []
    assert (
        protocol._session_lifecycle.flush_deferred_team_session_pushes(owner, trigger="test_world")
        == 1
    )
    target, payload = sent[0][0]
    assert target is owner and sent[0][1] == {"subtype": 10}
    fields = decode_proto(payload)
    assert [first_field(fields, n).value for n in (1, 2)] == [tid, peer.player_id]


@pytest.mark.parametrize("bad_id", [None, 0, -1, True, "22", 1 << 32])
def test_departure_never_guesses_a_player_id_from_an_invalid_record(setup, bad_id):
    protocol, store, tid, sent = setup
    owner, peer = actor("Owner", 11), actor("Peer", 22)
    protocol.sessions = {p.auth_token: p for p in (owner, peer)}
    protocol.team_sessions = directory(tid)
    protocol.team_sessions[tid]["members"]["Peer"]["player_id"] = bad_id
    before = deepcopy(store.state)
    assert protocol._remove_team_session_member_and_notify("Peer", tid) == ([tid], [], [])
    assert not sent
    assert set(protocol.team_sessions[tid]["members"]) == {"Owner"}
    assert store.state == before


def test_stale_purge_removes_empty_live_sessions_not_durable_teams(setup):
    protocol, store, tid, sent = setup
    owner, peer = actor("Owner", 11), actor("Peer", 22, 91)
    protocol.sessions = {p.auth_token: p for p in (owner, peer)}
    state = directory(tid)
    state[99] = {
        "session_id": 99,
        "team_id": 99,
        "members": {"Ghost": {"player_id": 99, "member_id": 1, "np_identity": b"G" * 37}},
    }
    protocol.team_sessions = state
    before = deepcopy(store.state)
    protocol._purge_stale_team_session_members()
    assert set(protocol.team_sessions) == {tid}
    assert set(protocol.team_sessions[tid]["members"]) == {"Owner"}
    assert store.state == before and not peer.team_session_ready
    assert not sent
    assert (
        protocol._session_lifecycle.flush_deferred_team_session_pushes(owner, trigger="test_world")
        == 1
    )
    assert len(sent) == 1


@pytest.mark.parametrize("opcode,reason", [(FIN, "fin"), (RST, "rst")])
@pytest.mark.parametrize("repeat", [False, True])
def test_transport_disconnect_cleans_live_state_only_and_preserves_order(
    setup, monkeypatch, opcode, reason, repeat
):
    protocol, store, tid, pushes = setup
    owner, peer = actor("Owner", 11), actor("Peer", 22)
    protocol.sessions = {p.auth_token: p for p in (owner, peer)}
    protocol.team_sessions = directory(tid)
    protocol.active_mercenaries = {"Owner", "Peer"}
    protocol.preparations = {"Peer": {"prepare_id": 7}}
    protocol.signaling_ready_pairs = {frozenset({"Owner", "Peer"})}
    before = deepcopy(store.state)
    order = []
    monkeypatch.setattr(
        "acvd_server.server.decrypt_client_datagram", lambda *a: (None, 0, b"decoded")
    )
    monkeypatch.setattr(
        "acvd_server.server.decode_reliable_packet", lambda _: ReliablePacket(19, 0, opcode)
    )
    monkeypatch.setattr(
        protocol,
        "_send",
        lambda target, packet: order.append(("send", packet.opcode, target.established)),
    )
    protocol.persist_sessions = lambda: order.append(("persist", peer.established))
    emit = protocol.telemetry.emit

    def record(event, **fields):
        if event == "game_session_closed":
            order.append(("closed", fields["reason"], peer.established))
        emit(event, **fields)

    monkeypatch.setattr(protocol.telemetry, "emit", record)
    protocol.datagram_received(peer.auth_token + b"wire", peer.peer)
    expected = [("send", FIN_ACK, True)] if opcode == FIN else []
    assert order == expected + [("closed", reason, False), ("persist", False)]
    assert not peer.established and not peer.team_session_ready
    assert owner.established and owner.team_session_ready
    assert set(protocol.team_sessions[tid]["members"]) == {"Owner"}
    assert protocol.active_mercenaries == {"Owner"}
    assert protocol.preparations == {} and protocol.signaling_ready_pairs == set()
    assert protocol.sessions[peer.auth_token] is peer
    assert store.state == before and pushes == []
    if repeat:
        protocol.datagram_received(peer.auth_token + b"wire", peer.peer)
        assert pushes == []
        assert store.state == before
        assert set(protocol.team_sessions[tid]["members"]) == {"Owner"}
    assert (
        protocol._session_lifecycle.flush_deferred_team_session_pushes(owner, trigger="test_world")
        == 1
    )
    assert len(pushes) == 1
