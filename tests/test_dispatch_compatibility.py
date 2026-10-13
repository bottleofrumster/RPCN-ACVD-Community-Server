"""Pin explicit compatibility limitations without mistaking ACKs for features."""

import time
from copy import deepcopy
from pathlib import Path

import pytest

from acvd_server import udp
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.result_messages import BATTLE_RESULT_FIXED_PUSH
from acvd_server.server import GameDatagramProtocol
from acvd_server.teams import TeamStore


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


@pytest.fixture
def dispatcher(tmp_path, monkeypatch):
    protocol = GameDatagramProtocol(
        Events(),
        {},
        Path("native/libacvd_cwc.so"),
        tmp_path / "players",
        TeamStore(tmp_path / "teams"),
    )
    actor = udp.GameSession(b"actor001", bytes(16), "Actor", 11)
    replies = []
    monkeypatch.setattr(protocol, "_send_game_reply", lambda *args: replies.append(args))
    packet = udp.ReliablePacket(3, 2, udp.DAT)
    return protocol, actor, packet, replies


def test_undercharged_special_gets_a_correlated_error_instead_of_silent_timeout(
    dispatcher, monkeypatch
):
    protocol, actor, packet, replies = dispatcher
    protocol.teams.create(
        online_id=actor.online_id, player_id=actor.player_id, name="Charge Test",
        operation_base=0, language=2, play_type=1, enlistment_method=2,
    )
    actor.established = True
    actor.peer = ("127.0.0.1", 11001)
    before = deepcopy(protocol.teams.state)
    errors = []
    monkeypatch.setattr(
        protocol, "_send_game_reply", lambda *a, **k: errors.append((a, k))
    )
    payload = (proto_varint(1, 3) + proto_varint(2, 2) + proto_varint(3, 2)
               + proto_bytes(6, proto_varint(1, 11) + proto_varint(2, 0)))
    message = udp.GameMessage(udp.REGISTER_PREPARE, b"\0\0\0\x27", payload, 39)
    protocol._dispatch_game_message(actor, packet, message)
    assert errors == [((actor, packet, message, b""), {"error": (1, 28)})]
    assert not replies and not protocol._matchmaking.preparations
    assert not protocol._matchmaking.matches and protocol.teams.state == before


@pytest.mark.parametrize(
    "opcode,event,payload,body",
    [
        (
            udp.GET_PLAYER_KEY_ITEM_LIST,
            "game_key_item_list_sent",
            proto_varint(2, 0x20000) + proto_varint(5, 0) + proto_varint(6, 2048),
            b"",
        ),
        (udp.GET_WELCOME_MESSAGE_INFO_LIST, "game_empty_collection_sent", b"", b""),
        (udp.GET_BATTLE_RECORD, "game_empty_collection_sent", b"", b""),
        # This no-team fixture remains empty; known comments now have a real reader.
        (udp.GET_TEAM_MEMBER_COMMENT_LIST, "game_team_member_comments_sent", b"", b""),
        (udp.GET_MY_MEDAL_INFO, "game_my_medal_info_sent", b"", b""),
        (udp.GET_PERSONAL_ANNOUNCE, "game_personal_announce_sent", b"", b""),
        (
            udp.SET_PERSONAL_ANNOUNCE_ALREADY_READ,
            "game_personal_announce_marked_read",
            b"",
            b"",
        ),
        (udp.BATTLE_START, "game_battle_start_acknowledged", b"", b""),
        (0x7FFE, "game_request_stubbed", b"", b""),
    ],
)
def test_inline_compatibility_reply_is_correlated_and_does_not_mutate_store(
    dispatcher, opcode, event, payload, body
):
    protocol, actor, packet, replies = dispatcher
    before = deepcopy(protocol.teams.state)
    message = udp.GameMessage(opcode, b"\0\0\0\1", payload, 17)
    protocol._dispatch_game_message(actor, packet, message)
    assert replies == [(actor, packet, message, body)]
    assert protocol.telemetry.rows[-1][0] == event
    assert protocol.teams.state == before


