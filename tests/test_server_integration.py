from __future__ import annotations

import asyncio
import json
import shutil
import socket
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from acvd_server.config import ServerConfig
from acvd_server.crypto import CwcCipher, generate_rsa_keypair
from acvd_server.protocol import (
    Message,
    Packet,
    decode_message,
    decode_packet,
    decode_proto,
    encode_message,
    encode_packet,
    encode_varint,
    first_field,
    proto_bytes,
    proto_varint,
)
from acvd_server.regulation import install_for_host, read_container
from acvd_server.server import (
    AcvdServer,
    GameDatagramProtocol,
    _advertised_host_for_route,
    _team_info,
)
from acvd_server.teams import TeamStore
from acvd_server.telemetry import Telemetry
from acvd_server.udp import (
    ACK,
    APPLY_WORLD_TOP_SCREENSHOT,
    CANCEL_MERCENARY_NEGOTIATION,
    CANCEL_PREPARE,
    CHECK_VALID_PLAYER,
    CONTINUE_MERCENARY_REGISTRATION,
    CREATE_TEAM,
    CREATE_TEAM_SESSION,
    DAT,
    DAT_ACK,
    FIN,
    FIN_ACK,
    GET_AC_DATA,
    GET_AI_EXTEND,
    GET_AI_INFO,
    GET_COMMENT,
    GET_GARAGE_CUSTOM_INFO,
    GET_JOINING_TEAM_STATUS,
    GET_MERCENARY_DATA,
    GET_MERCENARY_MESSAGE_LIST,
    GET_MISSION_SAVEDATA,
    GET_MY_DEFEAT_RANKING_INFO,
    GET_MY_USER_INFO,
    GET_PLAYER_EMBLEM,
    GET_PLAYER_SHOP_LINEUP_FOR_PARTS,
    GET_TEAM_INFO,
    GET_WAIT_TIME_FOR_APPLY_WORLD_TOP_SS,
    GET_WORLD_CURRENT_INFO,
    GET_WORLD_TOP_SCREENSHOT,
    GET_WORLD_TOP_SS_THUMBNAIL_LIST,
    JOIN_FACTION,
    JOIN_TEAM_AUTO,
    LEAVE_TEAM_SESSION,
    NOTIFY_MATCH_ROOM_INFO,
    REGISTER_AC_DATA,
    REGISTER_BATTLE_RESULT,
    REGISTER_EMBLEM,
    REGISTER_GARAGE_CUSTOM_INFO,
    REGISTER_MERCENARY,
    REGISTER_PLAYER_SCREENSHOT,
    REGISTER_PREPARE,
    SEARCH_MERCENARY,
    SEND_MERCENARY_MESSAGE,
    SET_PLAYER_EMBLEM_BY_UGC_ID,
    START_MERCENARY_NEGOTIATION,
    SYN,
    SYN_ACK,
    SYNC_MONEY,
    SYNC_OFFLINE_DATA,
    SYNCHRONIZE_DLC_PARTS_LINEUP,
    TRADE_MONEY,
    UNREGISTER_MERCENARY,
    UPDATE_AI_EXTEND,
    UPDATE_LEADER_TEAM_INFO,
    UPDATE_MEMBER_TEAM_INFO,
    UPDATE_TEAM_FACTION_INFO,
    UPDATE_TEAM_MEMBER_COMMENT,
    UPDATE_USER_PROPERTY,
    UPLOAD_AI_INFO,
    UPLOAD_MISSION_SAVEDATA,
    WAIT_FOR_USER_LOGIN,
    GameMessage,
    GameSession,
    ReliablePacket,
    decode_game_message,
    decode_reliable_packet,
    decrypt_server_datagram,
    encode_game_request,
    encode_game_request_fragments,
    encode_reliable_packet,
    encrypt_client_datagram,
)

ROOT = Path(__file__).resolve().parents[1]


def test_advertised_host_follows_client_route() -> None:
    public = "203.0.113.10"
    assert _advertised_host_for_route(public, "127.0.0.1", "127.0.0.1") == "127.0.0.1"
    assert _advertised_host_for_route(public, "10.20.30.50", "10.20.30.1") == "10.20.30.1"
    assert _advertised_host_for_route(public, "172.17.0.2", "172.17.0.1") == "172.17.0.1"
    # Use a global peer for this branch; TEST-NET ranges are classified as private.
    assert _advertised_host_for_route(public, "8.8.8.8", "10.20.30.1") == public


def test_create_team_uses_recovered_retail_field_mapping(tmp_path: Path) -> None:
    teams = TeamStore(tmp_path / "teams")
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        {},
        ROOT / "native/libacvd_cwc.so",
        tmp_path / "players",
        teams,
    )
    session = GameSession(b"token123", bytes(range(16)), "Pilot", 42)

    # Exact semantic values from the labeled creation of team `test`:
    # Free=2, Hokkaido=1, English=2, Friendship=0.
    captured = (
        proto_bytes(3, "test")
        + proto_varint(7, 2)
        + proto_bytes(8, b"")
        + proto_varint(9, 2)
        + proto_varint(11, 1)
        + proto_varint(18, 0)
        + proto_varint(21, 0)
        + proto_varint(22, 1)
        + proto_varint(23, 0)
    )
    protocol._handle_game_payload(
        session,
        ReliablePacket(
            1,
            0,
            DAT,
            payload=encode_game_request(GameMessage(CREATE_TEAM, b"\x00\x00\x00\x21", captured, 1)),
        ),
    )

    team = teams.team_for("Pilot")
    assert team is not None
    assert team["enlistment_method"] == 2
    assert team["recruitment_open"] is True
    assert team["operation_base"] == 1
    assert team["language"] == 2
    assert team["play_type"] == 1
    assert team["creation_wire_fields"] == {
        "7": 2,
        "9": 2,
        "11": 1,
        "18": 0,
        "21": 0,
        "22": 1,
        "23": 0,
    }
    fields = decode_proto(_team_info(team, datetime.now(timezone.utc)))
    assert first_field(fields, 3).value == 2
    assert first_field(fields, 7).value == team["desired_faction_id"] == 1
    assert team["team_fame_points"] == 50
    assert first_field(fields, 9).value == 1
    assert first_field(fields, 11).value == 2
    assert first_field(fields, 10).value == 1
    assert first_field(fields, 13).value == 0
    assert first_field(fields, 21).value == 0
    assert first_field(fields, 23) is None


def test_game_protocol_reassembles_large_player_screenshot(tmp_path: Path) -> None:
    player_data = tmp_path / "players"
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        {},
        ROOT / "native/libacvd_cwc.so",
        player_data,
        TeamStore(tmp_path / "teams"),
    )
    session = GameSession(b"token123", bytes(range(16)), "Pilot", 42)
    image = bytes(range(256)) * 796 + b"complete"
    screenshot_request = (
        proto_varint(1, 42)
        + proto_varint(2, 2)
        + proto_varint(3, 0xFFFFFFFF)
        + proto_varint(4, 501)
        + proto_bytes(5, b"")
        + proto_bytes(6, image)
        + proto_bytes(7, b"metadata")
        + proto_bytes(8, b"thumbnail")
    )
    fragments = encode_game_request_fragments(
        GameMessage(REGISTER_PLAYER_SCREENSHOT, b"\x00\x00\x00\x21", screenshot_request, 257),
        max_fragment_length=1012,
    )
    assert len(fragments) > 64
    assert int.from_bytes(fragments[0][3:8], "big") > 0xFFFF

    for sequence, fragment in enumerate(fragments):
        protocol._handle_game_payload(
            session,
            ReliablePacket(sequence % 4096, 0, DAT, payload=fragment),
        )

    assert (player_data / "42.player-screenshot.bin").read_bytes() == screenshot_request
    assert session.fragment_parts == {}
    assert session.fragment_totals == {}
    assert session.fragment_started_monotonic == {}
    assert session.fragment_updated_monotonic == {}


def test_trade_money_uses_captured_recipient_field_1_and_amount_field_3(
    tmp_path: Path,
) -> None:
    teams = TeamStore(tmp_path / "teams")
    teams.remember_player("Sender", 101_010)
    teams.remember_player("Recipient", 202_020)
    # This is a wire-layout test, not authority to fabricate missing wallets.
    teams.synchronize_balance("Sender", 1_000_000, initial=0)
    teams.synchronize_balance("Recipient", 1_000_000, initial=0)
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        {},
        ROOT / "native/libacvd_cwc.so",
        tmp_path / "players",
        teams,
    )
    session = GameSession(b"token123", bytes(range(16)), "Sender", 101_010)

    # Recovered retail field layout with synthetic three-byte recipient ID 202020.
    captured = bytes.fromhex("08a4aa0c18904e")
    protocol._handle_game_payload(
        session,
        ReliablePacket(
            1,
            0,
            DAT,
            payload=encode_game_request(GameMessage(TRADE_MONEY, b"\x00\x00\x00\x21", captured, 1)),
        ),
    )
    assert teams.balance("Sender") == 990_000
    assert teams.balance("Recipient") == 1_010_000

    # The old guessed field-2 amount must not perform another transfer.
    protocol._handle_game_payload(
        session,
        ReliablePacket(
            2,
            0,
            DAT,
            payload=encode_game_request(
                GameMessage(
                    TRADE_MONEY,
                    b"\x00\x00\x00\x23",
                    proto_varint(1, 202_020) + proto_varint(2, 10_000),
                    2,
                )
            ),
        ),
    )
    assert teams.balance("Sender") == 990_000
    assert teams.balance("Recipient") == 1_010_000


