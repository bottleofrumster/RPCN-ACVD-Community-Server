"""The client threshold and authoritative room-admission gate must agree."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from acvd_server.handlers.matching import register_preparation
from acvd_server.matchmaking import MatchmakingCoordinator, SpecialSortieChargeRequired
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.team_messages import _team_info
from acvd_server.team_progression import SPECIAL_SORTIE_SEGMENTS
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession
from acvd_server.world_messages import _world_basic_info


def setup(tmp_path, charge):
    store = TeamStore(tmp_path / "teams")
    team = store.create(
        online_id="Leader",
        player_id=11,
        name="Test",
        operation_base=0,
        language=2,
        play_type=1,
        enlistment_method=2,
    )
    state = deepcopy(store.state)
    state["teams"][str(team["team_id"])].update(
        special_sortie_progress=charge,
        current_faction_id=1,
        faction_id=1,
        territory_area_id=3,
    )
    store.commit_state(state)
    actor = GameSession(
        b"charge01", bytes(16), "Leader", 11, established=True, peer=("127.0.0.1", 11001)
    )
    replies, errors, pushes = [], [], []
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        send_push=lambda *a, **k: pushes.append((a, k)),
        telemetry=SimpleNamespace(emit=lambda *a, **k: None),
        room_log_paths=[],
        get_world_phase=lambda: 2,
        special_sortie_ready=store.special_sortie_ready,
        consume_special_sortie=store.consume_special_sortie,
    )
    coordinator.hires.get_sessions = lambda: [actor]
    coordinator.hires.get_team_id = lambda name: team["team_id"] if name == "Leader" else 0
    payload = (
        proto_varint(1, 3)
        + proto_varint(2, 2)
        + proto_varint(3, 2)
        + proto_bytes(6, proto_varint(1, 11) + proto_varint(2, 0))
    )
    message = GameMessage(0x3BD, b"\0\0\0\x01", payload)

    def submit():
        register_preparation(
            actor,
            message,
            matchmaking=coordinator,
            team_store=store,
            telemetry=coordinator.telemetry,
            reply=replies.append,
            reply_error=lambda *e: errors.append(e),
        )

    return store, team["team_id"], actor, coordinator, replies, errors, pushes, submit


@pytest.mark.parametrize("charge", range(6))
def test_undercharged_client_advertisement_and_stale_request_refusal(tmp_path, charge):
    store, team_id, actor, coordinator, replies, errors, pushes, submit = setup(tmp_path, charge)
    now = datetime.now(timezone.utc)
    requirement = first_field(decode_proto(_world_basic_info(now, phase=2)), 3).value
    displayed = first_field(decode_proto(_team_info(store.team_by_id(team_id), now)), 13).value
    assert displayed == charge < requirement == SPECIAL_SORTIE_SEGMENTS
    before = deepcopy(store.state)
    submit()
    assert errors == [(1, 28)] and not replies and not pushes
    assert not coordinator.matches and not coordinator.preparations
    assert store.state == before


@pytest.mark.parametrize("charge", [6, 7, 8])
def test_full_gauge_releases_once_and_exact_retry_does_not_charge_again(tmp_path, charge):
    store, team_id, actor, coordinator, replies, errors, pushes, submit = setup(tmp_path, charge)
    before = store.state_path.read_bytes()
    assert view_charge(store, team_id) == 6
    assert store.state_path.read_bytes() == before
    submit()
    assert len(replies) == 1 and not errors and len(pushes) == 1
    assert len(coordinator.matches) == 1
    match = next(iter(coordinator.matches.values()))
    assert match["battle_type"] == 6
    assert match["special_progress_consumed"]["previous_special_sortie_progress"] == 6
    assert store.team_by_id(team_id)["special_sortie_progress"] == 0
    before = deepcopy(store.state)
    submit()
    assert len(replies) == 2 and not errors
    assert len(coordinator.matches) == 1 and store.state == before


@pytest.mark.parametrize("change", ["team", "roster", "roles", "criteria"])
def test_reserved_charge_exception_cannot_authorize_a_different_request(tmp_path, change):
    store, team_id, actor, coordinator, replies, errors, pushes, submit = setup(tmp_path, 8)
    submit()
    criteria, ids, roles = {1: 3, 2: 2, 3: 2}, [11], [0]
    if change == "team":
        team_id += 1
    if change == "roster":
        ids = [11, 22]
    if change == "roles":
        roles = [1]
    if change == "criteria":
        criteria[8] = 7
    before = deepcopy(store.state)
    with pytest.raises(SpecialSortieChargeRequired):
        coordinator.validate_registration(actor, criteria, ids, team_id, 2, roles)
    assert store.state == before and len(coordinator.matches) == 1


def view_charge(store, team_id):
    return first_field(decode_proto(_team_info(store.team_by_id(team_id), datetime.now(timezone.utc))), 13).value


def test_postwar_requirement_is_separate_and_free():
    now = datetime.now(timezone.utc)
    for phase, expected in ((0, 0), (2, 6), (0, 0), (2, 6)):
        assert first_field(decode_proto(_world_basic_info(now, phase=phase)), 3).value == expected
