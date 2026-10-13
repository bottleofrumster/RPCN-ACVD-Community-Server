"""Authenticated multi-client squad models; these are not native RPCN tests."""

from copy import copy, deepcopy

import pytest

from acvd_server.handlers import matching
from acvd_server.match_policy import normal_pair_roles, preparation_squad, same_squad
from acvd_server.matchmaking import MatchmakingCoordinator
from acvd_server.protocol import decode_proto, proto_bytes, proto_varint
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


@pytest.fixture
def setup(tmp_path):
    teams = TeamStore(tmp_path / "teams")
    sessions, directory, pushes, events = {}, {}, [], Events()
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: directory,
        send_push=lambda *args, **kwargs: pushes.append((args, kwargs)),
        telemetry=events,
        room_log_paths=[],
        get_world_phase=lambda: 2,
        get_sessions=lambda: sessions.values(),
        get_team_id=lambda name: int((teams.team_for(name) or {}).get("team_id", 0)),
    )
    return teams, sessions, directory, pushes, events, coordinator


def group(setup, prefix, base, count, faction):
    teams, sessions, directory, _, _, _ = setup
    leader_name = f"{prefix}0"
    team = teams.create(
        online_id=leader_name,
        player_id=base,
        name=prefix,
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=2,
    )
    teams.set_faction(leader_name, 1, faction)
    tid = team["team_id"]
    result = []
    directory[tid] = {"session_id": tid, "team_id": tid, "members": {}}
    for index in range(count):
        name, player_id = f"{prefix}{index}", base + index
        if index:
            assert teams.join_team(online_id=name, player_id=player_id, team_id=tid)
        peer = GameSession(
            player_id.to_bytes(8, "big"),
            bytes(16),
            name,
            player_id,
            established=True,
            peer=("127.0.0.1", 15000 + player_id),
            team_session_ready=True,
        )
        sessions[peer.auth_token] = peer
        directory[tid]["members"][name] = {
            "player_id": player_id,
            "member_id": teams.player_profile(name)["member_id"],
            "np_identity": b"\x01" + name.encode().ljust(36, b"\0"),
        }
        result.append(peer)
    return result


def register(setup, peers, roles=None):
    teams, _, _, _, events, coordinator = setup
    payload = b"".join(
        proto_varint(k, v)
        for k, v in {
            1: 3,
            2: 2,
            3: 1,
            4: 1,
            5: 0,
            8: 3,
            9: 0,
            10: 1,
            11: 1,
            12: 1,
            13: 1,
            14: 1,
        }.items()
    )
    payload += b"".join(
        proto_bytes(6, proto_varint(1, p.player_id) + proto_varint(2, role))
        for p, role in zip(peers, roles or [0] * len(peers), strict=True)
    )
    replies = []
    matching.register_preparation(
        peers[0],
        GameMessage(0x3BD, bytes(4), payload),
        matchmaking=coordinator,
        team_store=teams,
        telemetry=events,
        reply=replies.append,
    )
    return replies


@pytest.mark.parametrize(
    "first_count,second_count", [(1, 2), (2, 1), (2, 3), (5, 5), (1, 5), (5, 1)]
)
def test_pair_tracks_every_human_but_keeps_two_captains(setup, first_count, second_count):
    first = group(setup, "Alpha", 100, first_count, 1)
    second = group(setup, "Beta", 200, second_count, 2)
    before = deepcopy(setup[0].state)
    assert register(setup, first) and register(setup, second)
    match = setup[-1].matches[1]
    assert len(match["members"]) == 2
    assert set(match["squad"]) == {peer.player_id for peer in first + second}
    assert len(match["squad"]) == first_count + second_count
    assert setup[0].state == before


@pytest.mark.parametrize("sizes", [(1, 2), (2, 3), (5, 5)])
def test_normal_pair_releases_host_without_cross_team_membership_pushes(setup, sizes):
    first = group(setup, "Alpha", 100, sizes[0], 1)
    second = group(setup, "Beta", 200, sizes[1], 2)
    durable, directory = deepcopy(setup[0].state), deepcopy(setup[2])
    register(setup, first)
    register(setup, second)
    match = setup[-1].matches[1]
    assert len(setup[3]) == 1
    (recipient, _), options = setup[3][0]
    assert recipient is match["members"][0]["session"] and options == {"subtype": 2}
    assert match["host_result_sent"]
    assert not match.get("preflight_retry_pending")
    assert not match.get("signaling_session_overlays")
    assert setup[0].state == durable and setup[2] == directory
    register(setup, first)
    register(setup, second)
    assert len(setup[3]) == 1  # Native request replay must not reset either client.


