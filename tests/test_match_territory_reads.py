"""Matched opponents can read only the selected Stronghold's native layout."""

import time
from copy import copy, deepcopy

import pytest

from acvd_server.handlers import territory
from acvd_server.matchmaking import MatchmakingCoordinator
from acvd_server.protocol import decode_proto, proto_varint
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **values):
        self.rows.append((event, values))


@pytest.fixture
def setup(tmp_path):
    store = TeamStore(tmp_path / "teams")
    events = Events()
    transports = []
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        send_push=lambda *args, **kwargs: None,
        telemetry=events,
        room_log_paths=[],
        get_sessions=lambda: transports,
        get_team_id=lambda name: int((store.team_for(name) or {}).get("team_id", 0)),
    )
    members = []
    for name, number in (("Attacker", 11), ("Defender", 22)):
        store.create(
            online_id=name,
            player_id=number,
            name=name,
            operation_base=0,
            language=1,
            play_type=1,
            enlistment_method=2,
        )
        actor = GameSession(
            name.encode().ljust(8, b"0"),
            bytes(16),
            name,
            number,
            established=True,
            peer=("127.0.0.1", 10000 + number),
        )
        member = {
            "session": actor,
            "team_id": store.team_for(name)["team_id"],
            "prepare_id": number,
            "match_id": 1,
        }
        members.append(member)
        transports.append(actor)
        coordinator.preparations[name] = member
    match = {
        "match_id": 1,
        "members": members,
        "combat_area_id": 3,
        "territory_layout_owner_id": 22,
        "created_monotonic": time.monotonic(),
        "squad": {m["session"].player_id: (m["session"], m["team_id"]) for m in members},
    }
    coordinator.matches[1] = match
    payload = b"\0\7" + bytes(1055)
    store.save_native_territory_layout(
        "Defender", player_id=22, season_id=3, area_id=3, layout_version=7, payload=payload
    )
    return store, coordinator, members, match, events, payload


