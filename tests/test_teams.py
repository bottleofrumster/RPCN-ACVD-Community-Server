import json
import os
import time
import zlib
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.server import (
    GameDatagramProtocol,
    _ac_data_response_payload,
    _ai_info_equivalent,
    _canonical_faction_selection_id,
    _emblem_response_payload,
    _empty_ranking_list_response,
    _faction_selection,
    _faction_struggle_results,
    _item_simple_info_list,
    _load_game_sessions,
    _load_team_sessions,
    _match_room_info_push,
    _matching_result_push,
    _mercenary_continuation_pairs,
    _mercenary_message_list_response,
    _mercenary_rank_response,
    _mercenary_ranking_list_response,
    _mercenary_search_response,
    _mercenary_self_ranking_response,
    _merge_offline_inventory,
    _mission_savedata_equivalent,
    _mission_snapshot_summary,
    _my_player_ranking_response,
    _offline_inventory_summary,
    _own_unac_list_response,
    _persist_game_sessions,
    _register_emblem_response,
    _register_prepare_response,
    _remove_team_session_member_from_state,
    _replace_player_blob,
    _requested_ac_snapshot,
    _requested_ai_snapshot,
    _requested_mission_snapshot,
    _requested_team_for_emblem,
    _rpcn_create_room_tuple,
    _sync_offline_data_response,
    _team_id_search_response,
    _team_info,
    _team_info_list_response,
    _team_medal_info,
    _team_ranking_info,
    _team_record,
    _team_session_join_response,
    _team_session_leave_push,
    _team_session_member_push,
    _team_session_member_push_pair,
    _team_session_response,
    _territory_cannon_layout,
    _territory_info,
    _territory_layout_update_response,
    _typed_push_payload,
    _ugc_id_validation_response,
    _user_info,
    _vote_kick_member_response,
    _world_basic_info,
    _world_current_info,
    _world_log,
)
from acvd_server.teams import TeamStore
from acvd_server.telemetry import Telemetry
from acvd_server.udp import (
    ACK,
    CREATE_TEAM_SESSION,
    DAT,
    DAT_ACK,
    HBT,
    REGISTER_PREPARE,
    RETRANSMIT,
    GameMessage,
    GameSession,
    ReliablePacket,
    decode_reliable_packet,
    decrypt_server_datagram,
    encode_game_request,
    encode_reliable_packet,
    encrypt_client_datagram,
)

ROOT = Path(__file__).resolve().parents[1]


def test_reconnecting_peer_is_not_announced_before_own_team_bootstrap(tmp_path: Path) -> None:
    teams = TeamStore(tmp_path / "teams")
    team = teams.create(
        online_id="Leader",
        player_id=10,
        name="Shared",
        operation_base=2,
        language=0,
        play_type=0,
        enlistment_method=2,
    )
    assert teams.join_team(online_id="Helper", player_id=20, team_id=team["team_id"])
    leader = GameSession(
        b"leader01", bytes(16), "Leader", 10, established=True, peer=("127.0.0.1", 1)
    )
    helper = GameSession(
        b"helper01", bytes(16), "Helper", 20, established=True, peer=("127.0.0.1", 2)
    )
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        {leader.auth_token: leader, helper.auth_token: helper},
        tmp_path / "unused.so",
        tmp_path / "players",
        teams,
    )
    sid = team["team_id"]
    # The previous transport's roster entry survives authentication replacement.
    protocol.team_sessions = {
        sid: {
            "session_id": sid,
            "members": {
                "Helper": {"player_id": 20, "member_id": 2, "np_identity": b"old-helper-identity"},
            },
        }
    }
    pushes = []
    protocol._send_game_reply = lambda *args, **kwargs: None
    protocol._send_game_push = lambda session, payload, *, subtype=None: pushes.append(
        (session.online_id, subtype)
    )

    def create(session, sequence):
        request = GameMessage(
            CREATE_TEAM_SESSION,
            sequence.to_bytes(4, "big"),
            proto_bytes(1, b"\x01" + session.online_id.encode().ljust(36, b"\0")),
            sequence,
        )
        protocol._handle_game_payload(
            session, ReliablePacket(sequence, 0, DAT, payload=encode_game_request(request))
        )

    create(leader, 1)
    assert leader.team_session_ready
    assert not helper.team_session_ready
    assert pushes == []
    create(helper, 2)
    assert helper.team_session_ready
    assert pushes == []
    assert protocol._session_lifecycle.flush_deferred_team_session_pushes(
        helper, trigger="team_session_validated"
    ) == 1
    assert pushes == [("Helper", 9)]
    assert protocol._session_lifecycle.flush_deferred_team_session_pushes(
        leader, trigger="test_world"
    ) == 1
    assert sorted(pushes) == [("Helper", 9), ("Leader", 9)]
    # Readiness belongs to this authenticated session, not the account name.
    fresh = GameSession(
        b"helper02", bytes(16), "Helper", 20, established=True, peer=("127.0.0.1", 3)
    )
    protocol.sessions.pop(helper.auth_token)
    protocol.sessions[fresh.auth_token] = fresh
    pushes.clear()
    create(leader, 3)
    assert pushes == []


def test_team_readiness_persists_without_promoting_fresh_transport(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    session = GameSession(
        b"pilot001",
        bytes(16),
        "Pilot",
        42,
        established=True,
        peer=("127.0.0.1", 1),
        last_seen_unix=1000,
    )
    roster = {
        7: {
            "session_id": 7,
            "members": {
                "Pilot": {
                    "player_id": 42,
                    "member_id": 1,
                    "np_identity": b"identity",
                }
            },
        }
    }
    for ready in (False, True):
        session.team_session_ready = ready
        _persist_game_sessions(path, {session.auth_token: session}, roster)
        restored = _load_game_sessions(path, now_unix=1001)
        _load_team_sessions(path, restored)
        assert restored[session.auth_token].team_session_ready is ready
    # Migrate old snapshots only when their retained membership is valid.
    document = json.loads(path.read_text())
    document["sessions"][0].pop("team_session_ready")
    path.write_text(json.dumps(document))
    restored = _load_game_sessions(path, now_unix=1001)
    assert not restored[session.auth_token].team_session_ready
    _load_team_sessions(path, restored)
    assert restored[session.auth_token].team_session_ready


def test_game_session_state_round_trip_is_private_and_expiring(tmp_path: Path) -> None:
    state_path = tmp_path / "sessions" / "active.json"
    token = bytes.fromhex("0102030405060708")
    key = bytes(range(16))
    original = GameSession(
        token,
        key,
        "Pilot",
        42,
        server_sequence=248,
        remote_sequence=77,
        established=True,
        peer=("127.0.0.1", 54321),
        fragment_counter=58,
        last_seen_unix=1_000.0,
        outbound_reliable={248: ReliablePacket(248, 77, DAT_ACK, payload=b"pending reply")},
    )
    older_token = bytes.fromhex("0002030405060708")
    older = GameSession(
        older_token,
        bytes(range(16)),
        "Pilot",
        42,
        last_seen_unix=900.0,
    )
    team_sessions = {
        7: {
            "session_id": 7,
            "team_id": 7,
            "members": {
                "Pilot": {
                    "player_id": 42,
                    "member_id": 2,
                    "np_identity": b"\x01" + b"Pilot".ljust(36, b"\0"),
                }
            },
        }
    }
    _persist_game_sessions(
        state_path,
        {older_token: older, token: original},
        team_sessions,
    )

    assert os.stat(state_path).st_mode & 0o777 == 0o600
    assert os.stat(state_path.parent).st_mode & 0o777 == 0o700
    restored = _load_game_sessions(state_path, now_unix=1_005.0)
    assert list(restored) == [token]
    session = restored[token]
    assert session.game_key == key
    assert session.online_id == "Pilot"
    assert session.player_id == 42
    assert session.server_sequence == 248
    assert session.remote_sequence == 77
    assert session.fragment_counter == 58
    assert session.outbound_reliable == {
        248: ReliablePacket(248, 77, DAT_ACK, payload=b"pending reply")
    }
    assert session.established is True
    assert session.peer == ("127.0.0.1", 54321)
    restored_team_sessions = _load_team_sessions(state_path, restored)
    assert restored_team_sessions[7]["team_id"] == 7
    assert restored_team_sessions[7]["members"]["Pilot"] == {
        "player_id": 42,
        "member_id": 2,
        "np_identity": b"\x01" + b"Pilot".ljust(36, b"\0"),
    }
    assert _load_game_sessions(state_path, now_unix=2_000.0, max_age_seconds=60.0) == {}

    state_path.write_text("not-json", encoding="utf-8")
    assert _load_game_sessions(state_path) == {}


def test_restored_game_session_accepts_encrypted_heartbeat(tmp_path: Path) -> None:
    state_path = tmp_path / "sessions" / "active.json"
    token = bytes.fromhex("1112131415161718")
    key = bytes(reversed(range(16)))
    original = GameSession(
        token,
        key,
        "Pilot",
        42,
        server_sequence=248,
        remote_sequence=247,
        established=True,
    )
    _persist_game_sessions(state_path, {token: original})
    sessions = _load_game_sessions(state_path)
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        sessions,
        ROOT / "native/libacvd_cwc.so",
        tmp_path / "players",
        TeamStore(tmp_path / "teams"),
        lambda: _persist_game_sessions(state_path, sessions),
    )

    class CaptureTransport:
        def __init__(self) -> None:
            self.sent: list[tuple[bytes, tuple[str, int]]] = []

        def sendto(self, payload: bytes, peer: tuple[str, int]) -> None:
            self.sent.append((payload, peer))

    transport = CaptureTransport()
    protocol.connection_made(transport)
    heartbeat = encode_reliable_packet(ReliablePacket(0, 247, HBT))
    protocol.datagram_received(
        encrypt_client_datagram(
            heartbeat,
            key,
            token,
            0,
            ROOT / "native/libacvd_cwc.so",
        ),
        ("127.0.0.1", 54321),
    )

    assert len(transport.sent) == 1
    encrypted_reply, peer = transport.sent[0]
    reply = decode_reliable_packet(
        decrypt_server_datagram(encrypted_reply, key, ROOT / "native/libacvd_cwc.so")
    )
    assert peer == ("127.0.0.1", 54321)
    assert reply == ReliablePacket(0, 0, HBT)
    reloaded = _load_game_sessions(state_path)
    assert reloaded[token].peer == ("127.0.0.1", 54321)
    assert reloaded[token].server_sequence == 248


