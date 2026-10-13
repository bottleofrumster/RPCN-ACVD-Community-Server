import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from tools.recover_offline_inventory_history import recover_history

ROOT = Path(__file__).resolve().parents[1]


def _cli(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "tools/recover_offline_inventory_history.py"), *arguments],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=10,
    )


def _fixture(tmp_path: Path) -> tuple[Path, bytes]:
    players = tmp_path / "players"
    history = players / ".history/42.offline-data.bin"
    history.mkdir(parents=True)
    (history / "9-old.bin").write_bytes(_record(1, 100, 111))
    # Numeric chronology matters: lexical sorting would put10 before9.
    (history / "10-new.bin").write_bytes(_record(1, 200, 222))
    live = _record(4, 400, 444)
    (players / "42.offline-data.bin").write_bytes(live)
    (players / "43.offline-data.bin").write_bytes(b"unrelated player")
    return players, _record(1, 200, 222) + live


def _files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _record(category: int, part: int, instance: int) -> bytes:
    return proto_bytes(
        1,
        proto_varint(1, category)
        + proto_varint(2, part)
        + proto_varint(3, category + 10)
        + proto_varint(4, instance)
        + proto_varint(5, 1000),
    )


def test_recover_history_replays_inventory_and_auxiliary_families(tmp_path: Path) -> None:
    history = tmp_path / ".history" / "42.offline-data.bin"
    history.mkdir(parents=True)
    (history / "100-a.bin").write_bytes(
        proto_bytes(3, proto_varint(1, 0x20000) + proto_varint(2, 1))
    )
    (history / "200-b.bin").write_bytes(_record(1, 110, 1111))
    (history / "300-c.bin").write_bytes(_record(4, 410, 4444))
    (tmp_path / "42.offline-data.bin").write_bytes(
        proto_bytes(3, proto_varint(1, 0x20000) + proto_varint(2, 9))
    )

    recovered, metadata = recover_history(tmp_path, 42)
    fields = decode_proto(recovered)
    inventory = [decode_proto(field.value) for field in fields if field.number == 1]
    auxiliary = [decode_proto(field.value) for field in fields if field.number == 3]

    assert [(first_field(item, 1).value, first_field(item, 2).value) for item in inventory] == [
        (1, 110),
        (4, 410),
    ]
    assert first_field(auxiliary[0], 1).value == 0x20000
    assert first_field(auxiliary[0], 2).value == 9
    assert metadata["current_summary"]["record_count"] == 0
    assert metadata["recovered_summary"]["instantiated_count"] == 2
    assert metadata["recovered_summary"]["auxiliary_record_count"] == 1


def test_history_is_read_only_and_replaces_same_category_in_numeric_order(tmp_path: Path) -> None:
    players, expected = _fixture(tmp_path)
    before = _files(players)
    recovered, metadata = recover_history(players, 42)
    assert recovered == expected
    assert [Path(source["path"]).name for source in metadata["sources"]] == [
        "9-old.bin",
        "10-new.bin",
        "42.offline-data.bin",
    ]
    assert _files(players) == before


def test_missing_history_does_not_create_player_data(tmp_path: Path) -> None:
    players = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        recover_history(players, 42)
    assert not players.exists()


def test_cli_stages_only_and_refuses_existing_output(tmp_path: Path) -> None:
    players, expected = _fixture(tmp_path)
    before = _files(players)
    output = tmp_path / "staging"
    args = (str(players), "--player-id", "42", "--output", str(output))
    first = _cli(tmp_path, *args)
    assert first.returncode == 0, first.stderr
    metadata = json.loads(first.stdout)
    assert "applied" not in metadata
    staged = output / "42.offline-data.recovered.bin"
    assert staged.read_bytes() == expected
    staged.write_bytes(b"preserve prior reviewed output")
    second = _cli(tmp_path, *args)
    assert second.returncode != 0
    assert "refusing to overwrite" in second.stderr
    assert staged.read_bytes() == b"preserve prior reviewed output"
    assert _files(players) == before


def test_force_overwrites_staging_not_player_data(tmp_path: Path) -> None:
    players, expected = _fixture(tmp_path)
    before = _files(players)
    output = tmp_path / "staging"
    output.mkdir()
    staged = output / "42.offline-data.recovered.bin"
    staged.write_bytes(b"old staging")
    result = _cli(tmp_path, str(players), "--player-id", "42", "--output", str(output), "--force")
    assert result.returncode == 0, result.stderr
    assert staged.read_bytes() == expected
    assert _files(players) == before


def test_apply_preserves_old_inode_and_other_player_then_noops_when_equal(tmp_path: Path) -> None:
    players, expected = _fixture(tmp_path)
    target = players / "42.offline-data.bin"
    old = target.read_bytes()
    old_inode = target.stat().st_ino
    other = (players / "43.offline-data.bin").read_bytes()
    args = (str(players), "--player-id", "42", "--output", str(tmp_path / "staging"), "--apply")
    result = _cli(tmp_path, *args)
    assert result.returncode == 0, result.stderr
    metadata = json.loads(result.stdout)
    backup = Path(metadata["previous_backup"])
    assert backup.parent == players / ".history/42.offline-data.bin"
    assert backup.read_bytes() == old
    assert backup.stat().st_ino == old_inode
    assert target.read_bytes() == expected
    before_repeat = _files(players)
    repeat = _cli(tmp_path, *args, "--force")
    assert repeat.returncode == 0, repeat.stderr
    assert json.loads(repeat.stdout)["previous_backup"] == ""
    assert _files(players) == before_repeat
    assert (players / "43.offline-data.bin").read_bytes() == other


def test_recovery_import_does_not_load_server_runtime(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import tools.recover_offline_inventory_history; "
            'assert "acvd_server.server" not in sys.modules; '
            'assert "acvd_server.teams" not in sys.modules',
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(ROOT), str(ROOT / "src"))),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
