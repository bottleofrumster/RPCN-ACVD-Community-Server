"""Encrypted loopback profile isolation; not native RPCN/login qualification."""

import asyncio
import socket
from copy import deepcopy
from pathlib import Path

from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.server import GameDatagramProtocol
from acvd_server.teams import TeamStore
from acvd_server.telemetry import Telemetry
from acvd_server.udp import (
    ACK,
    DAT,
    DAT_ACK,
    FIN,
    FIN_ACK,
    GET_MY_USER_INFO,
    SYN,
    SYN_ACK,
    UPDATE_USER_PROPERTY,
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

LIBRARY = Path(__file__).resolve().parents[1] / "native/libacvd_cwc.so"


async def test_two_encrypted_profiles_preserve_names_across_fin_and_store_reload(tmp_path):
    store = TeamStore(tmp_path / "teams")
    for name, number in (("Pilot", 11), ("Peer", 22)):
        store.remember_player(name, number)
    sessions = {}
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"), sessions, LIBRARY, tmp_path / "players", store
    )
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: protocol, local_addr=("127.0.0.1", 0)
    )
    address = transport.get_extra_info("sockname")
    sockets = []

    async def send(client, packet, initial=0):
        udp, session = client
        await loop.sock_sendto(
            udp,
            encrypt_client_datagram(
                encode_reliable_packet(packet),
                session.game_key,
                session.auth_token,
                initial,
                LIBRARY,
            ),
            address,
        )

    async def receive(client):
        udp, session = client
        data, sender = await asyncio.wait_for(loop.sock_recvfrom(udp, 65535), 2)
        assert sender == address
        return decode_reliable_packet(decrypt_server_datagram(data, session.game_key, LIBRARY))

    async def connect(name, number, token, key):
        # The auth registry is the test seam; real ticket/TCP login is not exercised.
        session = GameSession(token, key, name, number)
        sessions[token] = session
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sockets.append(udp)
        udp.setblocking(False)
        udp.bind(("127.0.0.1", 0))
        client = (udp, session)
        label = name.encode().ljust(17, b"\0")
        await send(
            client,
            ReliablePacket(
                100,
                0,
                SYN,
                payload=b"\x12\x10\x20\x20\x00\x00\xa0\x00",
                initial_prefix=label + b"\0" + label,
            ),
            initial=1,
        )
        replies = [await receive(client), await receive(client)]
        assert {reply.opcode for reply in replies} == {ACK, SYN_ACK}
        syn_ack = next(reply for reply in replies if reply.opcode == SYN_ACK)
        await send(client, ReliablePacket(0, syn_ack.local_sequence, ACK))
        return client

    async def request(client, sequence, message_type, payload=b""):
        message = GameMessage(message_type, sequence.to_bytes(4, "big"), payload, sequence)
        await send(client, ReliablePacket(sequence, 0, DAT, payload=encode_game_request(message)))
        replies = [await receive(client), await receive(client)]
        assert {reply.opcode for reply in replies} == {ACK, DAT_ACK}
        reply = next(reply for reply in replies if reply.opcode == DAT_ACK)
        assert reply.remote_sequence == sequence
        return decode_game_message(reply.payload).payload[16:]

    async def assert_info(client, sequence, expected):
        # Even the other player's real ID cannot redirect this self-only read.
        other_id = 22 if client[1].player_id == 11 else 11
        raw = await request(client, sequence, GET_MY_USER_INFO, proto_varint(1, other_id))
        fields = decode_proto(raw)
        assert first_field(fields, 1).value == client[1].player_id
        assert first_field(fields, 2).value == client[1].online_id.encode()
        assert first_field(fields, 3).value == expected
        return [(field.number, field.value) for field in fields if field.number != 3]

    try:
        pilot = await connect("Pilot", 11, b"pilot001", bytes(range(16)))
        peer = await connect("Peer", 22, b"peer0001", bytes(range(16, 32)))
        original = await asyncio.gather(
            assert_info(pilot, 101, b"Pilot"), assert_info(peer, 101, b"Peer")
        )
        # Equal request IDs on independent encrypted sessions must not share replies.
        replies = await asyncio.gather(
            request(pilot, 102, UPDATE_USER_PROPERTY, proto_bytes(2, "雷雨")),
            request(peer, 102, UPDATE_USER_PROPERTY, proto_bytes(2, "Peer Name")),
        )
        assert replies == [b"", b""]
        assert (
            await asyncio.gather(
                assert_info(pilot, 103, "雷雨".encode()), assert_info(peer, 103, b"Peer Name")
            )
            == original
        )
        assert await request(pilot, 104, UPDATE_USER_PROPERTY, proto_bytes(2, b"")) == b""
        assert (
            await request(peer, 104, UPDATE_USER_PROPERTY, proto_bytes(4, "Other property")) == b""
        )
        before = deepcopy(protocol.teams.state)
        await send(pilot, ReliablePacket(105, 0, FIN))
        reply = await receive(pilot)
        assert reply.opcode == FIN_ACK and reply.remote_sequence == 105
        assert not pilot[1].established and peer[1].established
        assert protocol.teams.state == before
        pilot[0].close()

        # Reload only the durable store, not a public server or emulator process.
        protocol.teams = TeamStore(store.root)
        assert protocol.teams.state == before
        pilot = await connect("Pilot", 11, b"pilot002", bytes(range(32, 48)))
        assert (
            await asyncio.gather(assert_info(pilot, 101, b""), assert_info(peer, 105, b"Peer Name"))
            == original
        )
        assert protocol.teams.state == before
    finally:
        for udp in sockets:
            udp.close()
        transport.close()
        await asyncio.sleep(0)
