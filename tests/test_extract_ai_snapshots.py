from __future__ import annotations

import struct
import zlib

import pytest

from acvd_server.protocol import decode_proto, first_field
from tools.extract_ai_snapshots import (
    AI_FILE_SIZE,
    AI_NAME_OFFSET,
    AI_NETWORK_HEAD_SIZE,
    AI_NETWORK_TAIL_OFFSET,
    AI_VALUE_OFFSET,
    extract_snapshot,
)


def test_extract_snapshot_projects_local_ai_save_into_retail_payload() -> None:
    ai_save = bytearray((index * 29) & 0xFF for index in range(AI_FILE_SIZE))
    name = "UNAC Alpha"
    ai_save[AI_NAME_OFFSET : AI_NAME_OFFSET + 32] = b"\x00" * 32
    encoded_name = name.encode("utf-16-be")
    ai_save[AI_NAME_OFFSET : AI_NAME_OFFSET + len(encoded_name)] = encoded_name
    struct.pack_into(">I", ai_save, AI_VALUE_OFFSET, 171_910)

    stored, metadata = extract_snapshot(bytes(ai_save), 2)
    fields = decode_proto(stored)
    raw = zlib.decompress(first_field(fields, 4).value[1:])

    assert first_field(fields, 1).value == 2
    assert first_field(fields, 2).value == name.encode()
    assert first_field(fields, 3).value == 171_910
    assert raw == bytes(ai_save[:AI_NETWORK_HEAD_SIZE] + ai_save[AI_NETWORK_TAIL_OFFSET:])
    assert metadata["network_raw_length"] == 37_672
    assert metadata["local_only_length"] == 16_400


def test_extract_snapshot_preserves_leading_spaces_in_name() -> None:
    ai_save = bytearray(AI_FILE_SIZE)
    name = "   Slot One"
    ai_save[AI_NAME_OFFSET : AI_NAME_OFFSET + len(name.encode("utf-16-be"))] = name.encode(
        "utf-16-be"
    )
    stored, _ = extract_snapshot(bytes(ai_save), 1)
    assert first_field(decode_proto(stored), 2).value == name.encode()


def test_extract_snapshot_rejects_wrong_length_or_slot() -> None:
    with pytest.raises(ValueError, match="exactly"):
        extract_snapshot(b"\x00" * (AI_FILE_SIZE - 1), 1)
    with pytest.raises(ValueError, match="between"):
        extract_snapshot(b"\x00" * AI_FILE_SIZE, 0)