def test_offline_inventory_batches_merge_by_authoritative_category(tmp_path: Path) -> None:
    player_data = tmp_path / "players"
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        {},
        ROOT / "native/libacvd_cwc.so",
        player_data,
        TeamStore(tmp_path / "teams"),
    )
    session = GameSession(b"token123", bytes(range(16)), "Pilot", 42)
    player_data.mkdir(parents=True)

    def record(category: int, part: int, instance: int) -> bytes:
        return proto_bytes(
            1,
            proto_varint(1, category)
            + proto_varint(2, part)
            + proto_varint(3, category + 10)
            + proto_varint(4, instance)
            + proto_varint(5, 1000),
        )

    category_1 = record(1, 110, 1111)
    category_4 = record(4, 410, 4444)
    category_1_update = record(1, 120, 2222)
    auxiliary = proto_bytes(3, proto_varint(1, 0x20000) + proto_varint(2, 1))
    for sequence, payload in enumerate((category_1, auxiliary, category_4, category_1_update), 1):
        protocol._handle_game_payload(
            session,
            ReliablePacket(
                sequence,
                0,
                DAT,
                payload=encode_game_request(
                    GameMessage(
                        SYNC_OFFLINE_DATA,
                        sequence.to_bytes(4, "big"),
                        payload,
                        sequence,
                    )
                ),
            ),
        )

    stored = decode_proto((player_data / "42.offline-data.bin").read_bytes())
    records = [decode_proto(field.value) for field in stored if field.number == 1]
    assert [(first_field(item, 1).value, first_field(item, 2).value) for item in records] == [
        (4, 410),
        (1, 120),
    ]
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    synchronized = [event for event in events if event["event"] == "game_offline_data_synchronized"]
    assert synchronized[-1]["merged_categories"] == [1]
    assert synchronized[-1]["merged_field_numbers"] == [1]
    assert synchronized[-1]["canonical_inventory_summary"]["category_count"] == 2
    assert synchronized[-1]["canonical_inventory_summary"]["instantiated_count"] == 2
    assert synchronized[-1]["canonical_inventory_summary"]["auxiliary_record_count"] == 1


def test_offline_inventory_preseed_defers_conflict_and_applies_exact_match(tmp_path: Path) -> None:
    player_data = tmp_path / "players"
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        {},
        ROOT / "native/libacvd_cwc.so",
        player_data,
        TeamStore(tmp_path / "teams"),
    )
    session = GameSession(b"token123", bytes(range(16)), "Pilot", 42)
    reviewed = proto_bytes(
        1,
        proto_varint(1, 0)
        + proto_varint(2, 110)
        + proto_varint(3, 0)
        + proto_varint(4, 123456)
        + proto_varint(5, 1000),
    )
    default_upload = proto_bytes(
        1,
        proto_varint(1, 0)
        + proto_varint(2, 110)
        + proto_varint(3, 0)
        + proto_varint(4, 0)
        + proto_varint(5, 1000),
    )
    player_data.mkdir(parents=True)
    preseed = player_data / "42.offline-data.preseed.bin"
    preseed.write_bytes(reviewed)

    protocol._handle_game_payload(
        session,
        ReliablePacket(
            1,
            0,
            DAT,
            payload=encode_game_request(
                GameMessage(SYNC_OFFLINE_DATA, b"\x00\x00\x00\x01", default_upload, 1)
            ),
        ),
    )
    assert preseed.read_bytes() == reviewed
    assert not (player_data / "42.offline-data.bin").exists()
    assert (player_data / "42.offline-data.deferred.bin").read_bytes() == default_upload

    protocol._handle_game_payload(
        session,
        ReliablePacket(
            2,
            0,
            DAT,
            payload=encode_game_request(
                GameMessage(SYNC_OFFLINE_DATA, b"\x00\x00\x00\x02", reviewed, 2)
            ),
        ),
    )
    assert (player_data / "42.offline-data.bin").read_bytes() == reviewed
    assert not preseed.exists()
    applied = list(player_data.glob("42.offline-data.preseed.applied-*.bin"))
    assert len(applied) == 1 and applied[0].read_bytes() == reviewed

    # A later default upload from the same reconstruction login is deferred;
    # it cannot immediately erase the exact reviewed promotion.
    protocol._handle_game_payload(
        session,
        ReliablePacket(
            3,
            0,
            DAT,
            payload=encode_game_request(
                GameMessage(SYNC_OFFLINE_DATA, b"\x00\x00\x00\x03", default_upload, 3)
            ),
        ),
    )
    assert (player_data / "42.offline-data.bin").read_bytes() == reviewed
    assert (player_data / "42.offline-data.deferred.bin").read_bytes() == default_upload


def free_port(sock_type: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, sock_type) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def send_message(writer, counter: int, message: Message) -> None:
    writer.write(encode_packet(Packet(counter, encode_message(message))))
    await writer.drain()


async def receive_message(reader) -> Message:
    length = struct.unpack(">H", await reader.readexactly(2))[0]
    packet = decode_packet(await reader.readexactly(length))
    return decode_message(packet.payload)


def recover_x931(public_key, ciphertext: bytes) -> bytes:
    numbers = public_key.public_numbers()
    block = pow(int.from_bytes(ciphertext, "big"), numbers.e, numbers.n).to_bytes(
        len(ciphertext), "big"
    )
    assert block[0] == 0x6B and block[-1] == 0xCC
    delimiter = block.index(b"\xba")
    return block[delimiter + 1 : -1]


def ticket_blob() -> bytes:
    def node(node_id: int, raw: bytes) -> bytes:
        return struct.pack(">HH", node_id, len(raw)) + raw

    def blob(blob_id: int, children: list[bytes]) -> bytes:
        return node(0x3000 | blob_id, b"".join(children))

    user = blob(
        0,
        [
            node(8, b"1".ljust(0x14, b"\0")),
            node(1, struct.pack(">I", 0x100)),
            node(7, struct.pack(">Q", 1000)),
            node(7, struct.pack(">Q", 2000)),
            node(2, struct.pack(">Q", 42)),
            node(4, b"Pilot".ljust(0x20, b"\0")),
            node(8, b"us\0\0"),
            node(4, b"un\0\0"),
            node(8, b"acvd-test".ljust(0x18, b"\0")),
            node(1, struct.pack(">I", 0)),
            node(0, b""),
            node(0, b""),
        ],
    )
    signature = blob(2, [node(8, b"RPCN"), node(8, bytes(56))])
    body = user + signature
    return struct.pack(">II", 0x21010000, len(body)) + body


