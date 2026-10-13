"""A Story result owns its cash, item identity and exact retry reply together."""

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest

from acvd_server.owned_items import OwnershipError, confirm_inventory, pending_inventory
from acvd_server.protocol import decode_proto, proto_bytes, proto_varint
from acvd_server.rewards import story_reward_plan
from acvd_server.story_items import (
    ITEM_POLICY,
    LEGACY_ITEM_POLICY,
    item_specs,
    request_identity,
    resolve_pending,
    settle,
)
from acvd_server.teams import TeamStore
from acvd_server.udp import REGISTER_BATTLE_RESULT, GameMessage, GameSession

RAW = bytes.fromhex((Path(__file__).parent / "fixtures/story-subquest-result.hex").read_text())
COOP_RAW = bytes.fromhex(
    "10cc011a1308c6f1061000180120d4990128901730003800"
    "1a1208e9d80b1000180120fd0e28d04130003800"
    "220b08e80710cc011885072000220b08e80710cc011886072000"
    "220b08e80710cc01188707200030033802"
)


def record(instance=10001, part=210, category=0, part_class=0):
    return proto_bytes(
        1,
        b"".join(
            proto_varint(n, value)
            for n, value in enumerate((category, part, part_class, instance, 1000), 1)
        ),
    )


def emblem_record(row_id=1231, selector=0x20032):
    return proto_bytes(3, proto_varint(1, selector) + proto_varint(2, row_id))


def story_request(mission_id):
    participant = b"".join(
        proto_varint(number, value)
        for number, value in ((1, 50627), (2, 0), (3, 1), (4, 0), (5, 0), (6, 0), (7, 0))
    )
    return (
        proto_varint(2, mission_id)
        + proto_bytes(3, participant)
        + proto_varint(6, 3)
        + proto_varint(7, 0)
    )


def coop_story_request(mission_id=2002):
    def owner(player_id):
        return proto_bytes(
            3,
            b"".join(
                proto_varint(number, value)
                for number, value in (
                    (1, player_id),
                    (2, 0),
                    (3, 1),
                    (4, 0),
                    (5, 0),
                    (6, 0),
                    (7, 0),
                )
            ),
        )

    return (
        proto_varint(2, mission_id)
        + owner(112838)
        + owner(191593)
        + proto_varint(6, 3)
        + proto_varint(7, 2)
    )


@pytest.fixture
def state(tmp_path):
    teams = TeamStore(tmp_path / "teams")
    teams.remember_player("Tester", 50627)
    teams.synchronize_balance("Tester", 37357)
    actor = GameSession(b"test0001", bytes(16), "Tester", 50627)
    return teams, actor


def submit(state, raw=RAW, **kwargs):
    options = dict(inventory=record(), reserved_instance_ids=frozenset({10001}))
    options.update(kwargs)
    return settle(*state, story_reward_plan(raw, 50627), raw, **options)


def story_message(payload=RAW, index=1, counter=1):
    return GameMessage(
        REGISTER_BATTLE_RESULT,
        index.to_bytes(4, "big"),
        payload,
        counter,
    )


def test_item_cash_receipt_and_reply_survive_reload_and_cash_history_pruning(state):
    teams, actor = state
    before = deepcopy(teams.state)
    response, result, count = submit(state)
    assert count == 1 and result["applied"] and teams.balance("Tester") == 70209
    receipt = next(iter(teams.state["story_rewards"].values()))
    (item,) = receipt["items"]
    assert (item["category"], item["part_id"], item["part_class"], item["instance_id"]) == (
        3,
        2610,
        3,
        10002,
    )
    assert teams.state["teams"] == before["teams"] and teams.state["members"] == before["members"]
    (outer,) = decode_proto(response)
    quests = [decode_proto(f.value) for f in decode_proto(outer.value) if f.number == 8]
    item_quest = next(q for q in quests if next(f.value for f in q if f.number == 1) == 100)
    reward = decode_proto(next(f.value for f in item_quest if f.number == 6))
    assert next(f.value for f in reward if f.number == 1) == 2
    child = decode_proto(next(f.value for f in reward if f.number == 4))
    assert {f.number: f.value for f in child} == {1: 3, 2: 2610, 3: 3, 4: 10002, 5: 1000}
    loaded = TeamStore(teams.root)
    updated = deepcopy(loaded.state)
    updated["battle_settlements"] = []
    loaded.commit_state(updated)
    retry = submit((loaded, actor), inventory=None, reserved_instance_ids=frozenset())
    assert retry[0] == response and not retry[1]["applied"]
    assert loaded.balance("Tester") == 70209 and len(loaded.state["story_rewards"]) == 1


def test_request_identity_distinguishes_missions_but_not_transport_resends(state):
    _, actor = state
    first = request_identity(actor, story_message(index=10, counter=1))
    assert first == request_identity(actor, story_message(index=10, counter=999))
    assert first != request_identity(actor, story_message(index=11, counter=1))
    assert first != request_identity(
        GameSession(b"newauth1", bytes(16), actor.online_id, actor.player_id),
        story_message(index=10),
    )


