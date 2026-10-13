"""Read-only source publication checks; findings never include matched private values."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    # Checking a pristine artifact must not add local import caches to that artifact.
    sys.dont_write_bytecode = True

if __package__:
    from .export_review import (
        PUBLIC_HEX_FIXTURES,
        REQUIRED_FILES,
        private_matches,
        selected_files,
        source_names,
    )
else:
    from export_review import (
        PUBLIC_HEX_FIXTURES,
        REQUIRED_FILES,
        private_matches,
        selected_files,
        source_names,
    )

FIXTURE_PROVENANCE = "tests/fixtures/public-fixtures.json"
_HOST_PATH = re.compile(rb"/(?:home|Users)/[A-Za-z0-9_.-]+/")
_TOKEN = re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|AKIA[A-Z0-9]{16})\b")


def check_contents(contents: dict[str, bytes], patterns: list[str] | None = None) -> dict:
    """Check exactly the bytes to be shared, without treating checks as approval."""
    issues = []

    def issue(code: str, path: str) -> None:
        issues.append({"code": code, "path": path})

    for name in sorted(REQUIRED_FILES - contents.keys()):
        issue("missing_required_file", name)
    if not any(contents.get(name, b"").strip() for name in ("LICENSE", "LICENSE.txt")):
        issue("license_not_selected", "LICENSE")
    for name, payload in sorted(contents.items()):
        if private_matches(payload, patterns or []) or _TOKEN.search(payload):
            issue("private_material", name)
        if _HOST_PATH.search(payload):
            issue("host_path", name)
    try:
        provenance = json.loads(contents.get(FIXTURE_PROVENANCE, b"{}"))
        if not isinstance(provenance, dict) or provenance.get("schema_version") != 1:
            raise ValueError
        entries = provenance["files"]
        if not isinstance(entries, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError, UnicodeError):
        entries = {}
    for name in sorted(PUBLIC_HEX_FIXTURES & contents.keys()):
        entry = entries.get(Path(name).name, {})
        if not (
            isinstance(entry, dict)
            and entry.get("sha256") == hashlib.sha256(contents[name]).hexdigest()
            and isinstance(entry.get("provenance"), str) and entry["provenance"].strip()
        ):
            issue("fixture_provenance_unresolved", name)
        try:
            bytes.fromhex(contents[name].decode("ascii"))
        except (ValueError, UnicodeError):
            issue("invalid_hex_fixture", name)
    return {
        "status": "blocked" if issues else "source_checks_passed",
        "issues": issues,
        "files_checked": len(contents),
        "limits": "Checks do not grant publication approval, certify ownership, or prove unknown private information absent.",
    }


def _manifest_files(root: Path) -> dict[str, str] | None:
    manifest = root / "review-manifest.json"
    if manifest.is_symlink():
        raise ValueError("Review manifest must not be a symlink")
    if not manifest.exists():
        return None
    data = json.loads(manifest.read_text())
    expected = data.get("files") if isinstance(data, dict) else None
    if not isinstance(expected, dict) or any(
        not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts
        or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
        for name, digest in expected.items()
    ):
        raise ValueError("Review manifest has no file hashes")
    return expected


def _artifact_issues(root: Path, expected: dict[str, str]) -> list[dict[str, str]]:
    """Inventory every entry without opening unexpected files or following links."""
    actual = set()
    issues = []

    def unreadable(error: OSError) -> None:
        raise error

    for directory, directories, filenames in os.walk(root, followlinks=False, onerror=unreadable):
        for name in directories + filenames:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                issues.append({"code": "artifact_symlink", "path": relative})
            if name in filenames:
                actual.add(relative)
    required = expected.keys() | {"review-manifest.json"}
    issues.extend(
        {"code": "unexpected_artifact_file", "path": name} for name in sorted(actual - required)
    )
    issues.extend(
        {"code": "missing_artifact_file", "path": name} for name in sorted(required - actual)
    )
    return issues


def check(
    root: Path, patterns: list[str] | None = None, *, verify_artifact: bool = False
) -> dict:
    if not root.is_dir():
        raise ValueError("Source root must be a directory")
    expected = _manifest_files(root)
    if verify_artifact:
        if expected is None or root.is_symlink():
            raise ValueError("Artifact verification needs an unlinked root and review manifest")
        issues = _artifact_issues(root, expected)
        if issues:
            return {
                "status": "blocked", "issues": issues, "files_checked": 0,
                "limits": "Artifact inventory rejected before reading source contents.",
            }
    files = selected_files(root, source_names(root))
    contents = {p.relative_to(root).as_posix(): p.read_bytes() for p in files}
    result = check_contents(contents, patterns)
    if expected is not None:
        actual = {name: hashlib.sha256(payload).hexdigest() for name, payload in contents.items()}
        for name in sorted(expected.keys() | actual.keys()):
            if expected.get(name) != actual.get(name):
                result["issues"].append({"code": "manifest_mismatch", "path": name})
        if result["issues"]:
            result["status"] = "blocked"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--private-patterns", type=Path, help="Optional local JSON string list")
    parser.add_argument(
        "--verify-artifact", action="store_true",
        help="require the exact manifest file inventory; run before install, build or initialization",
    )
    args = parser.parse_args(argv)
    try:
        patterns = []
        if args.private_patterns:
            patterns = json.loads(args.private_patterns.read_text())
            if not isinstance(patterns, list) or any(not isinstance(p, str) or not p for p in patterns):
                raise ValueError("Private patterns must be nonempty strings")
        report = check(args.root, patterns, verify_artifact=args.verify_artifact)
    except (OSError, ValueError, subprocess.SubprocessError):
        # OS and JSON errors can echo paths or source contents. Never print them.
        print(json.dumps({"status": "blocked", "error": "source_check_failed"}))
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "source_checks_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
