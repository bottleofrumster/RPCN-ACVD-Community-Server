"""Validate and relay the native room archive without rebuilding its records."""

import zlib

from .protocol import proto_bytes, proto_varint

ROOM_ARCHIVE_SIZE = 512
MAX_ENCODED_ROOM_SIZE = 1024


def validate_room_archive(encoded: bytes) -> None:
    """Bound decompression and require the observed NXR archive envelope."""
    if not isinstance(encoded, bytes) or not 1 < len(encoded) <= MAX_ENCODED_ROOM_SIZE:
        raise ValueError("Room archive size is invalid")
    if encoded[0] == 0:
        archive = encoded[1:]
    elif encoded[0] == 1:
        try:
            decoder = zlib.decompressobj()
            archive = decoder.decompress(encoded[1:], ROOM_ARCHIVE_SIZE + 1)
        except zlib.error as error:
            raise ValueError("Room archive compression is invalid") from error
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("Room archive stream is incomplete or has extra data")
    else:
        raise ValueError("Room archive encoding is unknown")
    if len(archive) != ROOM_ARCHIVE_SIZE or archive[:6] != b"NXRV\x03\xf2":
        raise ValueError("Room archive header or expanded size is invalid")


def native_room_info_push(recipient_prepare_id: int, host_prepare_id: int, encoded: bytes) -> bytes:
    """Keep native archive bytes; correlate the recipient and host preparations.

    The host builder copies its preparation into task+0x6c0. The receiving
    RoomInfo reader copies field 2 to the same output and decodes field 3.
    Field 1 selects the recipient's active preparation. See the room-21 audit.
    """
    for value in (recipient_prepare_id, host_prepare_id):
        if type(value) is not int or not 0 < value < 1 << 64:
            raise ValueError("Room preparation ID is invalid")
    return (
        proto_varint(1, recipient_prepare_id)
        + proto_varint(2, host_prepare_id)
        + proto_bytes(3, encoded)
    )
