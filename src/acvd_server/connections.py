"""FRPG2 TCP framing and route-aware endpoint advertisement.

This layer owns per-connection counters and framed reads/writes, not account or
matchmaking state. It does not preserve TCP sockets across server restarts.
Local route selection must not leak LAN endpoints to internet clients.
"""

from __future__ import annotations

import asyncio
import socket
import struct
from ipaddress import IPv4Address
from typing import Callable

from .config import ServerConfig
from .protocol import (
    Message,
    Packet,
    ProtocolError,
    decode_message,
    decode_packet,
    encode_message,
    encode_packet,
)
from .telemetry import Telemetry


class Frpg2Connection:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        config: ServerConfig,
        telemetry: Telemetry,
        service: str,
    ):
        self.reader = reader
        self.writer = writer
        self.config = config
        self.telemetry = telemetry
        self.service = service
        self.send_counter = 0
        self.peer = writer.get_extra_info("peername")

    async def read_message(self, decrypt: Callable[[bytes], bytes] | None = None) -> Message:
        prefix = await self.reader.readexactly(2)
        length = struct.unpack(">H", prefix)[0]
        if length == 0 or length > self.config.max_packet_length:
            raise ProtocolError(f"invalid frame length: {length}")
        packet = decode_packet(
            await self.reader.readexactly(length), max_length=self.config.max_packet_length
        )
        message = decode_message(packet.payload)
        encrypted = message.payload
        if decrypt is not None and encrypted:
            message.payload = decrypt(encrypted)
        self.telemetry.emit(
            "message_received",
            service=self.service,
            peer=self.peer,
            packet_counter=packet.send_counter,
            message_type=message.msg_type,
            message_index=message.msg_index,
            encrypted_payload=encrypted,
            plaintext_payload=message.payload,
        )
        return message

    async def send_message(
        self,
        payload: bytes,
        *,
        msg_type: int,
        msg_index: int,
        encrypt: Callable[[bytes], bytes] | None = None,
    ) -> None:
        plaintext = payload
        if encrypt is not None and payload:
            payload = encrypt(payload)
        message = Message(msg_type, msg_index, payload)
        self.send_counter = (self.send_counter + 1) & 0xFFFF
        frame = encode_packet(Packet(self.send_counter, encode_message(message)))
        self.writer.write(frame)
        await self.writer.drain()
        self.telemetry.emit(
            "message_sent",
            service=self.service,
            peer=self.peer,
            packet_counter=self.send_counter,
            message_type=msg_type,
            message_index=msg_index,
            plaintext_payload=plaintext,
            encrypted_payload=payload,
        )

    async def close(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()


def _game_server_info(host: str, port: int, token: bytes) -> bytes:
    if len(token) != 8:
        raise ValueError("game auth token must be 8 bytes")
    if not 0 < port <= 0xFFFF:
        raise ValueError("game port must be between 1 and 65535")
    values = [0x8000, 0x8000, 0xA000, 0xA000, 0x80, 0x8000, 0xA000, 0x493E0, 0x61A8, 0x0C]
    # ACVD's native Frpg2GameServerInfo is a compact 0x38-byte record.
    # Unlike the later DS3 variant, the address is a packed IPv4 value.
    return token + IPv4Address(host).packed + struct.pack(">HH10I", port, 0, *values)


def _advertised_host_for_route(configured_host: str, peer_host: str, local_host: str) -> str:
    """Choose an endpoint that is reachable from the connecting client.

    A public endpoint is required for internet players, but returning that same
    address to the host's loopback client can fail on routers without NAT
    hairpinning.  The accepted TCP connection already gives us the correct
    route: loopback clients receive loopback, LAN clients receive the server's
    LAN interface, and internet clients receive the configured public address.
    """
    peer = IPv4Address(peer_host)
    local = IPv4Address(local_host)
    if peer.is_loopback:
        return "127.0.0.1"
    if peer.is_private and not local.is_unspecified:
        return str(local)
    return str(IPv4Address(configured_host))


def _local_host_for_peer(peer_host: str) -> str:
    """Return the source address the kernel would use to reach a peer."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route_probe:
        route_probe.connect((peer_host, 9))
        return str(route_probe.getsockname()[0])


def _advertised_host_for_connection(connection: Frpg2Connection) -> str:
    peer = connection.peer
    sockname = connection.writer.get_extra_info("sockname")
    if not peer or not sockname:
        return connection.config.advertised_host
    try:
        peer_host = str(peer[0])
        local_host = str(sockname[0])
        peer_address = IPv4Address(peer_host)
        if peer_address.is_private and not peer_address.is_loopback:
            try:
                local_host = _local_host_for_peer(peer_host)
            except OSError:
                pass
        return _advertised_host_for_route(
            connection.config.advertised_host,
            peer_host,
            local_host,
        )
    except ValueError:
        return connection.config.advertised_host
