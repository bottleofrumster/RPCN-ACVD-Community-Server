#!/usr/bin/env python3
"""Stage ACVD's server ownership protobuf from a BLUS31194 GPROG.DAT save."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

INVENTORY_COUNT_OFFSET = 0x950
INVENTORY_RECORDS_OFFSET = 0x958
INVENTORY_RECORD_SIZE = 16
MAX_INVENTORY_RECORDS = 16_384

# GPROG's class byte is the server record's field 3. Field 1 folds weapon
# sides/classes into the retail inventory category used by SyncOfflineData.
_source_root = str(Path(__file__).resolve().parents[2] / "src")
if _source_root not in sys.path:
    sys.path.insert(0, _source_root)
from acvd_server.inventory import PART_CATEGORY_FOR_CLASS as FIELD1_FOR_CLASS  # noqa: E402


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


def extract_inventory(gprog: bytes) -> tuple[bytes, dict[str, object]]:
    """Project GPROG's fixed inventory array into SyncOfflineData records."""
    if len(gprog) < INVENTORY_RECORDS_OFFSET:
        raise ValueError(
            f"GPROG.DAT is {len(gprog)} bytes; at least {INVENTORY_RECORDS_OFFSET} are required"
        )
    count = int.from_bytes(gprog[INVENTORY_COUNT_OFFSET : INVENTORY_COUNT_OFFSET + 4], "big")
    if count > MAX_INVENTORY_RECORDS:
        raise ValueError(f"inventory count {count} exceeds safety limit {MAX_INVENTORY_RECORDS}")
    required = INVENTORY_RECORDS_OFFSET + count * INVENTORY_RECORD_SIZE
    if len(gprog) < required:
        raise ValueError(
            f"GPROG.DAT inventory needs {required} bytes for {count} records; file has {len(gprog)}"
        )

    output = bytearray()
    class_counts: Counter[int] = Counter()
    tuned_count = 0
    nonzero_instance_count = 0
    for index in range(count):
        offset = INVENTORY_RECORDS_OFFSET + index * INVENTORY_RECORD_SIZE
        metadata = gprog[offset : offset + 8]
        instance_id = int.from_bytes(gprog[offset + 8 : offset + 16], "big")
        class_code = metadata[0]
        if class_code not in FIELD1_FOR_CLASS:
            raise ValueError(
                f"inventory record {index} has unknown class code {class_code} at 0x{offset:x}"
            )
        part_id = int.from_bytes(metadata[2:4], "big")
        # The three decimal tune digits are packed as high/low nibbles of
        # byte 4 and the high nibble of byte 5. Retail prefixes them with 1.
        tune_code = 1000 + (metadata[4] >> 4) * 100 + (metadata[4] & 0xF) * 10 + (metadata[5] >> 4)
        record = (
            _proto_varint(1, FIELD1_FOR_CLASS[class_code])
            + _proto_varint(2, part_id)
            + _proto_varint(3, class_code)
            + _proto_varint(4, instance_id)
            + _proto_varint(5, tune_code)
        )
        output.extend(_proto_bytes(1, record))
        class_counts[class_code] += 1
        tuned_count += tune_code != 1000
        nonzero_instance_count += instance_id != 0

    stored = bytes(output)
    return stored, {
        "schema": "acvd-offline-inventory-preseed-v1",
        "source_length": len(gprog),
        "count_offset": INVENTORY_COUNT_OFFSET,
        "records_offset": INVENTORY_RECORDS_OFFSET,
        "record_size": INVENTORY_RECORD_SIZE,
        "record_count": count,
        "nonzero_instance_count": nonzero_instance_count,
        "tuned_count": tuned_count,
        "class_counts": dict(sorted(class_counts.items())),
        "source_sha256": hashlib.sha256(gprog).hexdigest(),
        "stored_length": len(stored),
        "stored_sha256": hashlib.sha256(stored).hexdigest(),
    }


def _write_atomic(path: Path, payload: bytes, *, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise FileExistsError(f"refusing to overwrite {path}; pass --force after preserving it")
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temporary:
        temporary.write(payload)
        temporary.flush()
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gprog", type=Path, help="source BLUS31194GAME*/GPROG.DAT")
    parser.add_argument("output", type=Path, help="staged .offline-data.preseed.bin output")
    parser.add_argument("--force", action="store_true", help="replace an existing staged output")
    args = parser.parse_args()

    stored, metadata = extract_inventory(args.gprog.read_bytes())
    _write_atomic(args.output, stored, force=args.force)
    metadata.update({"source": str(args.gprog), "output": str(args.output)})
    print(json.dumps(metadata, sort_keys=True))


if __name__ == "__main__":
    main()