def test_old_captain_readiness_is_not_used_for_a_new_crew(setup):
    first = group(setup, "Alpha", 100, 2, 1)
    second = group(setup, "Beta", 200, 1, 2)
    setup[-1].signaling_ready_pairs.add(frozenset(("Alpha0", "Beta0")))
    register(setup, first)
    register(setup, second)
    assert len(setup[3]) == 1 and setup[3][0][1] == {"subtype": 2}
    assert setup[-1].signaling_ready_pairs == {frozenset(("Alpha0", "Beta0"))}
    assert not setup[-1].matches[1].get("signaling_session_overlays")


@pytest.mark.parametrize("fault", ["missing", "short", "wrong_player"])
@pytest.mark.parametrize("cached", [False, True])
def test_incomplete_crew_identity_never_sends_a_partial_overlay_or_room_release(
    setup, fault, cached
):
    first = group(setup, "Alpha", 100, 2, 1)
    second = group(setup, "Beta", 200, 1, 2)
    if cached:
        setup[-1].signaling_ready_pairs.add(frozenset(("Alpha0", "Alpha1", "Beta0")))
    record = setup[2][1]["members"]["Alpha1"]
    if fault == "missing":
        setup[2][1]["members"].pop("Alpha1")
    elif fault == "short":
        record["np_identity"] = b"short"
    else:
        record["player_id"] = 999
    register(setup, first)
    register(setup, second)
    assert not setup[3]
    assert not setup[-1].matches[1].get("signaling_peers_announced")
    assert setup[-2].rows[-1][0] == "game_match_squad_identity_waiting"


def test_normal_handoff_does_not_change_or_notify_unmatched_team_members(setup):
    first = group(setup, "Alpha", 100, 2, 1)
    second = group(setup, "Beta", 200, 1, 2)
    setup[2][1]["members"]["Bystander"] = {
        "player_id": 900,
        "member_id": 25,
        "np_identity": b"\x01" + b"Bystander".ljust(36, b"\0"),
    }
    before = deepcopy(setup[2])
    register(setup, first)
    register(setup, second)
    for (recipient, _), options in setup[3]:
        assert recipient.online_id != "Bystander"
        assert options == {"subtype": 2}
    assert setup[2] == before
    assert not setup[-1].matches[1].get("signaling_session_overlays")


def test_six_clients_on_one_side_are_rejected_before_registration(setup):
    peers = group(setup, "Alpha", 100, 6, 1)
    assert not register(setup, peers)
    assert not setup[-1].preparations and not setup[-1].matches


def test_nonleader_disconnect_removes_both_captain_preparations_not_teams(setup):
    first = group(setup, "Alpha", 100, 3, 1)
    second = group(setup, "Beta", 200, 2, 2)
    register(setup, first)
    register(setup, second)
    before = deepcopy(setup[0].state)
    coordinator = setup[-1]
    coordinator.signaling_ready_pairs.add(frozenset((first[0].online_id, second[0].online_id)))
    coordinator._remove_matchmaking_member(first[2].online_id, reason="disconnect")
    assert (
        not coordinator.matches
        and not coordinator.preparations
        and not coordinator.signaling_ready_pairs
    )
    assert setup[0].state == before


def test_stale_nonleader_invalidates_normal_pair(setup):
    first = group(setup, "Alpha", 100, 3, 1)
    second = group(setup, "Beta", 200, 2, 2)
    register(setup, first)
    register(setup, second)
    first[2].last_seen_monotonic -= 91
    setup[-1]._purge_stale_matchmaking()
    assert not setup[-1].matches and not setup[-1].preparations


def test_busy_nonleader_cannot_enter_another_paired_match(setup):
    first = group(setup, "Alpha", 100, 3, 1)
    second = group(setup, "Beta", 200, 2, 2)
    third = group(setup, "Gamma", 300, 1, 2)
    register(setup, first)
    register(setup, second)
    register(setup, [first[2]])
    register(setup, third)
    assert len(setup[-1].matches) == 1
    assert setup[-1].preparations[third[0].online_id]["match_id"] == 0