def test_opcode_three_retransmits_first_sequence_after_cumulative_ack(tmp_path: Path) -> None:
    token = bytes.fromhex("2122232425262728")
    key = bytes(range(16))
    session = GameSession(
        token,
        key,
        "Pilot",
        42,
        server_sequence=832,
        remote_sequence=3897,
        established=True,
        peer=("127.0.0.1", 54321),
    )
    sessions = {token: session}
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        sessions,
        ROOT / "native/libacvd_cwc.so",
        tmp_path / "players",
        TeamStore(tmp_path / "teams"),
        lambda: None,
    )

    class CaptureTransport:
        def __init__(self) -> None:
            self.sent: list[tuple[bytes, tuple[str, int]]] = []

        def sendto(self, payload: bytes, peer: tuple[str, int]) -> None:
            self.sent.append((payload, peer))

    transport = CaptureTransport()
    protocol.connection_made(transport)
    missing = ReliablePacket(831, 3897, DAT_ACK, payload=b"missing reply")
    protocol._send(session, missing)
    assert list(session.outbound_reliable) == [831]
    transport.sent.clear()

    retransmit = encode_reliable_packet(ReliablePacket(0, 830, RETRANSMIT))
    protocol.datagram_received(
        encrypt_client_datagram(
            retransmit,
            key,
            token,
            0,
            ROOT / "native/libacvd_cwc.so",
        ),
        session.peer,
    )

    assert len(transport.sent) == 1
    replay = decode_reliable_packet(
        decrypt_server_datagram(transport.sent[0][0], key, ROOT / "native/libacvd_cwc.so")
    )
    assert replay == missing
    assert list(session.outbound_reliable) == [831]
    assert session.remote_sequence == 3897

    transport.sent.clear()
    acknowledgement = encode_reliable_packet(ReliablePacket(0, 831, ACK))
    protocol.datagram_received(
        encrypt_client_datagram(
            acknowledgement,
            key,
            token,
            0,
            ROOT / "native/libacvd_cwc.so",
        ),
        session.peer,
    )
    assert transport.sent == []
    assert session.outbound_reliable == {}
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    replay_events = [
        event for event in events if event["event"] == "game_reliable_packet_retransmitted"
    ]
    assert len(replay_events) == 1
    assert replay_events[0]["acknowledged_sequence"] == 830
    assert replay_events[0]["retransmitted_sequence"] == 831
    assert replay_events[0]["retransmitted_opcode"] == DAT_ACK


