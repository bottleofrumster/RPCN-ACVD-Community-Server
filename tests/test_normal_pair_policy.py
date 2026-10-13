"""Normal role preferences, queue order, and frozen pair roles; no live RPCN."""

import time

import pytest

from acvd_server.match_policy import normal_pair_roles
from acvd_server.matchmaking import MatchmakingCoordinator
from acvd_server.protocol import decode_proto
from acvd_server.udp import GameSession


class Events:
    def emit(self, *args, **kwargs):
        pass


def member(name, number, preference):
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
        "territory_area_id": 2 if number == 11 else 3,
        "territory_layout_owner_id": number,
        "criteria": {1: 3, 2: 2, 3: 1, 8: preference},
        "match_id": 0,
        "registered_monotonic": time.monotonic(),
    }


@pytest.mark.parametrize("factions", [(a, b) for a in (1, 2, 3) for b in (1, 2, 3)])
@pytest.mark.parametrize("targets", [(a, b) for a in range(4) for b in range(4)])
@pytest.mark.parametrize("reverse_roles", [False, True])
def test_normal_pair_honors_both_opponent_faction_preferences(factions, targets, reverse_roles):
    first, second = member("First", 11, 2 if reverse_roles else 1), member(
        "Second", 22, 1 if reverse_roles else 2
    )
    first["faction_id"], second["faction_id"] = factions
    first["criteria"][9], second["criteria"][9] = targets
    compatible = targets[0] in (0, factions[1]) and targets[1] in (0, factions[0])
    expected = ((second, first) if reverse_roles else (first, second)) if compatible else None
    assert normal_pair_roles(first, second) == expected


@pytest.mark.parametrize("target", [None, -1, 4, True, False, "2"])
def test_invalid_opponent_faction_never_means_no_preference(setup, target):
    coordinator, sent = setup
    first, second = member("First", 11, 1), member("Second", 22, 2)
    first["criteria"][9] = target
    coordinator.preparations = {"First": first, "Second": second}
    coordinator._match_preparation(second)
    assert coordinator.matches == {} and sent == []
    assert coordinator.next_match_id == 1


def test_queue_skips_wrong_faction_and_keeps_its_search(setup):
    coordinator, sent = setup
    wrong = member("Wrong", 11, 2)
    eligible = member("Venide", 22, 2)
    attacker = member("Attacker", 33, 1)
    attacker["faction_id"] = 3
    attacker["criteria"][9] = 2
    eligible["criteria"][9] = 3
    coordinator.preparations = {m["session"].online_id: m for m in (wrong, eligible, attacker)}
    coordinator._match_preparation(attacker)
    assert coordinator.matches[1]["members"] == [attacker, eligible]
    assert wrong["match_id"] == 0
    assert [s.online_id for s, _, _ in sent] == ["Attacker"]


@pytest.fixture
def setup():
    sent = []
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        telemetry=Events(),
        room_log_paths=[],
        send_push=lambda target, payload, subtype=None: sent.append((target, payload, subtype)),
    )
    return coordinator, sent


@pytest.mark.parametrize(
    "first_pref,second_pref,host_index",
    [
        (1, 1, None),
        (1, 2, 0),
        (1, 3, 0),
        (2, 1, 1),
        (2, 2, None),
        (2, 3, 1),
        (3, 1, 1),
        (3, 2, 0),
        (3, 3, 0),
    ],
)
def test_all_normal_role_preferences(setup, first_pref, second_pref, host_index):
    coordinator, sent = setup
    first, second = member("First", 11, first_pref), member("Second", 22, second_pref)
    coordinator.preparations = {"First": first, "Second": second}
    coordinator._get_team_sessions = lambda: {
        item["team_id"]: {
            "session_id": item["team_id"],
            "members": {item["session"].online_id: {
                "player_id": item["session"].player_id, "member_id": 1,
                "np_identity": b"\x01" + item["session"].online_id.encode().ljust(36, b"\0"),
            }},
        } for item in (first, second)
    }
    coordinator.signaling_ready_pairs.add(frozenset({"First", "Second"}))
    coordinator._match_preparation(second)
    if host_index is None:
        assert coordinator.matches == {}
        assert first["match_id"] == second["match_id"] == 0
        assert sent == []
        return
    host, defender = (first, second) if host_index == 0 else (second, first)
    match = coordinator.matches[1]
    assert match["members"] == [host, defender]
    assert match["combat_area_id"] == defender["territory_area_id"]
    assert match["territory_layout_owner_id"] == defender["territory_layout_owner_id"]
    assert [target for target, _, _ in sent] == [host["session"]]
    values = [{f.number: f.value for f in decode_proto(payload)} for _, payload, _ in sent]
    assert [v[3] for v in values] == [1]
    assert [v[5] for v in values] == [1]
    assert [v[4] for v in values] == [1]
    assert [v[6] for v in values] == [defender["territory_area_id"]]
    assert [v[7] for v in values] == [defender["territory_layout_owner_id"]]


