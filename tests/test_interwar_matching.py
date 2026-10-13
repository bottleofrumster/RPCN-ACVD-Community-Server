"""Owned solo boss lifecycle; no live RPCN, player data, or reward grants."""

import time
from copy import deepcopy
from pathlib import Path

import pytest

from acvd_server.combat_reports import decode_combat_report
from acvd_server.handlers import matching
from acvd_server.match_messages import interwar_boss_result
from acvd_server.matchmaking import INTERWAR_BOSS, MatchmakingCoordinator
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
    phase, pushes, replies, events = [0], [], [], Events()
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        send_push=lambda *a, **k: pushes.append((a, k)),
        telemetry=events,
        room_log_paths=[],
        get_world_phase=lambda: phase[0],
    )
    store = TeamStore(tmp_path / "teams")
    team = store.create(
        online_id="Owner",
        player_id=42,
        name="Test",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=1,
    )
    team["territory_area_id"] = 3
    store._save()
    actor = GameSession(
        b"owner001", bytes(16), "Owner", 42, established=True, peer=("127.0.0.1", 10001)
    )
    coordinator.hires.get_sessions = lambda: [actor]
    coordinator.hires.get_team_id = lambda name: int((store.team_for(name) or {}).get("team_id", 0))
    return coordinator, store, actor, phase, pushes, replies, events


def request(*, phase=0, season=3, sortie=2, players=(42,)):
    return (
        proto_varint(1, season)
        + proto_varint(2, phase)
        + proto_varint(3, sortie)
        + b"".join(proto_bytes(6, proto_varint(1, p) + proto_varint(2, 0)) for p in players)
    )


def register(setup, payload=None, actor=None):
    coordinator, store, default_actor, _, _, replies, events = setup
    matching.register_preparation(
        actor or default_actor,
        GameMessage(0x3BD, bytes(4), request() if payload is None else payload, 1),
        matchmaking=coordinator,
        team_store=store,
        telemetry=events,
        reply=replies.append,
    )


def report(key, *, player=42, cost=0, role=0):
    fields = {1: key, 2: 2, 3: 1, 4: 0, 5: cost, 6: 0, 7: 0, 8: 0}
    participant = b"".join(
        proto_varint(n, player if n == 1 else role if n == 8 else 1 if n in (3, 4) else 0)
        for n in range(1, 13)
    )
    return b"".join(proto_varint(n, value) for n, value in fields.items()) + proto_bytes(
        9, participant
    )


def test_owned_result_keeps_repeated_nonhuman_references_without_changing_money(setup):
    coordinator, store, actor, _, _, _, _ = setup
    register(setup)
    before = deepcopy(store.state)

    def unac(owner, ammo):
        values = (owner, 1002, 1, 2, 61800, 61800, ammo, 0, 0, 0, 0, 6)
        return proto_bytes(9, b"".join(proto_varint(n, v) for n, v in enumerate(values, 1)))

    raw = report(1, cost=3 * 61800) + b"".join(
        unac(42, ammo) for ammo in (10680, 10560, 10320)
    )
    assert not coordinator.observe_interwar_result(actor, raw + unac(99, 0), team_id=1)
    assert coordinator.observe_interwar_result(actor, raw, team_id=1)
    assert len(decode_combat_report(raw).units) == 4
    assert store.state == before


def test_native_solo_wire_and_owned_context(setup):
    coordinator, store, actor, _, pushes, replies, _ = setup
    before = deepcopy(store.state)
    register(setup)
    fields = {f.number: f.value for f in decode_proto(pushes[0][0][1])}
    assert fields == {1: 1, 2: 0, 3: 1, 4: 6, 5: 1, 6: 3, 7: 0, 8: 0, 9: 1}
    assert pushes[0][0][0] is actor and pushes[0][1] == {"subtype": 2}
    match = coordinator.matches[1]
    assert match["kind"] == INTERWAR_BOSS and len(match["members"]) == 1
    assert match["world_phase"] == 0 and first_field(decode_proto(replies[0]), 1).value == 1
    assert store.state == before