def test_two_identical_legitimate_reports_have_distinct_cash_and_item_receipts(state):
    teams, actor = state
    first_digest = request_identity(actor, story_message(index=10))
    second_digest = request_identity(actor, story_message(index=12))
    first_response, first_settlement, _ = submit(state, request_digest=first_digest)
    second_response, second_settlement, _ = submit(state, request_digest=second_digest)
    assert first_settlement["applied"] and second_settlement["applied"]
    assert teams.balance(actor.online_id) == 103061
    receipts = list(teams.state["story_rewards"].values())
    assert len(receipts) == 2
    assert [receipt["items"][0]["instance_id"] for receipt in receipts] == [10002, 10003]
    assert [len(receipt["emblems"]) for receipt in receipts] == [1, 0]
    assert {receipt["payload_sha256"] for receipt in receipts} == {hashlib.sha256(RAW).hexdigest()}
    assert first_response != second_response
    reloaded = TeamStore(teams.root)
    replay = submit((reloaded, actor), request_digest=first_digest, inventory=None)
    assert replay[0] == first_response and replay[1]["applied"] is False
    assert reloaded.balance(actor.online_id) == 103061


def test_old_payload_keyed_cash_receipt_does_not_suppress_a_new_request(state):
    teams, actor = state
    plan = story_reward_plan(RAW, actor.player_id)
    teams.settle_battle_money(
        actor.online_id,
        hashlib.sha256(RAW).hexdigest(),
        mission_id=plan.mission_id,
        gross_earnings=plan.gross_pay,
        expenses=plan.expenses,
        policy_version=plan.policy_version,
    )
    before = teams.balance(actor.online_id)
    _, settlement, count = submit(
        state,
        request_digest=request_identity(actor, story_message(index=50)),
    )
    assert settlement["applied"] and count == 1
    assert teams.balance(actor.online_id) == before + plan.gross_pay - plan.expenses
    assert len(teams.state["battle_settlements"]) == 2


def test_old_payload_keyed_item_receipt_does_not_suppress_a_new_request(state):
    teams, actor = state
    response, _, _ = submit(state)
    updated = deepcopy(teams.state)
    (receipt,) = updated["story_rewards"].values()
    receipt["policy_version"] = LEGACY_ITEM_POLICY
    receipt["settlement"]["policy_version"] = LEGACY_ITEM_POLICY
    updated["battle_settlements"][0]["policy_version"] = LEGACY_ITEM_POLICY
    teams.commit_state(updated)
    new_result = submit(
        state,
        request_digest=request_identity(actor, story_message(index=51)),
    )
    assert new_result[1]["applied"] and new_result[0] != response
    assert teams.balance(actor.online_id) == 103061
    assert len(teams.state["story_rewards"]) == 2
    old_replay = submit(state, inventory=None)
    assert old_replay[0] == response and old_replay[1]["applied"] is False


def test_cash_only_story_receipts_survive_bounded_history_and_distinguish_runs(state):
    teams, actor = state
    payload = story_request(201)
    plan = story_reward_plan(payload, actor.player_id)
    first_digest = request_identity(actor, story_message(payload, index=60))
    second_digest = request_identity(actor, story_message(payload, index=62))
    first = settle(teams, actor, plan, payload, request_digest=first_digest)
    second = settle(teams, actor, plan, payload, request_digest=second_digest)
    assert first[1]["applied"] and second[1]["applied"]
    assert teams.balance(actor.online_id) == 277357
    assert len(teams.state["story_rewards"]) == 2
    assert all(
        not receipt["items"] and not receipt["pending_specs"] and not receipt["emblems"]
        for receipt in teams.state["story_rewards"].values()
    )
    updated = deepcopy(teams.state)
    updated["battle_settlements"] = []
    teams.commit_state(updated)
    loaded = TeamStore(teams.root)
    replay = settle(loaded, actor, plan, payload, request_digest=first_digest)
    assert replay[0] == first[0] and replay[1]["applied"] is False
    assert loaded.balance(actor.online_id) == 277357


def test_empty_story_receipt_requires_the_new_request_policy(state):
    teams, actor = state
    before = deepcopy(teams.state)
    receipt = {
        "online_id": actor.online_id,
        "result_digest": hashlib.sha256(RAW).hexdigest(),
        "items": [],
        "pending_specs": [],
        "emblems": [],
        "reply_hex": "0a00",
    }
    with pytest.raises(ValueError, match="Story item receipt"):
        teams.settle_battle_money(
            actor.online_id,
            receipt["result_digest"],
            mission_id=2002,
            gross_earnings=100_000,
            expenses=0,
            policy_version=LEGACY_ITEM_POLICY,
            item_receipt=receipt,
        )
    assert teams.state == before


def test_story_handler_uses_authenticated_request_index_for_distinct_runs(
    state, monkeypatch, tmp_path
):
    from acvd_server import story_items
    from acvd_server.handlers.economy import register_battle_result

    class Events:
        def emit(self, *_args, **_fields):
            pass

    monkeypatch.setattr(
        story_items, "authorized_inventory", lambda *_args: (record(), frozenset({10001}))
    )
    teams, actor = state
    replies = []
    for index, counter in ((70, 1), (72, 1), (70, 9)):
        register_battle_result(
            actor,
            story_message(index=index, counter=counter),
            teams=teams,
            telemetry=Events(),
            reply=replies.append,
            initial_balance=0,
            player_data_path=tmp_path,
        )
    assert len(teams.state["story_rewards"]) == 2
    assert teams.balance(actor.online_id) == 103061
    assert replies[0] != replies[1] and replies[0] == replies[2]


