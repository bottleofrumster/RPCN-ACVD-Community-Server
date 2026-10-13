"""Export a public source candidate without private operations or Git history.

Private-pattern values come from an operator-supplied local file, never source.
The scanner is a safeguard, not proof that unknown personal information is absent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = {"src", "desktop", "tools", "scripts", "tests", "native", "client", "docs"}
SUFFIXES = {".py", ".c", ".h", ".sh", ".json", ".in", ".svg", ".ttf"}
ROOT_FILES = {
    "Launch Server UI.sh",
    "Launch Server UI.desktop",
    "Launch Server UI.cmd",
    "README.txt",
    "PENDING.txt",
    "CHANGES.txt",
    "pyproject.toml",
    ".gitignore",
    ".gitattributes",
    "LICENSE",
    "LICENSE.txt",
    "NOTICE",
    "THIRD_PARTY_NOTICES.txt",
}
PUBLIC_REGULATION_FILES = {
    "resources/regulation/contents_101.bin",
    "resources/regulation/publickey.pem",
    "resources/regulation/README.txt",
}
RESOURCE_NOTICES = {"FONT-LICENSE", "XCB-CURSOR-LICENSE", ".gitattributes"}
PUBLIC_HEX_FIXTURES = {
    "tests/fixtures/operator-combat-result.hex",
    "tests/fixtures/story-destroyed-unac-result.hex",
    "tests/fixtures/story-subquest-result.hex",
}
EXCLUDED_FILES = {
    "tools/analysis/ultimate_live_copy_control.py",
    "tests/test_deploy_approved_gate.py",
    "tests/test_reviewed_sale_metadata_recovery.py",
}
EXCLUDED = [
    "private evidence and Git history",
    "runtime data/keys/logs/state",
    "tools/ops and their deployment/recovery tests",
    "tools/analysis/ultimate_live_copy_control.py",
    "readme.txt (private host/research guide)",
    "client release kits",
    "game screenshots/templates and unreviewed binary fixtures",
    "compiled releases and local dependencies",
    "untracked research tools and probes",
]
REQUIRED_FILES = {
    "README.txt",
    "pyproject.toml",
    "scripts/build_native.sh",
    "scripts/setup.sh",
    "scripts/export_review.py",
    "scripts/check_public_source.py",
    "src/acvd_server/__init__.py",
    "src/acvd_server/server.py",
    "src/acvd_server/cli.py",
    "src/acvd_server/config.py",
    "src/acvd_server/crypto.py",
    "src/acvd_server/routing.py",
    "src/acvd_server/cdn.py",
    "src/acvd_server/telemetry.py",
    "src/acvd_server/audit_log.py",
    "src/acvd_server/diagnostics.py",
    "src/acvd_server/diagnostic_records.py",
    "src/acvd_server/protocol.py",
    "src/acvd_server/udp.py",
    "native/wrapper.c",
    "native/aes_modes/cwc.c",
    "native/aes_modes/cwc.h",
    "native/aes_modes/mode_hdr.h",
    "native/aes/aes.h",
    "native/aes/aescrypt.c",
    "native/aes/aeskey.c",
    "native/aes/aestab.c",
    "native/aes/aestab.h",
    "native/aes/aesopt.h",
    "native/aes/brg_endian.h",
    "native/aes/brg_types.h",
    *PUBLIC_HEX_FIXTURES,
    *PUBLIC_REGULATION_FILES,
}


def source_names(root: Path) -> list[str]:
    """Include new owned code without automatically publishing untracked research."""
    repository = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if repository.returncode or Path(repository.stdout.strip()).resolve() != root.resolve():
        # An extracted archive has no Git history. Inspect only public source roots.
        names = {
            name for name in ROOT_FILES if (root / name).exists() or (root / name).is_symlink()
        }
        for directory in SOURCE_DIRS | {".github", "resources/regulation"}:
            path = root / directory
            if path.is_symlink():
                raise ValueError("Source list contains a linked directory")
            names.update(p.relative_to(root).as_posix() for p in path.rglob("*"))
        return sorted(names)
    tracked = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"], text=True).split(
        "\0"
    )
    new = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"], text=True
    ).split("\0")
    owned_roots = {"src", "tests", "scripts", "docs", ".github"}
    names = set(filter(None, tracked))
    names.update(
        name
        for name in new
        if name
        and (
            name in ROOT_FILES
            or name in PUBLIC_REGULATION_FILES
            or Path(name).parts[0] in owned_roots
        )
    )
    return sorted(names)


def selected_files(root: Path, tracked: list[str]) -> list[Path]:
    selected = []
    for name in tracked:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Source list contains an unsafe path")
        if name in EXCLUDED_FILES or relative.parts[:2] == ("tools", "ops"):
            continue
        if name not in ROOT_FILES and name not in PUBLIC_REGULATION_FILES:
            workflow = relative.parts[:2] == (".github", "workflows")
            if not relative.parts or (relative.parts[0] not in SOURCE_DIRS and not workflow):
                continue
            if {"screen_templates", "SPRXPatcher", "__pycache__"} & set(relative.parts):
                continue
            documentation = relative.name == "README.txt" or (
                relative.parts[0] == "docs" and relative.suffix == ".txt"
            )
            client_patch = name == "client/rpcs3/signaling-context-ownership.patch"
            if not (
                relative.suffix in SUFFIXES
                or relative.name in RESOURCE_NOTICES
                or name in PUBLIC_HEX_FIXTURES
                or documentation
                or client_patch
                or (workflow and relative.suffix in {".yml", ".yaml"})
            ):
                continue
        path = root / relative
        linked = any(
            (root / Path(*relative.parts[:i])).is_symlink()
            for i in range(1, len(relative.parts) + 1)
        )
        if linked or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Source list contains a linked or outside-root path")
        if not path.exists():
            continue  # A tracked deletion must not reappear in the export.
        if path.is_file():
            selected.append(path)
    return sorted(set(selected))


def private_matches(payload: bytes, patterns: list[str]) -> bool:
    lower = payload.lower()
    for value in patterns:
        for encoding in ("utf-8", "utf-16-be", "utf-16-le"):
            encoded = value.encode(encoding)
            if encoded.lower() in lower or encoded.hex().encode().lower() in lower:
                return True
    # Actual PEM private-key blocks are prohibited; test strings naming one are not keys.
    return bool(
        re.search(
            rb"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/=\r\n]{32,}-----END (?:[A-Z]+ )?PRIVATE KEY-----",
            payload.replace(b"\\n", b"\n"),
        )
    )


def export(root: Path, output: Path, patterns: list[str]) -> dict:
    if output.exists() or output.is_symlink():
        raise ValueError("Choose a new export path; existing archives are not overwritten")
    files = selected_files(root, source_names(root))
    missing = sorted(REQUIRED_FILES - {p.relative_to(root).as_posix() for p in files})
    if missing:
        raise ValueError("Incomplete public source: " + ", ".join(missing))
    # Snapshot once: scanned bytes, archived bytes and manifest hashes must agree.
    contents = {p.relative_to(root).as_posix(): p.read_bytes() for p in files}
    offending = [name for name, payload in contents.items() if private_matches(payload, patterns)]
    if offending:
        raise ValueError("Private-pattern matches in: " + ", ".join(offending))
    if __package__:
        from .check_public_source import check_contents
    else:
        from check_public_source import check_contents

    checks = check_contents(contents, patterns)
    unsafe = [row for row in checks["issues"] if row["code"] in {"private_material", "host_path"}]
    if unsafe:
        raise ValueError("Public source risks in: " + ", ".join(row["path"] for row in unsafe))
    manifest = {name: hashlib.sha256(payload).hexdigest() for name, payload in contents.items()}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in contents.items():
            info = zipfile.ZipInfo("ACVD-source/" + name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (
                0o100755 if name.endswith((".sh", ".desktop")) else 0o100644
            ) << 16
            archive.writestr(info, payload)
        archive.writestr(
            "ACVD-source/review-manifest.json",
            json.dumps(
                {
                    "files": manifest,
                    "excluded": EXCLUDED,
                    "source_checks": checks,
                    "limits": "Source candidate, not publication approval or a turnkey host. Known-pattern scanning is not exhaustive privacy proof. No live gameplay qualification is implied.",
                },
                indent=2,
            ),
        )
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        for name, digest in manifest.items():
            assert hashlib.sha256(archive.read("ACVD-source/" + name)).hexdigest() == digest
    return {"files": len(files), "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--private-patterns",
        type=Path,
        required=True,
        help="Local JSON list of personal names/paths/addresses to reject; never include it in the export",
    )
    args = parser.parse_args()
    try:
        patterns = json.loads(args.private_patterns.read_text())
        if (
            not isinstance(patterns, list)
            or not patterns
            or any(not isinstance(x, str) or not x for x in patterns)
        ):
            raise ValueError(
                "Private-pattern file must contain a nonempty list of nonempty strings"
            )
        print(json.dumps(export(ROOT, args.output, patterns), indent=2))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(2, f"Export refused: {error}\n")


if __name__ == "__main__":
    main()