def test_active_retry_reuses_context_without_another_push(setup):
    coordinator, _, _, _, pushes, replies, _ = setup
    register(setup)
    created = coordinator.matches[1]["created_monotonic"]
    register(setup)
    assert len(pushes) == 1 and len(replies) == 2
    assert coordinator.matches[1]["created_monotonic"] == created
    assert coordinator.preparations["Owner"]["match_id"] == 1


@pytest.mark.parametrize(
    "server_phase,payload",
    [
        (2, request()),
        (0, request(phase=2)),
        (0, request(season=2)),
        (0, request(players=())),
        (0, request(players=(43,))),
        (0, request(players=(42, 43))),
        (0, request(players=(42, 42))),
    ],
)
def test_unqualified_or_spoofed_grace_requests_do_not_mutate(setup, server_phase, payload):
    coordinator, store, _, phase, pushes, replies, events = setup
    phase[0] = server_phase
    before = deepcopy(store.state)
    register(setup, payload)
    assert not pushes and not replies and not coordinator.preparations and not coordinator.matches
    assert store.state == before and events.rows[-1][0] == "game_message_rejected"


def test_grace_rejects_normal_without_disabling_special(setup):
    coordinator, store, _, _, pushes, replies, events = setup
    before = deepcopy(store.state)

    register(setup, request(sortie=1))
    assert not pushes and not replies and not coordinator.preparations and not coordinator.matches
    assert store.state == before
    assert events.rows[-1] == (
        "game_message_rejected",
        {
            "online_id": "Owner",
            "message_type": 0x3BD,
            "reason": "Normal Sortie is unavailable during the grace period",
        },
    )

    register(setup)
    assert len(pushes) == len(replies) == 1
    assert coordinator.matches[1]["kind"] == INTERWAR_BOSS


@pytest.mark.parametrize("payload", [request(sortie=1), request(season=2), request(players=(43,))])
def test_rejected_registration_replies_without_mutating_state(setup, payload):
    coordinator, store, actor, _, pushes, replies, events = setup
    errors = []
    before = deepcopy(store.state)
    matching.register_preparation(
        actor,
        GameMessage(0x3BD, bytes(4), payload, 1),
        matchmaking=coordinator,
        team_store=store,
        telemetry=events,
        reply=replies.append,
        reply_error=lambda *args: errors.append(args),
    )
    assert errors == [(1, 28)]
    assert not replies and not pushes and not coordinator.preparations and not coordinator.matches
    assert store.state == before


def test_active_phase_still_accepts_normal_registration(setup):
    coordinator, _, _, phase, pushes, replies, _ = setup
    phase[0] = 2

    register(setup, request(phase=2, sortie=1))

    assert not pushes and len(replies) == 1
    assert coordinator.preparations["Owner"]["criteria"][3] == 1
    assert not coordinator.matches


@pytest.mark.parametrize("missing", ["team", "session", "peer"])
def test_grace_requires_a_live_team_member(setup, missing):
    coordinator, store, actor, _, pushes, replies, _ = setup
    if missing == "team":
        store.leave_team("Owner")
    elif missing == "session":
        actor.established = False
    else:
        actor.peer = None
    register(setup)
    assert not pushes and not replies and not coordinator.preparations


def test_zero_player_identity_cannot_receive_a_boss_assignment(setup):
    coordinator, _, actor, _, pushes, replies, _ = setup
    actor.player_id = 0
    register(setup, request(players=(0,)))
    assert not pushes and not replies and not coordinator.preparations


def test_changed_active_registration_preserves_the_original_context(setup):
    coordinator, _, _, _, pushes, replies, _ = setup
    register(setup)
    current = coordinator.preparations["Owner"]
    register(setup, request() + proto_varint(4, 2))
    assert coordinator.preparations["Owner"] is current
    assert len(pushes) == len(replies) == 1


def test_grace_registration_cannot_replace_an_active_paired_mission(setup):
    coordinator, _, actor, _, pushes, replies, _ = setup
    current = {
        "prepare_id": 1,
        "match_id": 1,
        "session": actor,
        "team_id": 1,
        "criteria": {1: 3, 2: 2, 3: 1},
        "registered_monotonic": time.monotonic(),
    }
    coordinator.preparations[actor.online_id] = current
    coordinator.matches[1] = {
        "match_id": 1,
        "members": [current],
        "created_monotonic": time.monotonic(),
    }
    register(setup)
    assert coordinator.preparations[actor.online_id] is current
    assert not pushes and not replies