def test_captured_two_human_story_result_pays_cash_and_items_atomically(
    tmp_path, monkeypatch
):
    from acvd_server import story_items
    from acvd_server.handlers.economy import register_battle_result

    teams = TeamStore(tmp_path / "teams")
    leader = GameSession(
        b"leader01", bytes(16), "Leader", 112838, established=True, peer=("127.0.0.1", 1)
    )
    guest = GameSession(
        b"guest001", bytes(16), "Guest", 191593, established=True, peer=("127.0.0.1", 2)
    )
    for actor in (leader, guest):
        teams.remember_player(actor.online_id, actor.player_id)
        teams.synchronize_balance(actor.online_id, 100_000)
    monkeypatch.setattr(
        story_items, "authorized_inventory", lambda *_args: (record(), frozenset({10001}))
    )
    directory = {
        1: {
            "session_id": 1,
            "team_id": 1,
            "members": {
                "Leader": {"player_id": 112838},
                "Guest": {"player_id": 191593},
            },
        }
    }
    events, replies = [], []

    class Events:
        def emit(self, event, **fields):
            events.append((event, fields))

    submissions = (
        (
            leader,
            GameMessage(REGISTER_BATTLE_RESULT, (77).to_bytes(4, "big"), COOP_RAW, 1),
        ),
        (
            guest,
            GameMessage(REGISTER_BATTLE_RESULT, (78).to_bytes(4, "big"), COOP_RAW, 2),
        ),
    )
    for counter, (actor, message) in enumerate(submissions, 1):
        register_battle_result(
            actor,
            message,
            teams=teams,
            telemetry=Events(),
            reply=replies.append,
            initial_balance=0,
            player_data_path=tmp_path,
            game_sessions={leader.auth_token: leader, guest.auth_token: guest},
            team_sessions=directory,
        )
        if counter == 1:
            directory.clear()  # Exact committed retry no longer needs live peer state.
    assert teams.balance("Leader") == 287_372
    assert teams.balance("Guest") == 299_683
    assert len(teams.state["story_rewards"]) == 2
    assert len(teams.state["battle_settlements"]) == 2
    assert len(teams.state["story_group_rewards"]) == 1
    group = next(iter(teams.state["story_group_rewards"].values()))
    assert len(group["request_aliases"]) == 2
    assert group["participant_player_ids"] == [112838, 191593]
    assert replies[0] == replies[1]
    rewards = [decode_proto(field.value) for field in decode_proto(replies[0])]
    assert [next(field.value for field in reward if field.number == 9) for reward in rewards] == [
        112838,
        191593,
    ]
    assert all(
        next(field.value for field in reward if field.number == 7) == 210_000
        for reward in rewards
    )
    assert events[-1][0] == "game_story_coop_result_registered"
    assert events[-1][1]["applied"] is False


def test_guest_report_can_arrive_first_and_leader_replays_without_double_payment(tmp_path):
    from acvd_server.handlers.economy import register_battle_result

    teams = TeamStore(tmp_path / "teams")
    leader = GameSession(
        b"leader01", bytes(16), "Leader", 112838, established=True, peer=("127.0.0.1", 1)
    )
    guest = GameSession(
        b"guest001", bytes(16), "Guest", 191593, established=True, peer=("127.0.0.1", 2)
    )
    for actor in (leader, guest):
        teams.remember_player(actor.online_id, actor.player_id)
        teams.synchronize_balance(actor.online_id, 100_000)
    directory = {
        1: {
            "members": {
                "Leader": {"player_id": 112838},
                "Guest": {"player_id": 191593},
            }
        }
    }
    replies = []

    class Events:
        def emit(self, *_args, **_fields):
            pass

    for actor, index in ((guest, 90), (leader, 91)):
        register_battle_result(
            actor,
            GameMessage(REGISTER_BATTLE_RESULT, index.to_bytes(4, "big"), COOP_RAW, index),
            teams=teams,
            telemetry=Events(),
            reply=replies.append,
            initial_balance=0,
            player_data_path=tmp_path,
            game_sessions={leader.auth_token: leader, guest.auth_token: guest},
            team_sessions=directory,
        )
        directory.clear()
    assert teams.balance("Leader") == 287_372
    assert teams.balance("Guest") == 299_683
    assert len(teams.state["battle_settlements"]) == 2
    assert replies[0] == replies[1]