async def test_complete_login_and_auth_flow(tmp_path: Path) -> None:
    (tmp_path / "keys").mkdir()
    (tmp_path / "native").mkdir()
    (tmp_path / "logs").mkdir()
    generate_rsa_keypair(tmp_path / "keys/server-private.pem", tmp_path / "keys/publickey.pem")
    shutil.copy2(ROOT / "native/libacvd_cwc.so", tmp_path / "native/libacvd_cwc.so")
    login_port, auth_port, cdn_port, game_port = (
        free_port(),
        free_port(),
        free_port(),
        free_port(socket.SOCK_DGRAM),
    )
    config = ServerConfig(
        bind_host="127.0.0.1",
        advertised_host="127.0.0.1",
        login_ports=[login_port],
        auth_port=auth_port,
        game_port=game_port,
        cdn_port=cdn_port,
        project_root=tmp_path,
    )
    config.regulation_path.parent.mkdir(parents=True)
    install_for_host(
        ROOT / "resources/regulation/contents_101.bin",
        ROOT / "resources/regulation/publickey.pem",
        config.private_key_path,
        config.regulation_path,
    )
    expected_regulation = config.regulation_path.read_bytes()
    server = AcvdServer(config)
    await server.start()
    public_key = serialization.load_pem_public_key(config.public_key_path.read_bytes())
    try:
        cdn_reader, cdn_writer = await asyncio.open_connection("127.0.0.1", cdn_port)
        cdn_writer.write(b"GET /regulation/contents_101.bin HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await cdn_writer.drain()
        cdn_response = await cdn_reader.read()
        assert cdn_response.startswith(b"HTTP/1.1 200 OK\r\n")
        manifest = read_container(cdn_response.split(b"\r\n\r\n", 1)[1], public_key, byte_order="little")
        assert b"Patch.List.Count=1\n" in manifest
        assert f"http://127.0.0.1:{cdn_port}/regulation/regulation.bnd".encode() in manifest
        cdn_writer.close()
        await cdn_writer.wait_closed()

        cdn_reader, cdn_writer = await asyncio.open_connection("127.0.0.1", cdn_port)
        cdn_writer.write(b"GET /regulation/regulation.bnd HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await cdn_writer.drain()
        cdn_response = await cdn_reader.read()
        assert cdn_response.startswith(b"HTTP/1.1 200 OK\r\n")
        body = read_container(cdn_response.split(b"\r\n\r\n", 1)[1], public_key, byte_order="little")
        assert body == read_container(expected_regulation, public_key)
        cdn_writer.close()
        await cdn_writer.wait_closed()

        reader, writer = await asyncio.open_connection("127.0.0.1", login_port)
        login_plain = proto_bytes(1, "Pilot") + proto_varint(3, 101)
        login_cipher = public_key.encrypt(
            login_plain,
            padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None),
        )
        await send_message(writer, 1, Message(5, 1, login_cipher))
        login_response = await receive_message(reader)
        login_fields = decode_proto(recover_x931(public_key, login_response.payload))
        assert login_fields[0].value == auth_port
        writer.close()
        await writer.wait_closed()

        reader, writer = await asyncio.open_connection("127.0.0.1", auth_port)
        cwc_key = bytes(range(16))
        handshake = public_key.encrypt(
            proto_bytes(1, cwc_key),
            padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None),
        )
        await send_message(writer, 1, Message(6, 2, handshake))
        assert len((await receive_message(reader)).payload) == 27
        cwc = CwcCipher(cwc_key, config.native_library_path)
        status = cwc.encrypt(proto_varint(1, 1) + proto_bytes(2, "Pilot") + proto_varint(4, 101))
        await send_message(writer, 2, Message(2, 3, status))
        assert decode_proto(cwc.decrypt((await receive_message(reader)).payload))[-1].value == 101
        protocol_marker = b"VD\0\0\x01\0\0\x02"
        client_secret = bytes(range(8))
        challenge = protocol_marker + client_secret
        await send_message(writer, 3, Message(1, 4, cwc.encrypt(challenge)))
        key_response = cwc.decrypt((await receive_message(reader)).payload)
        assert key_response[:8] == client_secret and len(key_response) == 16
        game_key = key_response
        await send_message(writer, 4, Message(3, 5, cwc.encrypt(ticket_blob())))
        game_info = cwc.decrypt((await receive_message(reader)).payload)
        assert len(game_info) == 56
        assert game_info[8:12] == socket.inet_aton("127.0.0.1")
        assert struct.unpack_from(">H", game_info, 12)[0] == game_port

        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setblocking(False)
        udp.bind(("127.0.0.1", 0))
        loop = asyncio.get_running_loop()
        prefix = b"Pilot".ljust(17, b"\0") + b"\0" + b"Pilot".ljust(17, b"\0")
        syn = encode_reliable_packet(
            ReliablePacket(
                100, 0, SYN, payload=b"\x12\x10\x20\x20\x00\x00\xa0\x00", initial_prefix=prefix
            )
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(syn, game_key, game_info[:8], 1, config.native_library_path),
            ("127.0.0.1", config.game_port),
        )
        handshake_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            handshake_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        assert {packet.opcode for packet in handshake_packets} == {SYN_ACK, ACK}
        syn_ack = next(packet for packet in handshake_packets if packet.opcode == SYN_ACK)

        client_ack = encode_reliable_packet(ReliablePacket(0, syn_ack.local_sequence, ACK))
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                client_ack, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        login_proto = (
            proto_bytes(1, "Pilot") + proto_varint(2, 2) + proto_varint(3, 1) + proto_varint(4, 0)
        )
        login_request = encode_game_request(
            GameMessage(WAIT_FOR_USER_LOGIN, b"\x00\x00\x00\x07", login_proto, 1)
        )
        login_packet = encode_reliable_packet(ReliablePacket(101, 0, DAT, payload=login_request))
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                login_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        boot_packets = []
        for _ in range(3):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            boot_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        assert {packet.opcode for packet in boot_packets} == {ACK, DAT, DAT_ACK}

        info_request = encode_game_request(
            GameMessage(GET_MY_USER_INFO, b"\x00\x00\x00\x09", b"", 2)
        )
        info_packet = encode_reliable_packet(ReliablePacket(102, 0, DAT, payload=info_request))
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                info_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        info_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            info_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        info_reply = next(packet for packet in info_packets if packet.opcode == DAT_ACK)
        info_message = decode_game_message(info_reply.payload)
        assert [field.number for field in decode_proto(info_message.payload[16:])] == [
            1,
            2,
            3,
            5,
            6,
            8,
            9,
            10,
            11,
            12,
            13,
            14,
            15,
        ]
        info_fields = decode_proto(info_message.payload[16:])
        assert first_field(info_fields, 14).value == 1
        assert first_field(info_fields, 15).value == 0
        assert first_field(info_fields, 11).value == 100_000

        sync_proto = proto_varint(2, 0) + proto_varint(3, 915775718) + proto_varint(4, 100000)
        sync_request = encode_game_request(
            GameMessage(SYNC_MONEY, b"\x00\x00\x00\x0b", sync_proto, 3)
        )
        sync_packet = encode_reliable_packet(ReliablePacket(103, 0, DAT, payload=sync_request))
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                sync_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        sync_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            sync_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        sync_reply = next(packet for packet in sync_packets if packet.opcode == DAT_ACK)
        sync_message = decode_game_message(sync_reply.payload)
        assert first_field(decode_proto(sync_message.payload[16:]), 1).value == 915775718

        emblem_request = encode_game_request(
            GameMessage(GET_PLAYER_EMBLEM, b"\x00\x00\x00\x0d", proto_varint(1, 42), 4)
        )
        emblem_packet = encode_reliable_packet(ReliablePacket(104, 0, DAT, payload=emblem_request))
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                emblem_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        emblem_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            emblem_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        emblem_reply = next(packet for packet in emblem_packets if packet.opcode == DAT_ACK)
        emblem_message = decode_game_message(emblem_reply.payload)
        emblem_fields = decode_proto(emblem_message.payload[16:])
        assert [field.number for field in emblem_fields] == [1, 2, 3]
        assert first_field(emblem_fields, 1).value == b""
        assert first_field(emblem_fields, 2).value == 0
        assert first_field(emblem_fields, 3).value == 0

        raw_ac_data = b"local-ac-snapshot"
        local_ac_data = b"\x01" + zlib.compress(raw_ac_data)
        delivered_preseed = config.player_data_path / "42.ac-data.preseed.bin"
        config.player_data_path.mkdir(parents=True, exist_ok=True)
        delivered_preseed.write_bytes(b"reviewed-preseed")
        server.game_sessions[game_info[:8]].ac_preseed_delivered = delivered_preseed
        register_ac_data_request = encode_game_request(
            GameMessage(REGISTER_AC_DATA, b"\x00\x00\x00\x0f", proto_bytes(1, local_ac_data), 5)
        )
        register_ac_data_packet = encode_reliable_packet(
            ReliablePacket(105, 0, DAT, payload=register_ac_data_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                register_ac_data_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        for _ in range(2):
            await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
        assert (config.player_data_path / "42.ac-data.bin").read_bytes() == local_ac_data
        assert not delivered_preseed.exists()
        applied_preseeds = list(config.player_data_path.glob("42.ac-data.preseed.applied-*.bin"))
        assert len(applied_preseeds) == 1
        assert applied_preseeds[0].read_bytes() == b"reviewed-preseed"

        (config.player_data_path / "77.ac-data.bin").write_bytes(local_ac_data)
        ac_data_request = encode_game_request(
            GameMessage(GET_AC_DATA, b"\x00\x00\x00\x11", proto_varint(1, 77), 6)
        )
        ac_data_packet = encode_reliable_packet(
            ReliablePacket(106, 0, DAT, payload=ac_data_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                ac_data_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        ac_data_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            ac_data_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        ac_data_reply = next(packet for packet in ac_data_packets if packet.opcode == DAT_ACK)
        ac_data_message = decode_game_message(ac_data_reply.payload)
        ac_data_fields = decode_proto(ac_data_message.payload[16:])
        assert [field.number for field in ac_data_fields] == [1]
        assert first_field(ac_data_fields, 1).value == b"\x00" + raw_ac_data

        ai_info_request = encode_game_request(
            GameMessage(
                GET_AI_INFO,
                b"\x00\x00\x00\x13",
                proto_varint(1, 42) + proto_varint(2, 0),
                7,
            )
        )
        ai_info_packet = encode_reliable_packet(
            ReliablePacket(107, 0, DAT, payload=ai_info_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                ai_info_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        ai_info_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            ai_info_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        ai_info_reply = next(packet for packet in ai_info_packets if packet.opcode == DAT_ACK)
        ai_info_message = decode_game_message(ai_info_reply.payload)
        ai_info_fields = decode_proto(ai_info_message.payload[16:])
        assert [field.number for field in ai_info_fields] == [1, 2, 3, 4]
        assert first_field(ai_info_fields, 4).value == b"\x00"

        mission_request = encode_game_request(
            GameMessage(GET_MISSION_SAVEDATA, b"\x00\x00\x00\x15", b"", 8)
        )
        mission_packet = encode_reliable_packet(
            ReliablePacket(108, 0, DAT, payload=mission_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                mission_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        mission_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            mission_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        mission_reply = next(packet for packet in mission_packets if packet.opcode == DAT_ACK)
        mission_message = decode_game_message(mission_reply.payload)
        mission_fields = decode_proto(mission_message.payload[16:])
        assert first_field(mission_fields, 1).value == b"\x00"

        server.teams.create(
            online_id="Pilot",
            player_id=42,
            name="test",
            operation_base=2,
            language=2,
            play_type=1,
            enlistment_method=1,
        )
        update_team_settings_request = encode_game_request(
            GameMessage(
                UPDATE_LEADER_TEAM_INFO,
                b"\x00\x00\x00\x17",
                bytes.fromhex("08021200180022050a0a0a0a0a"),
                9,
            )
        )
        update_team_settings_packet = encode_reliable_packet(
            ReliablePacket(109, 0, DAT, payload=update_team_settings_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                update_team_settings_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        update_team_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            update_team_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        update_team_reply = next(
            packet for packet in update_team_packets if packet.opcode == DAT_ACK
        )
        assert decode_game_message(update_team_reply.payload).payload[16:] == b""
        updated_team = server.teams.team_for("Pilot")
        assert updated_team is not None
        assert updated_team["play_type"] == 1
        assert updated_team["pay_apportionment"] == 0
        assert updated_team["enlistment_method"] == 2
        assert updated_team["recruitment_open"] is True

        destination = server.teams.create(
            online_id="Leader",
            player_id=66,
            name="Destination",
            operation_base=2,
            language=0,
            play_type=0,
            enlistment_method=2,
        )
        server.teams.state["members"].pop("Pilot")
        server.teams._save()
        auto_join_payload = proto_bytes(
            1,
            proto_varint(1, 0) + proto_varint(2, destination["team_id"]),
        )
        auto_join_request = encode_game_request(
            GameMessage(JOIN_TEAM_AUTO, b"\x00\x00\x00\x18", auto_join_payload, 10)
        )
        auto_join_packet = encode_reliable_packet(
            ReliablePacket(110, 0, DAT, payload=auto_join_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                auto_join_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        auto_join_packets = []
        for _ in range(3):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            auto_join_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        auto_join_reply = next(packet for packet in auto_join_packets if packet.opcode == DAT_ACK)
        assert decode_game_message(auto_join_reply.payload).payload[16:] == b""
        auto_join_push = next(packet for packet in auto_join_packets if packet.opcode == DAT)
        auto_join_push_message = decode_game_message(auto_join_push.payload)
        assert int.from_bytes(auto_join_push_message.payload[:4], "big") == 3
        assert auto_join_push_message.payload[4:] == b""
        assert server.teams.team_for("Pilot")["team_id"] == destination["team_id"]
        assert [record["online_id"] for record in destination["member_records"]] == [
            "Leader",
            "Pilot",
        ]

        mercenary_registration = (
            proto_varint(1, 0)
            + proto_varint(2, 1)
            + proto_bytes(3, local_ac_data)
            + proto_varint(4, 0)
            + proto_varint(5, 0)
            + proto_varint(6, 1)
            + proto_varint(7, 3)
        )
        register_mercenary_request = encode_game_request(
            GameMessage(REGISTER_MERCENARY, b"\x00\x00\x00\x19", mercenary_registration, 11)
        )
        register_mercenary_packet = encode_reliable_packet(
            ReliablePacket(111, 0, DAT, payload=register_mercenary_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                register_mercenary_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        register_mercenary_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            register_mercenary_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        register_mercenary_reply = next(
            packet for packet in register_mercenary_packets if packet.opcode == DAT_ACK
        )
        register_mercenary_message = decode_game_message(register_mercenary_reply.payload)
        assert first_field(decode_proto(register_mercenary_message.payload[16:]), 1).value == 0
        assert server.teams.available_mercenaries()[0]["online_id"] == "Pilot"

        server.teams.register_mercenary(
            online_id="Helper",
            player_id=77,
            ac_data=b"\x01helper-ac",
            result_values=[0, 0, 1, 3],
        )
        assert server.game_protocol is not None
        server.game_protocol.active_mercenaries.add("Helper")
        server.game_sessions[b"helper1"] = GameSession(
            b"helper1",
            bytes(range(16)),
            "Helper",
            77,
            established=True,
            peer=("127.0.0.1", 9),
        )
        search_mercenary_request = encode_game_request(
            GameMessage(SEARCH_MERCENARY, b"\x00\x00\x00\x1b", proto_varint(11, 2), 12)
        )
        search_mercenary_packet = encode_reliable_packet(
            ReliablePacket(112, 0, DAT, payload=search_mercenary_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                search_mercenary_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        search_mercenary_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            search_mercenary_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        search_mercenary_reply = next(
            packet for packet in search_mercenary_packets if packet.opcode == DAT_ACK
        )
        search_mercenary_message = decode_game_message(search_mercenary_reply.payload)
        search_fields = decode_proto(search_mercenary_message.payload[16:])
        assert len([field for field in search_fields if field.number == 1]) == 1
        candidate = decode_proto(first_field(search_fields, 1).value)
        assert first_field(candidate, 1).value == 77
        assert first_field(candidate, 2).value == b"Helper"
        assert first_field(candidate, 4).value == b"\x01helper-ac"

        get_mercenary_data_request = encode_game_request(
            GameMessage(GET_MERCENARY_DATA, b"\x00\x00\x00\x1c", proto_varint(1, 77), 13)
        )
        get_mercenary_data_packet = encode_reliable_packet(
            ReliablePacket(113, 0, DAT, payload=get_mercenary_data_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                get_mercenary_data_packet,
                game_key,
                game_info[:8],
                0,
                config.native_library_path,
            ),
            ("127.0.0.1", game_port),
        )
        get_mercenary_data_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            get_mercenary_data_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        get_mercenary_data_reply = next(
            packet for packet in get_mercenary_data_packets if packet.opcode == DAT_ACK
        )
        get_mercenary_data_message = decode_game_message(get_mercenary_data_reply.payload)
        get_mercenary_data_fields = decode_proto(get_mercenary_data_message.payload[16:])
        assert [field.number for field in get_mercenary_data_fields] == [1]
        assert first_field(get_mercenary_data_fields, 1).value == b"\x01helper-ac"
        assert first_field(get_mercenary_data_fields, 1).value != b"\x00" + raw_ac_data

        send_contract_request = encode_game_request(
            GameMessage(START_MERCENARY_NEGOTIATION, b"\x00\x00\x00\x1d", proto_varint(1, 77), 14)
        )
        send_contract_packet = encode_reliable_packet(
            ReliablePacket(114, 0, DAT, payload=send_contract_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                send_contract_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        contract_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            contract_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        contract_reply = next(packet for packet in contract_packets if packet.opcode == DAT_ACK)
        assert decode_game_message(contract_reply.payload).payload[16:] == b""
        queued = server.teams.mercenary_messages_for("Helper")
        assert len(queued) == 1 and queued[0]["employer_online_id"] == "Pilot"

        send_message_request = encode_game_request(
            GameMessage(
                SEND_MERCENARY_MESSAGE,
                b"\x00\x00\x00\x1e",
                proto_bytes(1, proto_varint(1, 77) + proto_varint(2, 88800)),
                15,
            )
        )
        send_message_packet = encode_reliable_packet(
            ReliablePacket(115, 0, DAT, payload=send_message_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                send_message_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        feedback_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            feedback_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        denied_feedback = next(packet for packet in feedback_packets if packet.opcode == DAT_ACK)
        assert decode_game_message(denied_feedback.payload).payload[:8] == bytes.fromhex(
            "000000010000001c"
        )
        # A post-contract message cannot create another offer before acceptance.
        assert len(server.teams.mercenary_messages_for("Helper")) == 1
        assert not (config.player_data_path / "77.personal-notices.json").exists()

        cancel_contract_request = encode_game_request(
            GameMessage(CANCEL_MERCENARY_NEGOTIATION, b"\x00\x00\x00\x1f", b"", 16)
        )
        cancel_contract_packet = encode_reliable_packet(
            ReliablePacket(116, 0, DAT, payload=cancel_contract_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                cancel_contract_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        for _ in range(2):
            await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
        assert server.teams.mercenary_messages_for("Helper") == []

        uploaded_ai = (
            proto_varint(1, 3)
            + proto_bytes(2, "UNAC 3")
            + proto_varint(3, 44)
            + proto_bytes(4, b"\x01" + zlib.compress(b"ai-payload"))
        )
        delivered_ai_preseed = config.player_data_path / "42.ai-3.preseed.bin"
        delivered_ai_preseed.write_bytes(uploaded_ai)
        server.game_sessions[game_info[:8]].ai_preseeds_delivered[3] = delivered_ai_preseed
        upload_ai_request = encode_game_request(
            GameMessage(UPLOAD_AI_INFO, b"\x00\x00\x00\x21", uploaded_ai, 20)
        )
        upload_ai_packet = encode_reliable_packet(
            ReliablePacket(117, 0, DAT, payload=upload_ai_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                upload_ai_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        for _ in range(2):
            await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
        assert (config.player_data_path / "42.ai-3.bin").read_bytes() == uploaded_ai
        assert not delivered_ai_preseed.exists()
        applied_ai_preseeds = list(config.player_data_path.glob("42.ai-3.preseed.applied-*.bin"))
        assert len(applied_ai_preseeds) == 1
        assert applied_ai_preseeds[0].read_bytes() == uploaded_ai

        get_uploaded_ai_request = encode_game_request(
            GameMessage(
                GET_AI_INFO,
                b"\x00\x00\x00\x23",
                proto_varint(1, 42) + proto_varint(2, 3),
                21,
            )
        )
        get_uploaded_ai_packet = encode_reliable_packet(
            ReliablePacket(118, 0, DAT, payload=get_uploaded_ai_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                get_uploaded_ai_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        uploaded_ai_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            uploaded_ai_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        uploaded_ai_reply = next(
            packet for packet in uploaded_ai_packets if packet.opcode == DAT_ACK
        )
        assert decode_game_message(uploaded_ai_reply.payload).payload[16:] == uploaded_ai

        mission_savedata = b"\x01mission-payload"
        upload_mission_request = encode_game_request(
            GameMessage(
                UPLOAD_MISSION_SAVEDATA,
                b"\x00\x00\x00\x25",
                proto_bytes(1, mission_savedata),
                22,
            )
        )
        upload_mission_packet = encode_reliable_packet(
            ReliablePacket(119, 0, DAT, payload=upload_mission_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                upload_mission_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        for _ in range(2):
            await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
        assert (
            config.player_data_path / "42.mission-savedata.bin"
        ).read_bytes() == mission_savedata

        get_mission_request = encode_game_request(
            GameMessage(GET_MISSION_SAVEDATA, b"\x00\x00\x00\x27", b"", 23)
        )
        get_mission_packet = encode_reliable_packet(
            ReliablePacket(120, 0, DAT, payload=get_mission_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                get_mission_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        mission_roundtrip_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            mission_roundtrip_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        mission_roundtrip_reply = next(
            packet for packet in mission_roundtrip_packets if packet.opcode == DAT_ACK
        )
        mission_roundtrip_fields = decode_proto(
            decode_game_message(mission_roundtrip_reply.payload).payload[16:]
        )
        assert first_field(mission_roundtrip_fields, 1).value == mission_savedata

        dlc_payload = b"".join(
            proto_bytes(1, proto_varint(1, part_id)) for part_id in (1101, 1102, 1103)
        )
        dlc_request = encode_game_request(
            GameMessage(SYNCHRONIZE_DLC_PARTS_LINEUP, b"\x00\x00\x00\x29", dlc_payload, 24)
        )
        dlc_packet = encode_reliable_packet(ReliablePacket(121, 0, DAT, payload=dlc_request))
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                dlc_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        dlc_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            dlc_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        dlc_reply = next(packet for packet in dlc_packets if packet.opcode == DAT_ACK)
        dlc_fields = decode_proto(decode_game_message(dlc_reply.payload).payload[16:])
        assert [first_field(decode_proto(field.value), 1).value for field in dlc_fields] == [
            1101,
            1102,
            1103,
        ]

        # Keep this synthetic application blob below the single-datagram test
        # helper's fragmentation threshold. The real 1,184-byte projection is
        # covered by test_extract_mission_snapshot.
        mission_raw = bytes((index * 29) & 0xFF for index in range(512))
        mission_preseed = b"\x01" + zlib.compress(mission_raw, level=1)
        mission_preseed_path = config.player_data_path / "42.mission-savedata.preseed.bin"
        mission_preseed_path.write_bytes(mission_preseed)
        get_preseed_request = encode_game_request(
            GameMessage(GET_MISSION_SAVEDATA, b"\x00\x00\x00\x2b", b"", 25)
        )
        get_preseed_packet = encode_reliable_packet(
            ReliablePacket(122, 0, DAT, payload=get_preseed_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                get_preseed_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        preseed_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            preseed_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, game_key, config.native_library_path)
                )
            )
        preseed_reply = next(packet for packet in preseed_packets if packet.opcode == DAT_ACK)
        preseed_fields = decode_proto(decode_game_message(preseed_reply.payload).payload[16:])
        assert first_field(preseed_fields, 1).value == mission_preseed

        conflicting_mission = b"\x00" + mission_raw[:-1] + b"\xff"
        upload_conflict_request = encode_game_request(
            GameMessage(
                UPLOAD_MISSION_SAVEDATA,
                b"\x00\x00\x00\x2d",
                proto_bytes(1, conflicting_mission),
                26,
            )
        )
        upload_conflict_packet = encode_reliable_packet(
            ReliablePacket(123, 0, DAT, payload=upload_conflict_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                upload_conflict_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        for _ in range(2):
            await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
        assert (
            config.player_data_path / "42.mission-savedata.bin"
        ).read_bytes() == mission_savedata
        assert mission_preseed_path.read_bytes() == mission_preseed
        assert (
            config.player_data_path / "42.mission-savedata.deferred.bin"
        ).read_bytes() == conflicting_mission

        equivalent_mission = b"\x01" + zlib.compress(mission_raw, level=9)
        upload_equivalent_request = encode_game_request(
            GameMessage(
                UPLOAD_MISSION_SAVEDATA,
                b"\x00\x00\x00\x2f",
                proto_bytes(1, equivalent_mission),
                27,
            )
        )
        upload_equivalent_packet = encode_reliable_packet(
            ReliablePacket(124, 0, DAT, payload=upload_equivalent_request)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                upload_equivalent_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        for _ in range(2):
            await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
        assert (
            config.player_data_path / "42.mission-savedata.bin"
        ).read_bytes() == equivalent_mission
        assert not mission_preseed_path.exists()
        applied_mission_preseeds = list(
            config.player_data_path.glob("42.mission-savedata.preseed.applied-*.bin")
        )
        assert len(applied_mission_preseeds) == 1
        assert applied_mission_preseeds[0].read_bytes() == mission_preseed

        fin_packet = encode_reliable_packet(ReliablePacket(208, 0, FIN))
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                fin_packet, game_key, game_info[:8], 0, config.native_library_path
            ),
            ("127.0.0.1", game_port),
        )
        fin_data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
        fin_reply = decode_reliable_packet(
            decrypt_server_datagram(fin_data, game_key, config.native_library_path)
        )
        assert fin_reply.opcode == FIN_ACK
        assert fin_reply.remote_sequence == 208
        udp.close()
        writer.close()
        await writer.wait_closed()
        events = [json.loads(line) for line in config.telemetry_path.read_text().splitlines()]
        auth_event = next(event for event in events if event["event"] == "authentication_complete")
        assert "auth_token" not in auth_event
        assert "game_key" not in auth_event
        assert auth_event["auth_token_length"] == 8
        assert auth_event["game_key_length"] == 16
        assert config.session_state_path.is_file()
        assert config.session_state_path.stat().st_mode & 0o777 == 0o600
    finally:
        await server.close()


async def test_two_encrypted_clients_join_and_exchange_mercenary_contract(tmp_path: Path) -> None:
    (tmp_path / "keys").mkdir()
    (tmp_path / "native").mkdir()
    (tmp_path / "logs").mkdir()
    generate_rsa_keypair(tmp_path / "keys/server-private.pem", tmp_path / "keys/publickey.pem")
    shutil.copy2(ROOT / "native/libacvd_cwc.so", tmp_path / "native/libacvd_cwc.so")
    config = ServerConfig(
        bind_host="127.0.0.1",
        advertised_host="127.0.0.1",
        login_ports=[free_port()],
        auth_port=free_port(),
        game_port=free_port(socket.SOCK_DGRAM),
        cdn_port=free_port(),
        project_root=tmp_path,
    )
    config.regulation_path.parent.mkdir(parents=True)
    config.regulation_path.write_bytes(b"test-regulation")
    server = AcvdServer(config)
    await server.start()
    loop = asyncio.get_running_loop()

    async def connect_client(
        online_id: str,
        player_id: int,
        token: bytes,
        key: bytes,
        sequence: int,
    ) -> socket.socket:
        next_sequences[token] = (sequence + 1) % 4096
        server.game_sessions[token] = GameSession(token, key, online_id, player_id)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setblocking(False)
        udp.bind(("127.0.0.1", 0))
        prefix = online_id.encode().ljust(17, b"\0") + b"\0" + online_id.encode().ljust(17, b"\0")
        syn = encode_reliable_packet(
            ReliablePacket(
                sequence, 0, SYN, payload=b"\x12\x10\x20\x20\x00\x00\xa0\x00", initial_prefix=prefix
            )
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(syn, key, token, 1, config.native_library_path),
            ("127.0.0.1", config.game_port),
        )
        replies = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 4096), 1)
            replies.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, key, config.native_library_path)
                )
            )
        syn_ack = next(reply for reply in replies if reply.opcode == SYN_ACK)
        ack = encode_reliable_packet(ReliablePacket(0, syn_ack.local_sequence, ACK))
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(ack, key, token, 0, config.native_library_path),
            ("127.0.0.1", config.game_port),
        )
        return udp

    # Native packet order is independent of application correlation IDs.
    # The former fixture reused and skipped reliable sequences as test labels.
    next_sequences: dict[bytes, int] = {}

    def allocate_sequence(token: bytes) -> int:
        sequence = next_sequences[token]
        next_sequences[token] = (sequence + 1) % 4096
        return sequence

    async def request(
        udp: socket.socket,
        token: bytes,
        key: bytes,
        sequence: int,
        message_type: int,
        payload: bytes,
    ) -> bytes:
        encoded = encode_game_request(
            GameMessage(message_type, sequence.to_bytes(4, "big"), payload, sequence)
        )
        packet = encode_reliable_packet(
            ReliablePacket(allocate_sequence(token), 0, DAT, payload=encoded)
        )
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(packet, key, token, 0, config.native_library_path),
            ("127.0.0.1", config.game_port),
        )
        replies = []
        for _ in range(4):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 65535), 1)
            received = decode_reliable_packet(
                decrypt_server_datagram(data, key, config.native_library_path)
            )
            replies.append(received)
            if received.opcode == DAT:
                push_ack = encode_reliable_packet(ReliablePacket(0, received.local_sequence, ACK))
                await loop.sock_sendto(
                    udp,
                    encrypt_client_datagram(push_ack, key, token, 0, config.native_library_path),
                    ("127.0.0.1", config.game_port),
                )
            if received.opcode == DAT_ACK:
                break
        reply = next(item for item in replies if item.opcode == DAT_ACK)
        return decode_game_message(reply.payload).payload[16:]

    async def receive_typed_push(udp: socket.socket, token: bytes, key: bytes) -> tuple[int, bytes]:
        while True:
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 65535), 1)
            packet = decode_reliable_packet(
                decrypt_server_datagram(data, key, config.native_library_path)
            )
            if packet.opcode != DAT:
                continue
            message = decode_game_message(packet.payload)
            push_ack = encode_reliable_packet(ReliablePacket(0, packet.local_sequence, ACK))
            await loop.sock_sendto(
                udp,
                encrypt_client_datagram(push_ack, key, token, 0, config.native_library_path),
                ("127.0.0.1", config.game_port),
            )
            return int.from_bytes(message.payload[:4], "big"), message.payload[4:]

    leader_token, helper_token = b"leader01", b"helper01"
    leader_key, helper_key = bytes(range(16)), bytes(range(16, 32))
    leader_udp = helper_udp = None
    try:
        leader_udp = await connect_client("Leader", 10, leader_token, leader_key, 100)
        helper_udp = await connect_client("Helper", 20, helper_token, helper_key, 200)
        team = server.teams.create(
            online_id="Leader",
            player_id=10,
            name="Free Team",
            operation_base=2,
            language=2,
            play_type=0,
            enlistment_method=2,
        )

        faction_reply = decode_proto(
            await request(
                leader_udp,
                leader_token,
                leader_key,
                117,
                JOIN_FACTION,
                proto_varint(1, 2) + proto_varint(2, 3),
            )
        )
        assert first_field(faction_reply, 1).value == 2
        assert first_field(faction_reply, 2).value == 3
        assert server.teams.team_for("Leader")["faction_id"] == 2
        assert server.teams.team_for("Leader")["current_faction_id"] == 2

        assert (
            await request(
                leader_udp,
                leader_token,
                leader_key,
                119,
                GET_JOINING_TEAM_STATUS,
                proto_bytes(1, proto_varint(1, 0) + proto_varint(2, 32)),
            )
            == b""
        )

        leader_settings = (
            proto_varint(1, 2)
            + proto_bytes(2, "Leader")
            + proto_varint(3, 1)
            + proto_bytes(4, "line one\nline two")
        )
        assert (
            await request(
                leader_udp,
                leader_token,
                leader_key,
                101,
                UPDATE_LEADER_TEAM_INFO,
                leader_settings,
            )
            == b""
        )
        for sequence, field_number, value in (
            (102, 14, 15),
            (103, 26, 2),
            (104, 27, 6),
        ):
            assert (
                await request(
                    leader_udp,
                    leader_token,
                    leader_key,
                    sequence,
                    UPDATE_TEAM_FACTION_INFO,
                    proto_varint(1, team["team_id"]) + proto_varint(field_number, value),
                )
                == b""
            )
        color = b"\x08\x01\x10\x02\x18\x03\x20\x04"
        assert (
            await request(
                leader_udp,
                leader_token,
                leader_key,
                105,
                UPDATE_TEAM_FACTION_INFO,
                proto_varint(1, team["team_id"])
                + proto_bytes(11, color)
                + proto_bytes(24, "single-line comment")
                + proto_varint(13, 1)  # The newly created team owns its F-rank title.
                + proto_varint(20, 11)
                + proto_varint(25, 1),
            )
            == b""
        )
        assert (
            await request(
                leader_udp,
                leader_token,
                leader_key,
                106,
                UPDATE_USER_PROPERTY,
                proto_varint(6, 1),
            )
            == b""
        )
        assert (
            await request(
                leader_udp,
                leader_token,
                leader_key,
                107,
                UPDATE_MEMBER_TEAM_INFO,
                proto_varint(1, 2) + proto_varint(2, 0),
            )
            == b""
        )
        assert (
            await request(
                leader_udp,
                leader_token,
                leader_key,
                108,
                UPDATE_TEAM_MEMBER_COMMENT,
                proto_bytes(1, "ready for sorties"),
            )
            == b""
        )

        comment_reply = decode_proto(
            await request(leader_udp, leader_token, leader_key, 109, GET_COMMENT, b"")
        )
        assert first_field(comment_reply, 1).value == 0
        assert first_field(comment_reply, 2).value == b"ready for sorties"

        register_emblem_reply = decode_proto(
            await request(
                leader_udp,
                leader_token,
                leader_key,
                110,
                REGISTER_EMBLEM,
                proto_bytes(1, b"authoring metadata")
                + proto_bytes(2, b"\xff\xd8test jpeg\xff\xd9")
                + proto_varint(3, 0),
            )
        )
        assert [field.number for field in register_emblem_reply] == [1]
        assigned_ugc_id = first_field(register_emblem_reply, 1).value
        assert assigned_ugc_id >= 200000

        applied_emblem = b"\x01" + zlib.compress(bytes(1808))
        applied_reply = decode_proto(
            await request(
                leader_udp,
                leader_token,
                leader_key,
                112,
                SET_PLAYER_EMBLEM_BY_UGC_ID,
                proto_varint(1, assigned_ugc_id) + proto_bytes(2, applied_emblem),
            )
        )
        assert first_field(applied_reply, 1).value == 1
        emblem_reply = decode_proto(
            await request(
                helper_udp,
                helper_token,
                helper_key,
                204,
                GET_PLAYER_EMBLEM,
                proto_varint(1, 10),
            )
        )
        assert first_field(emblem_reply, 1).value == applied_emblem
        assert first_field(emblem_reply, 2).value == assigned_ugc_id

        garage_register_reply = decode_proto(
            await request(
                leader_udp,
                leader_token,
                leader_key,
                113,
                REGISTER_GARAGE_CUSTOM_INFO,
                proto_bytes(1, proto_varint(1, 16) + proto_varint(2, 127)),
            )
        )
        assert first_field(garage_register_reply, 1).value == 0
        garage_reply = decode_proto(
            await request(
                leader_udp,
                leader_token,
                leader_key,
                114,
                GET_GARAGE_CUSTOM_INFO,
                b"",
            )
        )
        garage_info = decode_proto(first_field(garage_reply, 1).value)
        assert first_field(garage_info, 1).value == 16
        assert first_field(garage_info, 2).value == 127

        ai_extend = bytes.fromhex(
            "0044c5140044c5140044bbb40044bbb40044e86e0044e86e0044b3670044988c"
            "0044a1ba005250a80044e6c5"
        )
        update_extend_reply = decode_proto(
            await request(
                leader_udp,
                leader_token,
                leader_key,
                115,
                UPDATE_AI_EXTEND,
                proto_varint(1, 0) + proto_bytes(2, ai_extend),
            )
        )
        assert first_field(update_extend_reply, 1).value == ai_extend
        get_extend_reply = decode_proto(
            await request(
                leader_udp,
                leader_token,
                leader_key,
                116,
                GET_AI_EXTEND,
                b"",
            )
        )
        assert first_field(get_extend_reply, 1).value == ai_extend
        assert server.game_protocol is not None
        assert (
            server.game_protocol.player_data_path / "10.ai-extend.bin"
        ).read_bytes() == ai_extend

        battle_result_request = bytes.fromhex(
            "10d00f1a11089fc31b100018012000289811300038001a11089fc31b100118012000"
            "28840730003800220b08e80710d00f1885072000220b08e80710d00f1886072000"
            "30033802"
        )
        battle_result_reply = decode_proto(
            await request(
                leader_udp,
                leader_token,
                leader_key,
                111,
                REGISTER_BATTLE_RESULT,
                battle_result_request,
            )
        )
        assert battle_result_reply == []
        # The captured payload belongs to another player and must not mutate
        # the authenticated account's canonical money.
        assert server.teams.balance("Leader") == 1_000_000

        # This store has a confirmed wallet; missing baselines must not mint cash.
        server.teams.synchronize_balance("Leader", 1_000_000)
        authenticated_result = (
            proto_varint(2, 110)
            + proto_bytes(
                3,
                proto_varint(1, 10)
                + proto_varint(2, 0)
                + proto_varint(3, 1)
                + proto_varint(4, 18_184)
                + proto_varint(5, 6_360)
                + proto_varint(6, 0)
                + proto_varint(7, 0),
            )
            + proto_varint(6, 3)
            + proto_varint(7, 2)
        )
        # The same application index is one exact retry, even when it
        # arrives on a new reliable datagram. A new index is a separate
        # completed mission, despite identical result bytes.
        for counter in (112, 112, 113):
            settlement_reply = decode_proto(
                await request(
                    leader_udp,
                    leader_token,
                    leader_key,
                    counter,
                    REGISTER_BATTLE_RESULT,
                    authenticated_result,
                )
            )
            assert [field.number for field in settlement_reply] == [1]
            reward = decode_proto(first_field(settlement_reply, 1).value)
            assert [first_field(reward, number).value for number in range(3, 8)] == [
                150_000,
                0,
                0,
                0,
                150_000,
            ]
            assert first_field(reward, 9).value == 10
        assert server.teams.balance("Leader") == 1_250_912
        assert len(server.teams.state["story_rewards"]) == 2
        assert len(server.teams.state["battle_settlements"]) == 2

        # No player listings is a valid initialized shop response and must not
        # be confused with the authenticated player's owned-parts inventory.
        assert (
            await request(
                leader_udp,
                leader_token,
                leader_key,
                118,
                GET_PLAYER_SHOP_LINEUP_FOR_PARTS,
                proto_varint(2, 3) + proto_varint(3, 0) + proto_varint(4, 512),
            )
            == b""
        )

        wait_reply = decode_proto(
            await request(
                leader_udp,
                leader_token,
                leader_key,
                121,
                GET_WAIT_TIME_FOR_APPLY_WORLD_TOP_SS,
                b"",
            )
        )
        assert first_field(wait_reply, 1).value == 0

        screenshot_registration = (
            proto_varint(1, 10)
            + proto_varint(2, 2)
            + proto_varint(3, 0xFFFFFFFF)
            + proto_varint(4, 501)
            + proto_bytes(5, b"test caption")
            + proto_bytes(6, b"full-image" * 20)
            + proto_bytes(7, b"auxiliary-image" * 8)
            + proto_bytes(8, b"thumbnail" * 5)
        )
        assert (
            await request(
                leader_udp,
                leader_token,
                leader_key,
                122,
                REGISTER_PLAYER_SCREENSHOT,
                screenshot_registration,
            )
            == b""
        )
        assert (
            await request(
                leader_udp,
                leader_token,
                leader_key,
                123,
                APPLY_WORLD_TOP_SCREENSHOT,
                proto_bytes(1, b"local-application-token"),
            )
            == b""
        )
        assert (
            config.player_data_path / "10.world-top-screenshot.bin"
        ).read_bytes() == screenshot_registration
        assert (
            config.player_data_path / "10.world-top-application.bin"
        ).read_bytes() == proto_bytes(1, b"local-application-token")

        thumbnail_list = decode_proto(
            await request(
                helper_udp,
                helper_token,
                helper_key,
                203,
                GET_WORLD_TOP_SS_THUMBNAIL_LIST,
                proto_varint(1, 5),
            )
        )
        assert [field.number for field in thumbnail_list] == [1]
        thumbnail = decode_proto(thumbnail_list[0].value)
        assert [field.number for field in thumbnail] == [1, 2, 3, 4, 5]
        screenshot_id_bytes = first_field(thumbnail, 1).value
        assert first_field(thumbnail, 1).wire_type == 1
        assert first_field(thumbnail, 2).value == 2
        assert first_field(thumbnail, 3).value == b"thumbnail" * 5
        assert first_field(thumbnail, 4).value == 0xFFFFFFFF
        assert first_field(thumbnail, 5).value == 501

        full_screenshot = decode_proto(
            await request(
                helper_udp,
                helper_token,
                helper_key,
                204,
                GET_WORLD_TOP_SCREENSHOT,
                encode_varint((1 << 3) | 1) + screenshot_id_bytes,
            )
        )
        assert [field.number for field in full_screenshot] == [1, 2, 3, 4, 5, 6, 7]
        assert first_field(full_screenshot, 1).value == screenshot_id_bytes
        assert first_field(full_screenshot, 2).value == b"Leader"
        assert first_field(full_screenshot, 3).value == b"test caption"
        assert first_field(full_screenshot, 4).value == 0xFFFFFFFF
        assert first_field(full_screenshot, 5).value == 501
        assert first_field(full_screenshot, 6).value == b"full-image" * 20
        assert first_field(full_screenshot, 7).value == b"auxiliary-image" * 8

        # The self defeat-ranking response is a repeated-only message; an
        # empty new-season list is initialized and must not fall through to
        # the generic unknown-request stub.
        assert (
            await request(
                leader_udp,
                leader_token,
                leader_key,
                120,
                GET_MY_DEFEAT_RANKING_INFO,
                proto_varint(1, 3),
            )
            == b""
        )

        join_payload = proto_bytes(
            1,
            proto_varint(1, 0) + proto_varint(2, team["team_id"]),
        )
        assert (
            await request(helper_udp, helper_token, helper_key, 201, JOIN_TEAM_AUTO, join_payload)
            == b""
        )
        assert server.teams.team_for("Helper")["team_id"] == team["team_id"]
        leader_profile_subtype, leader_profile_payload = await receive_typed_push(
            leader_udp, leader_token, leader_key
        )
        helper_profile_subtype, helper_profile_payload = await receive_typed_push(
            helper_udp, helper_token, helper_key
        )
        assert leader_profile_subtype == helper_profile_subtype == 3
        assert leader_profile_payload == helper_profile_payload == b""

        team_info_reply = await request(
            leader_udp, leader_token, leader_key, 110, GET_TEAM_INFO, b""
        )
        team_info = decode_proto(first_field(decode_proto(team_info_reply), 1).value)
        members = [decode_proto(field.value) for field in team_info if field.number == 28]
        assert [first_field(member, 1).value for member in members] == [10, 20]
        assert [first_field(member, 2).value for member in members] == [1, 1]
        assert first_field(members[0], 4).value == 2
        assert first_field(team_info, 9).value == 0
        assert first_field(team_info, 10).value == 2
        assert first_field(team_info, 11).value == 2
        assert first_field(team_info, 12).value == 2
        assert first_field(team_info, 13).value == 0
        assert first_field(team_info, 23) is None
        assert first_field(team_info, 14).value == b"single-line comment"
        assert first_field(team_info, 21).value == 15
        assert first_field(team_info, 15).value == 2
        assert first_field(team_info, 26).value == b"line one\nline two"
        assert first_field(team_info, 25).value == 11
        assert first_field(team_info, 27).value == 2
        assert first_field(team_info, 29).value == 6
        assert first_field(team_info, 31).value == 1
        team_color = decode_proto(first_field(team_info, 19).value)
        assert [first_field(team_color, number).value for number in range(1, 5)] == [1, 2, 3, 4]
        assert server.teams.player_profile("Leader")["status"] == "ready for sorties"
        assert server.teams.player_profile("Leader")["language"] == 1
        assert server.teams.player_profile("Leader")["member_id"] == 2

        leader_identity = b"\x01" + b"Leader".ljust(36, b"\0")
        helper_identity = b"\x01" + b"Helper".ljust(36, b"\0")
        leader_session = decode_proto(
            await request(
                leader_udp,
                leader_token,
                leader_key,
                130,
                CREATE_TEAM_SESSION,
                proto_bytes(1, leader_identity),
            )
        )
        external_session_id = first_field(leader_session, 1).value
        assert external_session_id == team["team_id"]
        helper_member_id = max(1, int(server.teams.player_profile("Helper").get("member_id", 1)))
        await request(
            helper_udp,
            helper_token,
            helper_key,
            230,
            CREATE_TEAM_SESSION,
            proto_bytes(1, helper_identity),
        )

        await request(
            helper_udp,
            helper_token,
            helper_key,
            231,
            CHECK_VALID_PLAYER,
            b"",
        )
        helper_join_subtype, _ = await receive_typed_push(helper_udp, helper_token, helper_key)
        await request(
            leader_udp,
            leader_token,
            leader_key,
            131,
            GET_WORLD_CURRENT_INFO,
            b"",
        )
        leader_join_subtype, leader_join_payload = await receive_typed_push(
            leader_udp, leader_token, leader_key
        )
        assert leader_join_subtype == helper_join_subtype == 8
        # Join push field 4 is the native active-peer enum, not the durable
        # team number returned by CreateTeamSession.
        assert first_field(decode_proto(leader_join_payload), 4).value == 1

        assert (
            await request(
                helper_udp,
                helper_token,
                helper_key,
                232,
                LEAVE_TEAM_SESSION,
                proto_varint(1, external_session_id)
                + proto_varint(2, helper_member_id)
                + proto_varint(3, 0),
            )
            == b""
        )
        await request(
            leader_udp,
            leader_token,
            leader_key,
            132,
            GET_WORLD_CURRENT_INFO,
            b"",
        )
        leave_subtype, leave_payload = await receive_typed_push(
            leader_udp, leader_token, leader_key
        )
        # Exact ELF factory: wire 7 changes host; wire 9 removes a member.
        assert leave_subtype == 9
        leave_fields = decode_proto(leave_payload)
        assert [first_field(leave_fields, number).value for number in (1, 2)] == [
            external_session_id,
            20,  # Native leave lookup uses global player ID, not team slot.
        ]

        registration = (
            proto_varint(1, 0)
            + proto_varint(2, 1)
            + proto_bytes(3, b"\x01helper-ac")
            + proto_varint(4, 0)
            + proto_varint(5, 0)
            + proto_varint(6, 1)
            + proto_varint(7, 3)
        )
        await request(helper_udp, helper_token, helper_key, 202, REGISTER_MERCENARY, registration)
        # Teammates cannot discover or hire one another as mercenaries.
        assert (
            await request(
                leader_udp,
                leader_token,
                leader_key,
                107,
                SEARCH_MERCENARY,
                proto_varint(11, 2),
            )
            == b""
        )
        assert (
            await request(
                leader_udp,
                leader_token,
                leader_key,
                109,
                START_MERCENARY_NEGOTIATION,
                proto_varint(1, 20),
            )
            == b""
        )
        assert (
            await request(
                helper_udp,
                helper_token,
                helper_key,
                203,
                GET_MERCENARY_MESSAGE_LIST,
                b"",
            )
            == b""
        )

        assert server.teams.leave_team("Helper") is not None
        server.teams.create(
            online_id="Helper",
            player_id=20,
            name="Mercenary Team",
            operation_base=2,
            language=0,
            play_type=0,
            enlistment_method=0,
        )
        server.teams.set_faction("Helper", 1, 2)
        search_reply = await request(
            leader_udp,
            leader_token,
            leader_key,
            208,
            SEARCH_MERCENARY,
            proto_varint(11, 2),
        )
        candidate = decode_proto(first_field(decode_proto(search_reply), 1).value)
        assert first_field(candidate, 1).value == 20
        assert first_field(candidate, 2).value == b"Helper"

        assert (
            await request(
                leader_udp,
                leader_token,
                leader_key,
                209,
                START_MERCENARY_NEGOTIATION,
                proto_varint(1, 20),
            )
            == b""
        )
        inbox_reply = await request(
            helper_udp,
            helper_token,
            helper_key,
            210,
            GET_MERCENARY_MESSAGE_LIST,
            b"",
        )
        inbox = decode_proto(inbox_reply)
        assert len(inbox) == 1
        message = decode_proto(first_field(inbox, 1).value)
        assert first_field(message, 3).value == b"Leader"
        assert first_field(message, 7).value == b"Helper"

        assert (
            await request(
                helper_udp,
                helper_token,
                helper_key,
                204,
                CONTINUE_MERCENARY_REGISTRATION,
                b"",
            )
            == b""
        )
        assert "Helper" in server.game_protocol.active_mercenaries

        def prepare_payload(player_id: int) -> bytes:
            return (
                proto_varint(1, 3)
                + proto_varint(2, 2)
                + proto_varint(3, 1)
                + proto_bytes(6, proto_varint(1, player_id) + proto_varint(2, 0))
            )

        leader_prepare = decode_proto(
            await request(
                leader_udp,
                leader_token,
                leader_key,
                120,
                REGISTER_PREPARE,
                prepare_payload(10),
            )
        )
        assert first_field(leader_prepare, 1).value == 1

        helper_prepare_request = encode_game_request(
            GameMessage(
                REGISTER_PREPARE,
                (220).to_bytes(4, "big"),
                prepare_payload(20),
                220,
            )
        )
        await loop.sock_sendto(
            helper_udp,
            encrypt_client_datagram(
                encode_reliable_packet(
                    ReliablePacket(
                        allocate_sequence(helper_token), 0, DAT, payload=helper_prepare_request
                    )
                ),
                helper_key,
                helper_token,
                0,
                config.native_library_path,
            ),
            ("127.0.0.1", config.game_port),
        )
        helper_packets = []
        for _ in range(2):
            data, _ = await asyncio.wait_for(loop.sock_recvfrom(helper_udp, 65535), 1)
            helper_packets.append(
                decode_reliable_packet(
                    decrypt_server_datagram(data, helper_key, config.native_library_path)
                )
            )
        leader_push_data, _ = await asyncio.wait_for(loop.sock_recvfrom(leader_udp, 65535), 1)
        leader_push_packet = decode_reliable_packet(
            decrypt_server_datagram(leader_push_data, leader_key, config.native_library_path)
        )
        leader_push = decode_game_message(leader_push_packet.payload)
        assert int.from_bytes(leader_push.payload[:4], "big") == 1
        leader_match = decode_proto(leader_push.payload[4:])
        assert [first_field(leader_match, number).value for number in (1, 2, 3, 9)] == [
            1,
            2,
            1,
            1,
        ]
        assert [first_field(leader_match, number).value for number in range(4, 8)] == [
            1,
            1,
            2,
            0,
        ]

        assert (
            await request(
                leader_udp,
                leader_token,
                leader_key,
                121,
                CANCEL_PREPARE,
                proto_varint(1, 1),
            )
            == b""
        )
        assert (
            await request(
                helper_udp,
                helper_token,
                helper_key,
                221,
                CANCEL_PREPARE,
                proto_varint(1, 2),
            )
            == b""
        )
        assert server.game_protocol is not None
        assert sorted(server.game_protocol.preparations) == ["Helper", "Leader"]
        assert list(server.game_protocol.matches) == [1]

        assert (
            await request(
                leader_udp,
                leader_token,
                leader_key,
                122,
                NOTIFY_MATCH_ROOM_INFO,
                proto_varint(1, 1) + proto_bytes(2, b"\x00" + b"NXRV\x03\xf2" + bytes(506)),
            )
            == b""
        )
        release_subtype, release_payload = await receive_typed_push(
            helper_udp, helper_token, helper_key
        )
        assert release_subtype == 1
        release_fields = decode_proto(release_payload)
        assert [first_field(release_fields, number).value for number in (1, 2, 3)] == [
            2,
            1,
            0,
        ]
        assert [first_field(release_fields, number).value for number in range(4, 8)] == [
            1,
            0,
            2,
            0,
        ]
        assert first_field(release_fields, 9).value == 1
        room_subtype, room_payload = await receive_typed_push(helper_udp, helper_token, helper_key)
        assert room_subtype == 4
        room_fields = decode_proto(room_payload)
        assert [(field.number, field.value) for field in room_fields] == [
            (1, 2),
            (2, 1),
            (
                3,
                b"\x00" + b"NXRV\x03\xf2" + bytes(506),
            ),
        ]

        # Retail uses recipient player ID field 1 and amount field 3. The
        # empty acknowledgement commits one durable atomic transfer.
        # Establish this synthetic recipient's wallet through the real sync
        # handler instead of relying on the rejected missing-balance fallback.
        helper_money = decode_proto(
            await request(
                helper_udp,
                helper_token,
                helper_key,
                221,
                SYNC_MONEY,
                proto_varint(2, 0) + proto_varint(3, 1_000_000) + proto_varint(4, 100_000),
            )
        )
        assert first_field(helper_money, 1).value == 1_000_000
        assert (
            await request(
                leader_udp,
                leader_token,
                leader_key,
                123,
                TRADE_MONEY,
                proto_varint(1, 20) + proto_varint(3, 250_000),
            )
            == b""
        )
        assert server.teams.balance("Leader") == 1_000_912
        assert server.teams.balance("Helper") == 1_250_000
        assert (
            await request(
                helper_udp,
                helper_token,
                helper_key,
                222,
                TRADE_MONEY,
                proto_varint(1, 10) + proto_varint(3, 100_000),
            )
            == b""
        )
        assert server.teams.balance("Leader") == 1_100_912
        assert server.teams.balance("Helper") == 1_150_000

        assert (
            await request(
                helper_udp,
                helper_token,
                helper_key,
                223,
                UNREGISTER_MERCENARY,
                # Retail leaves registration with this scalar false; the opcode,
                # not the value, authorizes removal.
                proto_varint(1, 0),
            )
            == b""
        )
        assert "Helper" not in server.game_protocol.active_mercenaries
        assert server.teams.mercenary_for_player_id(20) is None
    finally:
        if leader_udp is not None:
            leader_udp.close()
        if helper_udp is not None:
            helper_udp.close()
        await server.close()
