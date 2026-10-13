"""Retail screenshot validation and response encoding.

Full-image and thumbnail schemas differ. IDs derive from owner and content
without a second mutable index. Disk lifecycle remains in the request handler.
"""

from __future__ import annotations

import hashlib

from .protocol import ProtocolError, decode_proto, encode_varint, proto_bytes, proto_varint


def _required_screenshot_registration(payload: bytes):
    """Decode retail's four-scalar/four-blob screenshot registration."""
    try:
        fields = decode_proto(payload)
    except ProtocolError:
        return None
    expected_wire_types = {1: 0, 2: 0, 3: 0, 4: 0, 5: 2, 6: 2, 7: 2, 8: 2}
    result = {}
    for number, wire_type in expected_wire_types.items():
        matches = [field for field in fields if field.number == number]
        if len(matches) != 1 or matches[0].wire_type != wire_type:
            return None
        result[number] = matches[0]
    return result


def _proto_fixed64(number: int, value: int) -> bytes:
    return encode_varint((number << 3) | 1) + int(value).to_bytes(8, "little")


def _world_top_screenshot_id(player_id: int, registration: bytes) -> int:
    """Return a stable nonzero ID without introducing a second catalog."""
    digest = hashlib.sha256(int(player_id).to_bytes(8, "big") + registration).digest()
    return int.from_bytes(digest[:8], "big") or 1


def _world_top_thumbnail_info(registration: bytes) -> bytes | None:
    fields = _required_screenshot_registration(registration)
    if fields is None:
        return None
    player_id = int(fields[1].value)
    screenshot_id = _world_top_screenshot_id(player_id, registration)
    # Retail SSThumbnailInfo requires fixed64 field 1, scalar field 2,
    # thumbnail bytes field 3, and scalar metadata fields 4-5.
    return (
        _proto_fixed64(1, screenshot_id)
        + proto_varint(2, int(fields[2].value))
        + proto_bytes(3, fields[8].value)
        + proto_varint(4, int(fields[3].value))
        + proto_varint(5, int(fields[4].value))
    )


def _world_top_screenshot(registration: bytes, online_id: str) -> bytes | None:
    fields = _required_screenshot_registration(registration)
    if fields is None:
        return None
    player_id = int(fields[1].value)
    screenshot_id = _world_top_screenshot_id(player_id, registration)
    # The full retail response requires fields 1-7: ID, owner name, comment,
    # two scalar metadata values, full image data, and auxiliary image data.
    # The thumbnail remains in SSThumbnailInfo and is not duplicated here.
    return (
        _proto_fixed64(1, screenshot_id)
        + proto_bytes(2, online_id)
        + proto_bytes(3, fields[5].value)
        + proto_varint(4, int(fields[3].value))
        + proto_varint(5, int(fields[4].value))
        + proto_bytes(6, fields[6].value)
        + proto_bytes(7, fields[7].value)
    )