def test_same_payload_outside_debrief_window_is_not_aliased(tmp_path):
    from acvd_server.handlers.economy import register_battle_result

    teams = TeamStore(tmp_path / "teams")
    leader = GameSession(
        b"leader01", bytes(16), "Leader", 112838, established=True, peer=("127.0.0.1", 1)
    )
    guest = GameSession(
        b"guest001", bytes(16), "Guest", 191593, established=True, peer=("127.0.0.1", 2)
    )
    for actor in (leader, guest):
        teams.remember_player(actor.online_id, actor.player_id)
        teams.synchronize_balance(actor.online_id, 100_000)
    directory = {
        1: {
            "members": {
                "Leader": {"player_id": 112838},
                "Guest": {"player_id": 191593},
            }
        }
    }
    replies = []

    class Events:
        def emit(self, *_args, **_fields):
            pass

    common = dict(
        teams=teams,
        telemetry=Events(),
        reply=replies.append,
        initial_balance=0,
        player_data_path=tmp_path,
        game_sessions={leader.auth_token: leader, guest.auth_token: guest},
    )
    register_battle_result(
        leader,
        GameMessage(REGISTER_BATTLE_RESULT, (92).to_bytes(4, "big"), COOP_RAW, 92),
        team_sessions=directory,
        **common,
    )
    state = deepcopy(teams.state)
    next(iter(state["story_group_rewards"].values()))["settled_at_unix_ns"] = 1
    teams.commit_state(state)
    register_battle_result(
        guest,
        GameMessage(REGISTER_BATTLE_RESULT, (93).to_bytes(4, "big"), COOP_RAW, 93),
        team_sessions={},
        **common,
    )
    assert replies[1] == b""
    assert teams.balance("Leader") == 287_372
    assert teams.balance("Guest") == 299_683
    assert len(teams.state["battle_settlements"]) == 2


def test_two_human_story_result_requires_the_exact_live_team_session(tmp_path):
    from acvd_server.handlers.economy import register_battle_result

    teams = TeamStore(tmp_path / "teams")
    leader = GameSession(
        b"leader01", bytes(16), "Leader", 112838, established=True, peer=("127.0.0.1", 1)
    )
    guest = GameSession(
        b"guest001", bytes(16), "Guest", 191593, established=True, peer=("127.0.0.1", 2)
    )
    for actor in (leader, guest):
        teams.remember_player(actor.online_id, actor.player_id)
        teams.synchronize_balance(actor.online_id, 100_000)
    events, replies = [], []

    class Events:
        def emit(self, event, **fields):
            events.append((event, fields))

    register_battle_result(
        leader,
        GameMessage(REGISTER_BATTLE_RESULT, (78).to_bytes(4, "big"), COOP_RAW, 1),
        teams=teams,
        telemetry=Events(),
        reply=replies.append,
        initial_balance=0,
        player_data_path=tmp_path,
        game_sessions={leader.auth_token: leader, guest.auth_token: guest},
        team_sessions={},
    )
    assert replies == [b""]
    assert teams.balance("Leader") == teams.balance("Guest") == 100_000
    assert events[-1][0] == "game_story_reward_rejected"
    assert "team-session or hire binding" in events[-1][1]["reason"]


def test_two_human_story_result_allows_nonparticipating_teammate_in_lobby(tmp_path):
    from acvd_server.handlers.economy import register_battle_result

    teams = TeamStore(tmp_path / "teams")
    leader = GameSession(
        b"leader01", bytes(16), "Leader", 112838, established=True, peer=("127.0.0.1", 1)
    )
    guest = GameSession(
        b"guest001", bytes(16), "Guest", 191593, established=True, peer=("127.0.0.1", 2)
    )
    spectator = GameSession(
        b"watch001", bytes(16), "Spectator", 50627, established=True, peer=("127.0.0.1", 3)
    )
    for actor in (leader, guest, spectator):
        teams.remember_player(actor.online_id, actor.player_id)
        teams.synchronize_balance(actor.online_id, 100_000)
    directory = {
        1: {
            "members": {
                "Leader": {"player_id": 112838},
                "Guest": {"player_id": 191593},
                "Spectator": {"player_id": 50627},
            }
        }
    }
    events, replies = [], []

    class Events:
        def emit(self, event, **fields):
            events.append((event, fields))

    register_battle_result(
        leader,
        GameMessage(REGISTER_BATTLE_RESULT, (79).to_bytes(4, "big"), COOP_RAW, 1),
        teams=teams,
        telemetry=Events(),
        reply=replies.append,
        initial_balance=0,
        player_data_path=tmp_path,
        game_sessions={
            leader.auth_token: leader,
            guest.auth_token: guest,
            spectator.auth_token: spectator,
        },
        team_sessions=directory,
    )
    assert replies and replies[0]
    assert teams.balance("Leader") == 287_372
    assert teams.balance("Guest") == 299_683
    assert teams.balance("Spectator") == 100_000
    assert events[-1][0] == "game_story_coop_result_registered"
    assert events[-1][1]["participant_player_ids"] == [112838, 191593]