def test_frozen_squad_uses_object_identity_not_dataclass_equality(setup):
    peers = group(setup, "Alpha", 100, 2, 1)
    register(setup, peers)
    record = setup[-1].preparations[peers[0].online_id]
    original = preparation_squad(record)
    cloned = dict(original)
    cloned[peers[1].player_id] = copy(peers[1]), 1
    assert cloned == original
    assert not same_squad(cloned, original)
    bad_captain = {**record, "session": copy(peers[0])}
    assert preparation_squad(bad_captain) is None


def test_large_claimed_roster_cannot_be_silently_reduced_to_one_captain(setup):
    peers = group(setup, "Alpha", 100, 2, 1)
    other = group(setup, "Beta", 200, 1, 2)
    register(setup, peers)
    register(setup, other)
    a = dict(setup[-1].preparations[peers[0].online_id])
    b = setup[-1].preparations[other[0].online_id]
    a.pop("squad")
    assert preparation_squad(a) is None and normal_pair_roles(a, b) is None


def result_payload(peers, key, *, field2=1, field3=2):
    header = {1: key, 2: field2, 3: field3, 4: 0, 5: 0, 6: 0, 7: 0, 8: 0}
    result = b"".join(proto_varint(k, v) for k, v in header.items())
    for peer in peers:
        result += proto_bytes(
            9,
            b"".join(
                proto_varint(k, peer.player_id if k == 1 else 1 if k == 4 else 0)
                for k in range(1, 13)
            ),
        )
    return result


@pytest.mark.parametrize("result_fields", [(1, 2), (2, 1)])
def test_active_match_survives_the_old_handoff_deadline_and_accepts_all_participants(
    setup, result_fields
):
    first = group(setup, "Alpha", 100, 5, 1)
    second = group(setup, "Beta", 200, 5, 2)
    register(setup, first)
    register(setup, second)
    coordinator = setup[-1]
    match = coordinator.matches[1]
    match["room_published"] = True  # Model the already-verified room handoff boundary.
    match.pop("preflight_retry_pending", None)
    match["created_monotonic"] -= 203
    for member in match["members"]:
        member["registered_monotonic"] -= 203
    before = deepcopy(setup[0].state)
    coordinator._purge_stale_matchmaking()
    assert coordinator.matches[1] is match
    report = result_payload(
        first + second,
        match["members"][0]["prepare_id"],
        field2=result_fields[0],
        field3=result_fields[1],
    )
    assert coordinator.observe_combat_result(second[3], report, team_id=2)
    assert match["state"] == "result_received" and setup[0].state == before
    assert coordinator.observe_combat_result(first[0], report, team_id=1)
    assert setup[-2].rows[-1][1]["duplicate"] is True


def test_completed_match_can_start_fresh_without_old_retries_touching_it(setup):
    first = group(setup, "Alpha", 100, 2, 1)
    second = group(setup, "Beta", 200, 2, 2)
    register(setup, first)
    register(setup, second)
    coordinator = setup[-1]
    old = coordinator.matches[1]
    old["room_published"] = True
    old.pop("preflight_retry_pending", None)
    report = result_payload(first + second, old["members"][0]["prepare_id"])
    ready_after_entry = set(coordinator.signaling_ready_pairs)
    assert coordinator.observe_combat_result(first[0], report, team_id=1)
    assert coordinator.signaling_ready_pairs == ready_after_entry
    register(setup, first)
    register(setup, second)
    current = coordinator.matches[2]
    assert current["members"][0]["prepare_id"] > old["members"][0]["prepare_id"]
    ready_before = set(coordinator.signaling_ready_pairs)
    assert coordinator.observe_combat_result(second[0], report, team_id=2)
    assert coordinator.matches[2] is current and not current.get("result_sha256")
    assert coordinator.signaling_ready_pairs == ready_before