def test_team_medal_request_returns_the_earned_creation_medal(dispatcher):
    protocol, actor, packet, replies = dispatcher
    protocol.teams.create(
        online_id=actor.online_id,
        player_id=actor.player_id,
        name="Medal Team",
        operation_base=0,
        language=0,
        play_type=0,
        enlistment_method=2,
    )
    message = udp.GameMessage(udp.GET_MY_TEAM_MEDAL_INFO, bytes(4), b"", 19)

    protocol._dispatch_game_message(actor, packet, message)

    assert len(replies) == 1 and replies[0][:3] == (actor, packet, message)
    assert replies[0][3] == bytes.fromhex("08001000180020002800300038014000")
    assert protocol.telemetry.rows[-1] == (
        "game_my_team_medal_info_sent",
        {
            "peer": actor.peer,
            "online_id": actor.online_id,
            "team_id": 1,
            "medal_count": 1,
            "team_ac_kills": 0,
            "team_fame_points": 50,
            "team_player_medals": 0,
            "team_world_sorties": 0,
            "team_pvp_wins": 0,
            "team_best_ranking": 0,
        },
    )


def test_empty_removal_history_preserves_qualified_reply_with_native_request(dispatcher):
    protocol, actor, packet, replies = dispatcher
    protocol.teams.remember_player(actor.online_id, actor.player_id)
    before = deepcopy(protocol.teams.state)
    payload = proto_varint(1, 512) + proto_varint(2, 0) + proto_varint(3, (1 << 64) - 1)
    message = udp.GameMessage(udp.GET_TRASH_PARTS_LIST, bytes(4), payload, 1)
    protocol._dispatch_game_message(actor, packet, message)
    assert replies == [(actor, packet, message, proto_varint(2, 0) + proto_varint(3, 0))]
    assert protocol.teams.state == before


@pytest.mark.parametrize("opcode", [0, 0x0320])
def test_control_opcodes_are_not_generic_stub_replies(dispatcher, opcode):
    protocol, actor, packet, replies = dispatcher
    protocol._dispatch_game_message(actor, packet, udp.GameMessage(opcode, bytes(4), b"", 1))
    assert replies == []
    assert [event for event, _ in protocol.telemetry.rows] == ["game_message_received"]