def test_two_human_story_item_and_emblem_rewards_are_owner_scoped(tmp_path, monkeypatch):
    from acvd_server import story_items
    from acvd_server.handlers.economy import register_battle_result

    teams = TeamStore(tmp_path / "teams")
    leader = GameSession(
        b"leader01", bytes(16), "Leader", 112838, established=True, peer=("127.0.0.1", 1)
    )
    guest = GameSession(
        b"guest001", bytes(16), "Guest", 191593, established=True, peer=("127.0.0.1", 2)
    )
    for actor in (leader, guest):
        teams.remember_player(actor.online_id, actor.player_id)
        teams.synchronize_balance(actor.online_id, 100_000)
    monkeypatch.setattr(
        story_items, "authorized_inventory", lambda *_args: (record(), frozenset({10001}))
    )
    directory = {
        1: {
            "session_id": 1,
            "team_id": 1,
            "members": {
                "Leader": {"player_id": 112838},
                "Guest": {"player_id": 191593},
            },
        }
    }
    replies = []

    class Events:
        def emit(self, *_args, **_fields):
            pass

    register_battle_result(
        leader,
        GameMessage(
            REGISTER_BATTLE_RESULT,
            (79).to_bytes(4, "big"),
            coop_story_request(),
            1,
        ),
        teams=teams,
        telemetry=Events(),
        reply=replies.append,
        initial_balance=0,
        player_data_path=tmp_path,
        game_sessions={leader.auth_token: leader, guest.auth_token: guest},
        team_sessions=directory,
    )
    receipts = sorted(teams.state["story_rewards"].values(), key=lambda row: row["player_id"])
    assert [len(row["items"]) for row in receipts] == [1, 1]
    assert [len(row["emblems"]) for row in receipts] == [1, 1]
    assert all(row["items"][0]["instance_id"] == 10002 for row in receipts)
    assert all(not row["pending_specs"] for row in receipts)
    rewards = [decode_proto(field.value) for field in decode_proto(replies[0])]
    assert len(rewards) == 2
    assert all(any(field.number == 8 for field in reward) for reward in rewards)


def test_hired_j_fight_awards_all_uw_parts_to_both_owners(tmp_path, monkeypatch):
    from acvd_server import story_items
    from acvd_server.handlers.economy import register_battle_result
    from acvd_server.hire_bindings import HireBindings

    payload = bytes.fromhex(
        "10f207"
        "1a1308aae0161000180120e4950528b44130003800"
        "1a1308a7c3161000180120ffac0728814630023801"
        "220b08e80710f2071885072000"
        "30033802"
    )
    teams = TeamStore(tmp_path / "teams")
    employer = GameSession(
        b"employ01", bytes(16), "RUKKVI", 372778, established=True, peer=("127.0.0.1", 1)
    )
    mercenary = GameSession(
        b"merc0001",
        bytes(16),
        "etherdoubler",
        369063,
        established=True,
        peer=("127.0.0.1", 2),
    )
    sessions = {employer.auth_token: employer, mercenary.auth_token: mercenary}
    team_ids = {employer.online_id: 42, mercenary.online_id: 39}
    hires = HireBindings(lambda: sessions.values(), team_ids.__getitem__)
    hires.offer(employer, mercenary.player_id)
    assert hires.unregister(mercenary, True)
    for actor in (employer, mercenary):
        teams.remember_player(actor.online_id, actor.player_id)
        teams.synchronize_balance(actor.online_id, 100_000)
    monkeypatch.setattr(
        story_items, "authorized_inventory", lambda *_args: (record(), frozenset({10001}))
    )
    events, replies = [], []

    class Events:
        def emit(self, event, **fields):
            events.append((event, fields))

    register_battle_result(
        employer,
        GameMessage(REGISTER_BATTLE_RESULT, (101).to_bytes(4, "big"), payload, 1),
        teams=teams,
        telemetry=Events(),
        reply=replies.append,
        initial_balance=0,
        player_data_path=tmp_path,
        game_sessions=sessions,
        team_sessions={},
        hire_bindings=hires,
    )

    assert teams.balance("RUKKVI") == 286_920
    assert teams.balance("etherdoubler") == 250_592
    receipts = sorted(teams.state["story_rewards"].values(), key=lambda row: row["player_id"])
    assert len(receipts) == 2
    assert all(len(row["items"]) == 6 and not row["pending_specs"] for row in receipts)
    assert all(
        {(item["category"], item["part_id"]) for item in row["items"]}
        == {(11, 6010), (11, 110), (11, 310), (11, 610), (11, 810), (11, 1010)}
        for row in receipts
    )
    rewards = [decode_proto(field.value) for field in decode_proto(replies[0])]
    assert len(rewards) == 2
    quests = [
        [decode_proto(field.value) for field in reward if field.number == 8]
        for reward in rewards
    ]
    assert all(
        sum(
            next(detail.value for detail in decode_proto(field.value) if detail.number == 1) == 2
            for quest in owner_quests
            for field in quest
            if field.number == 6
        )
        == 6
        for owner_quests in quests
    )
    assert events[-1][0] == "game_story_coop_result_registered"
    assert events[-1][1]["reply_item_count"] == 12


def test_emblem_reward_is_encoded_and_committed_with_cash_and_item(state):
    teams, _ = state
    response, result, count = submit(state)
    receipt = next(iter(teams.state["story_rewards"].values()))
    assert count == 1 and result["emblem_reward_count"] == 1
    assert receipt["policy_version"] == ITEM_POLICY
    assert receipt["emblems"] == [
        {
            "condition": (100, 2002, 0),
            "category": 50,
            "selector": 0x20032,
            "row_id": 1231,
            "identity": 0,
        }
    ]
    (battle,) = decode_proto(response)
    quests = [
        decode_proto(field.value) for field in decode_proto(battle.value) if field.number == 8
    ]
    rewards = [decode_proto(field.value) for quest in quests for field in quest if field.number == 6]
    emblem = next(
        reward
        for reward in rewards
        if next(field.value for field in reward if field.number == 1) == 3
    )
    key_item = decode_proto(next(field.value for field in emblem if field.number == 3))
    assert {field.number: field.value for field in key_item} == {
        1: 0,
        2: 0x20032,
        3: 1231,
    }