@pytest.mark.parametrize("kind", [None, "active_single_team"])
def test_completed_war_mission_does_not_block_grace_boss_registration(setup, kind):
    coordinator, _, actor, _, pushes, replies, _ = setup
    current = {
        "prepare_id": 7, "match_id": 7, "session": actor, "team_id": 1,
        "criteria": {1: 3, 2: 2, 3: 1}, "registered_monotonic": time.monotonic(),
    }
    completed = {
        "match_id": 7, "kind": kind, "members": [current],
        "created_monotonic": time.monotonic(), "result_sha256": "00" * 32,
    }
    coordinator.preparations[actor.online_id] = current
    coordinator.matches[7] = completed
    coordinator.next_prepare_id = coordinator.next_match_id = 8
    register(setup)
    assert replies and pushes
    assert coordinator.matches[7] is completed  # Late result replay remains available.
    assert coordinator.matches[8]["kind"] == INTERWAR_BOSS
    assert coordinator.preparations[actor.online_id]["prepare_id"] == 8


def test_result_retry_and_new_mission_keep_distinct_keys(setup):
    coordinator, store, actor, _, pushes, replies, _ = setup
    before = deepcopy(store.state)
    register(setup)
    first = report(1)
    assert coordinator.observe_interwar_result(actor, first, team_id=1)
    register(setup)
    assert [first_field(decode_proto(r), 1).value for r in replies] == [1, 2]
    newer = coordinator.preparations["Owner"]
    assert coordinator.observe_interwar_result(actor, first, team_id=1)
    assert coordinator.preparations["Owner"] is newer and newer["prepare_id"] == 2
    assert not coordinator.observe_interwar_result(actor, report(1, cost=99), team_id=1)
    assert coordinator.matches[2]["state"] == "released" and len(pushes) == 2
    assert store.state == before


@pytest.mark.parametrize(
    "payload,team",
    [
        (report(999), 1),
        (report(1, player=43), 1),
        (report(1), 2),
        (b"", 1),
        (b"x" * 4097, 1),
        (b"\xff", 1),
        (proto_varint(1, 1), 1),
        (report(1) + proto_varint(1, 1), 1),
    ],
)
def test_invalid_report_cannot_close_a_context(setup, payload, team):
    coordinator, _, actor, _, _, _, _ = setup
    register(setup)
    assert not coordinator.observe_interwar_result(actor, payload, team_id=team)
    assert coordinator.matches[1]["state"] == "released"


def test_replaced_session_cannot_complete_the_new_context(setup):
    coordinator, _, actor, _, _, _, _ = setup
    register(setup)
    replacement = GameSession(
        b"owner002", bytes(16), "Owner", 42, established=True, peer=("127.0.0.1", 10002)
    )
    assert not coordinator.observe_interwar_result(replacement, report(1), team_id=1)
    coordinator.hires.get_sessions = lambda: [replacement]
    register(setup, actor=replacement)
    assert coordinator.preparations["Owner"]["prepare_id"] == 2
    assert not coordinator.observe_interwar_result(actor, report(2), team_id=1)


def test_result_identity_must_match_the_original_preparation_snapshot(setup):
    coordinator, _, actor, _, _, _, _ = setup
    register(setup)
    actor.player_id = 43
    assert not coordinator.observe_interwar_result(actor, report(1, player=43), team_id=1)
    assert coordinator.matches[1]["state"] == "released"


def test_phase_change_does_not_erase_an_existing_result_context(setup):
    coordinator, _, actor, phase, _, _, _ = setup
    register(setup)
    phase[0] = 2
    assert coordinator.observe_interwar_result(actor, report(1), team_id=1)
    register(setup, request(phase=2, sortie=1))
    newer = coordinator.preparations["Owner"]
    assert newer["prepare_id"] == 2 and not newer.get("interwar_boss")
    assert coordinator.observe_interwar_result(actor, report(1), team_id=1)
    assert coordinator.preparations["Owner"] is newer


