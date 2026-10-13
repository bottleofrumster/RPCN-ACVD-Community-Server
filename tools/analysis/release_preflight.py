"""Read-only gate for a prepared ACVD source release; never deploy or edit data.

This tool checks one immutable source export, the current local service and its
operator snapshot. An empty result is a momentary observation, not owner
authorization or a promise that the server will remain empty.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shlex
import socket
import stat
import subprocess
import tarfile
from pathlib import Path

MAX_STORE_BYTES = 32 * 1024 * 1024
MAX_SETTINGS_BYTES = 8192
MAX_ADMIN_REPLY_BYTES = 4 * 1024 * 1024
MAX_BALANCE = 999_999_999


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def candidate_source(root: Path, revision: str) -> dict:
    """Compare the read-only package with the exact Git archive, byte for byte."""
    resolved = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", f"{revision}^{{commit}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    release = root / "releases" / "server" / resolved[:7]
    source = release / "src"
    archive = subprocess.run(
        ["git", "-C", str(root), "archive", resolved, "src"],
        capture_output=True,
        check=True,
        timeout=20,
    ).stdout
    expected: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        for member in package:
            if not member.isfile():
                continue
            if not member.name.startswith("src/") or ".." in Path(member.name).parts:
                raise ValueError("Release archive has an unsafe member")
            stream = package.extractfile(member)
            if stream is None:
                raise ValueError("Release archive member is unreadable")
            expected[member.name] = _hash(stream.read())
    if not expected or not source.is_dir() or source.is_symlink():
        raise ValueError("Prepared release source is missing")
    if stat.S_IMODE(source.stat().st_mode) != 0o555:
        raise ValueError("Prepared release source directory is not read-only")
    actual: dict[str, str] = {}
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError("Prepared release contains a symlink")
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_dir():
            if mode != 0o555:
                raise ValueError("Prepared release directory is not read-only")
            continue
        if not path.is_file() or mode != 0o444:
            raise ValueError("Prepared release file is not read-only")
        actual[path.relative_to(release).as_posix()] = _hash(path.read_bytes())
    if actual != expected:
        raise ValueError("Prepared release differs from the source commit")
    # The checkout may contain ignored bytecode and editable-install egg-info.
    # Neither is exported or loaded from the immutable release path. Compare
    # every exact tracked archive member instead of treating those generated
    # checkout files as a change to the deployable source.
    worktree = {
        name: _hash((root / name).read_bytes())
        for name in expected
        if (root / name).is_file() and not (root / name).is_symlink()
    }
    if worktree != expected:
        raise ValueError("Current source differs from the prepared release")
    return {"commit": resolved, "release": str(release), "source_files": len(expected)}


def settings_compatibility(root: Path, source: Path) -> dict:
    """Validate saved settings under candidate code, without writing the file."""
    settings = root / "state" / "operator-settings.json"
    code = (
        "import json,sys;"
        "from acvd_server.administration import OperatorSettings;"
        "raw=json.load(sys.stdin);"
        "merged=OperatorSettings.validate(raw);"
        "names=sorted(set(OperatorSettings.DEFAULTS)-set(raw));"
        "print(json.dumps({'defaulted_keys':names,'boss_base':merged['world_boss_base_reward']}))"
    )
    process = subprocess.run(
        [str(root / ".venv" / "bin" / "python"), "-B", "-c", code],
        input=settings.read_text(),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        env={**os.environ, "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    result = json.loads(process.stdout)
    if not isinstance(result, dict) or not isinstance(result.get("defaulted_keys"), list):
        raise ValueError("Candidate settings validation returned an invalid summary")
    return result


def live_data_summary(root: Path) -> dict:
    """Read private data only for counts/hashes; never emit account identities."""
    teams_path = root / "data" / "teams" / "teams.json"
    settings_path = root / "state" / "operator-settings.json"
    for path, limit in ((teams_path, MAX_STORE_BYTES), (settings_path, MAX_SETTINGS_BYTES)):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            raise ValueError("Live state path is absent, symlinked or too large")
    raw_teams, raw_settings = teams_path.read_bytes(), settings_path.read_bytes()
    teams, settings = json.loads(raw_teams), json.loads(raw_settings)
    if not isinstance(teams, dict) or not isinstance(settings, dict):
        raise ValueError("Live state is not an object")
    profiles = teams.get("player_profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("Live player profiles need review")
    over_cap = sum(
        type(profile.get("balance")) is int and profile["balance"] > MAX_BALANCE
        for profile in profiles.values()
        if isinstance(profile, dict)
    )
    return {
        "teams_sha256": _hash(raw_teams),
        "settings_sha256": _hash(raw_settings),
        "profile_count": len(profiles),
        "over_cap_wallets": over_cap,
    }


def gate(snapshot: dict, unit: dict[str, str], *, root: Path, expected_public: Path) -> dict:
    """Require one live process and an empty player/match/session snapshot."""
    environment = dict(
        token.split("=", 1)
        for token in shlex.split(unit.get("Environment", ""))
        if "=" in token
    )
    pid = int(unit.get("MainPID", "0"))
    if (
        unit.get("ActiveState") != "active"
        or pid <= 0
        or snapshot.get("pid") != pid
        or snapshot.get("root") != str(root)
        or environment.get("PYTHONPATH") != str(expected_public)
    ):
        raise ValueError("Public process, data root or source pin differs from expectation")
    if environment.get("ACVD_MATCH_ROOM_LOG_PATHS"):
        raise ValueError("Obsolete RPCS3 room-log bridge is enabled on the public service")
    players = snapshot.get("players")
    matches, sessions = snapshot.get("matches"), snapshot.get("sessions")
    if (
        not isinstance(players, list)
        or type(matches) is not int
        or type(sessions) is not int
        or matches < 0
        or sessions < 0
    ):
        raise ValueError("Public player or match status is invalid")
    return {
        "pid": pid,
        "current_source": str(expected_public),
        "players": len(players),
        "matches": matches,
        "team_sessions": sessions,
        "empty_at_snapshot": not players and matches == 0 and sessions == 0,
    }


def _admin_status(root: Path) -> dict:
    path = root / "state" / "operator.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(str(path))
        connection.sendall(b'{"command":"status"}\n')
        with connection.makefile("rb") as stream:
            line = stream.readline(MAX_ADMIN_REPLY_BYTES + 1)
    if not line.endswith(b"\n") or len(line) > MAX_ADMIN_REPLY_BYTES:
        raise ValueError("Administrative status reply is incomplete or too large")
    response = json.loads(line)
    if response.get("ok") is not True or not isinstance(response.get("data"), dict):
        raise ValueError("Administrative status was not successful")
    return response["data"]


def _unit_status() -> dict[str, str]:
    output = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            "acvd-server.service",
            "-p",
            "ActiveState",
            "-p",
            "MainPID",
            "-p",
            "Environment",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--expected-public-release", required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if not re.fullmatch(r"[0-9a-f]{7,40}", args.expected_public_release):
        parser.error("expected public release must be a lowercase Git prefix")
    expected_public = root / "releases" / "server" / args.expected_public_release / "src"
    try:
        package = candidate_source(root, args.candidate_commit)
        data = live_data_summary(root)
        compatible = settings_compatibility(root, Path(package["release"]) / "src")
        public = gate(_admin_status(root), _unit_status(), root=root, expected_public=expected_public)
        report = {
            "candidate": package,
            "live_data": data,
            "settings_compatibility": compatible,
            "public": public,
            "deployment_authorized": False,
            "decision": "Read-only snapshot only; obtain owner approval and recheck immediately before change.",
        }
        print(json.dumps(report, sort_keys=True))
        return 0 if public["empty_at_snapshot"] else 2
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"preflight_error": str(error), "deployment_authorized": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