@pytest.mark.parametrize(
    "case", ["before_room", "wrong_member", "replaced", "wrong_team", "wrong_key", "expired"]
)
def test_normal_result_cannot_close_an_unrelated_or_unreleased_match(setup, case):
    first = group(setup, "Alpha", 100, 2, 1)
    second = group(setup, "Beta", 200, 2, 2)
    register(setup, first)
    register(setup, second)
    coordinator = setup[-1]
    match = coordinator.matches[1]
    match["room_published"] = case != "before_room"
    actor, team_id = first[0], 1
    peers = first + second
    key = match["members"][0]["prepare_id"]
    if case == "wrong_member":
        peers = first + second[:1]
    elif case == "replaced":
        actor = copy(actor)
    elif case == "wrong_team":
        team_id = 2
    elif case == "wrong_key":
        key += 1
    elif case == "expired":
        match["created_monotonic"] -= 3601
    assert not coordinator.observe_combat_result(actor, result_payload(peers, key), team_id=team_id)
    assert not match.get("result_sha256")


def completed_overlay(setup, first_count=2, second_count=3):
    """Seed historical synthetic state to test defensive cleanup, not new entry."""
    first = group(setup, "Alpha", 100, first_count, 1)
    second = group(setup, "Beta", 200, second_count, 2)
    register(setup, first)
    register(setup, second)
    coordinator = setup[-1]
    match = coordinator.matches[1]
    coordinator._announce_cross_session_match_peers(match)  # Legacy-state cleanup fixture only.
    match["room_published"] = True
    report = result_payload(first + second, match["members"][0]["prepare_id"])
    assert coordinator.observe_combat_result(first[0], report, team_id=1)
    setup[3].clear()
    return first, second, match


def test_completed_foreign_overlay_removal_is_caller_scoped_and_duplicate_safe(setup):
    first, second, match = completed_overlay(setup)
    durable, directory = deepcopy(setup[0].state), deepcopy(setup[2])
    coordinator = setup[-1]
    assert coordinator.retire_reported_overlay(first[0], 1, second[0].player_id)
    (recipient, raw), options = setup[3][0]
    assert recipient is first[0] and options["subtype"] == 10
    assert {f.number: f.value for f in decode_proto(raw)} == {1: 1, 2: second[0].player_id}
    assert sum(bool(o.get("retired")) for o in match["signaling_session_overlays"]) == 1
    assert not coordinator.retire_reported_overlay(first[0], 1, second[0].player_id)
    assert len(setup[3]) == 1
    assert setup[0].state == durable and setup[2] == directory
    assert not coordinator.signaling_ready_pairs


@pytest.mark.parametrize(
    "case",
    [
        "self",
        "wrong_session",
        "unknown_player",
        "replaced",
        "reused_slot",
        "duplicate_context",
        "active_combat",
        "expired",
        "team_changed",
        "stale_transport",
    ],
)
def test_overlay_cleanup_refuses_unsafe_or_unrelated_targets(setup, case):
    first, second, match = completed_overlay(setup)
    actor, sid, player_id = first[0], 1, second[0].player_id
    if case == "self":
        player_id = actor.player_id
    elif case == "wrong_session":
        sid = 2
    elif case == "unknown_player":
        player_id = 999
    elif case == "replaced":
        actor = copy(actor)
    elif case == "reused_slot":
        setup[2][1]["members"]["NewTeammate"] = {"member_id": 3, "player_id": 999}
    elif case == "duplicate_context":
        setup[-1].matches[2] = {**match, "match_id": 2}
    elif case == "active_combat":
        match.pop("result_sha256")
    elif case == "expired":
        match["created_monotonic"] -= 3601
    elif case == "team_changed":
        setup[-1].hires.get_team_id = lambda name: 3
    elif case == "stale_transport":
        actor.last_seen_monotonic -= 91
    before = deepcopy(setup[2])
    assert not setup[-1].retire_reported_overlay(actor, sid, player_id)
    assert not setup[3] and setup[2] == before


def test_late_old_disconnect_cannot_remove_a_new_active_match_overlay(setup):
    first, second, old = completed_overlay(setup)
    coordinator = setup[-1]
    assert coordinator.retire_reported_overlay(first[0], 1, second[0].player_id)
    register(setup, first)
    register(setup, second)
    setup[3].clear()
    assert not coordinator.matches[2].get("signaling_session_overlays")
    assert not coordinator.retire_reported_overlay(first[0], 1, second[0].player_id)
    assert not setup[3]