def test_boss_lifetime_exceeds_paired_handoff_but_stays_bounded(setup):
    coordinator, _, actor, _, _, _, _ = setup
    register(setup)
    match = coordinator.matches[1]
    match["created_monotonic"] = time.monotonic() - 600
    coordinator.preparations["Owner"]["registered_monotonic"] -= 600
    coordinator._purge_stale_matchmaking()
    assert coordinator.matches[1] is match
    match["created_monotonic"] = time.monotonic() - 1801
    coordinator._purge_stale_matchmaking()
    assert not coordinator.preparations and not coordinator.matches


def test_expired_receipt_does_not_remove_a_newer_mission(setup):
    coordinator, _, actor, _, _, _, _ = setup
    register(setup)
    assert coordinator.observe_interwar_result(actor, report(1), team_id=1)
    register(setup)
    coordinator.matches[1]["created_monotonic"] = time.monotonic() - 1801
    newer = coordinator.preparations["Owner"]
    coordinator._purge_stale_matchmaking()
    assert 1 not in coordinator.matches and 2 in coordinator.matches
    assert coordinator.preparations["Owner"] is newer


def test_cancel_closes_only_the_owned_grace_assignment(setup):
    coordinator, store, actor, _, _, replies, events = setup
    register(setup)
    before = deepcopy(store.state)
    matching.cancel_preparation(
        actor,
        GameMessage(0x3BA, bytes(4), b"", 1),
        matchmaking=coordinator,
        telemetry=events,
        reply=replies.append,
    )
    assert not coordinator.matches and not coordinator.preparations
    assert store.state == before


def test_completed_context_retention_is_bounded(setup):
    coordinator, _, actor, _, _, _, _ = setup
    for key in range(1, 12):
        register(setup)
        assert coordinator.observe_interwar_result(actor, report(key), team_id=1)
    assert len(coordinator.matches) == 8
    assert not coordinator.observe_interwar_result(actor, report(1), team_id=1)


@pytest.mark.parametrize("key,area", [(0, 3), (True, 3), (1 << 64, 3), (1, True), (1, 8)])
def test_boss_encoder_rejects_invalid_identifiers(key, area):
    with pytest.raises(ValueError):
        interwar_boss_result(key, area)


@pytest.fixture
def hired(setup):
    from acvd_server.handlers import mercenaries

    coordinator, store, actor, _, _, _, events = setup
    store.create(
        online_id="Mercenary",
        player_id=43,
        name="Other",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=1,
    )
    merc = GameSession(
        b"merc0001", bytes(16), "Mercenary", 43, established=True, peer=("127.0.0.1", 10002)
    )
    sessions = [actor, merc]
    coordinator.hires.get_sessions = lambda: sessions
    coordinator.hires.get_team_id = lambda name: int((store.team_for(name) or {}).get("team_id", 0))
    store.register_mercenary(
        online_id=merc.online_id, player_id=43, ac_data=b"test", result_values=[]
    )
    active, replies = {merc.online_id}, []
    mercenaries.queue_negotiation(
        actor,
        GameMessage(0x419, bytes(4), proto_varint(1, 43)),
        team_store=store,
        live_mercenary_online_ids=lambda: active,
        telemetry=events,
        reply=replies.append,
        hire_bindings=coordinator.hires,
    )
    assert coordinator.hires.offers and store.mercenary_messages_for(merc.online_id)
    mercenaries.unregister(
        merc,
        GameMessage(0x3D3, bytes(4), proto_varint(1, 1)),
        team_store=store,
        active_mercenaries=active,
        telemetry=events,
        reply=replies.append,
        hire_bindings=coordinator.hires,
    )
    assert not active and not store.mercenary_messages_for(merc.online_id)
    assert coordinator.hires.resolve(actor, 43) is not None
    return setup, merc, sessions


def squad_request():
    return request() + proto_bytes(6, proto_varint(1, 43) + proto_varint(2, 2))


def squad_report(key):
    return report(key) + proto_bytes(
        9, first_field(decode_proto(report(key, player=43, role=2)), 9).value
    )