def test_team_store_persists_membership_and_emblem(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    team = store.create(
        online_id="Pilot",
        player_id=42,
        name="test",
        operation_base=2,
        language=2,
        play_type=1,
        enlistment_method=1,
    )
    assert team["team_id"] == 1
    emblem_path = store.save_emblem("Pilot", 170400, b"emblem")
    assert emblem_path is not None and emblem_path.read_bytes() == b"emblem"

    reloaded = TeamStore(tmp_path).team_for("Pilot")
    assert reloaded is not None
    assert reloaded["name"] == "test"
    assert reloaded["emblem_ugc_id"] == 170400
    store.set_faction("Pilot", 1, 1)
    reloaded = TeamStore(tmp_path).team_for("Pilot")
    assert reloaded is not None
    assert reloaded["world_id"] == 1
    assert reloaded["faction_id"] == 1
    assert reloaded["territory_area_id"] == 1
    store.promote_faction("Pilot")
    reloaded = TeamStore(tmp_path).team_for("Pilot")
    assert reloaded is not None
    assert reloaded["current_faction_id"] == 1
    assert reloaded["territory_area_id"] == 1

    layout_path = store.save_territory_layout(
        "Pilot",
        season_id=3,
        player_id=42,
        area_id=1,
        payload=b"opaque-layout",
    )
    assert layout_path is not None and layout_path.read_bytes() == b"opaque-layout"
    reloaded_store = TeamStore(tmp_path)
    assert reloaded_store.load_territory_layout("Pilot", season_id=3, area_id=1) == b"opaque-layout"

    updated = store.update_leader_settings(
        "Pilot",
        pay_apportionment=0,
        leader_identity=b"Pilot",
        enlistment_method=2,
        profile_text=b"line one\nline two",
    )
    assert updated is not None
    reloaded = TeamStore(tmp_path).team_for("Pilot")
    assert reloaded is not None
    assert reloaded["play_type"] == 1
    assert reloaded["pay_apportionment"] == 0
    assert reloaded["comment"] == ""
    assert reloaded["leader_update_identity"] == "Pilot"
    assert reloaded["enlistment_method"] == 2
    assert reloaded["recruitment_open"] is True
    assert reloaded["profile_text"] == "line one\nline two"
    assert store.search_team_ids(name="TEST") == [1]
    assert store.search_team_ids(name="missing") == []

    changed = store.update_team_properties(
        "Pilot",
        scalar_fields={13: 1, 14: 15, 25: 1, 26: 2, 27: 6},
        color=b"\x08\x01\x10\x02\x18\x03\x20\x04",
        comment=b"story and co-op",
    )
    assert changed is not None
    reloaded = TeamStore(tmp_path).team_for("Pilot")
    assert reloaded["pay_apportionment"] == 0
    assert reloaded["play_type"] == 1
    assert reloaded["desired_faction_id"] == 2
    assert reloaded["desired_area_id"] == 6
    assert reloaded["soldier_id"] == 15
    assert reloaded["headquarters_id"] == 1
    assert reloaded["comment"] == "story and co-op"
    assert reloaded["team_color_b64"] == "CAEQAhgDIAQ="

    profile = store.update_user_property("Pilot", b"ready for sorties")
    assert profile["status"] == "ready for sorties"
    assert TeamStore(tmp_path).player_profile("Pilot")["status"] == "ready for sorties"

    store.register_mercenary(
        online_id="Pilot",
        player_id=42,
        ac_data=b"\x01compressed-ac",
        result_values=[0, 0, 1, 3],
    )
    mercenaries = TeamStore(tmp_path).available_mercenaries()
    assert mercenaries[0]["online_id"] == "Pilot"
    assert mercenaries[0]["ac_data"] == b"\x01compressed-ac"
    assert mercenaries[0]["result_values"] == [0, 0, 1, 3]
    assert store.mercenary_for_player_id(42)["online_id"] == "Pilot"
    assert store.mercenary_for_player_id(9999) is None
    queued = store.queue_mercenary_message(
        target_player_id=42,
        employer_online_id="Employer",
        employer_player_id=99,
        employer_team_name="Clients",
    )
    assert queued is not None
    messages = store.mercenary_messages_for("Pilot")
    response = decode_proto(_mercenary_message_list_response(messages))
    encoded_message = decode_proto(first_field(response, 1).value)
    assert [field.number for field in encoded_message] == [1, 2, 3, 4, 5, 7]
    matching = decode_proto(first_field(encoded_message, 2).value)
    assert [field.number for field in matching] == list(range(1, 8))
    assert store.cancel_mercenary_messages("Employer") == 1
    assert store.mercenary_messages_for("Pilot") == []
    assert store.unregister_mercenary("Pilot") is True
    assert store.unregister_mercenary("Pilot") is False
    assert store.available_mercenaries() == []


def test_mercenary_message_rejects_same_team_employer(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    team = store.create(
        online_id="Pilot",
        player_id=42,
        name="Shared",
        operation_base=2,
        language=0,
        play_type=0,
        enlistment_method=2,
    )
    assert store.join_team(online_id="Employer", player_id=99, team_id=team["team_id"])
    store.register_mercenary(
        online_id="Pilot",
        player_id=42,
        ac_data=b"ac",
        result_values=[0, 0, 1, 3],
    )

    assert (
        store.queue_mercenary_message(
            target_player_id=42,
            employer_online_id="Employer",
            employer_player_id=99,
            employer_team_name="Shared",
        )
        is None
    )
    assert store.mercenary_messages_for("Pilot") == []


def test_team_join_persists_members_and_team_info(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    team = store.create(
        online_id="Leader",
        player_id=10,
        name="Open Team",
        operation_base=2,
        language=0,
        play_type=0,
        enlistment_method=2,
    )

    joined = store.join_team(online_id="Helper", player_id=20, team_id=team["team_id"])
    assert joined is not None
    reloaded = TeamStore(tmp_path)
    assert reloaded.team_for("Helper")["team_id"] == team["team_id"]
    assert reloaded.player_profile("Helper")["member_id"] == 2

    fields = decode_proto(_team_info(reloaded.team_for("Leader"), datetime.now(timezone.utc)))
    members = [decode_proto(field.value) for field in fields if field.number == 28]
    assert [first_field(member, 1).value for member in members] == [10, 20]
    assert [first_field(member, 2).value for member in members] == [1, 1]
    assert [first_field(member, 4).value for member in members] == [1, 2]
    member_record = reloaded.member_by_player_id(20)
    assert member_record is not None and member_record[0] == "Helper"


def test_money_sync_and_transfer_are_persistent_and_atomic(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    store.remember_player("Sender", 10)
    store.remember_player("Recipient", 20)

    # Legacy enrollment adopts the exact local balance rather than replacing it.
    assert store.synchronize_balance("Sender", 100_000) == 100_000
    assert store.synchronize_balance("Sender", 900_000) == 900_000
    assert store.synchronize_balance("Recipient", 50_000) == 50_000
    assert store.synchronize_balance("Recipient", 75_000) == 75_000

    transfer = store.transfer_money("Sender", 20, 250_000)
    assert transfer == {
        "sender_online_id": "Sender",
        "recipient_online_id": "Recipient",
        "recipient_player_id": 20,
        "amount": 250_000,
        "sender_balance": 650_000,
        "recipient_balance": 325_000,
    }

    reloaded = TeamStore(tmp_path)
    assert reloaded.balance("Sender") == 650_000
    assert reloaded.balance("Recipient") == 325_000
    assert reloaded.state["money_transfers"] == [transfer]

    before = reloaded.state_path.read_bytes()
    assert reloaded.transfer_money("Sender", 10, 1) is None
    assert reloaded.transfer_money("Sender", 999, 1) is None
    assert reloaded.transfer_money("Sender", 20, 650_001) is None
    assert reloaded.transfer_money("Sender", 20, 0) is None
    assert reloaded.state_path.read_bytes() == before


def test_battle_money_settlement_is_net_persistent_and_idempotent(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    store.synchronize_balance("Pilot", 1_000_000)

    won = store.settle_battle_money(
        "Pilot",
        "result-a",
        mission_id=110,
        gross_earnings=18_184,
        expenses=6_360,
    )
    assert won["applied"] is True
    assert won["delta"] == 11_824
    assert won["balance"] == 1_011_824

    replay = store.settle_battle_money(
        "Pilot",
        "result-a",
        mission_id=110,
        gross_earnings=18_184,
        expenses=6_360,
    )
    assert replay["applied"] is False
    assert store.balance("Pilot") == 1_011_824

    loss = store.settle_battle_money(
        "Pilot",
        "result-b",
        mission_id=2_000,
        gross_earnings=0,
        expenses=2_200,
    )
    assert loss["delta"] == -2_200
    assert loss["balance"] == 1_009_624

    reloaded = TeamStore(tmp_path)
    assert reloaded.balance("Pilot") == 1_009_624
    assert len(reloaded.state["battle_settlements"]) == 2


def test_authenticated_player_identity_is_available_without_team_membership(
    tmp_path: Path,
) -> None:
    store = TeamStore(tmp_path)
    store.remember_player("Solo", 77)
    assert TeamStore(tmp_path).member_by_player_id(77) == ("Solo", {})


def test_leave_team_deletes_last_member_and_resets_profile(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    team = store.create(
        online_id="Leader",
        player_id=10,
        name="Solo",
        operation_base=2,
        language=0,
        play_type=0,
        enlistment_method=2,
    )
    result = store.leave_team("Leader")
    assert result == {
        "team_id": team["team_id"],
        "team_deleted": True,
        "leader_promoted": "",
    }
    assert TeamStore(tmp_path).team_for("Leader") is None
    assert TeamStore(tmp_path).player_profile("Leader")["member_id"] == 0


def test_leave_team_promotes_remaining_member(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    team = store.create(
        online_id="Leader",
        player_id=10,
        name="Open",
        operation_base=2,
        language=0,
        play_type=0,
        enlistment_method=2,
    )
    store.join_team(online_id="Helper", player_id=20, team_id=team["team_id"])
    result = store.leave_team("Leader")
    assert result["team_deleted"] is False
    assert result["leader_promoted"] == "Helper"
    reloaded = TeamStore(tmp_path).team_for("Helper")
    assert reloaded["leader_online_id"] == "Helper"
    assert reloaded["leader_player_id"] == 20
    assert reloaded["member_records"][0]["leader"] is True


def test_user_info_maps_member_profile_fields() -> None:
    team = {
        "team_id": 17,
        "operation_base": 2,
        "language": 2,
        "play_type": 3,
    }
    fields = decode_proto(_user_info(20, "Helper", team, "ready", 2, 1, False, True))
    assert first_field(fields, 1).value == 20
    assert first_field(fields, 2).value == b"Helper"
    assert first_field(fields, 6).value == 17
    assert first_field(fields, 7).value == 0
    assert first_field(fields, 9).value == 2
    assert first_field(fields, 10).value == 1
    assert first_field(fields, 11).value == 3
    assert first_field(fields, 12).value == 2
    assert first_field(fields, 13).value == 1
    assert first_field(fields, 14).value == 0
    assert first_field(fields, 15).value == 1


def test_member_team_info_persists_in_profile_and_team_record(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    store.create(
        online_id="Leader",
        player_id=10,
        name="Slots",
        operation_base=2,
        language=2,
        play_type=0,
        enlistment_method=2,
    )
    profile = store.update_member_team_info("Leader", member_id=2, secondary=4)
    assert profile["member_id"] == 2
    assert profile["member_team_secondary"] == 4

    reloaded = TeamStore(tmp_path)
    assert reloaded.player_profile("Leader")["member_id"] == 2
    team = reloaded.team_for("Leader")
    assert team["member_records"][0]["member_id"] == 2
    fields = decode_proto(_team_info(team, datetime.now(timezone.utc)))
    member = decode_proto(first_field(fields, 28).value)
    assert first_field(member, 3).value == 4
    assert first_field(member, 4).value == 2


def test_member_number_update_rejects_a_teammates_existing_number_atomically(
    tmp_path: Path,
) -> None:
    store = TeamStore(tmp_path)
    team = store.create(
        online_id="Leader",
        player_id=10,
        name="Unique Numbers",
        operation_base=0,
        language=0,
        play_type=1,
        enlistment_method=2,
    )
    assert store.join_team(online_id="Joiner", player_id=20, team_id=team["team_id"])
    store.update_member_team_info("Leader", member_id=11, secondary=0)
    assert store.player_profile("Joiner")["member_id"] == 2
    before = deepcopy(store.state)
    disk = store.state_path.read_bytes()
    with pytest.raises(ValueError, match="already in use"):
        store.update_member_team_info("Joiner", member_id=11, secondary=0)
    assert store.state == before and store.state_path.read_bytes() == disk
    records = {row["online_id"]: row["member_id"] for row in team["member_records"]}
    assert records == {"Leader": 11, "Joiner": 2}


def test_team_join_migrates_legacy_leader_member(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    team = store.create(
        online_id="Leader",
        player_id=10,
        name="Legacy Team",
        operation_base=2,
        language=0,
        play_type=0,
        enlistment_method=2,
    )
    team.pop("member_records")
    store._save()

    joined = store.join_team(online_id="Helper", player_id=20, team_id=team["team_id"])
    assert joined is not None
    assert joined["member_records"] == [
        {
            "online_id": "Leader",
            "player_id": 10,
            "leader": True,
            "member_status": 1,
            "member_id": 1,
            "member_team_secondary": 0,
        },
        {
            "online_id": "Helper",
            "player_id": 20,
            "leader": False,
            "member_status": 1,
            "member_id": 2,
            "member_team_secondary": 0,
        },
    ]


def test_team_emblem_lookup_uses_requested_team_not_viewer_team(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    viewer = store.create(
        online_id="Viewer",
        player_id=10,
        name="Viewer Team",
        operation_base=2,
        language=0,
        play_type=0,
        enlistment_method=2,
    )
    target = store.create(
        online_id="Target",
        player_id=20,
        name="Target Team",
        operation_base=2,
        language=0,
        play_type=0,
        enlistment_method=2,
    )

    assert _requested_team_for_emblem(store, "Viewer", proto_varint(1, target["team_id"])) is target
    assert _requested_team_for_emblem(store, "Viewer", b"") is viewer
    assert _requested_team_for_emblem(store, "Viewer", proto_varint(1, 9999)) is None


def test_closed_team_is_not_searchable_or_auto_joinable(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    team = store.create(
        online_id="Leader",
        player_id=10,
        name="Closed Team",
        operation_base=2,
        language=0,
        play_type=0,
        enlistment_method=0,
    )
    assert store.search_team_ids() == []
    assert store.search_team_ids(open_only=False) == [team["team_id"]]
    assert store.join_team(online_id="Helper", player_id=20, team_id=team["team_id"]) is None


def test_auto_join_transfers_existing_member_and_deletes_abandoned_team(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    old_team = store.create(
        online_id="Mover",
        player_id=20,
        name="Old Team",
        operation_base=2,
        language=0,
        play_type=0,
        enlistment_method=2,
    )
    target = store.create(
        online_id="TargetLeader",
        player_id=10,
        name="Target Team",
        operation_base=2,
        language=0,
        play_type=0,
        enlistment_method=2,
    )

    joined = store.join_team(
        online_id="Mover",
        player_id=20,
        team_id=target["team_id"],
        replace_existing=True,
    )

    assert joined is not None
    assert store.team_by_id(old_team["team_id"]) is None
    assert store.team_for("Mover")["team_id"] == target["team_id"]
    assert [record["online_id"] for record in joined["member_records"]] == [
        "TargetLeader",
        "Mover",
    ]
    assert store.player_profile("Mover")["member_id"] == 2


def test_auto_join_transfer_promotes_old_team_member(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    old_team = store.create(
        online_id="OldLeader",
        player_id=10,
        name="Old Team",
        operation_base=2,
        language=0,
        play_type=0,
        enlistment_method=2,
    )
    store.join_team(online_id="Remaining", player_id=11, team_id=old_team["team_id"])
    target = store.create(
        online_id="TargetLeader",
        player_id=20,
        name="Target Team",
        operation_base=2,
        language=0,
        play_type=0,
        enlistment_method=2,
    )

    assert (
        store.join_team(
            online_id="OldLeader",
            player_id=10,
            team_id=target["team_id"],
            replace_existing=True,
        )
        is not None
    )

    retained = store.team_by_id(old_team["team_id"])
    assert retained["leader_online_id"] == "Remaining"
    assert retained["leader_player_id"] == 11
    assert retained["member_records"][0]["leader"] is True


def test_sparse_team_property_updates_merge_without_resetting(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    store.create(
        online_id="Leader",
        player_id=10,
        name="Settings",
        operation_base=2,
        language=2,
        play_type=0,
        enlistment_method=2,
    )
    store.update_team_properties("Leader", scalar_fields={13: 1})
    store.update_team_properties("Leader", scalar_fields={8: 2})
    store.update_team_properties("Leader", scalar_fields={14: 15})
    store.update_team_properties("Leader", scalar_fields={20: 11})
    store.update_team_properties("Leader", scalar_fields={23: 1})
    store.update_team_properties("Leader", scalar_fields={25: 1})
    store.update_team_properties("Leader", scalar_fields={26: 2})
    store.update_team_properties("Leader", scalar_fields={27: 6})
    store.update_team_properties(
        "Leader",
        scalar_fields={},
        color=b"\x08\x01\x10\x02\x18\x03\x20\x04",
    )
    team = TeamStore(tmp_path).team_for("Leader")
    assert team["play_type"] == 0
    assert team["operation_base"] == 1
    assert team["language"] == 2
    assert team["desired_faction_id"] == 2
    assert team["desired_area_id"] == 6
    assert team["soldier_id"] == 15
    assert team["title_id"] == 1
    assert team["headquarters_id"] == 1
    assert team["changeable_properties"] == {
        "8": 2,
        "13": 1,
        "14": 15,
        "20": 11,
        "23": 1,
        "25": 1,
        "26": 2,
        "27": 6,
    }
    fields = decode_proto(_team_info(team, datetime.now(timezone.utc)))
    assert first_field(fields, 9).value == 0
    assert first_field(fields, 11).value == 2
    assert first_field(fields, 23) is None
    assert first_field(fields, 12).value == 1
    assert first_field(fields, 13).value == 0
    assert first_field(fields, 10).value == 1
    assert first_field(fields, 21).value == 15
    assert first_field(fields, 25).value == 11
    assert first_field(fields, 27).value == 2
    assert first_field(fields, 29).value == 6
    assert first_field(fields, 31).value == 1


def test_legacy_team_schema_is_normalized_on_load(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    team = store.create(
        online_id="Leader",
        player_id=10,
        name="Legacy",
        operation_base=2,
        language=0,
        play_type=0,
        enlistment_method=0,
    )
    team.pop("member_records")
    team.pop("recruitment_open")
    team["faction_id"] = 3
    team["current_faction_id"] = 0
    team.pop("desired_faction_id")
    team.pop("headquarters_id")
    team.pop("title_id", None)
    team["changeable_properties"] = {"25": 3}
    store._save()

    normalized = TeamStore(tmp_path).team_for("Leader")
    assert normalized["recruitment_open"] is False
    assert normalized["current_faction_id"] == 3
    assert normalized["desired_faction_id"] == 3
    assert "headquarters_id" not in normalized
    assert normalized["member_records"] == [
        {
            "online_id": "Leader",
            "player_id": 10,
            "leader": True,
            "member_status": 1,
            "member_id": 1,
            "member_team_secondary": 0,
        }
    ]


def test_legacy_sparse_editor_state_repairs_comment_and_independent_fields(
    tmp_path: Path,
) -> None:
    store = TeamStore(tmp_path)
    team = store.create(
        online_id="FirstDystopian",
        player_id=12,
        name="Behemoth",
        operation_base=2,
        language=2,
        play_type=0,
        enlistment_method=2,
    )
    team.pop("soldier_id")
    team.pop("title_id")
    team.pop("headquarters_id")
    team.pop("desired_area_id")
    team["comment"] = "FirstDystopian"
    team["team_profile_text"] = "Ignonomy"
    team["profile_text"] = "Please don't look.\n\n\n\n\n"
    team["changeable_properties"] = {
        "14": 15,
        "13": 1,
        "25": 1,
        "26": 2,
        "27": 4,
    }
    team["desired_faction_id"] = 2
    store._save()

    normalized = TeamStore(tmp_path).team_for("FirstDystopian")
    assert normalized["comment"] == "Ignonomy"
    assert normalized["profile_text"] == "Please don't look.\n\n\n\n\n"
    assert normalized["soldier_id"] == 15
    assert normalized["headquarters_id"] == 1
    assert "title_id" not in normalized  # Recovered in readback without a startup rewrite.
    assert normalized["desired_faction_id"] == 2
    assert normalized["desired_area_id"] == 4

    fields = decode_proto(_team_info(normalized, datetime.now(timezone.utc)))
    assert first_field(fields, 10).value == 2
    assert first_field(fields, 14).value == b"Ignonomy"
    assert first_field(fields, 21).value == 15
    assert first_field(fields, 25).value == 0
    assert first_field(fields, 26).value == b"Please don't look.\n\n\n\n\n"
    assert first_field(fields, 29).value == 4
    assert first_field(fields, 31).value == 1


def test_legacy_leader_identity_comment_is_cleared_when_text_was_not_retained(
    tmp_path: Path,
) -> None:
    store = TeamStore(tmp_path)
    team = store.create(
        online_id="Leader",
        player_id=10,
        name="Legacy Comment",
        operation_base=2,
        language=2,
        play_type=0,
        enlistment_method=2,
    )
    team["comment"] = "Leader"
    store._save()

    normalized = TeamStore(tmp_path).team_for("Leader")
    assert normalized["comment"] == ""


def test_register_prepare_response_is_fully_initialized() -> None:
    fields = decode_proto(_register_prepare_response(7, state=1, token="Pilot:7"))
    assert [(field.number, field.value) for field in fields] == [
        (1, 7),
        (2, 7),
        (3, 1),
        (4, b"Pilot:7"),
    ]


def test_match_pushes_use_retail_subtype_prefix_and_required_fields() -> None:
    matching = _typed_push_payload(
        2,
        _matching_result_push(
            7,
            9,
            is_host=True,
            criteria={4: 1, 5: 0, 8: 3, 9: 0, 10: 1},
        ),
    )
    assert matching[:4] == b"\x00\x00\x00\x01"
    matching_fields = decode_proto(matching[4:])
    assert [field.number for field in matching_fields] == list(range(1, 10))
    assert [field.value for field in matching_fields] == [7, 9, 1, 0, 0, 0, 0, 0, 1]

    room = _match_room_info_push(
        recipient_prepare_id=9,
        room_id=55,
        room_password=b"PRVTPSWD",
        room_owner_online_id="ExampleRoomHost_",
    )
    fields = decode_proto(room)
    assert [(field.number, field.value) for field in fields] == [
        (1, 9),
        (2, 55),
        (
            3,
            b"\x00NXRV\x03\xf2"
            + b"\x00" * 3
            + b"ExampleRoomHost_"
            + b"\x00\x10"
            + b"PRVTPSWD"
            + (55).to_bytes(8, "big")
            + b"\x00" * 469,
        ),
    ]
    typed_room = _typed_push_payload(5, room)
    assert typed_room[:4] == b"\x00\x00\x00\x04"
    assert typed_room[4:] == room


def test_rpcn_create_room_tuple_uses_latest_complete_response() -> None:
    log_text = """
sceNp2: SceNpMatching2CreateJoinRoomResponse:
sceNp2: serverId: 1
sceNp2: worldId: 65537
sceNp2: roomId: 98
sceNp2: SceNpMatching2CreateJoinRoomResponse:
sceNp2: serverId: 2
sceNp2: worldId: 65538
"""
    assert _rpcn_create_room_tuple(log_text) is None
    assert _rpcn_create_room_tuple(log_text + "sceNp2: roomId: 99\n") == {
        1: 2,
        2: 65538,
        3: 99,
    }


def test_new_player_ac_data_keeps_required_encoding_byte() -> None:
    default_payload = _ac_data_response_payload(b"")
    assert default_payload[:1] == b"\x00"
    assert len(default_payload) == 26121
    assert _ac_data_response_payload(b"\x01" + zlib.compress(b"snapshot")) == b"\x00snapshot"


def test_requested_ac_snapshot_loads_self_and_remote_storage(tmp_path: Path) -> None:
    (tmp_path / "42.ac-data.bin").write_bytes(b"self")
    (tmp_path / "77.ac-data.bin").write_bytes(b"remote")
    (tmp_path / "42.ac-data.preseed.bin").write_bytes(b"preseed")
    self_payload, self_preseed = _requested_ac_snapshot(
        tmp_path, requester_player_id=42, requested_player_id=42
    )
    remote_payload, remote_preseed = _requested_ac_snapshot(
        tmp_path, requester_player_id=42, requested_player_id=77
    )
    assert self_payload == b"preseed"
    assert self_preseed == tmp_path / "42.ac-data.preseed.bin"
    assert remote_payload == b"remote"
    assert remote_preseed is None

    (tmp_path / "42.ac-data.preseed.bin").unlink()
    assert _requested_ac_snapshot(tmp_path, requester_player_id=42, requested_player_id=42) == (
        b"self",
        None,
    )


def test_requested_ai_snapshot_limits_preseed_to_self(tmp_path: Path) -> None:
    (tmp_path / "42.ai-3.bin").write_bytes(b"self-live")
    (tmp_path / "77.ai-3.bin").write_bytes(b"remote-live")
    (tmp_path / "42.ai-3.preseed.bin").write_bytes(b"self-preseed")

    assert _requested_ai_snapshot(
        tmp_path,
        requester_player_id=42,
        requested_player_id=42,
        slot=3,
    ) == (b"self-preseed", tmp_path / "42.ai-3.preseed.bin")
    assert _requested_ai_snapshot(
        tmp_path,
        requester_player_id=42,
        requested_player_id=77,
        slot=3,
    ) == (b"remote-live", None)


def test_requested_mission_snapshot_prefers_reviewed_preseed(tmp_path: Path) -> None:
    (tmp_path / "42.mission-savedata.bin").write_bytes(b"live")
    preseed = tmp_path / "42.mission-savedata.preseed.bin"
    preseed.write_bytes(b"preseed")

    assert _requested_mission_snapshot(tmp_path, player_id=42) == (b"preseed", preseed)
    preseed.unlink()
    assert _requested_mission_snapshot(tmp_path, player_id=42) == (b"live", None)


def test_mission_savedata_equivalence_uses_decompressed_semantics() -> None:
    raw = bytes((index * 29) & 0xFF for index in range(1184))
    first = b"\x01" + zlib.compress(raw, level=1)
    second = b"\x01" + zlib.compress(raw, level=9)
    uncompressed = b"\x00" + raw
    conflicting = b"\x01" + zlib.compress(raw[:-1] + b"\xff")

    assert first != second
    assert _mission_savedata_equivalent(first, second) is True
    assert _mission_savedata_equivalent(first, uncompressed) is True
    assert _mission_savedata_equivalent(first, conflicting) is False
    assert _mission_savedata_equivalent(first, b"bad") is False


def test_mission_snapshot_summary_reports_bounded_semantic_deltas() -> None:
    old_raw = bytes(1184)
    new_raw = bytearray(old_raw)
    new_raw[0] = 1
    new_raw[32 + 7 * 6 + 2] = 6
    new_raw[32 + 3 * 18 + 5] = 9
    old = b"\x01" + zlib.compress(old_raw)
    new = b"\x01" + zlib.compress(bytes(new_raw))

    summary = _mission_snapshot_summary(new, old)

    assert summary["encoding_valid"] is True
    assert summary["layout_valid"] is True
    assert summary["raw_length"] == 1184
    assert summary["nonzero_flag_indices"] == [0]
    assert summary["nonzero_result_slot_indices"] == [7, 9]
    assert summary["nonzero_record_indices"] == [2, 3]
    assert summary["grade_counts"] == {"D": 0, "C": 0, "B": 0, "A": 0, "S": 1}
    assert summary["changed_byte_count"] == 3
    assert summary["changed_flag_indices"] == [0]
    assert summary["changed_result_slot_indices"] == [7, 9]
    assert summary["changed_record_indices"] == [2, 3]


def test_ai_info_equivalence_uses_decompressed_semantics() -> None:
    raw = bytes((index * 31) & 0xFF for index in range(4096))
    first = (
        proto_varint(1, 3)
        + proto_bytes(2, b"UNAC 3")
        + proto_varint(3, 44)
        + proto_bytes(4, b"\x01" + zlib.compress(raw, level=1))
    )
    second = (
        proto_varint(1, 3)
        + proto_bytes(2, b"UNAC 3")
        + proto_varint(3, 44)
        + proto_bytes(4, b"\x01" + zlib.compress(raw, level=9))
    )
    conflicting = (
        proto_varint(1, 3)
        + proto_bytes(2, b"UNAC 3")
        + proto_varint(3, 44)
        + proto_bytes(4, b"\x01" + zlib.compress(raw[:-1] + b"\xff"))
    )

    assert first != second
    assert _ai_info_equivalent(first, second) is True
    assert _ai_info_equivalent(first, conflicting) is False
    assert _ai_info_equivalent(first, b"not-protobuf") is False


def test_player_blob_replacement_keeps_changed_previous_version(tmp_path: Path) -> None:
    target = tmp_path / "42.ai-1.bin"

    assert _replace_player_blob(target, b"first") is None
    assert target.read_bytes() == b"first"
    assert _replace_player_blob(target, b"first") is None
    assert not (tmp_path / ".history").exists()

    backup = _replace_player_blob(target, b"second")
    assert backup is not None
    assert backup.read_bytes() == b"first"
    assert target.read_bytes() == b"second"


def test_offline_inventory_summary_distinguishes_catalog_and_owned_records() -> None:
    def record(category: int, part: int, instance: int) -> bytes:
        return b"".join(
            proto_varint(number, value)
            for number, value in (
                (1, category),
                (2, part),
                (3, category),
                (4, instance),
                (5, 1000),
            )
        )

    payload = (
        proto_bytes(1, record(1, 110, 0))
        + proto_bytes(1, record(1, 110, 1234))
        + proto_bytes(1, record(4, 410, 5678))
    )
    assert _offline_inventory_summary(payload) == {
        "record_count": 3,
        "instantiated_count": 2,
        "category_count": 2,
        "auxiliary_record_count": 0,
        "malformed_count": 0,
    }
    assert _offline_inventory_summary(b"\x0a\x01\x08") == {
        "record_count": 0,
        "instantiated_count": 0,
        "category_count": 0,
        "auxiliary_record_count": 0,
        "malformed_count": 1,
    }


def test_offline_inventory_merge_replaces_only_incoming_categories() -> None:
    def record(category: int, part: int, instance: int) -> bytes:
        return b"".join(
            proto_varint(number, value)
            for number, value in (
                (1, category),
                (2, part),
                (3, category + 10),
                (4, instance),
                (5, 1000),
            )
        )

    old_category_1 = record(1, 110, 1111)
    old_category_4 = record(4, 410, 4444)
    new_category_1 = record(1, 120, 2222)
    new_category_7 = record(7, 710, 7777)
    existing = proto_bytes(1, old_category_1) + proto_bytes(1, old_category_4)
    incoming = proto_bytes(1, new_category_1) + proto_bytes(1, new_category_7)

    merged, categories, field_numbers = _merge_offline_inventory(existing, incoming)
    records = [field.value for field in decode_proto(merged)]

    assert categories == [1, 7]
    assert field_numbers == [1]
    assert records == [old_category_4, new_category_1, new_category_7]
    assert _offline_inventory_summary(merged) == {
        "record_count": 3,
        "instantiated_count": 3,
        "category_count": 3,
        "auxiliary_record_count": 0,
        "malformed_count": 0,
    }


def test_team_emblem_download_requires_fixed_retail_structure() -> None:
    raw_emblem = bytes(1808)
    encoded = _emblem_response_payload(raw_emblem)
    assert encoded[:1] == b"\x01"
    assert zlib.decompress(encoded[1:]) == raw_emblem
    assert _emblem_response_payload(encoded) == encoded
    neutral = _emblem_response_payload(b"\xff\xd8jpeg\xff\xd9")
    assert neutral[:1] == b"\x01"
    assert len(zlib.decompress(neutral[1:])) == 1808


def test_team_info_contains_name_leader_and_one_member() -> None:
    team = {
        "team_id": 1,
        "name": "test",
        "leader_online_id": "Pilot",
        "leader_player_id": 42,
        "nationality_id": 2,
        "operation_base": 2,
        "language": 2,
        "play_type": 0,
        "enlistment_method": 2,
        "comment": "story and co-op",
        "faction_id": 1,
        "current_faction_id": 1,
        "territory_area_id": 1,
    }
    fields = decode_proto(_team_info(team, datetime.now(timezone.utc)))
    assert first_field(fields, 1).value == 1
    assert first_field(fields, 2).value == 42
    assert first_field(fields, 3).value == 2
    assert first_field(fields, 4).value == b"Pilot"
    assert first_field(fields, 5).value == b"test"
    assert first_field(fields, 8).value == 1
    assert first_field(fields, 6).value == 1
    assert first_field(fields, 7).value == 1
    assert first_field(fields, 9).value == 0
    assert [first_field(fields, number).value for number in (10, 11, 12)] == [2, 2, 1]
    assert first_field(fields, 23) is None
    assert first_field(fields, 13).value == 0
    assert first_field(fields, 14).value == b"story and co-op"
    assert first_field(fields, 15).value == 2
    update_date = decode_proto(first_field(fields, 16).value)
    assert [field.number for field in update_date] == [1, 2, 3, 4, 5]
    assert [field.wire_type for field in update_date] == [0, 2, 2, 2, 2]
    candidate = decode_proto(first_field(fields, 30).value)
    assert [(field.number, field.wire_type, field.value) for field in candidate] == [
        (1, 0, 1),
        (2, 0, 1),
        (3, 0, 0),
        (4, 0, 1000),
    ]
    color = decode_proto(first_field(fields, 19).value)
    assert [(field.number, field.wire_type, field.value) for field in color] == [
        (1, 0, 0),
        (2, 0, 0),
        (3, 0, 0),
        (4, 0, 0),
    ]
    assert first_field(fields, 25).value == 0
    assert first_field(fields, 27).value == 1
    assert first_field(fields, 21).value == 0
    assert first_field(fields, 29).value == 0
    assert first_field(fields, 31).value == 0
    member = decode_proto(first_field(fields, 28).value)
    assert first_field(member, 1).value == 42
    assert first_field(member, 2).value == 1


def test_team_info_round_trips_every_editable_team_setting() -> None:
    team = {
        "team_id": 1,
        "name": "test",
        "leader_online_id": "Pilot",
        "leader_player_id": 42,
        "operation_base": 2,
        "language": 2,
        "play_type": 3,
        "enlistment_method": 2,
        "comment": "line one\nline two",
        "profile_text": "five-line profile",
        "pay_apportionment": 1,
        "soldier_id": 15,
        "headquarters_id": 1,
        "title_id": 1,
        "sortie_text_id": 11,
        "desired_faction_id": 2,
        "desired_area_id": 6,
        "team_color_b64": "CAEQAhgDIAQ=",
    }
    fields = decode_proto(_team_info(team, datetime.now(timezone.utc)))
    assert first_field(fields, 9).value == 3
    assert first_field(fields, 10).value == 2
    assert first_field(fields, 11).value == 2
    assert first_field(fields, 23) is None
    assert first_field(fields, 12).value == 2
    assert first_field(fields, 13).value == 0
    assert first_field(fields, 14).value == b"line one\nline two"
    assert first_field(fields, 15).value == 2
    assert first_field(fields, 26).value == b"five-line profile"
    assert first_field(fields, 21).value == 15
    assert first_field(fields, 25).value == 11
    assert first_field(fields, 27).value == 2
    assert first_field(fields, 29).value == 6
    assert first_field(fields, 31).value == 1
    color = decode_proto(first_field(fields, 19).value)
    assert [first_field(color, number).value for number in range(1, 5)] == [1, 2, 3, 4]


def test_team_search_and_info_list_use_recovered_response_envelopes() -> None:
    ids = decode_proto(_team_id_search_response([1, 7]))
    assert [(field.number, field.value) for field in ids] == [(1, 1), (1, 7)]

    team = {
        "team_id": 7,
        "name": "Tolima",
        "leader_online_id": "Helper",
        "leader_player_id": 77,
        "faction_id": 1,
        "current_faction_id": 1,
        "territory_area_id": 1,
    }
    listed = decode_proto(_team_info_list_response([team], datetime.now(timezone.utc)))
    info = decode_proto(first_field(listed, 1).value)
    assert first_field(info, 1).value == 7
    assert first_field(info, 5).value == b"Tolima"


def test_team_session_response_uses_team_member_ordinal_not_global_player_id() -> None:
    response = decode_proto(_team_session_response({"team_id": 7}, {"member_id": 2}, 451473))
    assert first_field(response, 1).value == 7
    assert first_field(response, 2).value == 2


def test_own_unac_list_response_is_sorted_and_deduplicated() -> None:
    fields = decode_proto(_own_unac_list_response([3, 1, 3, 0]))
    assert [(field.number, field.value) for field in fields] == [
        (1, 0),
        (1, 1),
        (1, 3),
    ]


def test_sync_offline_data_response_preserves_local_items_by_default() -> None:
    assert first_field(decode_proto(_sync_offline_data_response()), 1).value == 0
    assert (
        first_field(decode_proto(_sync_offline_data_response(reject_local_items=True)), 1).value
        == 1
    )


def test_unknown_imported_ugc_ids_are_valid_unless_explicitly_banned() -> None:
    response = decode_proto(_ugc_id_validation_response([171617, 172223, 0], banned_ids={172223}))
    records = [decode_proto(field.value) for field in response]
    assert [first_field(record, 1).value for record in records] == [171617, 172223, 0]
    assert [first_field(record, 2).value for record in records] == [1, 0, 0]


def test_pending_emblem_attaches_atomically_when_team_is_created(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    pending = store.register_pending_emblem("Helper", b"metadata", b"jpeg-body")
    response = decode_proto(_register_emblem_response(int(pending["ugc_id"])))
    assert [field.number for field in response] == [1]
    assert first_field(response, 1).value == pending["ugc_id"]

    team = store.create(
        online_id="Helper",
        player_id=77,
        name="Tolima",
        operation_base=2,
        language=0,
        play_type=0,
        enlistment_method=0,
    )
    assert team["emblem_ugc_id"] == pending["ugc_id"]
    assert (tmp_path / team["emblem_file"]).read_bytes() == b"jpeg-body"
    assert TeamStore(tmp_path).state["pending_emblems"] == {}
    personal = TeamStore(tmp_path).player_emblem("Helper")
    assert personal is not None
    assert personal["ugc_id"] == pending["ugc_id"]
    assert personal["emblem"] == b"jpeg-body"


def test_player_emblem_assignment_round_trips_application_payload(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    payload = b"\x01" + zlib.compress(bytes(1808))
    assigned = store.set_player_emblem_by_ugc_id("Pilot", 77, payload)
    assert assigned["ugc_id"] == 77

    reloaded = TeamStore(tmp_path).player_emblem("Pilot")
    assert reloaded is not None
    assert reloaded["ugc_id"] == 77
    assert reloaded["emblem"] == payload


def test_garage_custom_info_persists_both_scalars(tmp_path: Path) -> None:
    store = TeamStore(tmp_path)
    assert store.garage_custom_info("Pilot") == {"primary": 0, "secondary": 0}
    assert store.set_garage_custom_info("Pilot", 16, 511) == {
        "primary": 16,
        "secondary": 511,
    }
    assert TeamStore(tmp_path).garage_custom_info("Pilot") == {
        "primary": 16,
        "secondary": 511,
    }


def test_world_current_info_keeps_required_fields() -> None:
    fields = decode_proto(_world_current_info(datetime.now(timezone.utc)))
    assert [field.number for field in fields] == [1, 2, 3, 4, 5, 6]
    assert first_field(fields, 3).value == 2
    assert first_field(fields, 5).value == 0
    assert first_field(fields, 2).value == 3
    area = decode_proto(first_field(fields, 4).value)
    assert first_field(area, 2).value == 0
    assert first_field(area, 4).value == 0


def test_world_basic_info_keeps_required_fields() -> None:
    fields = decode_proto(_world_basic_info(datetime.now(timezone.utc)))
    assert [field.number for field in fields] == [1, 2, 3, 4, 7, 8]
    assert first_field(fields, 3).value == 6  # Wartime Special's full-gauge requirement.
    season = decode_proto(first_field(fields, 2).value)
    assert first_field(season, 3).value == 2
    assert first_field(season, 1).value == 3
    area = decode_proto(first_field(fields, 4).value)
    assert [first_field(area, number).value for number in (1, 2, 3)] == [1, 0, 0]


def test_world_log_includes_current_phase_without_fake_area_history() -> None:
    fields = decode_proto(_world_log(datetime.now(timezone.utc)))
    assert [field.number for field in fields] == [1, 2, 5, 6, 7]
    assert first_field(fields, 1).value == 1
    assert first_field(fields, 2).value == 3
    world_phase = decode_proto(first_field(fields, 5).value)
    assert first_field(world_phase, 1).value == 2
    assert len(decode_proto(first_field(world_phase, 4).value)) == 7
    assert len(decode_proto(first_field(fields, 6).value)) == 7
    assert len(decode_proto(first_field(fields, 7).value)) == 7


def test_world_log_returns_empty_page_for_current_phase_cursor() -> None:
    fields = decode_proto(_world_log(datetime.now(timezone.utc), 2))
    assert [field.number for field in fields] == [1, 2, 6, 7]
    assert first_field(fields, 1).value == 1
    assert first_field(fields, 2).value == 3


def test_team_ranking_info_keeps_required_fields() -> None:
    fields = decode_proto(_team_ranking_info({"name": "test"}))
    assert [field.number for field in fields] == [1, 2, 3, 4]
    assert first_field(fields, 2).value == b"test"


def test_team_record_initializes_required_scalar_fields() -> None:
    fields = decode_proto(_team_record())
    assert [(field.number, field.wire_type, field.value) for field in fields] == [
        (1, 0, 0),
        (2, 0, 0),
        (3, 5, b"\0\0\0\0"),
        (5, 0, 0),
        (6, 0, 0),
    ]


def test_register_prepare_response_has_required_identity() -> None:
    fields = decode_proto(_register_prepare_response(7))
    assert [(field.number, field.wire_type, field.value) for field in fields] == [
        (1, 0, 7),
        (2, 0, 7),
        (3, 0, 1),
        (4, 2, b""),
    ]


def test_send_mercenary_message_decodes_repeated_matching_pairs() -> None:
    payload = proto_bytes(1, proto_varint(1, 77) + proto_varint(2, 3)) + proto_bytes(
        1, proto_varint(1, 88) + proto_varint(2, 4)
    )
    assert _mercenary_continuation_pairs(payload) == [(77, 3), (88, 4)]


def test_mercenary_rank_response_initializes_new_rank() -> None:
    fields = decode_proto(_mercenary_rank_response())
    assert [(field.number, field.wire_type, field.value) for field in fields] == [(1, 0, 0)]


def test_empty_ranking_list_has_required_result_count() -> None:
    fields = decode_proto(_empty_ranking_list_response())
    assert [(field.number, field.wire_type, field.value) for field in fields] == [(1, 0, 0)]


def test_mercenary_self_ranking_initializes_required_info() -> None:
    fields = decode_proto(_mercenary_self_ranking_response("Pilot", "test"))
    info = decode_proto(first_field(fields, 1).value)
    assert first_field(info, 1).value == 0
    assert first_field(info, 2).value == b"Pilot"
    assert first_field(info, 4).value == b"test"
    assert first_field(info, 5).value == 0
    assert first_field(info, 6).value == 0


def test_mercenary_ranking_list_has_field_two_result_count() -> None:
    fields = decode_proto(_mercenary_ranking_list_response())
    assert [(field.number, field.wire_type, field.value) for field in fields] == [(2, 0, 0)]


def test_mercenary_search_response_initializes_all_retail_fields() -> None:
    registration = {
        "online_id": "Helper",
        "player_id": 77,
        "ac_data": b"\x01compressed-ac",
        "result_values": [0, 0, 1, 3],
    }
    response = decode_proto(_mercenary_search_response([(registration, {"name": "Tolima"})]))
    result = decode_proto(first_field(response, 1).value)
    assert [field.number for field in result] == list(range(1, 11))
    assert first_field(result, 1).value == 77
    assert first_field(result, 2).value == b"Helper"
    # Native hired-mercenary entry uses this field as the contract/room
    # identity.  Returning the team name renders correctly but prevents the
    # accepted mercenary from entering the employer's shared session.
    assert first_field(result, 3).value == b"Helper"
    assert first_field(result, 4).value == b"\x01compressed-ac"
    assert [first_field(result, number).value for number in range(5, 9)] == [0, 0, 1, 3]
    nested = decode_proto(first_field(result, 9).value)
    assert [(field.number, field.wire_type, field.value) for field in nested] == [
        (1, 0, 0),
        *[(number, 0, 0) for number in range(2, 8)],
    ]
    assert first_field(result, 10).value == 0


def test_vote_kick_member_response_initializes_no_active_target() -> None:
    fields = decode_proto(_vote_kick_member_response())
    assert [(field.number, field.wire_type, field.value) for field in fields] == [(1, 0, 0)]


def test_my_player_ranking_initializes_name_strings_and_scalars() -> None:
    fields = decode_proto(_my_player_ranking_response())
    info = decode_proto(first_field(fields, 1).value)
    assert first_field(info, 1).value == 0
    for number in (2, 3):
        assert first_field(info, number).value == b""
    assert first_field(info, 4).value == 0
    assert first_field(info, 5).value == 0


def test_territory_layout_uses_recovered_exact_response_schema() -> None:
    empty = decode_proto(_territory_cannon_layout(50627))
    assert [(field.number, field.wire_type) for field in empty] == [(2, 0), (3, 2), (4, 0)]
    assert first_field(empty, 2).value == 50627
    assert first_field(empty, 3).value == b""
    assert first_field(empty, 4).value == 1

    persisted = decode_proto(_territory_cannon_layout(50627, b"opaque-layout"))
    assert first_field(persisted, 3).value == b"opaque-layout"
    assert first_field(persisted, 4).value == 0


def test_territory_layout_update_response_initializes_all_nine_fields() -> None:
    fields = decode_proto(_territory_layout_update_response())
    assert [(field.number, field.wire_type, field.value) for field in fields] == [
        (number, 0, 0) for number in range(1, 10)
    ]


def test_territory_info_initializes_identity_and_ap() -> None:
    fields = decode_proto(_territory_info(50627, 3, 1))
    assert [field.value for field in fields if field.number == 1 and field.wire_type == 0][
        -1
    ] == 1_000
    assert [field.value for field in fields if field.number == 2 and field.wire_type == 0][
        -1
    ] == 100_000
    assert [field.value for field in fields if field.number == 3 and field.wire_type == 0][-1] == 1


def test_item_simple_info_returns_bounded_owned_defenses() -> None:
    # Formations are paid key-item offers, not synthetic owned cannons.
    assert _item_simple_info_list(0xA0000, 64) == b""
    assert _item_simple_info_list(0xB0000, 64) == b""
    heat_cannon = decode_proto(_item_simple_info_list(0x90000, 256))
    heat_entries = [decode_proto(field.value) for field in heat_cannon if field.number == 1]
    assert len(heat_entries) == 1
    assert first_field(heat_entries[0], 1).value == 0x90000


def test_item_simple_info_replays_canonical_parts_for_reconstruction() -> None:
    def record(
        category: int,
        item_class: int,
        part_id: int,
        instance_id: int,
        tuning: int = 1000,
    ) -> bytes:
        return b"".join(
            proto_varint(number, value)
            for number, value in (
                (1, category),
                (2, part_id),
                (3, item_class),
                (4, instance_id),
                (5, tuning),
            )
        )

    placeholder = record(0, 0, 110, 0)
    first = record(0, 0, 110, 63_895_880_457_287_871)
    # Classes 10 and 11 intentionally fold into the same offline category 10.
    # The response must retain the original class rather than substituting a
    # constant or the folded category.
    second = record(10, 11, 220, 63_895_880_457_287_872)
    duplicate_part = record(10, 10, 220, 63_895_880_457_287_873, 1123)
    canonical = (
        proto_bytes(1, placeholder)
        + proto_bytes(1, first)
        + proto_bytes(1, second)
        + proto_bytes(1, duplicate_part)
    )

    response = decode_proto(
        _item_simple_info_list(
            3000,
            0,
            offline_inventory=canonical,
        )
    )
    entries = [decode_proto(field.value) for field in response]
    assert [[first_field(entry, number).value for number in range(1, 7)] for entry in entries] == [
        [63_895_880_457_287_871, 3000, 110, 0, 1, 1000],
        [63_895_880_457_287_872, 3001, 220, 11, 1, 1000],
        [63_895_880_457_287_873, 3002, 220, 10, 1, 1123],
    ]
    paged = decode_proto(
        _item_simple_info_list(
            3000,
            1,
            offset=1,
            offline_inventory=canonical,
        )
    )
    assert [first_field(decode_proto(paged[0].value), number).value for number in range(1, 7)] == [
        63_895_880_457_287_872,
        3001,
        220,
        11,
        1,
        1000,
    ]
    assert _item_simple_info_list(3000, 0, offline_inventory=b"malformed") == b""


def test_team_medal_info_initializes_summary_and_uses_the_correct_medal_field() -> None:
    team = {
        "team_id": 1,
        "team_rank_score": 0,
        "team_rank_floor_position": 0,
        "special_sortie_progress": 0,
    }
    assert _team_medal_info(team) == bytes.fromhex("08001000180020002800300038014000")
    assert _team_medal_info() == bytes.fromhex("0800100018002000280030004000")


def test_faction_selection_offers_three_factions() -> None:
    fields = decode_proto(_faction_selection(2))
    offers = [field for field in fields if field.number == 1]
    assert [first_field(decode_proto(field.value), 1).value for field in offers] == [1, 2, 3]
    assert first_field(fields, 2).value == 2


def test_faction_selection_maps_visual_rows_to_canonical_ids() -> None:
    assert [_canonical_faction_selection_id(value) for value in (1, 2, 3)] == [1, 2, 3]
    assert _canonical_faction_selection_id(0) == 0
    assert _canonical_faction_selection_id(4) == 0


def test_team_session_member_push_uses_announced_members_np_identity() -> None:
    identity = b"\x01" + b"Helper".ljust(36, b"\0")
    fields = decode_proto(_team_session_member_push(7, 451473, identity, "Helper"))
    assert first_field(fields, 1).value == 7
    assert first_field(fields, 2).value == 451473
    assert first_field(fields, 3).value == b"Helper"
    assert first_field(fields, 4).value == 1
    assert first_field(fields, 5).value == identity


def test_team_session_member_push_pair_keeps_directional_identities_aligned() -> None:
    leader_identity = b"\x01" + b"Leader".ljust(36, b"\0")
    helper_identity = b"\x01" + b"Helper".ljust(36, b"\0")
    to_helper, to_leader = _team_session_member_push_pair(
        7,
        existing_online_id="Leader",
        existing_member={"player_id": 10, "member_id": 54, "np_identity": leader_identity},
        new_online_id="Helper",
        new_player_id=20,
        new_np_identity=helper_identity,
    )
    helper_fields = decode_proto(to_helper)
    leader_fields = decode_proto(to_leader)
    assert first_field(helper_fields, 3).value == b"Leader"
    assert first_field(helper_fields, 5).value == leader_identity
    assert first_field(helper_fields, 4).value == 1
    assert first_field(leader_fields, 3).value == b"Helper"
    assert first_field(leader_fields, 5).value == helper_identity
    assert first_field(leader_fields, 4).value == 1


def test_cross_session_match_announces_counterparts_in_recipient_local_sessions(
    tmp_path: Path,
) -> None:
    leader_identity = b"\x01" + b"Leader".ljust(36, b"\0")
    helper_identity = b"\x01" + b"Helper".ljust(36, b"\0")
    leader = GameSession(b"leader01", bytes(16), "Leader", 10, established=True)
    helper = GameSession(b"helper01", bytes(16), "Helper", 20, established=True)
    leader.peer = ("127.0.0.1", 10001)
    helper.peer = ("127.0.0.1", 10002)
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        {leader.auth_token: leader, helper.auth_token: helper},
        tmp_path / "unused-native.so",
        tmp_path / "players",
        TeamStore(tmp_path / "teams"),
    )
    protocol.team_sessions = {
        1: {
            "session_id": 1,
            "team_id": 1,
            "members": {
                "Leader": {
                    "player_id": 10,
                    "member_id": 1,
                    "np_identity": leader_identity,
                }
            },
        },
        17: {
            "session_id": 17,
            "team_id": 17,
            "members": {
                "Helper": {
                    "player_id": 20,
                    "member_id": 1,
                    "np_identity": helper_identity,
                }
            },
        },
    }
    pushes: list[tuple[str, int | None, bytes]] = []
    protocol._send_game_push = lambda session, payload, *, subtype=None: pushes.append(
        (session.online_id, subtype, payload)
    )
    match = {
        "match_id": 9,
        "members": [
            {"session": leader, "team_id": 1},
            {"session": helper, "team_id": 17},
        ],
    }

    assert protocol._announce_cross_session_match_peers(match) == ["Leader", "Helper"]
    assert [(online_id, subtype) for online_id, subtype, _payload in pushes] == [
        ("Leader", 9),
        ("Helper", 9),
    ]
    leader_fields = decode_proto(pushes[0][2])
    helper_fields = decode_proto(pushes[1][2])
    assert [first_field(leader_fields, number).value for number in range(1, 6)] == [
        1,
        20,
        b"Helper",
        1,
        helper_identity,
    ]
    assert [first_field(helper_fields, number).value for number in range(1, 6)] == [
        17,
        10,
        b"Leader",
        1,
        leader_identity,
    ]
    assert protocol._announce_cross_session_match_peers(match) == []

    next_match = {
        "match_id": 10,
        "members": match["members"],
    }
    assert protocol._announce_cross_session_match_peers(next_match) == []
    assert next_match["signaling_peers_announced"] is True
    assert len(pushes) == 2

    protocol._clear_signaling_ready_for("Leader")
    assert protocol.signaling_ready_pairs == set()


def test_cross_session_match_releases_host_once_without_team_join_pushes(
    tmp_path: Path,
) -> None:
    leader_identity = b"\x01" + b"Leader".ljust(36, b"\0")
    helper_identity = b"\x01" + b"Helper".ljust(36, b"\0")
    leader = GameSession(b"leader01", bytes(16), "Leader", 10, established=True)
    helper = GameSession(b"helper01", bytes(16), "Helper", 20, established=True)
    leader.peer = ("127.0.0.1", 10001)
    helper.peer = ("127.0.0.1", 10002)
    telemetry_path = tmp_path / "events.jsonl"
    protocol = GameDatagramProtocol(
        Telemetry(telemetry_path),
        {leader.auth_token: leader, helper.auth_token: helper},
        tmp_path / "unused-native.so",
        tmp_path / "players",
        TeamStore(tmp_path / "teams"),
    )
    protocol.team_sessions = {
        1: {
            "session_id": 1,
            "team_id": 1,
            "members": {
                "Leader": {
                    "player_id": 10,
                    "member_id": 1,
                    "np_identity": leader_identity,
                }
            },
        },
        17: {
            "session_id": 17,
            "team_id": 17,
            "members": {
                "Helper": {
                    "player_id": 20,
                    "member_id": 1,
                    "np_identity": helper_identity,
                }
            },
        },
    }
    pushes: list[tuple[str, int | None, bytes]] = []
    protocol._send_game_push = lambda session, payload, *, subtype=None: pushes.append(
        (session.online_id, subtype, payload)
    )
    criteria = {1: 3, 2: 2, 3: 1}
    first = {
        "prepare_id": 1,
        "session": leader,
        "criteria": criteria,
        "team_id": 1,
        "claimed_player_ids": [10],
        "faction_id": 1,
        "registered_monotonic": time.monotonic(),
        "match_id": 0,
    }
    second = {
        "prepare_id": 2,
        "session": helper,
        "criteria": criteria,
        "team_id": 17,
        "claimed_player_ids": [20],
        "faction_id": 2,
        "registered_monotonic": time.monotonic(),
        "match_id": 0,
    }
    protocol.preparations = {"Leader": first}
    protocol._match_preparation(first)
    protocol.preparations["Helper"] = second
    protocol._match_preparation(second)

    match = protocol.matches[1]
    assert not match.get("preflight_retry_pending")
    assert [(online_id, subtype) for online_id, subtype, _ in pushes] == [("Leader", 2)]
    assert not match.get("signaling_session_overlays")
    for original in (first, second):
        retry = dict(original)
        protocol.preparations[original["session"].online_id] = retry
        protocol._match_preparation(retry)
    assert len(pushes) == 1
    events = [json.loads(line) for line in telemetry_path.read_text().splitlines()]
    assert not any(event["event"] == "game_match_signaling_retry_waiting" for event in events)
    assert len([event for event in events if event["event"] == "game_match_result_published"]) == 1


def test_battle_disconnect_cleanup_removes_only_finished_match_state(
    tmp_path: Path,
) -> None:
    leader = GameSession(b"leader01", bytes(16), "Leader", 10, established=True)
    helper = GameSession(b"helper01", bytes(16), "Helper", 20, established=True)
    bystander = GameSession(b"other001", bytes(16), "Other", 30, established=True)
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        {},
        tmp_path / "unused-native.so",
        tmp_path / "players",
        TeamStore(tmp_path / "teams"),
    )
    leader_prepare = {"session": leader, "match_id": 7}
    helper_prepare = {"session": helper, "match_id": 7}
    other_prepare = {"session": bystander, "match_id": 8}
    protocol.preparations = {
        "Leader": leader_prepare,
        "Helper": helper_prepare,
        "Other": other_prepare,
    }
    protocol.matches = {
        7: {"match_id": 7, "members": [leader_prepare, helper_prepare]},
        8: {"match_id": 8, "members": [other_prepare]},
    }
    protocol.signaling_ready_pairs = {
        frozenset({"Leader", "Helper"}),
        frozenset({"Other", "SomeoneElse"}),
    }

    assert protocol._remove_matchmaking_state(7) == ["Leader", "Helper"]
    assert sorted(protocol.preparations) == ["Other"]
    assert list(protocol.matches) == [8]
    assert protocol.signaling_ready_pairs == {frozenset({"Other", "SomeoneElse"})}


def test_team_session_join_response_returns_initialized_member_list() -> None:
    leader_identity = b"\x01" + b"Leader".ljust(36, b"\0")
    helper_identity = b"\x01" + b"Helper".ljust(36, b"\0")
    fields = decode_proto(
        _team_session_join_response(
            {
                "members": {
                    "Leader": {"player_id": 10, "member_id": 1, "np_identity": leader_identity},
                    "Helper": {"player_id": 20, "member_id": 2, "np_identity": helper_identity},
                }
            }
        )
    )
    members = [decode_proto(field.value) for field in fields if field.number == 1]
    assert len(members) == 2
    assert all(
        all(first_field(member, number) is not None for number in range(1, 5)) for member in members
    )
    by_player = {first_field(member, 1).value: member for member in members}
    assert first_field(by_player[10], 2).value == leader_identity
    assert first_field(by_player[10], 3).value == 1
    assert first_field(by_player[20], 2).value == helper_identity
    assert first_field(by_player[20], 3).value == 2


def test_team_session_member_cleanup_removes_only_target_and_empty_sessions() -> None:
    state = {
        1: {"members": {"Leader": {}, "Helper": {}}},
        2: {"members": {"Leader": {}}},
    }
    removed, deleted = _remove_team_session_member_from_state(state, "Leader", 1)
    assert removed == [1]
    assert deleted == []
    assert set(state[1]["members"]) == {"Helper"}
    assert "Leader" in state[2]["members"]
    removed, deleted = _remove_team_session_member_from_state(state, "Leader")
    assert removed == [2]
    assert deleted == [2]
    assert 2 not in state


def test_team_session_leave_push_has_required_session_and_member_fields() -> None:
    fields = decode_proto(_team_session_leave_push(7, 2))
    assert [(field.number, field.value) for field in fields] == [(1, 7), (2, 2)]


def test_team_session_departure_notifies_remaining_live_members(tmp_path: Path) -> None:
    leader = GameSession(b"leader01", bytes(16), "Leader", 10, established=True,
                         team_session_ready=True)
    helper = GameSession(b"helper01", bytes(16), "Helper", 20, established=True,
                         team_session_ready=True)
    leader.peer = ("127.0.0.1", 10001)
    helper.peer = ("127.0.0.1", 10002)
    teams = TeamStore(tmp_path / "teams")
    teams.state["next_team_id"] = 7
    teams.create(online_id="Leader", player_id=10, name="Shared", operation_base=0,
                 language=0, play_type=0, enlistment_method=2)
    teams.join_team(online_id="Helper", player_id=20, team_id=7)
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        {leader.auth_token: leader, helper.auth_token: helper},
        tmp_path / "unused-native.so",
        tmp_path / "players",
        teams,
    )
    protocol.team_sessions = {
        7: {
            "session_id": 7,
            "members": {
                "Leader": {"player_id": 10, "member_id": 1, "np_identity": b"leader"},
                "Helper": {"player_id": 20, "member_id": 2, "np_identity": b"helper"},
            },
        }
    }
    pushes = []
    protocol._send_game_push = lambda session, payload, *, subtype=None: pushes.append(
        (session.online_id, subtype, payload)
    )

    removed, deleted, notified = protocol._remove_team_session_member_and_notify("Leader", 7)

    assert removed == [7]
    assert deleted == []
    assert notified == ["Helper"]
    assert set(protocol.team_sessions[7]["members"]) == {"Helper"}
    assert pushes == []
    assert protocol._session_lifecycle.flush_deferred_team_session_pushes(
        helper, trigger="test_world"
    ) == 1
    assert pushes == [("Helper", 10, _team_session_leave_push(7, 10))]


def test_register_prepare_telemetry_records_authenticated_team_and_claimed_player(
    tmp_path: Path,
) -> None:
    teams = TeamStore(tmp_path / "teams")
    team = teams.create(
        online_id="Leader",
        player_id=10,
        name="Shared",
        operation_base=2,
        language=0,
        play_type=0,
        enlistment_method=2,
    )
    assert teams.join_team(online_id="Helper", player_id=20, team_id=team["team_id"])
    telemetry_path = tmp_path / "events.jsonl"
    protocol = GameDatagramProtocol(
        Telemetry(telemetry_path),
        {},
        tmp_path / "unused-native.so",
        tmp_path / "players",
        teams,
    )
    protocol._send_game_reply = lambda *args, **kwargs: None
    protocol._send_game_push = lambda *args, **kwargs: None

    def register(session: GameSession, sequence: int) -> None:
        payload = (
            proto_varint(1, 3)
            + proto_varint(2, 2)
            + proto_varint(3, 1)
            + proto_bytes(
                6,
                proto_varint(1, session.player_id) + proto_varint(2, 0),
            )
        )
        protocol._handle_game_payload(
            session,
            ReliablePacket(
                sequence,
                0,
                DAT,
                payload=encode_game_request(
                    GameMessage(
                        REGISTER_PREPARE,
                        sequence.to_bytes(4, "big"),
                        payload,
                        sequence,
                    )
                ),
            ),
        )

    leader = GameSession(b"leader01", bytes(16), "Leader", 10, established=True)
    helper = GameSession(b"helper01", bytes(16), "Helper", 20, established=True)
    leader.peer = ("127.0.0.1", 10001)
    helper.peer = ("127.0.0.1", 10002)
    protocol.sessions.update({leader.auth_token: leader, helper.auth_token: helper})
    register(leader, 1)
    register(helper, 2)

    events = [json.loads(line) for line in telemetry_path.read_text().splitlines()]
    accepted = [event for event in events if event["event"] == "game_register_prepare_accepted"]
    assert [(event["team_id"], event["claimed_player_ids"]) for event in accepted] == [
        (team["team_id"], [10]),
        (team["team_id"], [20]),
    ]
    assert all(event["claimed_player_matches_session"] for event in accepted)
    # These are two preparations from one team, not two opposing squads.
    # Same-team Mock uses the separate team-session path.
    assert not any(event["event"] == "game_match_result_published" for event in events)
    waiting = [event for event in events if event["event"] == "game_match_waiting"]
    assert [event["team_id"] for event in waiting] == [team["team_id"]] * 2
    assert all(event["criteria"] == {"1": 3, "2": 2, "3": 1} for event in waiting)


def test_faction_struggle_results_initialize_all_three_factions() -> None:
    fields = decode_proto(_faction_struggle_results())
    results = [decode_proto(field.value) for field in fields if field.number == 1]
    assert [first_field(result, 1).value for result in results] == [1, 2, 3]
    assert [first_field(result, 2).value for result in results] == [1, 2, 3]
    assert [first_field(result, 3).value for result in results] == [100, 50, 25]
    assert all(first_field(result, 8).value == 1 for result in results)