def test_authenticated_owned_emblem_is_not_returned_again(state):
    teams, _ = state
    response, result, count = submit(state, inventory=record() + emblem_record())
    receipt = next(iter(teams.state["story_rewards"].values()))
    assert count == 1 and result["emblem_reward_count"] == 0 and receipt["emblems"] == []
    (battle,) = decode_proto(response)
    rewards = [
        decode_proto(field.value)
        for quest in (decode_proto(f.value) for f in decode_proto(battle.value) if f.number == 8)
        for field in quest
        if field.number == 6
    ]
    assert all(
        next(field.value for field in reward if field.number == 1) != 3 for reward in rewards
    )


def test_committed_emblem_is_not_reissued_before_the_next_upload(state):
    teams, _ = state
    submit(state)
    changed = RAW.replace(bytes.fromhex("208219"), bytes.fromhex("208319"))
    _, result, _ = submit(state, changed)
    receipts = list(teams.state["story_rewards"].values())
    assert result["emblem_reward_count"] == 0
    assert [len(receipt["emblems"]) for receipt in receipts] == [1, 0]


def test_emblem_only_mission_uses_a_valid_durable_receipt(state):
    teams, actor = state
    payload = story_request(101)
    response, result, count = settle(
        teams,
        actor,
        story_reward_plan(payload, actor.player_id),
        payload,
        inventory=record(),
        reserved_instance_ids=frozenset({10001}),
    )
    receipt = next(iter(teams.state["story_rewards"].values()))
    assert count == 0 and result["emblem_reward_count"] == 1
    assert receipt["items"] == [] and receipt["pending_specs"] == []
    assert receipt["emblems"][0]["row_id"] == 1274
    loaded = TeamStore(teams.root)
    replay = settle(
        loaded,
        actor,
        story_reward_plan(payload, actor.player_id),
        payload,
        inventory=None,
    )
    assert replay[0] == response and replay[1]["applied"] is False


@pytest.mark.parametrize("method", ["write_text", "replace"])
def test_failed_storage_grants_nothing_and_retry_uses_same_instance(state, monkeypatch, method):
    teams, _ = state
    before, disk = deepcopy(teams.state), teams.state_path.read_bytes()
    temporary = teams.state_path.with_suffix(".json.tmp")
    original = getattr(Path, method)

    def fail(path, *args, **kwargs):
        if path == temporary:
            raise OSError("item commit failed")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, method, fail)
        with pytest.raises(OSError, match="item commit"):
            submit(state)
    assert teams.state == before and teams.state_path.read_bytes() == disk
    submit(state)
    assert next(iter(teams.state["story_rewards"].values()))["items"][0]["instance_id"] == 10002


def test_confirmation_and_disposal_do_not_resurrect_reward(state):
    teams, actor = state
    submit(state)
    merged = pending_inventory(teams, actor, record())
    assert len(decode_proto(merged)) == 2
    assert confirm_inventory(teams, actor, merged) == 1
    assert pending_inventory(teams, actor, record()) == record()
    assert confirm_inventory(teams, actor, record()) == 0
    with pytest.raises(OwnershipError, match="conflicts"):
        pending_inventory(teams, actor, record() + record(10002, part=99))


def test_unknown_inventory_pays_cash_and_retains_earned_item_without_guessing(state):
    teams, _ = state
    response, result, count = submit(state, inventory=None)
    assert result["applied"] and count == 0 and teams.balance("Tester") == 70209
    receipt = next(iter(teams.state["story_rewards"].values()))
    assert receipt["items"] == [] and receipt["pending_specs"][0]["part_id"] == 2610
    assert receipt["reply_hex"] == response.hex()
    before = deepcopy(teams.state)
    again = submit(state, inventory=None)
    assert again[0] == response and not again[1]["applied"] and teams.state == before


def test_full_inventory_pays_cash_and_retains_earned_item(state):
    teams, _ = state
    full = b"".join(record(10000 + i) for i in range(2980))
    _, result, count = submit(
        state, inventory=full, reserved_instance_ids=frozenset(range(10000, 12980))
    )
    assert result["applied"] and count == 0 and teams.balance("Tester") == 70209
    assert next(iter(teams.state["story_rewards"].values()))["pending_specs"]


def test_deferred_item_recovers_after_reload_without_another_cash_payment(state):
    teams, actor = state
    response, _, _ = submit(state, inventory=None)
    teams = TeamStore(teams.root)
    wallet = teams.balance(actor.online_id)
    assert (
        resolve_pending(teams, actor, inventory=record(), reserved_instance_ids=frozenset({10001}))
        == 1
    )
    receipt = next(iter(teams.state["story_rewards"].values()))
    assert receipt["pending_specs"] == [] and receipt["items"][0]["instance_id"] == 10002
    assert receipt["reply_hex"] == response.hex() and receipt["reply_item_count"] == 0
    assert teams.balance(actor.online_id) == wallet
    assert resolve_pending(teams, actor, inventory=None) == 0
    again = submit((teams, actor), inventory=None)
    assert again[0] == response and not again[1]["applied"] and again[2] == 0
    assert len(decode_proto(pending_inventory(teams, actor, record()))) == 2