def test_hired_boss_uses_one_preparation_and_only_leader_push(hired):
    setup, merc, _ = hired
    coordinator, store, actor, _, pushes, replies, _ = setup
    before = deepcopy(store.state)
    register(setup, squad_request())
    assert len(pushes) == len(replies) == len(coordinator.preparations) == 1
    assert pushes[0][0][0] is actor
    assert coordinator.matches[1]["squad"] == {42: (actor, 1), 43: (merc, 2)}
    assert coordinator.preparations["Owner"]["claimed_player_roles"] == [0, 2]
    register(setup, squad_request())
    assert len(pushes) == 1 and len(replies) == 2
    assert store.state == before


def test_hire_retries_preserve_binding_without_extending_its_lifetime(hired):
    setup, merc, _ = hired
    coordinator, _, actor, _, _, _, _ = setup
    item = coordinator.hires.resolve(actor, 43)
    coordinator.hires.offer(actor, 43)
    assert coordinator.hires.unregister(merc, True)
    assert coordinator.hires.resolve(actor, 43) is item


def test_registering_again_discards_the_previous_hire(hired):
    from acvd_server.handlers import mercenaries

    setup, merc, _ = hired
    coordinator, store, actor, _, _, _, events = setup
    mercenaries.register(
        merc,
        GameMessage(0x3CB, bytes(4), proto_bytes(3, b"test")),
        team_store=store,
        active_mercenaries=set(),
        telemetry=events,
        reply=lambda _: None,
        hire_bindings=coordinator.hires,
    )
    assert coordinator.hires.resolve(actor, 43) is None


@pytest.mark.parametrize(
    "fault", ["expired", "replaced", "team", "identity", "disconnected", "stale", "cancelled"]
)
def test_stale_or_changed_hire_cannot_enter_boss(hired, fault):
    from dataclasses import replace

    setup, merc, sessions = hired
    coordinator, store, actor, _, pushes, replies, _ = setup
    if fault == "expired":
        coordinator.hires.accepted[merc.online_id] = replace(
            coordinator.hires.accepted[merc.online_id], expires=0
        )
    elif fault == "replaced":
        sessions.remove(merc)
        sessions.append(
            GameSession(
                b"merc0002", bytes(16), "Mercenary", 43, established=True, peer=("127.0.0.1", 10003)
            )
        )
    elif fault == "team":
        store.leave_team(merc.online_id)
    elif fault == "identity":
        merc.player_id = 99
    elif fault == "disconnected":
        merc.established = False
    elif fault == "stale":
        merc.last_seen_monotonic -= 91
    else:
        coordinator.hires.discard(actor.online_id)
    register(setup, squad_request())
    assert not pushes and not replies and not coordinator.matches


@pytest.mark.parametrize(
    "extra",
    [
        proto_bytes(6, proto_varint(1, 43) + proto_varint(2, 0)),
        proto_bytes(6, proto_varint(1, 43) + proto_varint(2, 2) + proto_varint(1, 44)),
        proto_bytes(6, proto_varint(1, 43)),
        proto_varint(6, 43),
        proto_bytes(6, b""),
    ],
)
def test_hired_boss_rejects_malformed_or_wrong_role_roster(hired, extra):
    setup, _, _ = hired
    register(setup, request() + extra)
    assert not setup[4] and not setup[5] and not setup[0].matches


def test_hired_result_requires_exact_roster_and_allows_authenticated_mercenary(hired):
    setup, merc, _ = hired
    coordinator, store, actor, _, pushes, _, _ = setup
    register(setup, squad_request())
    before = deepcopy(store.state)
    assert not coordinator.observe_interwar_result(actor, report(1), team_id=1)
    assert not coordinator.observe_interwar_result(merc, squad_report(1), team_id=1)
    duplicate = report(1) + proto_bytes(9, first_field(decode_proto(report(1)), 9).value)
    assert not coordinator.observe_interwar_result(actor, duplicate, team_id=1)
    assert coordinator.observe_interwar_result(merc, squad_report(1), team_id=2)
    assert coordinator.observe_interwar_result(actor, squad_report(1), team_id=1)
    participants = {
        participant.player_id: participant
        for participant in coordinator.matches[1]["reward_participants"]
    }
    assert participants[42].role == 0
    assert participants[42].social_team_id == participants[42].settlement_team_id == 1
    assert participants[43].role == 2 and participants[43].pay_policy is None
    assert participants[43].social_team_id == 2 and participants[43].settlement_team_id == 1
    register(setup, squad_request())
    assert coordinator.preparations["Owner"]["prepare_id"] == 2 and len(pushes) == 2
    assert store.state == before