def test_queue_skips_incompatible_defender_without_reassigning_it(setup):
    coordinator, _ = setup
    waiting_defense = member("Defense", 11, 2)
    waiting_attack = member("Attack", 22, 1)
    new_defense = member("New", 33, 2)
    coordinator.preparations = {
        "Defense": waiting_defense,
        "Attack": waiting_attack,
        "New": new_defense,
    }
    coordinator._match_preparation(new_defense)
    assert coordinator.matches[1]["members"] == [waiting_attack, new_defense]
    assert waiting_defense["match_id"] == 0


def test_locked_cq_pool_freezes_a_new_paired_normal_without_changing_territories(setup):
    coordinator, sent = setup
    settings = {
        "cq_map_mode": "locked_pool",
        "cq_map_pool": [3101],
    }
    coordinator.get_cq_map_settings = lambda: settings
    attacker = member("Attack", 11, 1)
    defender = member("Defense", 22, 2)
    coordinator.preparations = {"Attack": attacker, "Defense": defender}

    coordinator._match_preparation(defender)

    match = coordinator.matches[1]
    assert attacker["territory_area_id"] == 2
    assert defender["territory_area_id"] == 3
    assert match["source_combat_area_id"] == 3
    assert match["combat_area_id"] == 7
    assert match["cq_map_mode"] == "locked_pool" and match["cq_map_locked"] is True
    assert match["cq_map_mission_id"] == 3101
    assert match["cq_map_name"] == "Route R1024"
    fields = {field.number: field.value for field in decode_proto(sent[0][1])}
    assert fields[6] == 7

    settings["cq_map_pool"] = [3602]
    next_attacker = member("NextAttack", 33, 1)
    next_defender = member("NextDefense", 44, 2)
    coordinator.preparations.update(
        {"NextAttack": next_attacker, "NextDefense": next_defender}
    )
    coordinator._match_preparation(next_defender)

    assert coordinator.matches[1]["combat_area_id"] == 7
    assert coordinator.matches[2]["combat_area_id"] == 2
    assert coordinator.matches[2]["cq_map_mission_id"] == 3602


@pytest.mark.parametrize(
    "case",
    [
        "same_team",
        "no_team",
        "same_session",
        "disconnected",
        "no_peer",
        "other_season",
        "other_phase",
        "special_attack",
        "two_specials",
        "bad_preference",
        "bool_preference",
    ],
)
def test_unqualified_pairs_stay_queued(setup, case):
    coordinator, sent = setup
    first, second = member("First", 11, 1), member("Second", 22, 2)
    if case == "same_team":
        second["team_id"] = first["team_id"]
    elif case == "no_team":
        second["team_id"] = 0
    elif case == "same_session":
        second["session"] = first["session"]
    elif case == "disconnected":
        first["session"].established = False
    elif case == "no_peer":
        first["session"].peer = None
    elif case == "other_season":
        second["criteria"][1] = 4
    elif case == "other_phase":
        second["criteria"][2] = 0
    elif case == "special_attack":
        first["criteria"][3] = 2
    elif case == "two_specials":
        first["criteria"][3] = second["criteria"][3] = 2
    elif case == "bad_preference":
        second["criteria"][8] = 99
    elif case == "bool_preference":
        second["criteria"][8] = True
    coordinator.preparations = {"First": first, "Second": second}
    coordinator._match_preparation(second)
    assert coordinator.matches == {}
    assert sent == []


