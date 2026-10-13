"""Native-proven peer reset, with modeled roster and stale-report boundaries."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from acvd_server.protocol import decode_proto, first_field, proto_varint
from acvd_server.server import GameDatagramProtocol
from acvd_server.teams import TeamStore
from acvd_server.udp import (
    CHECK_VALID_PLAYER,
    GET_WORLD_BASIC_INFO,
    GET_WORLD_CURRENT_INFO,
    REPORT_DISCONNECTED_TEAM_SESSION_MEMBER,
    GameMessage,
    GameSession,
)


@pytest.fixture
def world(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("acvd_server.session_lifecycle.time.monotonic", lambda: clock[0])

    def build(count=2, team_count=1):
        store = TeamStore(tmp_path / f"teams-{count}-{team_count}")
        events, sent, groups = [], [], []
        telemetry = SimpleNamespace(emit=lambda event, **kw: events.append((event, kw)))
        protocol = GameDatagramProtocol(telemetry, {}, None, tmp_path / "players", store)
        protocol._send_game_push = lambda target, body, **kw: sent.append((target, body, kw))
        for t in range(team_count):
            names = [f"Player{t}_{i}" for i in range(count)]
            team = store.create(
                online_id=names[0],
                player_id=100 + t * 100,
                name=f"Team{t}",
                operation_base=0,
                language=1,
                play_type=1,
                enlistment_method=2,
            )
            tid = team["team_id"]
            members, group = {}, []
            for i, name in enumerate(names):
                pid = 100 + t * 100 + i
                if i:
                    assert store.join_team(online_id=name, player_id=pid, team_id=tid)
                actor = GameSession(
                    pid.to_bytes(8, "big"),
                    bytes(16),
                    name,
                    pid,
                    established=True,
                    peer=("127.0.0.1", 10000 + pid),
                    team_session_ready=True,
                    last_seen_monotonic=clock[0],
                )
                protocol.sessions[actor.auth_token] = actor
                members[name] = {
                    "player_id": pid,
                    "member_id": store.player_profile(name)["member_id"],
                    "np_identity": name.encode().ljust(37, b"\0"),
                }
                group.append(actor)
            protocol.team_sessions[tid] = {"session_id": tid, "team_id": tid, "members": members}
            groups.append((tid, group))
        return protocol, store, groups, sent, events, clock

    return build


def recover(protocol, reporter, tid, player_id):
    return protocol._session_lifecycle.recover_reported_peer(
        reporter, tid, player_id, can_recover=protocol._matchmaking.team_peer_recovery_allowed
    )


@pytest.mark.parametrize("count,teams", [(2, 1), (3, 1), (5, 1), (5, 2)])
def test_each_real_peer_recovers_once_without_changing_rosters(world, count, teams):
    protocol, store, groups, sent, _, _ = world(count, teams)
    state, directory = deepcopy(store.state), deepcopy(protocol.team_sessions)
    for tid, group in groups:
        for reporter in group:
            for peer in group:
                if peer is reporter:
                    continue
                assert recover(protocol, reporter, tid, peer.player_id)
                assert not recover(protocol, reporter, tid, peer.player_id)
                leave, join = sent[-2:]
                assert leave[0] is join[0] is reporter
                assert [leave[2]["subtype"], join[2]["subtype"]] == [10, 9]
                assert [f.value for f in decode_proto(leave[1])] == [tid, peer.player_id]
                fields = decode_proto(join[1])
                assert first_field(fields, 2).value == peer.player_id
                assert first_field(fields, 4).value == 1
                assert (
                    first_field(fields, 5).value
                    == directory[tid]["members"][peer.online_id]["np_identity"]
                )
    assert len(sent) == teams * count * (count - 1) * 2
    assert store.state == state and protocol.team_sessions == directory
    assert all(s.team_session_ready for s in protocol.sessions.values())


@pytest.mark.parametrize(
    "failure",
    [
        "stale_caller",
        "stale_peer",
        "unready_peer",
        "no_peer",
        "replaced_caller",
        "duplicate_transport",
        "bad_identity",
        "duplicate_ordinal",
        "changed_ordinal",
        "missing_caller",
        "wrong_directory_team",
        "duplicate_player",
    ],
)
def test_invalid_or_stale_peer_does_not_reset_any_client(world, failure):
    protocol, _, groups, sent, _, _ = world()
    tid, (reporter, peer) = groups[0]
    members = protocol.team_sessions[tid]["members"]
    if failure == "stale_caller":
        reporter.last_seen_monotonic -= 91
    elif failure == "stale_peer":
        peer.last_seen_monotonic -= 91
    elif failure == "unready_peer":
        peer.team_session_ready = False
    elif failure == "no_peer":
        peer.peer = None
    elif failure == "replaced_caller":
        protocol.sessions[reporter.auth_token] = replace(reporter)
    elif failure == "duplicate_transport":
        duplicate = replace(peer, auth_token=b"second00")
        protocol.sessions[duplicate.auth_token] = duplicate
    elif failure == "bad_identity":
        members[peer.online_id]["np_identity"] = b"x"
    elif failure == "duplicate_ordinal":
        members[peer.online_id]["member_id"] = members[reporter.online_id]["member_id"]
    elif failure == "changed_ordinal":
        members[peer.online_id]["member_id"] = 99
    elif failure == "missing_caller":
        members.pop(reporter.online_id)
    elif failure == "wrong_directory_team":
        protocol.team_sessions[tid]["team_id"] = 999
    elif failure == "duplicate_player":
        members["Duplicate"] = dict(members[peer.online_id])
    assert not recover(protocol, reporter, tid, peer.player_id)
    assert sent == []


def test_foreign_overlay_is_not_a_real_team_recovery(world):
    protocol, _, groups, sent, _, _ = world(2, 2)
    tid, (reporter, _) = groups[0]
    foreign_tid, (foreign, _) = groups[1]
    record = dict(protocol.team_sessions[foreign_tid]["members"][foreign.online_id], member_id=3)
    protocol.team_sessions[tid]["members"][foreign.online_id] = record
    assert not recover(protocol, reporter, tid, foreign.player_id)
    assert sent == []


def test_later_cycle_can_recover_but_flood_cannot(world):
    protocol, _, groups, sent, events, clock = world()
    tid, (reporter, peer) = groups[0]
    assert recover(protocol, reporter, tid, peer.player_id)
    clock[0] += 14
    assert not recover(protocol, reporter, tid, peer.player_id)
    clock[0] += 3  # Live clients reported the same lost link again at 17s.
    assert recover(protocol, reporter, tid, peer.player_id)
    clock[0] += 16
    recovery_events = [data for event, data in events if event == "game_team_peer_recovery_sent"]
    assert recovery_events[-1]["previous_attempt_age_seconds"] == 17
    assert recovery_events[-1]["peer_connection_verified"] is False
    assert not recover(protocol, reporter, tid, peer.player_id)
    assert not any(event == "game_team_peer_recovery_escalated" for event, _data in events)
    clock[0] += 60
    reporter.last_seen_monotonic = peer.last_seen_monotonic = clock[0]
    assert recover(protocol, reporter, tid, peer.player_id)
    assert len(sent) == 6
    assert len(protocol._session_lifecycle._peer_recovery_times) == 1
    assert [data["attempt"] for event, data in events if event == "game_team_peer_recovery_sent"] == [1, 2, 1]


def test_report_coalesces_with_an_existing_safe_world_refresh(world):
    protocol, _, groups, sent, events, _ = world()
    tid, (reporter, peer) = groups[0]
    assert protocol._session_lifecycle.defer_post_battle_team_peer_refreshes(reporter) == 1

    assert recover(protocol, reporter, tid, peer.player_id)
    assert sent == []
    assert events[-1][0] == "game_team_peer_recovery_coalesced"
    assert protocol._session_lifecycle.flush_deferred_team_session_pushes(
        reporter, trigger="world_current_info"
    ) == 1
    assert len(sent) == 2


def test_safe_world_refresh_resets_only_the_refreshed_reporters_backoff(world):
    protocol, _, groups, _sent, _events, _ = world()
    tid, (first, second) = groups[0]
    assert recover(protocol, first, tid, second.player_id)
    assert recover(protocol, second, tid, first.player_id)
    state = protocol._session_lifecycle
    assert {key[0] for key in state._peer_recovery_times} == {
        first.auth_token,
        second.auth_token,
    }

    assert state.defer_post_battle_team_peer_refreshes(first) == 1
    assert state.flush_deferred_team_session_pushes(
        first, trigger="world_current_info"
    ) == 1

    assert {key[0] for key in state._peer_recovery_times} == {second.auth_token}


@pytest.mark.parametrize(
    "sid,pid", [(0, 101), (-1, 101), (2**64, 101), (1, 0), (1, -1), (1, 2**32), (1, 100), (1, 999)]
)
def test_bad_identifiers_do_not_send_pushes(world, sid, pid):
    protocol, _, groups, sent, _, _ = world()
    _, (reporter, _) = groups[0]
    assert not recover(protocol, reporter, sid, pid)
    assert sent == []


def test_recovery_history_is_bounded_and_expires(world):
    protocol, _, groups, sent, _, clock = world()
    tid, (reporter, peer) = groups[0]
    state = protocol._session_lifecycle
    state._peer_recovery_times = {
        (i.to_bytes(8, "big"), tid, b"other000"): clock[0] for i in range(4096)
    }
    assert not recover(protocol, reporter, tid, peer.player_id)
    assert sent == []
    clock[0] += 60
    assert recover(protocol, reporter, tid, peer.player_id)
    assert len(state._peer_recovery_times) == 1


def test_second_peer_recovery_still_respects_active_battle_guard(world):
    protocol, _, groups, sent, _, clock = world()
    tid, (reporter, peer) = groups[0]
    assert recover(protocol, reporter, tid, peer.player_id)
    clock[0] += 17
    prep = {"session": reporter, "squad": {reporter.player_id: (reporter, tid)}, "match_id": 1}
    protocol.preparations[reporter.online_id] = prep
    assert not recover(protocol, reporter, tid, peer.player_id)
    assert len(sent) == 2


def test_unrelated_battle_does_not_block_real_peer_recovery(world):
    protocol, _, groups, sent, _, _ = world(2, 2)
    tid, (reporter, peer) = groups[0]
    other_tid, other = groups[1]
    prep = {
        "session": other[0],
        "match_id": 1,
        "squad": {s.player_id: (s, other_tid) for s in other},
    }
    protocol.preparations[other[0].online_id] = prep
    protocol.matches[1] = {"members": [prep], "squad": prep["squad"], "created_monotonic": 1000.0}
    assert recover(protocol, reporter, tid, peer.player_id)
    assert len(sent) == 2 and protocol.matches[1]["members"][0] is prep


@pytest.mark.parametrize(
    "state", ["search", "preflight", "combat", "old_result_new_search", "reused_key"]
)
@pytest.mark.parametrize("subject", [0, 1])
def test_new_activity_of_either_peer_blocks_old_disconnect(world, state, subject):
    protocol, _, groups, sent, _, _ = world()
    tid, group = groups[0]
    reporter, peer = group
    participant = group[subject]
    prep = {
        "session": participant,
        "squad": {participant.player_id: (participant, tid)},
        "match_id": 1,
    }
    match = {
        "match_id": 1,
        "members": [prep],
        "squad": dict(prep["squad"]),
        "created_monotonic": 1000.0,
    }
    protocol.preparations[participant.online_id] = prep
    if state != "search":
        protocol.matches[1] = match
    if state == "preflight":
        match["preflight_retry_pending"] = True
    if state in ("old_result_new_search", "reused_key"):
        match["result_sha256"] = "completed"
        protocol.preparations[participant.online_id] = dict(
            prep, match_id=0 if state == "old_result_new_search" else 1
        )
    assert not recover(protocol, reporter, tid, peer.player_id)
    assert sent == []


def test_completed_battle_allows_recovery_without_erasing_receipt(world):
    protocol, _, groups, sent, _, _ = world()
    tid, (reporter, peer) = groups[0]
    prep = {"session": reporter, "match_id": 1}
    match = {
        "members": [prep],
        "squad": {reporter.player_id: (reporter, tid), peer.player_id: (peer, tid)},
        "created_monotonic": 1000.0,
        "result_sha256": "receipt",
    }
    protocol.preparations[reporter.online_id] = prep
    protocol.matches[1] = match
    assert recover(protocol, reporter, tid, peer.player_id)
    assert protocol.matches[1] is match and match["result_sha256"] == "receipt"
    assert protocol.preparations[reporter.online_id] is prep and len(sent) == 2


@pytest.mark.parametrize("subject", [0, 1])
@pytest.mark.parametrize("state", ["search", "preflight", "combat", "old_result_new_search"])
@pytest.mark.parametrize("message_type", [GET_WORLD_CURRENT_INFO, GET_WORLD_BASIC_INFO, CHECK_VALID_PLAYER])
def test_deferred_refresh_cannot_reset_either_peer_in_new_activity(world, subject, state, message_type):
    protocol, _, groups, sent, _, clock = world()
    tid, group = groups[0]
    reporter, peer = group
    coordinator = protocol._session_lifecycle
    assert coordinator.defer_post_battle_team_peer_refreshes(reporter) == 1
    pending = coordinator._pending_team_session_pushes[reporter.auth_token]
    queued = pending[(tid, peer.player_id)]
    participant = group[subject]
    prep = {
        "session": participant,
        "squad": {participant.player_id: (participant, tid)},
        "match_id": 1,
    }
    protocol.preparations[participant.online_id] = prep
    if state != "search":
        protocol.matches[1] = {
            "members": [prep],
            "squad": dict(prep["squad"]),
            "created_monotonic": clock[0],
            "preflight_retry_pending": state == "preflight",
        }
    if state == "old_result_new_search":
        protocol.matches[1]["result_sha256"] = "old-receipt"
        protocol.preparations[participant.online_id] = dict(prep, match_id=0)

    # All three dispatch triggers must respect the same guard as immediate
    # recovery. A read request does not supersede a known live match context.
    replies = []
    protocol._send_game_reply = lambda *args: replies.append(args[-1])
    message = GameMessage(message_type, b"\0\0\0\1", b"", 1)
    protocol._dispatch_game_message(reporter, None, message)
    assert len(replies) == 1
    assert sent == []
    assert coordinator._pending_team_session_pushes[reporter.auth_token][
        (tid, peer.player_id)
    ] is queued

    clock[0] += 10
    protocol.preparations.clear()
    protocol.matches.clear()
    protocol._dispatch_game_message(reporter, None, message)
    assert len(replies) == 2
    assert [row[2]["subtype"] for row in sent] == [10, 9]
    assert reporter.auth_token not in coordinator._pending_team_session_pushes


def test_deferred_refresh_holds_only_the_busy_peer_and_revalidates_later(world):
    protocol, store, groups, sent, _, _ = world(3)
    tid, (reporter, busy, available) = groups[0]
    coordinator = protocol._session_lifecycle
    assert coordinator.defer_post_battle_team_peer_refreshes(reporter) == 2
    queued = coordinator._pending_team_session_pushes[reporter.auth_token][(tid, busy.player_id)]
    protocol.preparations[busy.online_id] = {"session": busy, "match_id": 0}
    busy_key = (reporter.auth_token, tid, busy.auth_token)
    available_key = (reporter.auth_token, tid, available.auth_token)
    for key in (busy_key, available_key):
        coordinator._peer_recovery_times[key] = 999.0
        coordinator._peer_recovery_attempts[key] = 1

    assert coordinator.flush_deferred_team_session_pushes(reporter, trigger="world_current_info") == 1
    assert all(first_field(decode_proto(body), 2).value == available.player_id for _, body, _ in sent)
    assert coordinator._pending_team_session_pushes[reporter.auth_token] == {
        (tid, busy.player_id): queued
    }
    assert store.team_for(busy.online_id)["team_id"] == tid
    assert coordinator._peer_recovery_times == {busy_key: 999.0}
    assert coordinator._peer_recovery_attempts == {busy_key: 1}

    # A held refresh must still use the canonical identity at actual delivery.
    updated_identity = b"updated".ljust(37, b"\0")
    protocol.team_sessions[tid]["members"][busy.online_id]["np_identity"] = updated_identity
    protocol.preparations.clear()
    assert coordinator.flush_deferred_team_session_pushes(reporter, trigger="world_current_info") == 1
    assert first_field(decode_proto(sent[-1][1]), 5).value == updated_identity
    assert not coordinator._peer_recovery_times and not coordinator._peer_recovery_attempts


@pytest.mark.parametrize("extra", [b"", proto_varint(2, 999), proto_varint(3, 1)])
def test_real_dispatch_ack_precedes_recovery_and_ambiguous_payloads_do_not_act(world, extra):
    protocol, _, groups, sent, _, _ = world()
    tid, (reporter, peer) = groups[0]
    acknowledgements = []
    protocol._send_game_reply = lambda *args: acknowledgements.append(args[-1])
    push = protocol._send_game_push

    def ordered_push(*args, **kw):
        assert acknowledgements == [b""]
        push(*args, **kw)

    protocol._send_game_push = ordered_push
    payload = proto_varint(1, tid) + proto_varint(2, peer.player_id) + extra
    msg = GameMessage(REPORT_DISCONNECTED_TEAM_SESSION_MEMBER, b"\0\0\0\1", payload, 1)
    protocol._dispatch_game_message(reporter, None, msg)
    assert acknowledgements == [b""] and len(sent) == (0 if extra else 2)