def test_mercenary_disconnect_removes_its_owned_boss_context(hired):
    setup, merc, _ = hired
    coordinator = setup[0]
    register(setup, squad_request())
    coordinator._remove_matchmaking_member(merc.online_id, reason="disconnect")
    assert (
        not coordinator.matches and not coordinator.preparations and not coordinator.hires.accepted
    )


@pytest.mark.parametrize("fault", ["no_offer", "declined", "expired", "ambiguous", "replacement"])
def test_acceptance_bit_is_not_sufficient_authority(hired, fault):
    from dataclasses import replace

    setup, merc, sessions = hired
    coordinator, store, actor, _, _, _, _ = setup
    hires = coordinator.hires
    hires.discard(merc.online_id)
    if fault != "no_offer":
        hires.offer(actor, 43)
    if fault == "expired":
        key = (actor.online_id, merc.online_id)
        hires.offers[key] = replace(hires.offers[key], expires=0)
    elif fault == "ambiguous":
        store.create(
            online_id="OtherEmployer",
            player_id=44,
            name="Third",
            operation_base=0,
            language=1,
            play_type=1,
            enlistment_method=1,
        )
        other = GameSession(
            b"third001", bytes(16), "OtherEmployer", 44, established=True, peer=("127.0.0.1", 10004)
        )
        sessions.append(other)
        hires.offer(other, 43)
    elif fault == "replacement":
        sessions.remove(actor)
    assert not hires.unregister(merc, fault != "declined")
    assert hires.resolve(actor, 43) is None


def add_teammates(setup, count, sessions=None):
    """Create real temporary-store membership and matching authenticated sessions."""
    coordinator, store, actor, _, _, _, _ = setup
    sessions = [actor] if sessions is None else sessions
    store.team_for(actor.online_id)["recruitment_open"] = True
    for index in range(count):
        player_id = 50 + index
        peer = GameSession(
            player_id.to_bytes(8, "big"),
            bytes(16),
            f"Member{index}",
            player_id,
            established=True,
            peer=("127.0.0.1", 10100 + index),
        )
        assert store.join_team(online_id=peer.online_id, player_id=player_id, team_id=1)
        sessions.append(peer)
    coordinator.hires.get_sessions = lambda: sessions
    return sessions


@pytest.mark.parametrize("clients", [1, 2, 3, 4, 5])
def test_boss_supports_five_authenticated_teammates_without_extra_leader_pushes(setup, clients):
    coordinator, store, actor, _, pushes, replies, _ = setup
    sessions = add_teammates(setup, clients - 1)
    before = deepcopy(store.state)
    payload = request(players=tuple(peer.player_id for peer in sessions))
    register(setup, payload)
    assert len(pushes) == len(replies) == 1
    assert coordinator.matches[1]["squad"] == {peer.player_id: (peer, 1) for peer in sessions}
    assert pushes[0][0][0] is actor
    register(setup, payload)
    assert len(pushes) == 1 and len(replies) == 2
    assert store.state == before


def test_boss_rejects_six_clients_without_allocating_state(setup):
    peers = add_teammates(setup, 5)
    register(setup, request(players=tuple(peer.player_id for peer in peers)))
    assert not setup[0].matches and not setup[0].preparations
    assert not setup[4] and not setup[5]
    assert "five" in setup[6].rows[-1][1]["reason"]


@pytest.mark.parametrize("teammate_count", [1, 2, 3])
def test_boss_accepts_mixed_teammates_and_accepted_mercenary_up_to_five(hired, teammate_count):
    setup, merc, sessions = hired
    add_teammates(setup, teammate_count, sessions)
    teammates = [peer.player_id for peer in sessions if peer is not merc]
    payload = request(players=teammates) + proto_bytes(
        6, proto_varint(1, merc.player_id) + proto_varint(2, 2)
    )
    before = deepcopy(setup[1].state)
    register(setup, payload)
    squad = setup[0].matches[1]["squad"]
    assert len(squad) == teammate_count + 2 and squad[43] == (merc, 2)
    assert all(squad[player_id][1] == 1 for player_id in teammates)
    assert setup[1].state == before