@pytest.mark.parametrize("reuse_readiness", [False, True])
@pytest.mark.parametrize("counts", [(1, 2), (2, 3), (5, 5)])
def test_unretired_old_overlay_cannot_remove_peer_in_new_match(setup, reuse_readiness, counts):
    first, second, old = completed_overlay(setup, *counts)
    coordinator = setup[-1]
    if not reuse_readiness:
        coordinator.signaling_ready_pairs.clear()
    register(setup, first)
    register(setup, second)
    current = coordinator.matches[2]
    assert current is not old and not current.get("result_sha256")
    ready = set(coordinator.signaling_ready_pairs)
    pushes = len(setup[3])
    assert not coordinator.retire_reported_overlay(first[0], 1, second[0].player_id)
    assert len(setup[3]) == pushes
    assert coordinator.signaling_ready_pairs == ready
    assert not any(o.get("retired") for o in old["signaling_session_overlays"])


def test_unknown_disconnect_does_not_clear_established_readiness(setup):
    first, _, _ = completed_overlay(setup)
    coordinator = setup[-1]
    ready = set(coordinator.signaling_ready_pairs)
    assert ready
    assert not coordinator.retire_reported_overlay(first[0], 1, 999)
    assert coordinator.signaling_ready_pairs == ready and not setup[3]


@pytest.mark.parametrize("sizes", [(2, 1), (3, 2), (5, 5)])
def test_registered_squad_crew_can_download_the_selected_enemy_layout(setup, sizes):
    from acvd_server.handlers import territory

    first = group(setup, "Alpha", 100, sizes[0], 1)
    second = group(setup, "Beta", 200, sizes[1], 2)
    store, _, _, _, events, coordinator = setup
    payload = b"\0\7" + bytes(1055)
    for peers in (first, second):
        captain = peers[0]
        store.team_for(captain.online_id)["territory_area_id"] = 3
        store.save_native_territory_layout(
            captain.online_id,
            player_id=captain.player_id,
            season_id=3,
            area_id=3,
            layout_version=7,
            payload=payload,
        )
    for peers in (first, second, first, second):
        assert register(setup, peers)
    match = coordinator.matches[1]
    assert not match.get("preflight_retry_pending")
    owner = match["members"][1]
    owner_id = match["territory_layout_owner_id"]
    area = match["combat_area_id"]
    assert owner_id == owner["session"].player_id
    before = deepcopy(store.state)
    push_count = len(setup[3])
    readers = [
        peer
        for peer in first + second
        if store.team_for(peer.online_id)["team_id"] != owner["team_id"]
    ]
    for actor in readers:
        replies = []
        territory.get_layout(
            actor,
            GameMessage(
                0x3BB,
                bytes(4),
                proto_varint(1, 3) + proto_varint(2, owner_id) + proto_varint(3, area),
            ),
            team_store=store,
            telemetry=events,
            reply=replies.append,
            matchmaking=coordinator,
            phase=0,
        )
        assert {f.number: f.value for f in decode_proto(replies[0])} == {2: 7, 3: payload, 4: 0}
        assert events.rows[-1][1]["match_scoped"] is True
    assert store.state == before
    assert len(setup[3]) == push_count


def disconnect_report(key, player_id):
    # Captured scalar widths are uint64,uint32,uint64,uint32. Field3 remains
    # opaque; this value is from a native report, not an invented error enum.
    return b"".join(
        proto_varint(k, v) for k, v in {1: key, 2: 0, 3: 9223653516126453760, 4: player_id}.items()
    )


@pytest.mark.parametrize("sizes", [(2, 1), (3, 2), (5, 5)])
@pytest.mark.parametrize("zero_target", [False, True])
def test_crew_disconnect_closes_only_its_matching_host_preparation(setup, sizes, zero_target):
    first = group(setup, "Alpha", 100, sizes[0], 1)
    second = group(setup, "Beta", 200, sizes[1], 2)
    for peers in (first, second, first, second):
        register(setup, peers)
    coordinator = setup[-1]
    match = coordinator.matches[1]
    key = match["members"][0]["prepare_id"]
    before = deepcopy(setup[0].state)
    payload = disconnect_report(key, 0 if zero_target else second[0].player_id)
    replies = []
    matching.report_disconnect(
        first[1],
        GameMessage(0x41B, bytes(4), payload),
        matchmaking=coordinator,
        telemetry=setup[4],
        reply=replies.append,
    )
    assert not coordinator.matches and not coordinator.preparations
    assert replies == [b""] and setup[0].state == before
    matching.report_disconnect(
        first[1],
        GameMessage(0x41B, bytes(4), payload),
        matchmaking=coordinator,
        telemetry=setup[4],
        reply=replies.append,
    )
    assert not coordinator.matches and not coordinator.preparations
    assert replies == [b"", b""] and setup[0].state == before


