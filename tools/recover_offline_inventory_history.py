#!/usr/bin/env python3
"""Rebuild one canonical SyncOfflineData snapshot from versioned batches."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from acvd_server.inventory import (
    _merge_offline_inventory,
    _offline_inventory_summary,
)
from acvd_server.player_storage import _replace_player_blob


def recover_history(player_data: Path, player_id: int) -> tuple[bytes, dict]:
    target = player_data / f"{player_id}.offline-data.bin"
    history = player_data / ".history" / target.name
    historical = (
        sorted(
            history.glob("*.bin"),
            key=lambda path: int(path.name.split("-", 1)[0]),
        )
        if history.is_dir()
        else []
    )
    sources = historical + ([target] if target.is_file() else [])
    if not sources:
        raise FileNotFoundError(f"no offline-data history or live file for player {player_id}")
    canonical = b""
    evidence = []
    for source in sources:
        payload = source.read_bytes()
        canonical, categories, field_numbers = _merge_offline_inventory(canonical, payload)
        evidence.append(
            {
                "path": str(source),
                "length": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "summary": _offline_inventory_summary(payload),
                "merged_categories": categories,
                "merged_field_numbers": field_numbers,
            }
        )
    current = target.read_bytes() if target.is_file() else b""
    return canonical, {
        "schema": "acvd-offline-history-recovery-v1",
        "player_id": player_id,
        "sources": evidence,
        "current_length": len(current),
        "current_sha256": hashlib.sha256(current).hexdigest(),
        "current_summary": _offline_inventory_summary(current) if current else {},
        "recovered_length": len(canonical),
        "recovered_sha256": hashlib.sha256(canonical).hexdigest(),
        "recovered_summary": _offline_inventory_summary(canonical),
    }


def _write_atomic(path: Path, payload: bytes, *, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise FileExistsError(f"refusing to overwrite {path}; pass --force after review")
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temporary:
        temporary.write(payload)
        temporary.flush()
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("player_data", type=Path)
    parser.add_argument("--player-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="promote the reviewed recovery to the live blob with history backup",
    )
    args = parser.parse_args()
    recovered, metadata = recover_history(args.player_data, args.player_id)
    staged = args.output / f"{args.player_id}.offline-data.recovered.bin"
    _write_atomic(staged, recovered, force=args.force)
    metadata["staged"] = str(staged)
    if args.apply:
        current = metadata["current_summary"]
        result = metadata["recovered_summary"]
        for key in ("record_count", "instantiated_count", "category_count"):
            if int(result.get(key, 0)) < int(current.get(key, 0)):
                raise RuntimeError(f"refusing promotion: recovered {key} regresses current state")
        target = args.player_data / f"{args.player_id}.offline-data.bin"
        backup = _replace_player_blob(target, recovered)
        metadata["applied"] = str(target)
        metadata["previous_backup"] = str(backup) if backup is not None else ""
    print(json.dumps(metadata, sort_keys=True))


if __name__ == "__main__":
    main()