def test_one_pass_keeps_reversed_roles_without_a_preflight_barrier(setup, monkeypatch):
    coordinator, sent = setup
    defender, attacker = member("Defense", 11, 2), member("Attack", 22, 1)
    coordinator.preparations = {"Defense": defender, "Attack": attacker}
    monkeypatch.setattr(
        coordinator,
        "_announce_cross_session_match_peers",
        lambda _: pytest.fail("Normal matching must not announce team joins"),
    )
    coordinator._match_preparation(attacker)
    match = coordinator.matches[1]
    assert match["members"] == [attacker, defender]
    assert not match.get("preflight_retry_pending")
    assert [(target.online_id, subtype) for target, _, subtype in sent] == [("Attack", 2)]
    for old in (defender, attacker):
        retry = dict(old)
        coordinator.preparations[old["session"].online_id] = retry
        coordinator._match_preparation(retry)
    assert len(sent) == 1
    assert [m["session"].online_id for m in match["members"]] == ["Attack", "Defense"]
    assert [m["prepare_id"] for m in match["members"]] == [22, 11]
    assert match["combat_area_id"] == 2


def test_cancelled_unpublished_host_retry_requeues_both_live_searches(setup):
    coordinator, sent = setup
    first, second = member("First", 11, 3), member("Second", 22, 3)
    coordinator.preparations = {"First": first, "Second": second}
    coordinator._match_preparation(second)
    failed = coordinator.matches[1]
    assert failed["members"] == [first, second]
    assert failed["host_result_sent"] and not failed.get("room_published")
    failed["host_transition_cancelled"] = True

    retry = dict(first)
    coordinator.preparations["First"] = retry
    coordinator._match_preparation(retry)

    assert 1 not in coordinator.matches
    replacement = coordinator.matches[2]
    assert replacement["members"] == [second, retry]
    assert [target.online_id for target, _payload, _subtype in sent] == [
        "First",
        "Second",
    ]
    assert retry["match_id"] == second["match_id"] == 2


def test_host_retry_after_room_publication_never_requeues_the_match(setup):
    coordinator, sent = setup
    first, second = member("First", 11, 3), member("Second", 22, 3)
    coordinator.preparations = {"First": first, "Second": second}
    coordinator._match_preparation(second)
    match = coordinator.matches[1]
    match["host_transition_cancelled"] = True
    match["room_published"] = True

    retry = dict(first)
    coordinator.preparations["First"] = retry
    coordinator._match_preparation(retry)

    assert coordinator.matches == {1: match}
    assert len(sent) == 1


@pytest.mark.parametrize("first_faction", [1, 2, 3])
@pytest.mark.parametrize("second_faction", [1, 2, 3])
def test_faction_classification_is_shared_and_frozen(setup, first_faction, second_faction):
    coordinator, sent = setup
    first, second = member("First", 11, 1), member("Second", 22, 2)
    first["faction_id"], second["faction_id"] = first_faction, second_faction
    coordinator.preparations = {"First": first, "Second": second}
    coordinator.signaling_ready_pairs.add(frozenset({"First", "Second"}))
    coordinator._match_preparation(second)
    match = coordinator.matches[1]
    expected = first_faction == second_faction
    assert match["is_intra_faction"] is expected
    for _, payload, subtype in sent:
        assert subtype == 2
        fields = {f.number: f.value for f in decode_proto(payload)}
        assert fields[9] == int(expected) and fields[8] == 0
    sent.clear()
    second["faction_id"] = 99
    coordinator._publish_match_room(
        first["session"], room_id=77, room_password=b"PRVTPSWD", source_values={}
    )
    fields = {f.number: f.value for f in decode_proto(sent[0][1])}
    assert fields[9] == int(expected)


@pytest.mark.parametrize("faction", [None, 0, 4, -1, True, "1"])
def test_unknown_faction_does_not_invent_a_classification(setup, faction):
    coordinator, sent = setup
    first, second = member("First", 11, 1), member("Second", 22, 2)
    second["faction_id"] = faction
    coordinator.preparations = {"First": first, "Second": second}
    coordinator._match_preparation(second)
    assert coordinator.matches == {} and sent == []


def test_retry_cannot_change_the_frozen_faction(setup):
    coordinator, sent = setup
    first, second = member("First", 11, 1), member("Second", 22, 2)
    coordinator.preparations = {"First": first, "Second": second}
    coordinator._match_preparation(second)
    original = list(coordinator.matches[1]["members"])
    sent.clear()
    retry = {**second, "faction_id": 1}
    coordinator.preparations["Second"] = retry
    coordinator._match_preparation(retry)
    assert coordinator.matches[1]["members"] == original and sent == []