def test_late_report_for_an_old_host_key_cannot_cancel_current_battle(setup):
    first, second, old = completed_overlay(setup)
    key = old["members"][0]["prepare_id"]
    for peers in (first, second, first, second):
        register(setup, peers)
    coordinator = setup[-1]
    match = coordinator.matches[2]
    assert match["members"][0]["prepare_id"] != key
    preparations = dict(coordinator.preparations)
    ready = set(coordinator.signaling_ready_pairs)
    replies = []
    matching.report_disconnect(
        first[0],
        GameMessage(0x41B, bytes(4), disconnect_report(key, second[0].player_id)),
        matchmaking=coordinator,
        telemetry=setup[4],
        reply=replies.append,
    )
    assert coordinator.matches[2] is match and coordinator.matches[1] is old
    assert coordinator.preparations == preparations and coordinator.signaling_ready_pairs == ready
    assert replies == [b""]


@pytest.mark.parametrize(
    "case",
    [
        "replaced",
        "stale",
        "outsider",
        "wrong_peer",
        "wrong_key",
        "expired",
        "completed",
        "preflight",
        "ambiguous",
    ],
)
def test_disconnect_cannot_remove_an_unrelated_or_ambiguous_battle(setup, case):
    first = group(setup, "Alpha", 100, 2, 1)
    second = group(setup, "Beta", 200, 1, 2)
    for peers in (first, second, first, second):
        register(setup, peers)
    coordinator = setup[-1]
    match = coordinator.matches[1]
    actor, target = first[1], second[0].player_id
    key = match["members"][0]["prepare_id"]
    if case == "replaced":
        actor = copy(actor)
    elif case == "stale":
        actor.last_seen_monotonic -= 91
    elif case == "outsider":
        actor = group(setup, "Outside", 300, 1, 3)[0]
    elif case == "wrong_peer":
        target = 999
    elif case == "wrong_key":
        key += 100
    elif case == "expired":
        match["created_monotonic"] -= 3601
    elif case == "completed":
        match["result_sha256"] = "paid-or-recorded"
    elif case == "preflight":
        match["preflight_retry_pending"] = {first[0].online_id}
    elif case == "ambiguous":
        coordinator.matches[2] = {**match, "match_id": 2}
    ready = set(coordinator.signaling_ready_pairs)
    preparations = dict(coordinator.preparations)
    replies = []
    matching.report_disconnect(
        actor,
        GameMessage(0x41B, bytes(4), disconnect_report(key, target)),
        matchmaking=coordinator,
        telemetry=setup[4],
        reply=replies.append,
    )
    assert coordinator.matches[1] is match and coordinator.preparations == preparations
    assert coordinator.signaling_ready_pairs == ready and replies == [b""]


def test_report_can_close_a_match_even_when_its_reported_host_is_already_offline(setup):
    first = group(setup, "Alpha", 100, 2, 1)
    second = group(setup, "Beta", 200, 1, 2)
    for peers in (first, second, first, second):
        register(setup, peers)
    coordinator = setup[-1]
    match = coordinator.matches[1]
    host = match["members"][0]
    host["session"].established = False
    replies = []
    matching.report_disconnect(
        first[1],
        GameMessage(
            0x41B, bytes(4), disconnect_report(host["prepare_id"], host["session"].player_id)
        ),
        matchmaking=coordinator,
        telemetry=setup[4],
        reply=replies.append,
    )
    assert not coordinator.matches and replies == [b""]


