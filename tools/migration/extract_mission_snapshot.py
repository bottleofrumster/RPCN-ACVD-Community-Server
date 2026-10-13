#!/usr/bin/env python3
"""Stage ACVD's server mission/rank snapshot from a BLUS31194 GPROG.DAT save."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import zlib
from pathlib import Path

MISSION_FLAGS_OFFSET = 0x1C
MISSION_FLAGS_SIZE = 0x20
MISSION_RECORDS_OFFSET = 0x4D0
MISSION_RECORDS_END = 0x950
MISSION_RAW_SIZE = MISSION_FLAGS_SIZE + (MISSION_RECORDS_END - MISSION_RECORDS_OFFSET)


def extract_mission_snapshot(gprog: bytes) -> tuple[bytes, dict[str, object]]:
    """Project the exact local mission/rank ranges used by opcode 0x040F."""
    if len(gprog) < MISSION_RECORDS_END:
        raise ValueError(
            f"GPROG.DAT is {len(gprog)} bytes; at least {MISSION_RECORDS_END} are required"
        )
    raw = (
        gprog[MISSION_FLAGS_OFFSET : MISSION_FLAGS_OFFSET + MISSION_FLAGS_SIZE]
        + gprog[MISSION_RECORDS_OFFSET:MISSION_RECORDS_END]
    )
    if len(raw) != MISSION_RAW_SIZE:
        raise AssertionError("internal mission snapshot projection length mismatch")
    stored = b"\x01" + zlib.compress(raw)
    return stored, {
        "schema": "acvd-mission-savedata-preseed-v1",
        "source_length": len(gprog),
        "flags_offset": MISSION_FLAGS_OFFSET,
        "flags_length": MISSION_FLAGS_SIZE,
        "records_offset": MISSION_RECORDS_OFFSET,
        "records_end": MISSION_RECORDS_END,
        "raw_length": len(raw),
        "source_sha256": hashlib.sha256(gprog).hexdigest(),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
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
    parser.add_argument(
        "output", type=Path, help="staging directory; never installed automatically"
    )
    parser.add_argument(
        "--player-id", type=int, required=True, help="authenticated server player ID"
    )
    parser.add_argument("--force", action="store_true", help="replace existing staged outputs")
    args = parser.parse_args()

    stored, metadata = extract_mission_snapshot(args.gprog.read_bytes())
    target = args.output / f"{args.player_id}.mission-savedata.preseed.bin"
    metadata.update({"source": str(args.gprog), "output": str(target)})
    _write_atomic(target, stored, force=args.force)
    print(json.dumps(metadata, sort_keys=True))


if __name__ == "__main__":
    main()
