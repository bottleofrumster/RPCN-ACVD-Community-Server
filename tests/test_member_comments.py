"""Current-team comment readback keeps identity and missing/empty data distinct."""

from copy import deepcopy
from pathlib import Path

import pytest

from acvd_server.handlers import team_queries
from acvd_server.protocol import decode_proto, first_field, proto_varint
from acvd_server.server import GameDatagramProtocol
from acvd_server.teams import TeamStore
from acvd_server.telemetry import Telemetry
from acvd_server.udp import (
    ACK,
    DAT,
    DAT_ACK,
    GET_TEAM_MEMBER_COMMENT_LIST,
    GameMessage,
    GameSession,
    ReliablePacket,
    decode_game_message,
    decode_reliable_packet,
    decrypt_server_datagram,
    encode_game_request,
    encode_reliable_packet,
    encrypt_client_datagram,
)


def make_store(root):
    store = TeamStore(root / "teams")
    team = store.create(
        online_id="Leader",
        player_id=101,
        name="First",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=2,
    )
    store.join_team(online_id="Peer", player_id=202, team_id=team["team_id"])
    foreign = store.create(
        online_id="Foreign",
        player_id=303,
        name="Other",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=2,
    )
    store.update_member_comment("Leader", b"Ready")
    store.update_member_comment("Peer", b"")
    store.update_member_comment("Foreign", b"Outside team")
    return store, team, foreign


def decode_comments(payload):
    records = []
    for field in decode_proto(payload):
        assert field.number == 1 and isinstance(field.value, bytes)
        record = decode_proto(field.value)
        assert [f.number for f in record] == [1, 2]
        records.append((first_field(record, 1).value, first_field(record, 2).value))
    return records


def read(store, root, name="Leader", payload=b""):
    replies = []
    session = GameSession(b"test0001", bytes(16), name, 101)
    team_queries.get_team_member_comments(
        session,
        GameMessage(GET_TEAM_MEMBER_COMMENT_LIST, bytes(4), payload, 1),
        team_store=store,
        telemetry=Telemetry(root / "events.jsonl"),
        reply=replies.append,
    )
    assert len(replies) == 1
    return decode_comments(replies[0])


def test_own_team_global_ids_and_explicit_empty_without_mutation(tmp_path):
    store, _, foreign = make_store(tmp_path)
    before = store.state_path.read_bytes()
    assert read(store, tmp_path, payload=proto_varint(1, foreign["team_id"])) == [
        (101, b"Ready"),
        (202, b""),
    ]
    assert store.state_path.read_bytes() == before
    assert read(store, tmp_path, "Foreign") == [(303, b"Outside team")]


@pytest.mark.parametrize("value", [None, 7, [], {}, "\ud800"])
def test_corrupt_comments_are_not_stringified_or_used_as_clears(tmp_path, value):
    store, _, _ = make_store(tmp_path)
    store.state["player_profiles"]["Peer"]["status"] = value
    before = deepcopy(store.state)
    assert read(store, tmp_path) == [(101, b"Ready")]
    assert store.state == before


def test_missing_comment_is_omitted_not_fabricated(tmp_path):
    store, _, _ = make_store(tmp_path)
    del store.state["player_profiles"]["Peer"]["status"]
    assert read(store, tmp_path) == [(101, b"Ready")]


def test_utf8_update_and_explicit_clear_survive_reload(tmp_path):
    store, _, _ = make_store(tmp_path)
    for value in ("準備完了".encode(), b""):
        store.update_member_comment("Peer", value)
        loaded = TeamStore(store.root)
        before = loaded.state_path.read_bytes()
        assert read(loaded, tmp_path) == [(101, b"Ready"), (202, value)]
        assert loaded.state_path.read_bytes() == before


def test_no_team_returns_empty_without_creating_data(tmp_path):
    store, _, _ = make_store(tmp_path)
    before = deepcopy(store.state)
    assert read(store, tmp_path, "Unaffiliated") == []
    assert store.state == before


def test_stale_foreign_or_departed_records_do_not_leak_comments(tmp_path):
    store, team, foreign = make_store(tmp_path)
    stale = deepcopy(team["member_records"][1])
    assert (
        store.join_team(
            online_id="Peer", player_id=202, team_id=foreign["team_id"], replace_existing=True
        )
        is foreign
    )
    assert store.team_for("Peer") is foreign
    team["member_records"].extend([stale, deepcopy(foreign["member_records"][0])])
    before = deepcopy(store.state)
    assert read(store, tmp_path) == [(101, b"Ready")]
    assert store.state == before


@pytest.mark.parametrize("identifier", [0, -1, 2**32, True, "202"])
def test_invalid_record_identifiers_are_not_coerced(tmp_path, identifier):
    store, team, _ = make_store(tmp_path)
    team["member_records"][1]["player_id"] = identifier
    assert read(store, tmp_path) == [(101, b"Ready")]


def test_conflicting_profile_identity_is_not_reassigned(tmp_path):
    store, _, _ = make_store(tmp_path)
    store.remember_player("Peer", 404)
    assert read(store, tmp_path) == [(101, b"Ready")]


def test_duplicate_conflicting_ids_are_omitted_not_first_wins(tmp_path):
    store, team, _ = make_store(tmp_path)
    store.state["player_profiles"]["Peer"].pop("player_id", None)
    team["member_records"][1]["player_id"] = 101
    assert read(store, tmp_path) == []


def test_encrypted_comment_dispatch_uses_authenticated_team_and_request_id(tmp_path):
    store, _, foreign = make_store(tmp_path)
    session = GameSession(b"test0001", bytes(range(16)), "Leader", 101, established=True)
    library = Path(__file__).resolve().parents[1] / "native/libacvd_cwc.so"
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        {session.auth_token: session},
        library,
        tmp_path / "players",
        store,
    )

    class Transport:
        def __init__(self):
            self.packets = []

        def sendto(self, data, peer):
            self.packets.append((data, peer))

    transport = Transport()
    protocol.connection_made(transport)
    request = GameMessage(
        GET_TEAM_MEMBER_COMMENT_LIST, b"\x12\x34\x56\x78", proto_varint(1, foreign["team_id"]), 3
    )
    wire = encrypt_client_datagram(
        encode_reliable_packet(ReliablePacket(100, 0, DAT, payload=encode_game_request(request))),
        session.game_key,
        session.auth_token,
        0,
        library,
    )
    before = store.state_path.read_bytes()
    try:
        protocol.datagram_received(wire, ("127.0.0.1", 40001))
        packets = [
            decode_reliable_packet(decrypt_server_datagram(raw, session.game_key, library))
            for raw, _ in transport.packets
        ]
        assert [p.opcode for p in packets] == [ACK, DAT_ACK]
        response = decode_game_message(packets[-1].payload)
        assert response.message_index == request.message_index
        assert decode_comments(response.payload[16:]) == [(101, b"Ready"), (202, b"")]
        assert all(peer == ("127.0.0.1", 40001) for _, peer in transport.packets)
        assert store.state_path.read_bytes() == before
    finally:
        protocol.connection_lost(None)