@pytest.mark.parametrize("removal", ["explicit", "disconnect", "timeout"])
@pytest.mark.parametrize("counts", [(1, 1), (2, 3), (5, 5)])
def test_terminal_match_cleanup_retires_only_its_foreign_peer_entries(setup, removal, counts):
    first = group(setup, "Alpha", 100, counts[0], 1)
    second = group(setup, "Beta", 200, counts[1], 2)
    register(setup, first)
    register(setup, second)
    coordinator = setup[-1]
    match = coordinator.matches[1]
    coordinator._announce_cross_session_match_peers(match)  # Legacy-state cleanup fixture only.
    overlays = deepcopy(match["signaling_session_overlays"])
    durable, directory = deepcopy(setup[0].state), deepcopy(setup[2])
    setup[3].clear()
    if removal == "disconnect":
        first[0].established = False
        coordinator._remove_matchmaking_member(first[0].online_id, reason="fin")
        overlays = [o for o in overlays if o["recipient_online_id"] != first[0].online_id]
    elif removal == "timeout":
        match["created_monotonic"] -= 181
        coordinator._purge_stale_matchmaking()
    else:
        coordinator._remove_matchmaking_state(1)
    expected = {
        (o["recipient_online_id"], o["shared_session_id"], o["announced_player_id"])
        for o in overlays
    }
    actual = set()
    for (recipient, raw), options in setup[3]:
        assert options["subtype"] == 10
        fields = {f.number: f.value for f in decode_proto(raw)}
        actual.add((recipient.online_id, fields[1], fields[2]))
    assert actual == expected and len(setup[3]) == len(expected)
    assert not coordinator.matches and not coordinator.signaling_ready_pairs
    assert setup[0].state == durable and setup[2] == directory
    coordinator._remove_matchmaking_state(1)
    assert len(setup[3]) == len(expected)


@pytest.mark.parametrize(
    "hazard", ["replaced", "team_changed", "real_slot", "real_player", "new_match", "other_overlay"]
)
def test_terminal_overlay_cleanup_protects_current_membership_and_new_sessions(setup, hazard):
    first, second, match = completed_overlay(setup)
    actor = first[0]
    if hazard == "replaced":
        setup[1][actor.auth_token] = copy(actor)
    elif hazard == "team_changed":
        setup[0].state["members"][actor.online_id] = 2
    elif hazard in ("real_slot", "real_player"):
        for overlay in match["signaling_session_overlays"]:
            if overlay["recipient_online_id"] == actor.online_id:
                setup[2][1]["members"]["Real" + str(overlay["announced_member_id"])] = {
                    "member_id": overlay["announced_member_id"] if hazard == "real_slot" else 99,
                    "player_id": overlay["announced_player_id"] if hazard == "real_player" else 999,
                }
    elif hazard == "other_overlay":
        setup[-1].matches[2] = {
            "match_id": 2,
            "result_sha256": "completed",
            "signaling_session_overlays": [
                dict(o) for o in match["signaling_session_overlays"]
                if o["recipient_online_id"] == actor.online_id
            ],
        }
    else:
        setup[-1].matches[2] = {"match_id": 2, "squad": {actor.player_id: (actor, 1)}}
    before = deepcopy(setup[2])
    setup[-1]._remove_matchmaking_state(1)
    assert setup[3], "Other unchanged recipients must still receive their cleanup"
    assert all(args[0] is not actor for args, _ in setup[3])
    assert setup[2] == before


def test_failed_two_versus_three_with_cross_team_hires_removes_only_shadows(setup):
    first = group(setup, "Alpha", 100, 3, 1)
    second = group(setup, "Beta", 200, 1, 2)
    third = group(setup, "Gamma", 300, 1, 3)
    coordinator = setup[-1]
    # Match the live shape: one hired player belongs to the opposing captain's
    # real team; the other has a third home team. Neither membership can move.
    for mercenary in (third[0], first[2]):
        coordinator.hires.offer(second[0], mercenary.player_id)
        assert coordinator.hires.unregister(mercenary, True)
    register(setup, first[:2])
    register(setup, second + third + first[2:], roles=[0, 2, 2])
    match = coordinator.matches[1]
    coordinator._announce_cross_session_match_peers(match)  # Legacy-state cleanup fixture only.
    expected = {
        (o["recipient_online_id"], o["shared_session_id"], o["announced_player_id"])
        for o in match["signaling_session_overlays"]
    }
    assert len(match["squad"]) == 5 and len(expected) == 14
    durable, directory = deepcopy(setup[0].state), deepcopy(setup[2])
    setup[3].clear()
    coordinator._remove_matchmaking_state(1)
    actual = set()
    for (recipient, raw), options in setup[3]:
        assert options["subtype"] == 10
        fields = {f.number: f.value for f in decode_proto(raw)}
        actual.add((recipient.online_id, fields[1], fields[2]))
    assert actual == expected
    assert setup[0].state == durable and setup[2] == directory


