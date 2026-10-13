"""The release gate is read-only and never turns an empty snapshot into approval."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.analysis.release_preflight import candidate_source, gate, live_data_summary


def _run(*args, cwd: Path) -> str:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()


def test_exact_immutable_git_export_is_required(tmp_path):
    root = tmp_path
    source = root / "src" / "acvd_server"
    source.mkdir(parents=True)
    (source / "server.py").write_text("RELEASE = 1\n")
    _run("git", "init", "-q", cwd=root)
    _run("git", "add", "src", cwd=root)
    _run(
        "git",
        "-c",
        "user.name=ACVD Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "test release",
        cwd=root,
    )
    revision = _run("git", "rev-parse", "HEAD", cwd=root)
    release = root / "releases" / "server" / revision[:7]
    shutil.copytree(root / "src", release / "src")
    for path in (release / "src").rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    (release / "src").chmod(0o555)
    assert candidate_source(root, revision) == {
        "commit": revision,
        "release": str(release),
        "source_files": 1,
    }
    generated = root / "src" / "acvd_server.egg-info"
    generated.mkdir()
    (generated / "PKG-INFO").write_text("Generated metadata, not in the release\n")
    assert candidate_source(root, revision)["source_files"] == 1
    target = release / "src" / "acvd_server" / "server.py"
    target.chmod(0o644)
    target.write_text("RELEASE = 2\n")
    target.chmod(0o444)
    with pytest.raises(ValueError, match="differs"):
        candidate_source(root, revision)


def test_store_summary_never_returns_an_account_identity(tmp_path):
    team_dir, state_dir = tmp_path / "data" / "teams", tmp_path / "state"
    team_dir.mkdir(parents=True)
    state_dir.mkdir()
    (team_dir / "teams.json").write_text(
        json.dumps(
            {
                "player_profiles": {
                    "PrivateAccount": {"balance": 1_000_000_001},
                    "OtherAccount": {"balance": -100},
                }
            }
        )
    )
    (state_dir / "operator-settings.json").write_text('{"login_enabled":true}')
    result = live_data_summary(tmp_path)
    assert result["profile_count"] == 2 and result["over_cap_wallets"] == 1
    assert "PrivateAccount" not in json.dumps(result)
    assert len(result["teams_sha256"]) == len(result["settings_sha256"]) == 64


def test_gate_checks_current_pin_and_never_claims_deployment_approval(tmp_path):
    expected = tmp_path / "releases" / "server" / "old1234" / "src"
    snapshot = {"pid": 42, "root": str(tmp_path), "players": [], "matches": 0, "sessions": 0}
    unit = {
        "ActiveState": "active",
        "MainPID": "42",
        "Environment": f'"PYTHONPATH={expected}" "PYTHONDONTWRITEBYTECODE=1"',
    }
    assert gate(snapshot, unit, root=tmp_path, expected_public=expected)["empty_at_snapshot"]
    assert not gate(
        {**snapshot, "players": [{}]}, unit, root=tmp_path, expected_public=expected
    )["empty_at_snapshot"]
    with pytest.raises(ValueError, match="source pin"):
        gate(snapshot, unit, root=tmp_path, expected_public=tmp_path / "wrong")
    with pytest.raises(ValueError, match="room-log bridge"):
        gate(
            snapshot,
            {
                **unit,
                "Environment": (
                    unit["Environment"]
                    + ' "ACVD_MATCH_ROOM_LOG_PATHS=/tmp/primary.log:/tmp/secondary.log"'
                ),
            },
            root=tmp_path,
            expected_public=expected,
        )
