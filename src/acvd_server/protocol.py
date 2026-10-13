"""Encode and decode shared framing and protobuf wire primitives."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

PACKET_HEADER = struct.Struct(">HBBIHH")
RESPONSE_HEADER = struct.Struct(">IIII")
MESSAGE_HEADER_SIZE = 12
PACKET_HEADER_SIZE = PACKET_HEADER.size


def _typed_push_payload(subtype: int, payload: bytes = b"") -> bytes:
    """Encode an internal push type using the retail zero-based wire ID.

    ACVD's dispatcher numbers typed pushes from one while its receive factory
    selects parser cases from zero. The complete factory jump table proves the
    contiguous mapping, including internal JoinSessionMember type 9 -> wire 8.
    Resolve each message name from its factory case, not neighboring names.
    """
    if not 1 <= int(subtype) <= 0x100000000:
        raise ValueError("push subtype must fit in a one-based 32-bit range")
    return struct.pack(">I", int(subtype) - 1) + bytes(payload)


def _protobuf_summary(payload: bytes) -> list[dict[str, object]]:
    return [
        {"number": field.number, "wire_type": field.wire_type, "value": field.value}
        for field in decode_proto(payload)
    ]


class ProtocolError(ValueError):
    pass


@dataclass(slots=True)
class Packet:
    send_counter: int
    payload: bytes
    unknown_1: int = 0
    unknown_2: int = 0
    unknown_3: int = 0


@dataclass(slots=True)
class Message:
    msg_type: int
    msg_index: int
    payload: bytes
    response_header: tuple[int, int, int, int] | None = None


@dataclass(slots=True)
class ProtoField:
    number: int
    wire_type: int
    value: int | bytes


def encode_packet(packet: Packet) -> bytes:
    size = len(packet.payload)
    if size > 0xFFFF:
        raise ProtocolError(f"packet payload too large: {size}")
    header = PACKET_HEADER.pack(
        packet.send_counter & 0xFFFF,
        packet.unknown_1 & 0xFF,
        packet.unknown_2 & 0xFF,
        size,
        packet.unknown_3 & 0xFFFF,
        size,
    )
    body = header + packet.payload
    if len(body) > 0xFFFF:
        raise ProtocolError(f"framed packet too large: {len(body)}")
    return struct.pack(">H", len(body)) + body


def decode_packet(body: bytes, *, max_length: int = 65535) -> Packet:
    if len(body) < PACKET_HEADER_SIZE:
        raise ProtocolError("packet shorter than header")
    counter, u1, u2, payload_length, u3, payload_length_short = PACKET_HEADER.unpack_from(body)
    if payload_length > max_length:
        raise ProtocolError(f"declared payload exceeds limit: {payload_length}")
    if payload_length != payload_length_short:
        raise ProtocolError("long and short payload lengths disagree")
    payload = body[PACKET_HEADER_SIZE:]
    if len(payload) != payload_length:
        raise ProtocolError(f"payload length mismatch: {len(payload)} != {payload_length}")
    return Packet(counter, payload, u1, u2, u3)


def encode_message(message: Message) -> bytes:
    header = struct.pack(">II", MESSAGE_HEADER_SIZE, message.msg_type & 0xFFFFFFFF)
    header += struct.pack("<I", message.msg_index & 0xFFFFFFFF)
    if message.msg_type == 0:
        response = message.response_header or (0, 1, 0, 0)
        header += RESPONSE_HEADER.pack(*response)
    return header + message.payload


def decode_message(payload: bytes) -> Message:
    if len(payload) < MESSAGE_HEADER_SIZE:
        raise ProtocolError("message shorter than header")
    header_size, msg_type = struct.unpack_from(">II", payload)
    msg_index = struct.unpack_from("<I", payload, 8)[0]
    if header_size != MESSAGE_HEADER_SIZE:
        raise ProtocolError(f"unexpected message header size: {header_size}")
    offset = MESSAGE_HEADER_SIZE
    response = None
    if msg_type == 0:
        if len(payload) < offset + RESPONSE_HEADER.size:
            raise ProtocolError("response shorter than response header")
        response = RESPONSE_HEADER.unpack_from(payload, offset)
        offset += RESPONSE_HEADER.size
    return Message(msg_type, msg_index, payload[offset:], response)


def encode_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    for index in range(offset, min(len(data), offset + 10)):
        byte = data[index]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index + 1
        shift += 7
    raise ProtocolError("invalid or truncated varint")


def proto_varint(number: int, value: int) -> bytes:
    return encode_varint((number << 3) | 0) + encode_varint(value)


def proto_bytes(number: int, value: bytes | str) -> bytes:
    raw = value.encode() if isinstance(value, str) else value
    return encode_varint((number << 3) | 2) + encode_varint(len(raw)) + raw


def decode_proto(data: bytes) -> list[ProtoField]:
    fields: list[ProtoField] = []
    offset = 0
    while offset < len(data):
        tag, offset = decode_varint(data, offset)
        number, wire_type = tag >> 3, tag & 7
        if number == 0:
            raise ProtocolError("protobuf field number zero")
        if wire_type == 0:
            value, offset = decode_varint(data, offset)
        elif wire_type == 1:
            if offset + 8 > len(data):
                raise ProtocolError("truncated fixed64")
            value = data[offset : offset + 8]
            offset += 8
        elif wire_type == 2:
            length, offset = decode_varint(data, offset)
            if offset + length > len(data):
                raise ProtocolError("truncated length-delimited field")
            value = data[offset : offset + length]
            offset += length
        elif wire_type == 5:
            if offset + 4 > len(data):
                raise ProtocolError("truncated fixed32")
            value = data[offset : offset + 4]
            offset += 4
        else:
            raise ProtocolError(f"unsupported protobuf wire type: {wire_type}")
        fields.append(ProtoField(number, wire_type, value))
    return fields


def first_field(fields: Iterable[ProtoField], number: int) -> ProtoField | None:
    return next((field for field in fields if field.number == number), None)


def _proto_datetime(value: datetime) -> bytes:
    value = value.astimezone(timezone.utc)
    return b"".join(
        proto_varint(number, field_value)
        for number, field_value in enumerate(
            (
                value.year,
                value.month,
                value.day,
                value.hour,
                value.minute,
                value.second,
                value.microsecond // 1000,
            ),
            start=1,
        )
    )