@pytest.mark.parametrize("sizes", [(1, 1), (2, 3), (5, 5)])
def test_native_handoff_orders_host_then_guest_and_never_merges_team_rows(setup, sizes):
    first = group(setup, "Alpha", 100, sizes[0], 1)
    second = group(setup, "Beta", 200, sizes[1], 2)
    coordinator = setup[-1]
    before = deepcopy(setup[2]), deepcopy(setup[0].state)
    register(setup, first)
    register(setup, second)
    match = coordinator.matches[1]
    host, guest = match["members"]
    assert [(args[0], options["subtype"]) for args, options in setup[3]] == [(host["session"], 2)]
    archive = b"\0NXRV\x03\xf2" + bytes(506)
    replies = []
    coordinator.publish_native_room(
        host["session"], prepare_id=host["prepare_id"], encoded=archive, reply=replies.append
    )
    assert replies == [b""]
    assert [(args[0], options["subtype"]) for args, options in setup[3]] == [
        (host["session"], 2), (guest["session"], 2), (guest["session"], 5)
    ]
    room = {f.number: f.value for f in decode_proto(setup[3][-1][0][1])}
    assert room == {1: guest["prepare_id"], 2: host["prepare_id"], 3: archive}
    coordinator.publish_native_room(
        host["session"], prepare_id=host["prepare_id"], encoded=archive, reply=replies.append
    )
    assert replies == [b"", b""] and len(setup[3]) == 3
    assert (setup[2], setup[0].state) == before
    assert not match.get("signaling_session_overlays") and not match.get("preflight_retry_pending")
    coordinator._remove_matchmaking_member(first[-1].online_id, reason="fin")
    assert not coordinator.matches and not coordinator.preparations
    assert len(setup[3]) == 3  # No artificial team departure is needed.
    assert (setup[2], setup[0].state) == before


@pytest.mark.parametrize("fault", ["disconnect", "replaced", "changed_team", "stale", "lost_identity"])
def test_native_room_rechecks_every_frozen_crew_member_before_publication(setup, fault):
    first = group(setup, "Alpha", 100, 3, 1)
    second = group(setup, "Beta", 200, 2, 2)
    register(setup, first)
    register(setup, second)
    coordinator = setup[-1]
    match = coordinator.matches[1]
    host = match["members"][0]
    crew = first[-1]
    if fault == "disconnect":
        crew.established = False
    elif fault == "replaced":
        setup[1][crew.auth_token] = copy(crew)
    elif fault == "changed_team":
        setup[0].state["members"][crew.online_id] = 2
    elif fault == "stale":
        crew.last_seen_monotonic -= 91
    else:
        setup[2][1]["members"].pop(crew.online_id)
    replies = []
    with pytest.raises(ValueError, match="frozen room squad"):
        coordinator.publish_native_room(
            host["session"],
            prepare_id=host["prepare_id"],
            encoded=b"\0NXRV\x03\xf2" + bytes(506),
            reply=replies.append,
        )
    assert not replies and len(setup[3]) == 1
    assert "native_room_archive" not in match


def test_released_host_key_cannot_be_replaced_by_a_new_registration_key(setup):
    first = group(setup, "Alpha", 100, 1, 1)
    second = group(setup, "Beta", 200, 1, 2)
    register(setup, first)
    register(setup, second)
    coordinator = setup[-1]
    match = coordinator.matches[1]
    original = list(match["members"])
    retry = {**original[0], "prepare_id": 999}
    coordinator.preparations[retry["session"].online_id] = retry
    coordinator._match_preparation(retry)
    assert all(a is b for a, b in zip(match["members"], original))
    assert len(setup[3]) == 1
    assert setup[-2].rows[-1][0] == "game_match_retry_rejected"