@pytest.mark.parametrize(
    "fault", ["foreign_team", "stale", "offline", "missing", "ambiguous", "wrong_role", "duplicate"]
)
def test_multi_client_boss_rejects_untrusted_rosters(setup, fault):
    peers = add_teammates(setup, 2)
    peer = peers[-1]
    payload = request(players=tuple(s.player_id for s in peers))
    if fault == "foreign_team":
        setup[1].leave_team(peer.online_id)
    elif fault == "stale":
        peer.last_seen_monotonic -= 91
    elif fault == "offline":
        peer.established = False
    elif fault == "missing":
        peers.pop()
    elif fault == "ambiguous":
        peers.append(
            GameSession(
                b"ambig001",
                bytes(16),
                "Ambiguous",
                peer.player_id,
                established=True,
                peer=("127.0.0.1", 11000),
            )
        )
    elif fault == "wrong_role":
        payload = request(players=(42,)) + proto_bytes(
            6, proto_varint(1, peer.player_id) + proto_varint(2, 2)
        )
    else:
        payload = request(players=(42, peer.player_id, peer.player_id))
    register(setup, payload)
    assert not setup[0].matches and not setup[4] and not setup[5]


def test_five_client_result_scope_and_teammate_cleanup(setup):
    coordinator, store, actor, _, _, _, _ = setup
    peers = add_teammates(setup, 4)
    register(setup, request(players=tuple(peer.player_id for peer in peers)))
    payload = report(1)
    for peer in peers[1:]:
        payload += proto_bytes(
            9, first_field(decode_proto(report(1, player=peer.player_id)), 9).value
        )
    before = deepcopy(store.state)
    assert coordinator.observe_interwar_result(peers[-1], payload, team_id=1)
    assert store.state == before  # Observing a result does not invent reward grants.
    coordinator._remove_matchmaking_member(peers[2].online_id, reason="disconnect")
    assert not coordinator.matches and not coordinator.preparations


@pytest.mark.parametrize("count", [2, 3, 5])
def test_zero_cost_operator_record_does_not_drop_a_registered_teammate(setup, count):
    coordinator, store, _, _, _, _, _ = setup
    peers = add_teammates(setup, count - 1)
    register(setup, request(players=tuple(peer.player_id for peer in peers)))
    raw = bytes.fromhex((Path(__file__).parent / "fixtures/operator-combat-result.hex").read_text())
    operator = decode_combat_report(raw).units[1]
    result = report(1)
    for peer in peers[1:-1]:
        result += proto_bytes(
            9, first_field(decode_proto(report(1, player=peer.player_id)), 9).value
        )
    # Only the authenticated player ID changes in the captured Operator form.
    # The five-person case is modeled, not a native five-client qualification.
    result += proto_bytes(
        9,
        b"".join(
            proto_varint(i, peer_value)
            for i, peer_value in enumerate((peers[-1].player_id, *operator[1:]), 1)
        ),
    )
    before = deepcopy(store.state)
    assert coordinator.observe_interwar_result(peers[-1], result, team_id=1)
    assert coordinator.observe_interwar_result(peers[0], result, team_id=1)
    assert store.state == before
    assert len(coordinator.matches[1]["squad"]) == count


def test_same_teammate_cannot_be_used_by_two_active_boss_squads(setup):
    peers = add_teammates(setup, 2)
    register(setup, request(players=tuple(peer.player_id for peer in peers)))
    register(setup, request(players=(peers[1].player_id,)), actor=peers[1])
    assert len(setup[0].matches) == 1 and len(setup[4]) == 1
    assert peers[1].online_id not in setup[0].preparations


def test_stale_nonleader_invalidates_the_frozen_boss_context(setup):
    peers = add_teammates(setup, 2)
    register(setup, request(players=tuple(peer.player_id for peer in peers)))
    peers[-1].last_seen_monotonic -= 91
    setup[0]._purge_stale_matchmaking()
    assert not setup[0].matches and not setup[0].preparations