def read(setup, *, actor=None, player_id=22, area_id=3, season_id=3, matched=True, phase=2):
    store, coordinator, members, _, events, _ = setup
    before = deepcopy(store.state)
    files = {str(p): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    replies = []
    territory.get_layout(
        actor or members[0]["session"],
        GameMessage(
            0x3BB,
            bytes(4),
            proto_varint(1, season_id) + proto_varint(2, player_id) + proto_varint(3, area_id),
        ),
        team_store=store,
        telemetry=events,
        reply=replies.append,
        matchmaking=coordinator if matched else None,
        phase=phase,
    )
    assert store.state == before
    assert {str(p): p.read_bytes() for p in store.root.rglob("*") if p.is_file()} == files
    return {f.number: f.value for f in decode_proto(replies[0])} if replies else {}


def test_live_matched_attacker_reads_exact_defender_layout_without_writes(setup):
    fields = read(setup)
    assert fields == {2: 7, 3: setup[-1], 4: 0}
    assert setup[4].rows[-1][0] == "game_native_territory_layout_sent"
    assert setup[4].rows[-1][1]["match_scoped"] is True


def test_unmatched_cross_team_read_remains_denied(setup):
    assert read(setup, matched=False)[4] == 1


@pytest.mark.parametrize(
    "case",
    [
        "no_match",
        "wrong_owner",
        "wrong_area",
        "wrong_season",
        "stale_session",
        "expired",
        "preflight",
        "unreleased",
        "disconnected",
        "owner_disconnected",
        "owner_replaced",
        "requester_changed_team",
        "owner_changed_team",
    ],
)
def test_match_read_cannot_escape_its_current_scope(setup, case):
    store, coordinator, members, match, _, _ = setup
    arguments = {}
    if case == "no_match":
        coordinator.matches.clear()
    elif case == "wrong_owner":
        arguments["player_id"] = 33
    elif case == "wrong_area":
        arguments["area_id"] = 2
    elif case == "wrong_season":
        arguments["season_id"] = 2
    elif case == "stale_session":
        arguments["actor"] = copy(members[0]["session"])
    elif case == "expired":
        match["created_monotonic"] -= 181
    elif case == "preflight":
        match["preflight_retry_pending"] = {"Defender"}
    elif case == "unreleased":
        match.pop("combat_area_id")
    elif case == "disconnected":
        members[0]["session"].established = False
    elif case == "owner_disconnected":
        members[1]["session"].peer = None
    elif case == "owner_replaced":
        coordinator.preparations["Defender"] = dict(members[1])
    elif case == "requester_changed_team":
        store.team_for("Attacker")["team_id"] = 99
    elif case == "owner_changed_team":
        store.team_for("Defender")["team_id"] = 99
    assert read(setup, **arguments)[4] == 1


def test_outside_authenticated_player_cannot_read_another_match(setup):
    store = setup[0]
    store.create(
        online_id="Outside",
        player_id=33,
        name="Outside",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=2,
    )
    actor = GameSession(
        b"outside1", bytes(16), "Outside", 33, established=True, peer=("127.0.0.1", 33)
    )
    assert read(setup, actor=actor)[4] == 1


def add_crew(setup, side, number, *, matched=True):
    store, coordinator, members, match, _, _ = setup
    captain = members[side]
    name = f"Crew{number}"
    assert store.join_team(online_id=name, player_id=number, team_id=captain["team_id"])
    actor = GameSession(
        number.to_bytes(8, "big"),
        bytes(16),
        name,
        number,
        established=True,
        peer=("127.0.0.1", 10000 + number),
    )
    coordinator.hires.get_sessions().append(actor)
    if matched:
        match["squad"][number] = (actor, captain["team_id"])
    return actor


@pytest.mark.parametrize("sizes", [(2, 1), (3, 2), (5, 5)])
@pytest.mark.parametrize("phase", [2, 0])
def test_frozen_crew_can_read_the_same_defender_layout_as_captain(setup, sizes, phase):
    attackers = [setup[2][0]["session"]]
    for side, count in enumerate(sizes):
        for index in range(1, count):
            actor = add_crew(setup, side, 100 + side * 10 + index)
            if side == 0:
                attackers.append(actor)
    for actor in attackers:
        assert read(setup, actor=actor, phase=phase) == {2: 7, 3: setup[-1], 4: 0}
        assert setup[1].accepted_world_phase(actor) == 2


@pytest.mark.parametrize(
    "case",
    [
        "unclaimed",
        "replaced",
        "stale",
        "team_changed",
        "captain_replaced",
        "completed",
        "ambiguous",
        "preflight",
        "expired",
        "wrong_region",
        "wrong_owner",
        "wrong_season",
    ],
)
def test_crew_layout_permission_cannot_escape_the_frozen_match(setup, case):
    store, coordinator, members, match, _, _ = setup
    actor = add_crew(setup, 0, 101, matched=case != "unclaimed")
    arguments = {}
    if case == "replaced":
        actor = copy(actor)
    elif case == "stale":
        actor.last_seen_monotonic -= 91
    elif case == "team_changed":
        store.leave_team(actor.online_id)
    elif case == "captain_replaced":
        coordinator.preparations["Attacker"] = dict(members[0])
    elif case == "completed":
        match["result_sha256"] = "completed"
    elif case == "ambiguous":
        coordinator.matches[2] = {**match, "match_id": 2}
    elif case == "preflight":
        match["preflight_retry_pending"] = {"Defender"}
    elif case == "expired":
        match["created_monotonic"] -= 181
    elif case == "wrong_region":
        arguments["area_id"] = 2
    elif case == "wrong_owner":
        arguments["player_id"] = 33
    elif case == "wrong_season":
        arguments["season_id"] = 2
    assert read(setup, actor=actor, **arguments)[4] == 1
    if not case.startswith("wrong_"):
        assert coordinator.accepted_world_phase(actor) is None


def test_frozen_hired_participant_keeps_own_team_without_losing_layout_access(setup):
    store, coordinator, _, match, _, _ = setup
    team = store.create(
        online_id="Merc",
        player_id=333,
        name="MercTeam",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=2,
    )
    actor = GameSession(
        b"merc0001", bytes(16), "Merc", 333, established=True, peer=("127.0.0.1", 13333)
    )
    coordinator.hires.get_sessions().append(actor)
    # Admission/hire validation owns creation of this frozen roster. A read
    # must not reassign the accepted mercenary to the employer's durable team.
    match["squad"][333] = (actor, team["team_id"])
    assert read(setup, actor=actor, phase=0) == {2: 7, 3: setup[-1], 4: 0}
    assert store.team_for("Merc")["team_id"] == team["team_id"]
