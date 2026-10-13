"""Storage lifecycle guarantees exercised only on disposable files."""

from pathlib import Path

import pytest

from acvd_server.player_storage import (
    _replace_player_blob,
    _requested_ac_snapshot,
    _requested_ai_snapshot,
)


def test_snapshot_reads_do_not_promote_or_expose_another_players_preseed(tmp_path):
    live = tmp_path / "42.ac-data.bin"
    preseed = tmp_path / "42.ac-data.preseed.bin"
    live.write_bytes(b"live")
    preseed.write_bytes(b"reviewed")
    assert _requested_ac_snapshot(tmp_path, requester_player_id=42, requested_player_id=42) == (
        b"reviewed",
        preseed,
    )
    assert _requested_ac_snapshot(tmp_path, requester_player_id=99, requested_player_id=42) == (
        b"live",
        None,
    )
    assert live.read_bytes() == b"live" and preseed.read_bytes() == b"reviewed"


def test_ai_preseed_is_scoped_by_owner_and_slot(tmp_path):
    preseed = tmp_path / "42.ai-2.preseed.bin"
    preseed.write_bytes(b"slot2")
    assert _requested_ai_snapshot(
        tmp_path, requester_player_id=42, requested_player_id=42, slot=2
    ) == (b"slot2", preseed)
    assert _requested_ai_snapshot(
        tmp_path, requester_player_id=42, requested_player_id=42, slot=1
    ) == (b"", None)
    assert _requested_ai_snapshot(
        tmp_path, requester_player_id=99, requested_player_id=42, slot=2
    ) == (b"", None)


def test_noop_write_does_not_create_history_or_replace_inode(tmp_path):
    target = tmp_path / "42.offline-data.bin"
    target.write_bytes(b"original")
    inode = target.stat().st_ino
    assert _replace_player_blob(target, b"original") is None
    assert target.stat().st_ino == inode
    assert not (tmp_path / ".history").exists()


def test_failed_final_replace_keeps_canonical_and_recoverable_history(tmp_path, monkeypatch):
    target = tmp_path / "42.offline-data.bin"
    target.write_bytes(b"original")

    def fail_replace(self, destination):
        raise OSError("simulated filesystem failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        _replace_player_blob(target, b"new")
    assert target.read_bytes() == b"original"
    backups = list((tmp_path / ".history" / target.name).iterdir())
    assert len(backups) == 1 and backups[0].read_bytes() == b"original"
