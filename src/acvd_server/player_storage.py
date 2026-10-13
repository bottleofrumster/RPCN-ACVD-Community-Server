"""File-backed player snapshots with reviewed-preseed isolation and history.

Never expose a pending self-preseed to a remote player. This layer preserves
old blob inodes before atomic replacement. Shared read selection validates
reviewed inputs; handlers own mutation and promotion policy. An absent blob is
not permission to invent historical data.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .inventory import _merge_offline_inventory
from .protocol import ProtocolError


@dataclass(frozen=True, slots=True)
class OfflineSnapshot:
    payload: bytes | None
    canonical_payload: bytes | None
    preseed_path: Path | None = None
    preseed_ignored_reason: str = ""


def _requested_offline_snapshot(player_data_path: Path, *, player_id: int) -> OfflineSnapshot:
    """Select the self inventory once for both readback and diagnostics.

    A reviewed preseed takes priority, but a malformed preseed does not hide
    a valid canonical snapshot. None records absence; b'' records an existing
    empty snapshot. Do not create files or promote recovery data during reads.
    """
    target = player_data_path / f"{int(player_id)}.offline-data.bin"
    canonical = target.read_bytes() if target.is_file() else None
    preseed = player_data_path / f"{int(player_id)}.offline-data.preseed.bin"
    if preseed.is_file():
        reviewed = preseed.read_bytes()
        try:
            _merge_offline_inventory(b"", reviewed)
        except ProtocolError as error:
            return OfflineSnapshot(canonical, canonical, preseed_ignored_reason=str(error))
        return OfflineSnapshot(reviewed, canonical, preseed)
    return OfflineSnapshot(canonical, canonical)


def _requested_ac_snapshot(
    player_data_path,
    *,
    requester_player_id: int,
    requested_player_id: int,
) -> tuple[bytes, Path | None]:
    """Load a live snapshot or a reviewed one-shot self-preseed."""
    if int(requester_player_id) == int(requested_player_id):
        preseed = player_data_path / f"{int(requested_player_id)}.ac-data.preseed.bin"
        if preseed.is_file():
            return preseed.read_bytes(), preseed
    target = player_data_path / f"{int(requested_player_id)}.ac-data.bin"
    return (target.read_bytes() if target.is_file() else b""), None


def _requested_ai_snapshot(
    player_data_path,
    *,
    requester_player_id: int,
    requested_player_id: int,
    slot: int,
) -> tuple[bytes, Path | None]:
    """Load a live UNAC snapshot or a reviewed one-shot self-preseed."""
    if int(requester_player_id) == int(requested_player_id):
        preseed = player_data_path / f"{int(requested_player_id)}.ai-{int(slot)}.preseed.bin"
        if preseed.is_file():
            return preseed.read_bytes(), preseed
    target = player_data_path / f"{int(requested_player_id)}.ai-{int(slot)}.bin"
    return (target.read_bytes() if target.is_file() else b""), None


def _requested_mission_snapshot(
    player_data_path,
    *,
    player_id: int,
) -> tuple[bytes, Path | None]:
    """Load a live mission/rank snapshot or a reviewed one-shot preseed."""
    preseed = player_data_path / f"{int(player_id)}.mission-savedata.preseed.bin"
    if preseed.is_file():
        return preseed.read_bytes(), preseed
    target = player_data_path / f"{int(player_id)}.mission-savedata.bin"
    return (target.read_bytes() if target.is_file() else b""), None


def _replace_player_blob(target: Path, payload: bytes) -> Path | None:
    """Atomically replace one player blob while retaining its prior inode."""
    target.parent.mkdir(parents=True, exist_ok=True)
    previous_backup = None
    if target.is_file():
        previous = target.read_bytes()
        if previous == payload:
            return None
        history_dir = target.parent / ".history" / target.name
        history_dir.mkdir(parents=True, exist_ok=True)
        previous_backup = history_dir / (
            f"{time.time_ns()}-{hashlib.sha256(previous).hexdigest()[:16]}.bin"
        )
        # A hard link is atomic, space-efficient, and continues to reference
        # the old inode after the live pathname is replaced below.
        os.link(target, previous_backup)
    temporary = target.with_name(f".{target.name}.{time.time_ns()}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(target)
    return previous_backup