def test_world_result_atomically_pays_the_roster_and_replays_each_exact_reply(
    tmp_path, monkeypatch
):
    telemetry = Events()
    teams = TeamStore(tmp_path / "teams")
    winner = udp.GameSession(b"winner01", bytes(16), "Winner", 50_627)
    loser = udp.GameSession(b"loser001", bytes(16), "Loser", 451_473)
    for session, team_id in ((winner, 1), (loser, 2)):
        session.established = True
        session.peer = ("127.0.0.1", 50_000 + team_id)
        teams.remember_player(session.online_id, session.player_id)
        teams.synchronize_balance(session.online_id, 1_000_000)
    state = deepcopy(teams.state)
    state["teams"] = {
        "1": {"team_id": 1, "faction_id": 1},
        "2": {"team_id": 2, "faction_id": 2},
    }
    state["members"] = {"Winner": 1, "Loser": 2}
    teams.commit_state(state)
    reward_settings = {
        "world_normal_base_reward": 150_000,
        "world_special_base_reward": 1_000_000,
        "world_small_subtarget_reward": 80_000,
        "world_large_subtarget_reward": 120_000,
        "world_mercenary_rank": 0,
    }
    protocol = GameDatagramProtocol(
        telemetry,
        {winner.auth_token: winner, loser.auth_token: loser},
        Path("native/libacvd_cwc.so"),
        tmp_path / "players",
        teams,
        get_world_reward_settings=lambda: reward_settings,
    )
    winner_member = {
        "session": winner,
        "prepare_id": 1,
        "team_id": 1,
        "pay_policy": "pay_costs_equally",
        "claimed_player_ids": [winner.player_id],
        "claimed_player_roles": [0],
        "squad": {winner.player_id: (winner, 1)},
    }
    loser_member = {
        "session": loser,
        "prepare_id": 2,
        "team_id": 2,
        "pay_policy": "pay_costs_equally",
        "claimed_player_ids": [loser.player_id],
        "claimed_player_roles": [0],
        "squad": {loser.player_id: (loser, 2)},
    }
    match_template = {
        "match_id": 1,
        "members": [winner_member, loser_member],
        "squad": {
            winner.player_id: (winner, 1),
            loser.player_id: (loser, 2),
        },
        "criteria": {1: 3, 2: 2, 3: 1},
        "world_phase": 2,
        "room_published": True,
        "created_monotonic": time.monotonic(),
    }
    protocol._matchmaking.matches[1] = dict(match_template)
    payload = bytes.fromhex(
        "080110021801200028a0c803300038004000"
        "4a1e08c38b0310001800200128c0b60b30f5fb03380040004800500058006000"
        "4a1e0891c71b10001801200228a0c80330a0c803380040004800500058006001"
    )
    replies = []
    pushes = []
    monkeypatch.setattr(protocol, "_send_game_reply", lambda *args: replies.append(args[-1]))
    monkeypatch.setattr(
        protocol,
        "_send_game_push",
        lambda target, payload, *, subtype=None: pushes.append((target, payload, subtype)),
    )
    packet = udp.ReliablePacket(3, 2, udp.DAT)
    for index, session in enumerate((winner, winner), 1):
        protocol._dispatch_game_message(
            session,
            packet,
            udp.GameMessage(udp.REGISTER_COMBAT_RESULT, index.to_bytes(4, "big"), payload, index),
        )
        if index == 1:
            reward_settings["world_normal_base_reward"] = 900_000
    assert len(replies) == 2 and replies[0] == replies[1]
    assert teams.balance("Winner") == 1_150_000
    assert teams.balance("Loser") == 1_000_000
    assert len(teams.state["world_battle_settlements"]) == 1

    # The first reporter can disconnect before a slower peer receives its
    # already-committed reply. Keep that peer's frozen result context.
    winner_peer = winner.peer
    winner.established = False
    winner.peer = None
    assert protocol._matchmaking._remove_matchmaking_member("Winner", reason="fin") == []
    assert 1 in protocol._matchmaking.matches
    protocol._dispatch_game_message(
        loser,
        packet,
        udp.GameMessage(udp.REGISTER_COMBAT_RESULT, (3).to_bytes(4, "big"), payload, 3),
    )
    assert len(replies) == 3
    winner_reward = decode_proto(first_field(decode_proto(replies[0]), 1).value)
    loser_reward = decode_proto(first_field(decode_proto(replies[2]), 1).value)
    assert first_field(winner_reward, 3).value == 150_000
    assert first_field(winner_reward, 7).value == 150_000
    assert first_field(loser_reward, 3).value == 0
    assert teams.balance("Winner") == 1_150_000
    assert teams.balance("Loser") == 1_000_000
    assert len(teams.state["world_battle_settlements"]) == 1

    reauthenticated = udp.GameSession(b"winner02", bytes(16), "Winner", winner.player_id)
    reauthenticated.established = True
    reauthenticated.peer = ("127.0.0.1", 51_001)
    reply_count = len(replies)
    protocol._dispatch_game_message(
        reauthenticated,
        packet,
        udp.GameMessage(udp.REGISTER_COMBAT_RESULT, (8).to_bytes(4, "big"), payload, 8),
    )
    assert len(replies) == reply_count  # A transport outside the auth directory is not current.
    protocol.sessions.pop(winner.auth_token)
    protocol.sessions[reauthenticated.auth_token] = reauthenticated
    protocol._dispatch_game_message(
        reauthenticated,
        packet,
        udp.GameMessage(udp.REGISTER_COMBAT_RESULT, (6).to_bytes(4, "big"), payload, 6),
    )
    assert replies[-1] == replies[0]
    assert teams.balance("Winner") == 1_150_000
    assert len(teams.state["world_battle_settlements"]) == 1

    # A replacement transport can never create a new payment if the durable
    # receipt is missing or damaged. This model-only corruption is restored.
    paid_state = deepcopy(teams.state)
    missing_receipt = deepcopy(paid_state)
    missing_receipt["world_battle_settlements"] = []
    teams.commit_state(missing_receipt)
    reply_count = len(replies)
    protocol._dispatch_game_message(
        reauthenticated,
        packet,
        udp.GameMessage(udp.REGISTER_COMBAT_RESULT, (7).to_bytes(4, "big"), payload, 7),
    )
    assert len(replies) == reply_count and teams.balance("Winner") == 1_150_000
    teams.commit_state(paid_state)

    # A second authenticated match may produce identical client report bytes.
    # Its fresh server nonce must not replay the first match's payment.
    protocol._matchmaking.matches[1]["created_monotonic"] -= 3601
    protocol._matchmaking._purge_stale_matchmaking()
    assert not protocol._matchmaking.matches
    protocol.sessions.pop(reauthenticated.auth_token)
    protocol.sessions[winner.auth_token] = winner
    winner.established = True
    winner.peer = winner_peer
    protocol._matchmaking.matches[2] = {
        **match_template,
        "match_id": 2,
        "created_monotonic": time.monotonic(),
    }
    reward_settings["world_normal_base_reward"] = 150_000
    protocol._dispatch_game_message(
        winner,
        packet,
        udp.GameMessage(udp.REGISTER_COMBAT_RESULT, (4).to_bytes(4, "big"), payload, 4),
    )
    assert teams.balance("Winner") == 1_300_000
    assert len(teams.state["world_battle_settlements"]) == 2
    assert len({row["result_digest"] for row in teams.state["world_battle_settlements"]}) == 2
    assert len(pushes) == len(replies)
    assert all(subtype == BATTLE_RESULT_FIXED_PUSH for _target, _payload, subtype in pushes)
    for _target, push, _subtype in pushes:
        fields = decode_proto(push)
        assert [(field.number, field.wire_type) for field in fields] == [
            (1, 0),
            (2, 0),
            (3, 0),
            (4, 0),
            (5, 2),
        ]
        assert all(field.value == 0 for field in fields[:4])
        assert {field.number for field in decode_proto(fields[4].value)} == set(range(1, 8))


