"""TCP framing/route contracts; no RPCN or live server required."""

import asyncio
import struct

import pytest

from acvd_server import connections
from acvd_server.config import ServerConfig
from acvd_server.protocol import (
    Message,
    Packet,
    ProtocolError,
    decode_message,
    decode_packet,
    encode_message,
    encode_packet,
)


class Writer:
    def __init__(self):
        self.frames = []
        self.closed = False
        self.drained = 0

    def get_extra_info(self, key):
        return {"peername": ("127.0.0.1", 40000), "sockname": ("127.0.0.1", 50011)}.get(key)

    def write(self, data):
        self.frames.append(data)

    async def drain(self):
        self.drained += 1

    def close(self):
        self.closed = True

    async def wait_closed(self):
        assert self.closed


class Telemetry:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


def connection(max_packet_length=65535):
    reader, writer, telemetry = asyncio.StreamReader(), Writer(), Telemetry()
    channel = connections.Frpg2Connection(
        reader, writer, ServerConfig(max_packet_length=max_packet_length), telemetry, "test"
    )
    return channel, reader, writer, telemetry


async def test_receive_preserves_message_identity_and_decrypts_payload():
    channel, reader, _, telemetry = connection()
    reader.feed_data(encode_packet(Packet(7, encode_message(Message(5, 99, b"encrypted")))))
    message = await channel.read_message(lambda data: b"plain:" + data)
    assert (message.msg_type, message.msg_index, message.payload) == (5, 99, b"plain:encrypted")
    assert telemetry.events[0][1]["encrypted_payload"] == b"encrypted"
    assert telemetry.events[0][1]["plaintext_payload"] == b"plain:encrypted"


async def test_send_encrypts_once_and_wraps_packet_counter():
    channel, _, writer, telemetry = connection()
    channel.send_counter = 65535
    await channel.send_message(
        b"plain", msg_type=5, msg_index=99, encrypt=lambda data: b"cipher:" + data
    )
    frame = writer.frames[0]
    assert int.from_bytes(frame[:2], "big") == len(frame) - 2
    packet = decode_packet(frame[2:])
    message = decode_message(packet.payload)
    assert packet.send_counter == 0 and writer.drained == 1
    assert (message.msg_type, message.msg_index, message.payload) == (5, 99, b"cipher:plain")
    assert telemetry.events[0][1]["plaintext_payload"] == b"plain"


async def test_empty_send_does_not_call_encryption_callback():
    channel, _, writer, _ = connection()

    def forbidden(_):
        raise AssertionError("Empty payload must not invoke encryption")

    await channel.send_message(b"", msg_type=5, msg_index=1, encrypt=forbidden)
    assert decode_message(decode_packet(writer.frames[0][2:]).payload).payload == b""


@pytest.mark.parametrize("length", [0, 101])
async def test_invalid_frame_bound_rejected_before_body_read(length):
    channel, reader, _, telemetry = connection(max_packet_length=100)
    reader.feed_data(struct.pack(">H", length))
    with pytest.raises(ProtocolError, match="invalid frame length"):
        await channel.read_message()
    assert not telemetry.events


async def test_truncated_stream_propagates_failure_and_can_close():
    channel, reader, writer, telemetry = connection()
    reader.feed_data(struct.pack(">H", 20) + b"partial")
    reader.feed_eof()
    with pytest.raises(asyncio.IncompleteReadError):
        await channel.read_message()
    assert not telemetry.events
    await channel.close()
    assert writer.closed


def test_game_endpoint_record_retains_fixed_native_layout():
    raw = connections._game_server_info("127.0.0.1", 52103, b"token123")
    assert len(raw) == 0x38
    assert raw[:12] == b"token123\x7f\x00\x00\x01"
    assert int.from_bytes(raw[12:14], "big") == 52103


@pytest.mark.parametrize("port,token", [(0, b"token123"), (65536, b"token123"), (52103, b"bad")])
def test_invalid_endpoint_record_is_rejected(port, token):
    with pytest.raises(ValueError):
        connections._game_server_info("127.0.0.1", port, token)
