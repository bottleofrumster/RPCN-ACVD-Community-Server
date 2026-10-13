#!/usr/bin/env python3
"""Stage ACVD server UNAC snapshots from BLUS31194 AI000-AI008 saves."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import tempfile
import zlib
from pathlib import Path

AI_FILE_SIZE = 54_072
AI_NETWORK_HEAD_SIZE = 26_320
AI_NETWORK_TAIL_OFFSET = 42_720
AI_NETWORK_RAW_SIZE = 37_672
AI_NAME_OFFSET = 42_736
AI_NAME_SIZE = 32
AI_VALUE_OFFSET = 24_516
AI_SLOT_COUNT = 9


def _varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("protobuf varints must be non-negative")
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _proto_varint(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _proto_bytes(number: int, value: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _fixed_utf16be(raw: bytes) -> str:
    end = len(raw)
    for offset in range(0, len(raw) - 1, 2):
        if raw[offset : offset + 2] == b"\x00\x00":
            end = offset
            break
    return raw[:end].decode("utf-16-be", errors="strict")


def extract_snapshot(ai_save: bytes, slot: int) -> tuple[bytes, dict[str, object]]:
    """Build one RequestUploadAiInfo payload from a local AI save.

    Retail's network representation omits the 16,400-byte local-only region
    at offsets 26,320..42,719.  The two retained ranges concatenate to the
    exact 37,672-byte application payload observed in all nine live slots.
    """
    if not 1 <= int(slot) <= AI_SLOT_COUNT:
        raise ValueError(f"AI slot must be between 1 and {AI_SLOT_COUNT}")
    if len(ai_save) != AI_FILE_SIZE:
        raise ValueError(f"AI save is {len(ai_save)} bytes; exactly {AI_FILE_SIZE} are required")

    raw = ai_save[:AI_NETWORK_HEAD_SIZE] + ai_save[AI_NETWORK_TAIL_OFFSET:]
    if len(raw) != AI_NETWORK_RAW_SIZE:
        raise AssertionError("internal AI network projection length mismatch")
    name = _fixed_utf16be(ai_save[AI_NAME_OFFSET : AI_NAME_OFFSET + AI_NAME_SIZE])
    value = struct.unpack_from(">I", ai_save, AI_VALUE_OFFSET)[0]
    application_payload = b"\x01" + zlib.compress(raw)
    stored = (
        _proto_varint(1, int(slot))
        + _proto_bytes(2, name.encode("utf-8"))
        + _proto_varint(3, value)
        + _proto_bytes(4, application_payload)
    )
    return stored, {
        "schema": "acvd-ai-preseed-v1",
        "slot": int(slot),
        "source_length": len(ai_save),
        "network_head_length": AI_NETWORK_HEAD_SIZE,
        "local_only_offset": AI_NETWORK_HEAD_SIZE,
        "local_only_length": AI_NETWORK_TAIL_OFFSET - AI_NETWORK_HEAD_SIZE,
        "network_raw_length": len(raw),
        "name": name,
        "value": value,
        "source_sha256": hashlib.sha256(ai_save).hexdigest(),
        "network_raw_sha256": hashlib.sha256(raw).hexdigest(),
        "stored_sha256": hashlib.sha256(stored).hexdigest(),
        "stored_length": len(stored),
    }


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temporary:
        temporary.write(payload)
        temporary.flush()
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("savedata", type=Path, help="source BLUS31194GAME*/ directory")
    parser.add_argument(
        "output", type=Path, help="staging directory; never installed automatically"
    )
    parser.add_argument(
        "--player-id", type=int, required=True, help="authenticated server player ID"
    )
    parser.add_argument("--force", action="store_true", help="replace existing staged outputs")
    args = parser.parse_args()

    staged: list[tuple[Path, bytes, dict[str, object]]] = []
    for index in range(AI_SLOT_COUNT):
        source = args.savedata / f"AI{index:03}.DAT"
        if not source.is_file():
            raise FileNotFoundError(f"missing {source}")
        stored, metadata = extract_snapshot(source.read_bytes(), index + 1)
        target = args.output / f"{args.player_id}.ai-{index + 1}.preseed.bin"
        metadata.update({"source": str(source), "output": str(target)})
        staged.append((target, stored, metadata))

    conflicts = [str(target) for target, _, _ in staged if target.exists()]
    if conflicts and not args.force:
        raise FileExistsError(
            "refusing to overwrite staged output(s); pass --force after preserving them: "
            + ", ".join(conflicts)
        )
    for target, stored, _ in staged:
        _write_atomic(target, stored)
    print(
        json.dumps(
            {"schema": "acvd-ai-preseed-set-v1", "slots": [item[2] for item in staged]},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