def test_briefing_end_routes_to_native_empty_ack_instead_of_generic_stub(dispatcher):
    protocol, actor, packet, replies = dispatcher
    message = udp.GameMessage(udp.REPORT_BRIEFING_END, bytes(4), proto_varint(1, 64), 1)
    protocol._dispatch_game_message(actor, packet, message)
    assert replies == [(actor, packet, message, b"")]
    assert protocol.telemetry.rows[-1] == (
        "game_briefing_end_reported",
        {"peer": actor.peer, "online_id": "Actor", "detail64": 64},
    )
    assert not any(event == "game_request_stubbed" for event, _ in protocol.telemetry.rows)


def test_report_disconnected_routes_to_native_empty_ack_instead_of_generic_stub(dispatcher):
    protocol, actor, packet, replies = dispatcher
    message = udp.GameMessage(udp.REPORT_DISCONNECTED, bytes(4), bytes.fromhex("08351000"), 1)
    protocol._dispatch_game_message(actor, packet, message)
    assert replies == [(actor, packet, message, b"")]
    assert protocol.telemetry.rows[-1][0] == "game_disconnected_reported"
    assert protocol.telemetry.rows[-1][1]["reported_id"] == 53
    assert not any(event == "game_request_stubbed" for event, _ in protocol.telemetry.rows)


def test_regulation_without_a_trusted_source_fails_closed(dispatcher, monkeypatch):
    protocol, actor, packet, replies = dispatcher
    errors = []
    monkeypatch.setattr(protocol, "_send_game_reply", lambda *a, **kw: errors.append((a, kw)))
    message = udp.GameMessage(udp.CHECK_REGULATION_DATA, bytes(4), b"", 1)
    protocol._dispatch_game_message(actor, packet, message)
    assert not replies
    assert errors == [((actor, packet, message, b""), {"error": (1, 28)})]
    assert protocol.telemetry.rows[-1][0] == "game_regulation_data_unavailable"


def test_login_remembers_identity_then_replies_before_upload_config(dispatcher, monkeypatch):
    protocol, actor, packet, replies = dispatcher
    pushes = []

    def push(target, payload):
        assert replies and protocol.teams.player_profile("Actor")["player_id"] == 11
        pushes.append((target, payload))

    monkeypatch.setattr(protocol, "_send_game_push", push)
    message = udp.GameMessage(udp.WAIT_FOR_USER_LOGIN, bytes(4), b"", 1)
    protocol._dispatch_game_message(actor, packet, message)
    assert replies == [(actor, packet, message, proto_bytes(1, "Actor") + proto_varint(2, 11))]
    assert pushes == [
        (
            actor,
            proto_varint(1, 0x038C)
            + proto_bytes(2, proto_varint(2, 15))
            + proto_varint(3, 600)
            + proto_varint(4, 300),
        )
    ]
