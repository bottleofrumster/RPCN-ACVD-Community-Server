"""Canonical migration utilities and legacy CLI/import compatibility."""

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NAMES = (
    "extract_ac_snapshot",
    "extract_ai_snapshots",
    "extract_mission_snapshot",
    "extract_offline_inventory",
)


def command(path, *arguments, cwd):
    env = dict(os.environ, PYTHONPATH="")
    return subprocess.run(
        [sys.executable, str(path), *map(str, arguments)],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )


@pytest.mark.parametrize("name", NAMES)
def test_legacy_import_is_the_canonical_module_not_a_copy(name):
    legacy = importlib.import_module("tools." + name)
    canonical = importlib.import_module("tools.migration." + name)
    assert legacy is canonical


@pytest.mark.parametrize("name", NAMES)
def test_old_and_new_help_work_outside_repository(name, tmp_path):
    old = command(ROOT / "tools" / f"{name}.py", "--help", cwd=tmp_path)
    new = command(ROOT / "tools/migration" / f"{name}.py", "--help", cwd=tmp_path)
    assert old.returncode == new.returncode == 0, (old.stderr, new.stderr)
    assert old.stdout == new.stdout
    assert not list(tmp_path.iterdir())


def test_inventory_cli_refuses_overwrite_without_modifying_source(tmp_path):
    source = tmp_path / "input GPROG.DAT"
    source.write_bytes(bytes(0x958))
    output = tmp_path / "reviewed.bin"
    output.write_bytes(b"keep")
    result = command(ROOT / "tools/extract_offline_inventory.py", source, output, cwd=tmp_path)
    assert result.returncode != 0 and "refusing to overwrite" in result.stderr
    assert output.read_bytes() == b"keep" and source.read_bytes() == bytes(0x958)


def test_ac_cli_output_matches_through_both_paths(tmp_path):
    source = tmp_path / "DESIGN.DAT"
    source.write_bytes(bytes(26344))
    old_output = tmp_path / "old.bin"
    new_output = tmp_path / "new.bin"
    old = command(ROOT / "tools/extract_ac_snapshot.py", source, old_output, cwd=tmp_path)
    new = command(ROOT / "tools/migration/extract_ac_snapshot.py", source, new_output, cwd=tmp_path)
    assert old.returncode == new.returncode == 0, (old.stderr, new.stderr)
    assert old_output.read_bytes() == new_output.read_bytes()
    a, b = json.loads(old.stdout), json.loads(new.stdout)
    a.pop("output")
    b.pop("output")
    assert a == b


def test_ai_batch_conflict_refuses_all_outputs_before_writing(tmp_path):
    source = tmp_path / "savedata"
    source.mkdir()
    for slot in range(9):
        (source / f"AI{slot:03}.DAT").write_bytes(bytes(54072))
    output = tmp_path / "staged"
    output.mkdir()
    conflict = output / "11.ai-5.preseed.bin"
    conflict.write_bytes(b"keep")
    result = command(
        ROOT / "tools/extract_ai_snapshots.py", source, output, "--player-id", "11", cwd=tmp_path
    )
    assert result.returncode != 0 and "refusing to overwrite" in result.stderr
    assert list(output.iterdir()) == [conflict] and conflict.read_bytes() == b"keep"