@pytest.mark.parametrize("method", ["write_text", "replace"])
def test_failed_deferred_delivery_keeps_entitlement_and_paid_cash(state, monkeypatch, method):
    teams, actor = state
    submit(state, inventory=None)
    before, disk = deepcopy(teams.state), teams.state_path.read_bytes()
    temporary = teams.state_path.with_suffix(".json.tmp")
    original = getattr(Path, method)

    def fail(path, *args, **kwargs):
        if path == temporary:
            raise OSError("deferred commit failed")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, method, fail)
        with pytest.raises(OSError, match="deferred commit"):
            resolve_pending(
                teams, actor, inventory=record(), reserved_instance_ids=frozenset({10001})
            )
    assert teams.state == before and teams.state_path.read_bytes() == disk
    assert (
        resolve_pending(teams, actor, inventory=record(), reserved_instance_ids=frozenset({10001}))
        == 1
    )
    assert teams.balance(actor.online_id) == 70209


def test_deferred_delivery_never_grants_to_another_account(state):
    teams, _ = state
    submit(state, inventory=None)
    other = GameSession(b"other001", bytes(16), "Other", 77)
    before = deepcopy(teams.state)
    assert (
        resolve_pending(teams, other, inventory=record(), reserved_instance_ids=frozenset({10001}))
        == 0
    )
    assert teams.state == before


def test_capacity_delivers_only_available_prefix_and_keeps_the_rest(state):
    teams, actor = state
    changed = RAW.replace(bytes.fromhex("208219"), bytes.fromhex("208319"))
    submit(state, inventory=None)
    submit(state, changed, inventory=None)
    inventory = b"".join(record(10000 + i) for i in range(2979))
    reserved = frozenset(range(10000, 12979))
    before = teams.balance(actor.online_id)
    assert resolve_pending(teams, actor, inventory=inventory, reserved_instance_ids=reserved) == 1
    receipts = list(teams.state["story_rewards"].values())
    assert len(receipts[0]["items"]) == 1 and len(receipts[1]["pending_specs"]) == 1
    assert resolve_pending(teams, actor, inventory=inventory, reserved_instance_ids=reserved) == 0
    assert teams.balance(actor.online_id) == before


def test_native_claim_then_cursor_read_delivers_deferred_item_once(state, tmp_path):
    from acvd_server.handlers.inventory import acknowledge_owned_parts, get_owned_items
    from acvd_server.udp import GET_MY_ITEM_SIMPLE_INFO_LIST_ALT, GameMessage

    teams, actor = state
    submit(state, inventory=None)
    players = tmp_path / "players"
    players.mkdir()
    (players / f"{actor.player_id}.offline-data.bin").write_bytes(record())
    replies = []

    class Events:
        def __init__(self):
            self.rows = []

        def emit(self, event, **fields):
            self.rows.append((event, fields))

    events = Events()

    def read(cursor):
        get_owned_items(
            actor,
            GameMessage(
                GET_MY_ITEM_SIMPLE_INFO_LIST_ALT,
                bytes(4),
                proto_varint(2, 3000) + proto_varint(4, cursor),
            ),
            teams=teams,
            player_data_path=players,
            telemetry=events,
            reply=replies.append,
        )
        return replies[-1]

    # The first read precedes the fresh claim. It cannot allocate from a guess.
    assert read(10001) == b""
    assert next(iter(teams.state["story_rewards"].values()))["pending_specs"]
    acknowledge_owned_parts(
        actor,
        GameMessage(0x41D, bytes(4), proto_varint(1, 1) + proto_varint(2, 10001)),
        teams=teams,
        player_data_path=players,
        telemetry=events,
        reply=replies.append,
    )
    response = decode_proto(read(10001))
    assert len(response) == 1
    item = {f.number: f.value for f in decode_proto(response[0].value)}
    assert item[1] == 10002 and item[3] == 2610 and item[4] == 3
    assert read(10002) == b"" and teams.balance(actor.online_id) == 70209
    assert len([event for event, _ in events.rows if event == "game_story_items_prepared"]) == 1


