#!/usr/bin/env python3
"""Extract ACVD's server AC body from a BLUS31194 DESIGN.DAT file."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import zlib
from pathlib import Path

AC_BODY_OFFSET = 224
AC_BODY_SIZE = 26_120
AC_NAME_OFFSET = 8
AC_NAME_SIZE = AC_BODY_OFFSET - AC_NAME_OFFSET


def _ac_name(design: bytes) -> str:
    raw = design[AC_NAME_OFFSET : AC_NAME_OFFSET + AC_NAME_SIZE]
    terminator = raw.find(b"\x00\x00")
    if terminator >= 0:
        terminator -= terminator % 2
        raw = raw[:terminator]
    return raw.decode("utf-16-be", errors="replace")


def extract_snapshot(design: bytes) -> tuple[bytes, dict[str, object]]:
    required = AC_BODY_OFFSET + AC_BODY_SIZE
    if len(design) < required:
        raise ValueError(f"DESIGN.DAT is {len(design)} bytes; at least {required} are required")
    raw = design[AC_BODY_OFFSET:required]
    stored = b"\x01" + zlib.compress(raw)
    return stored, {
        "schema": "acvd-ac-preseed-v1",
        "source_length": len(design),
        "body_offset": AC_BODY_OFFSET,
        "body_length": len(raw),
        "stored_length": len(stored),
        "ac_name": _ac_name(design),
        "body_sha256": hashlib.sha256(raw).hexdigest(),
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
    parser.add_argument("design", type=Path, help="source BLUS31194GAME*/DESIGN.DAT")
    parser.add_argument(
        "output", type=Path, help="staged .ac-data.bin output (never installed automatically)"
    )
    parser.add_argument("--force", action="store_true", help="replace an existing staged output")
    args = parser.parse_args()

    stored, metadata = extract_snapshot(args.design.read_bytes())
    _write_atomic(args.output, stored, force=args.force)
    metadata.update({"source": str(args.design), "output": str(args.output)})
    print(json.dumps(metadata, sort_keys=True))


if __name__ == "__main__":
    main()
