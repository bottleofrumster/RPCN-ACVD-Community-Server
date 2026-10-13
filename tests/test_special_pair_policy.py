"""Active Special Sorties stay out of Normal pair matchmaking."""

import time

import pytest

from acvd_server.match_messages import _matching_result_for_members
from acvd_server.match_policy import normal_pair_roles
from acvd_server.matchmaking import MatchmakingCoordinator
from acvd_server.protocol import decode_proto
from acvd_server.udp import GameSession


def member(name, number, sortie, preference):
    return {
        "session": GameSession(
            name.encode().ljust(8, b"0"),
            bytes(16),
            name,
            number,
            established=True,
            peer=("127.0.0.1", 10000 + number),
        ),
        "prepare_id": number,
        "team_id": number,
        "faction_id": 1 if number == 11 else 2,
        "territory_area_id": 3,
        "territory_layout_owner_id": number,
        "world_phase": 2,
        "criteria": {
            1: 3,
            2: 2,
            3: sortie,
            4: 1,
            5: 0,
            8: preference,
            9: 0,
            10: 1,
            11: 1,
            12: 1,
            13: 1,
            14: 1,
        },
        "match_id": 0,
        "registered_monotonic": time.monotonic(),
    }


@pytest.mark.parametrize("attacker_first", [True, False])
@pytest.mark.parametrize("stale_preference", [1, 2, 3])
@pytest.mark.parametrize("defense_preference", [2, 3])
def test_special_never_enters_normal_pair_matching(
    attacker_first,
    stale_preference,
    defense_preference,
):
    defender = member("Defender", 11, 1, defense_preference)
    attacker = member("Attacker", 22, 2, stale_preference)
    order = (attacker, defender) if attacker_first else (defender, attacker)
    assert normal_pair_roles(*order) is None


@pytest.mark.parametrize(
    "case",
    [
        "two_specials",
        "defender_offense_only",
        "same_faction",
        "unknown_faction",
        "missing_layout",
        "wrong_owner",
        "unknown_area",
        "bad_area",
        "grace",
        "other_season",
        "unmapped_special_condition",
        "unmapped_defender_condition",
    ],
)
def test_unqualified_special_pair_stays_unassigned(case):
    defender = member("Defender", 11, 1, 2)
    attacker = member("Attacker", 22, 2, 1)
    if case == "two_specials":
        defender["criteria"][3] = 2
    elif case == "defender_offense_only":
        defender["criteria"][8] = 1
    elif case == "same_faction":
        attacker["faction_id"] = 1
    elif case == "unknown_faction":
        attacker["faction_id"] = 0
    elif case == "missing_layout":
        defender["territory_layout_owner_id"] = 0
    elif case == "wrong_owner":
        defender["territory_layout_owner_id"] = 999
    elif case == "unknown_area":
        defender["territory_area_id"] = 0
    elif case == "bad_area":
        defender["territory_area_id"] = True
    elif case == "grace":
        attacker["criteria"][2] = 0
    elif case == "other_season":
        attacker["criteria"][1] = 4
    elif case == "unmapped_special_condition":
        attacker["criteria"][9] = 1
    elif case == "unmapped_defender_condition":
        defender["criteria"][10] = 0
    assert normal_pair_roles(defender, attacker) is None


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


def paired():
    sent = []
    events = Events()
    team_sessions = {}
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: team_sessions,
        telemetry=events,
        room_log_paths=[],
        send_push=lambda actor, data, subtype=None: sent.append((actor, data, subtype)),
    )
    defender = member("Defender", 11, 1, 2)
    attacker = member("Attacker", 22, 2, 2)
    for item in (defender, attacker):
        actor = item["session"]
        team_sessions[item["team_id"]] = {
            "session_id": item["team_id"],
            "team_id": item["team_id"],
            "members": {actor.online_id: {
                "player_id": actor.player_id,
                "member_id": 1,
                "np_identity": bytes([item["team_id"]]) * 37,
            }},
        }
    coordinator.hires.get_sessions = lambda: [defender["session"], attacker["session"]]
    coordinator.hires.get_team_id = lambda name: {"Defender": 11, "Attacker": 22}[name]
    coordinator.preparations = {"Defender": defender, "Attacker": attacker}
    coordinator.signaling_ready_pairs.add(frozenset(coordinator.preparations))
    coordinator._match_preparation(attacker)
    return coordinator, attacker, defender, sent, events


def test_special_releases_one_team_world_boss_without_a_defending_team():
    coordinator, attacker, defender, sent, _ = paired()
    match = coordinator.matches[1]
    assert match["members"] == [attacker]
    assert match["battle_type"] == 6
    assert match["territory_layout_owner_id"] == 0
    assert [(actor.online_id, subtype) for actor, _, subtype in sent] == [("Attacker", 2)]
    result = {f.number: f.value for f in decode_proto(sent[0][1])}
    assert (result[4], result[7]) == (6, 0)
    assert defender["match_id"] == 0


def test_special_release_is_immediate_and_does_not_publish_a_native_pair_room():
    coordinator, attacker, defender, sent, _ = paired()
    match = coordinator.matches[1]
    assert [(actor.online_id, subtype) for actor, _, subtype in sent] == [("Attacker", 2)]
    with pytest.raises(ValueError, match="no matched recipient"):
        coordinator.publish_native_room(
            attacker["session"],
            prepare_id=22,
            encoded=b"\0NXRV\x03\xf2" + bytes(506),
            reply=lambda _payload: None,
        )
    assert not match.get("room_published") and defender["match_id"] == 0


def test_retry_cannot_change_the_frozen_special_world_boss_contract():
    coordinator, attacker, _defender, sent, events = paired()
    original = list(coordinator.matches[1]["members"])
    sent.clear()
    retry = {**attacker, "criteria": {**attacker["criteria"], 3: 1}}
    coordinator.preparations[attacker["session"].online_id] = retry
    coordinator._match_preparation(retry)
    assert coordinator.matches[1]["members"] == original and sent == []
    assert events.rows[-1][0] == "game_match_retry_rejected"


@pytest.mark.parametrize("kind", [0, 4, 5, 6, True, "3", None])
def test_paired_encoder_rejects_solo_or_invalid_battle_types(kind):
    with pytest.raises(ValueError, match="paired battle type"):
        _matching_result_for_members(
            {"prepare_id": 1}, {"prepare_id": 2}, criteria={}, is_host=True, battle_type=kind
        )