def test_fresh_install_mismatch_uses_same_login_claim_and_cursor_for_new_reward(
    state, tmp_path
):
    from acvd_server.handlers.inventory import acknowledge_owned_parts, get_owned_items
    from acvd_server.udp import GET_MY_ITEM_SIMPLE_INFO_LIST_ALT, GameMessage

    teams, actor = state
    submit(state, inventory=None)
    players = tmp_path / "players"
    players.mkdir()
    (players / f"{actor.player_id}.offline-data.bin").write_bytes(record(10001))
    actor.owned_parts_max_instance_id = 10050
    actor.owned_parts_evidence_serial = 1
    events, replies = [], []

    class Events:
        def emit(self, event, **fields):
            events.append((event, fields))

    acknowledge_owned_parts(
        actor,
        GameMessage(
            0x41D,
            bytes(4),
            proto_varint(1, 2) + proto_varint(2, 999),
            1,
        ),
        teams=teams,
        player_data_path=players,
        telemetry=Events(),
        reply=replies.append,
    )
    receipt = next(iter(teams.state["story_rewards"].values()))
    assert receipt["pending_specs"] == []
    assert receipt["items"][0]["instance_id"] == 10051
    prepared = [fields for event, fields in events if event == "game_story_items_prepared"]
    assert prepared == [
        {
            "online_id": actor.online_id,
            "count": 1,
            "delivery": "next_owned_item_cursor_read",
            "cash_changed": False,
            "inventory_authority": "same_login_claim_and_cursor",
        }
    ]

    get_owned_items(
        actor,
        GameMessage(
            GET_MY_ITEM_SIMPLE_INFO_LIST_ALT,
            bytes(4),
            proto_varint(2, 3000) + proto_varint(4, 10050),
            2,
        ),
        teams=teams,
        player_data_path=players,
        telemetry=Events(),
        reply=replies.append,
    )
    item = decode_proto(decode_proto(replies[-1])[0].value)
    assert next(field.value for field in item if field.number == 1) == 10051


def test_next_distinct_result_gets_new_instance_and_old_cash_receipt_is_not_upgraded(state):
    teams, actor = state
    submit(state)
    # A changed native expense gives a different completed result, not a retry.
    changed = RAW.replace(bytes.fromhex("208219"), bytes.fromhex("208319"))
    assert changed != RAW
    submit(state, changed)
    identities = [r["items"][0]["instance_id"] for r in teams.state["story_rewards"].values()]
    assert identities == [10002, 10003]
    import hashlib

    other = RAW + proto_varint(30, 0)
    plan = story_reward_plan(other, actor.player_id)
    teams.settle_battle_money(
        actor.online_id,
        hashlib.sha256(other).hexdigest(),
        mission_id=plan.mission_id,
        gross_earnings=plan.gross_pay,
        expenses=plan.expenses,
        policy_version=plan.policy_version,
    )
    before = deepcopy(teams.state)
    _, result, count = submit(state, other, inventory=None)
    assert not result["applied"] and count == 0 and teams.state == before


def test_table_selection_does_not_grant_unearned_subquest():
    from dataclasses import replace

    plan = replace(story_reward_plan(RAW, 50627), mission_id=203, subquest_ids=())
    assert item_specs(plan) == ()
    assert item_specs(replace(plan, subquest_ids=(904,)))[0]["part_id"] == 6720


def test_compiled_rules_match_recovered_table_fixture():
    import json

    from acvd_server.story_item_data import STORY_PART_REWARDS

    rows = json.loads((Path(__file__).parent / "fixtures/story-item-rules.json").read_text())
    expected = {
        tuple(row["condition"][:3]): tuple(tuple(part) for part in row["parts"]) for row in rows
    }
    assert STORY_PART_REWARDS == expected


def test_shop_and_story_share_instance_reservations(state):
    from acvd_server.shop_catalogue import CataloguePart
    from acvd_server.shop_transactions import purchase
    from acvd_server.udp import GameMessage

    teams, actor = state
    submit(state)
    offer = CataloguePart(1, 0, 0, 210, 1)
    payload = b"".join(proto_varint(n, v) for n, v in ((2, 1), (3, 1), (4, 0), (5, 1), (7, 1000)))
    response, replay = purchase(
        teams,
        actor,
        GameMessage(0x3DD, bytes(4), payload, 1),
        offer=offer,
        shop_key=1,
        verified_level=0,
        inventory=record(),
        reserved_instance_ids=frozenset({10001}),
    )
    assert not replay and decode_proto(response)[0].value == 10003
    assert len(decode_proto(pending_inventory(teams, actor, record()))) == 3


def test_lost_reply_reuses_owned_item_without_reading_inventory_again(state, monkeypatch, tmp_path):
    from acvd_server import story_items
    from acvd_server.handlers.economy import register_battle_result
    from acvd_server.udp import GameMessage

    class Events:
        def emit(self, *args, **kwargs):
            pass

    monkeypatch.setattr(
        story_items, "authorized_inventory", lambda *args: (record(), frozenset({10001}))
    )
    seen = []

    def lost(response):
        seen.append(response)
        raise OSError("lost item reply")

    teams, actor = state
    message = GameMessage(0x3C2, bytes(4), RAW)
    with pytest.raises(OSError, match="lost item reply"):
        register_battle_result(
            actor,
            message,
            teams=teams,
            telemetry=Events(),
            reply=lost,
            initial_balance=0,
            player_data_path=tmp_path,
        )
    before = deepcopy(teams.state)

    def unavailable(*args):
        raise AssertionError("Retry must read the receipt, not the inventory")

    monkeypatch.setattr(story_items, "authorized_inventory", unavailable)
    register_battle_result(
        actor,
        message,
        teams=teams,
        telemetry=Events(),
        reply=seen.append,
        initial_balance=0,
        player_data_path=tmp_path,
    )
    assert seen[0] == seen[1] and teams.state == before
