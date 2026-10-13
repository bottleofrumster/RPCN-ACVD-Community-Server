"""Encrypted UDP persistence qualification with temporary, synthetic accounts."""

import asyncio
import socket
from pathlib import Path

from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.server import GameDatagramProtocol
from acvd_server.teams import TeamStore
from acvd_server.telemetry import Telemetry
from acvd_server.udp import (
    ACK,
    DAT,
    DAT_ACK,
    GET_MY_ITEM_SIMPLE_INFO_LIST_ALT,
    SYN,
    SYN_ACK,
    SYNC_OFFLINE_DATA,
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

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "native/libacvd_cwc.so"


async def test_encrypted_inventory_survives_invalid_upload_and_reauthentication(tmp_path):
    players = tmp_path / "players"
    players.mkdir()
    sessions = {}
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        sessions,
        LIBRARY,
        players,
        TeamStore(tmp_path / "teams"),
    )
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol,
        local_addr=("127.0.0.1", 0),
    )
    address = transport.get_extra_info("sockname")
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setblocking(False)
    udp.bind(("127.0.0.1", 0))
    token, key = b"retain01", bytes(range(16))

    async def send(packet, packet_type=0):
        wire = encrypt_client_datagram(
            encode_reliable_packet(packet),
            key,
            token,
            packet_type,
            LIBRARY,
        )
        await loop.sock_sendto(udp, wire, address)

    async def receive():
        raw, _ = await asyncio.wait_for(loop.sock_recvfrom(udp, 65535), 2)
        return decode_reliable_packet(decrypt_server_datagram(raw, key, LIBRARY))

    async def connect():
        sessions[token] = GameSession(token, key, "Pilot", 42)
        prefix = b"Pilot".ljust(17, b"\0") + b"\0" + b"Pilot".ljust(17, b"\0")
        await send(
            ReliablePacket(
                100,
                0,
                SYN,
                payload=b"\x12\x10\x20\x20\x00\x00\xa0\x00",
                initial_prefix=prefix,
            ),
            1,
        )
        replies = [await receive(), await receive()]
        assert {p.opcode for p in replies} == {SYN_ACK, ACK}
        syn_ack = next(p for p in replies if p.opcode == SYN_ACK)
        await send(ReliablePacket(0, syn_ack.local_sequence, ACK))

    async def request(sequence, message_type, payload):
        await send(
            ReliablePacket(
                sequence,
                0,
                DAT,
                payload=encode_game_request(
                    GameMessage(message_type, sequence.to_bytes(4, "big"), payload, sequence),
                ),
            )
        )
        for _ in range(4):
            reply = await receive()
            if reply.opcode == DAT_ACK:
                await send(ReliablePacket(0, reply.local_sequence, ACK))
                return decode_game_message(reply.payload).payload[16:]
        raise AssertionError("No correlated encrypted response")

    def record(instance, tuning):
        return proto_bytes(
            1,
            b"".join(
                proto_varint(n, v)
                for n, v in (
                    (1, 10),
                    (2, 401),
                    (3, 11),
                    (4, instance),
                    (5, tuning),
                )
            ),
        )

    # Same part/class, distinct 64-bit instances and tuning: no deduplication.
    instances = [(0x123456789ABCDEF0, 1123), (0x123456789ABCDEF1, 1456)]
    owned = b"".join(record(*item) for item in instances)
    auxiliary = proto_bytes(3, proto_varint(1, 0x20000) + proto_varint(2, 9001))
    readback_request = proto_varint(2, 3000) + proto_varint(3, 0) + proto_varint(4, 0)
    try:
        await connect()
        for sequence, payload in enumerate((owned, auxiliary, b"\x0a\x01\x08"), 101):
            response = await request(sequence, SYNC_OFFLINE_DATA, payload)
            assert first_field(decode_proto(response), 1).value == 0
        assert sessions[token].established
        assert (players / "42.offline-data.bin").read_bytes() == owned + auxiliary
        assert (players / "42.offline-data.deferred.bin").read_bytes() == b"\x0a\x01\x08"

        async def verify_items(sequence, expected=instances):
            reply = await request(sequence, GET_MY_ITEM_SIMPLE_INFO_LIST_ALT, readback_request)
            rows = [decode_proto(f.value) for f in decode_proto(reply) if f.number == 1]
            assert len(rows) == len(expected)
            assert [
                (first_field(row, 1).value, first_field(row, 6).value) for row in rows
            ] == expected
            assert all(first_field(row, 3).value == 401 for row in rows)
            assert all(first_field(row, 4).value == 11 for row in rows)
            assert all(first_field(row, 5).value == 1 for row in rows)

        await verify_items(104)
        # Simulate a new authenticated session, not a native login/save proof.
        sessions.clear()
        token, key = b"retain02", bytes(range(16, 32))
        await connect()
        await verify_items(101)
        assert (players / "42.offline-data.bin").read_bytes() == owned + auxiliary
        # A reviewed recovery is installed only in this synthetic player's
        # temp store. Its readback must agree with the upload-deferral guard.
        expanded = instances + [(0x123456789ABCDEF2, 1789)]
        reviewed = b"".join(record(*item) for item in expanded) + auxiliary
        preseed = players / "42.offline-data.preseed.bin"
        preseed.write_bytes(reviewed)
        await request(102, SYNC_OFFLINE_DATA, owned)
        await verify_items(103, expanded)
        assert preseed.read_bytes() == reviewed
        assert (players / "42.offline-data.bin").read_bytes() == owned + auxiliary
        await request(104, SYNC_OFFLINE_DATA, reviewed)
        assert not preseed.exists()
        assert (players / "42.offline-data.bin").read_bytes() == reviewed
        await verify_items(105, expanded)
    finally:
        udp.close()
        transport.close()
