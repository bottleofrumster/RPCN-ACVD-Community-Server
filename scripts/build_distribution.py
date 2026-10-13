"""Validate a reviewed client kit before building its release archive.

Never source player assets from the repository automatically. The operator
supplies the reviewed kit. Existing runtime patches must remain byte-exact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Import only this script's own source tree, never an operator-selected kit.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DIST = ROOT / "dist"
KIT_NAME = "ACVD Helper Drop-In Files"
KIT_DIR = ROOT / "client user patch files"
ARCHIVE_PATH = DIST / "ACVD Helper Drop-In Files.zip"
DEFAULT_PACK_PATH = "RPCS3/dev_hdd0/game/BLUS31194/USRDIR/bind/patch.bnd"
# Reviewed binder: all 33 Default payloads, plus the public regulation key.
# Recheck payloads before approving another binder; a filename is not proof.
DEFAULT_PACK_SHA256 = "7e0532486528e2cb90d561a6a942443367abce26de3566086fc91bcc135a556b"
PUBLIC_KEY_SHA256 = "51ce9b1dadcf024ecfe6bb2132947d639a75eefedf7c8bed70eca62d3bc741cd"
PRESET_HASHES = json.loads((ROOT / "client/mock_presets.json").read_text())
OPERATION_BASE_HASHES = json.loads((ROOT / "client/operation_base_tables.json").read_text())
if set(OPERATION_BASE_HASHES) != {"cn", "en", "fr", "ge", "it", "kr", "sp"} or any(
    not re.fullmatch(r"[0-9a-f]{64}", value) for value in OPERATION_BASE_HASHES.values()
):
    raise ValueError("Invalid reviewed Operation Base manifest.")
PRESET_ROOTS = (
    "RPCS3/games/ACVD Unbound/PS3_GAME/USRDIR/custommap",
    "RPCS3/games/Armored Core Verdict Day [BLUS31194]/PS3_GAME/USRDIR/custommap",
)
# Original preset bytes are approved by hash, not by filename alone.
EXPECTED_PRESETS = {
    f"{area * 10 + level:04d}_preset.layout"
    for area in range(1, 8)
    for level in range(1, 6)
}
if set(PRESET_HASHES) != EXPECTED_PRESETS or any(
    not re.fullmatch(r"[0-9a-f]{64}", value) for value in PRESET_HASHES.values()
):
    raise ValueError("Invalid reviewed Mock preset manifest.")
REQUIRED_FILES = {
    *(
        f"{root.rsplit('/', 1)[0]}/lang/{language}/param/hometowninfo.bin"
        for root in PRESET_ROOTS for language in OPERATION_BASE_HASHES
    ),
    DEFAULT_PACK_PATH,
    "README.txt",
    "RPCS3/config/patch_config.yml",
    "RPCS3/games/ACVD Unbound/PS3_GAME/USRDIR/system/publickey.pem",
    "RPCS3/games/Armored Core Verdict Day [BLUS31194]/PS3_GAME/USRDIR/system/publickey.pem",
    "RPCS3/patches/BLUS31194_patch.yml",
    *(f"{root}/{name}" for root in PRESET_ROOTS for name in PRESET_HASHES),
}
FORBIDDEN_NAMES = {
    "EBOOT.BIN",
    "AcvdLib.sprx",
    "install_client.py",
    "uninstall_client.py",
    "server-private.pem",
    "privatekey.pem",
}


def _canonical_entry(text: str, header: str, name: str) -> str:
    """Read one canonical entry inside one PPU section, not arbitrary YAML."""
    lines = text.splitlines()
    # This is a narrow generated-kit grammar, not a permissive YAML parser.
    # Reject alternate quoting/comments rather than guessing duplicate-key rules.
    root_keys, patch_keys = set(), set()
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        prefix = line[: len(line) - len(line.lstrip())]
        if "\t" in prefix:
            raise ValueError("Noncanonical YAML indentation.")
        if not prefix:
            if line != "Version: 1.2" and not re.fullmatch(r"PPU-[0-9a-f]+:", line):
                raise ValueError("Noncanonical root key.")
            key = line.split(":", 1)[0]
            if key in root_keys:
                raise ValueError("Duplicate root key.")
            root_keys.add(key)
        elif len(prefix) == 2:
            if not re.fullmatch(r'  "[^"\\]+":', line):
                raise ValueError("Noncanonical patch key.")
            key = line[3:-2]
            if key in patch_keys:
                raise ValueError(f"Duplicate patch key: {key}")
            patch_keys.add(key)
    starts = [i for i, line in enumerate(lines) if line == header]
    entry_header = f'  "{name}":'
    if len(starts) != 1 or lines.count(entry_header) != 1:
        raise ValueError("Missing or duplicate PPU/patch definition.")
    start = starts[0] + 1
    end = next(
        (
            i
            for i in range(start, len(lines))
            if lines[i] and not lines[i].startswith((" ", "\t", "#"))
        ),
        len(lines),
    )
    entries = [i for i in range(start, end) if lines[i] == entry_header]
    if len(entries) != 1:
        raise ValueError("Patch is outside the expected PPU section.")
    first = entries[0]
    last = next(
        (
            i
            for i in range(first + 1, end)
            if lines[i].startswith("  ")
            and len(lines[i]) > 2
            and not lines[i][2].isspace()
            and lines[i][2] != "#"
        ),
        end,
    )
    return "\n".join(lines[first:last]).strip()


def _validate_patch(kit_dir: Path, source_name: str, label: str) -> None:
    """Require the canonical entry and fresh-install activation block."""
    spec = spec_from_file_location(
        "acvd_client_" + source_name, ROOT / "client" / (source_name + ".py")
    )
    if spec is None or spec.loader is None:
        raise SystemExit("Cannot load the client patch definition.")
    fallback = module_from_spec(spec)
    spec.loader.exec_module(fallback)
    patch = (kit_dir / "RPCS3/patches/BLUS31194_patch.yml").read_text()
    config = (kit_dir / "RPCS3/config/patch_config.yml").read_text()
    header = f"PPU-{fallback.PPU_HASH}:"
    try:
        patch_entry = _canonical_entry(patch, header, fallback.PATCH_NAME)
        activation = _canonical_entry(config, header, fallback.PATCH_NAME)
    except ValueError as error:
        raise SystemExit(f"Invalid reviewed {label} patch placement: {error}") from error
    if patch_entry != fallback.patch_entry().strip():
        raise SystemExit(f"client kit does not contain the reviewed {label} patch")
    if activation != fallback.activation_block().strip():
        raise SystemExit(f"client kit does not enable the reviewed {label} patch")


def validate_fallback(kit_dir: Path) -> None:
    """Keep the established fallback-only validation entry point."""
    _validate_patch(kit_dir, "free_battle_cache", "fallback")


def validate_operation_bases(kit_dir: Path) -> None:
    """Country selection needs BOTH the regional branch and localized tables."""
    from client.free_battle_cache import PPU_HASH

    patch = (kit_dir / "RPCS3/patches/BLUS31194_patch.yml").read_text()
    for route in ("Public", "LAN"):
        if route == "LAN" and '  "ACVD Community Server - LAN":' not in patch.splitlines():
            continue  # The public helper kit intentionally has no private LAN route.
        try:
            entry = _canonical_entry(patch, f"PPU-{PPU_HASH}:", f"ACVD Community Server - {route}")
        except ValueError as error:
            raise SystemExit(f"Invalid Operation Base patch placement: {error}") from error
        for address, word in (
            ("004b51e4", "409e003c"),
            ("004b5264", "419eff9c"),
            ("004b5268", "4800004c"),
        ):
            values = re.findall(
                rf"\[\s*be32\s*,\s*0x{address}\s*,\s*0x([0-9a-f]+)\s*\]",
                entry, re.IGNORECASE,
            )
            if [value.lower() for value in values] != [word]:
                raise SystemExit(f"Unreviewed Operation Base branch/guard in {route}")
    for root in PRESET_ROOTS:
        for language, expected in OPERATION_BASE_HASHES.items():
            name = f"{root.rsplit('/', 1)[0]}/lang/{language}/param/hometowninfo.bin"
            if hashlib.sha256((kit_dir / name).read_bytes()).hexdigest() != expected:
                raise SystemExit(f"Unreviewed Operation Base table: {name}")


def validate_kit(kit_dir: Path = KIT_DIR) -> list[Path]:
    if not kit_dir.is_dir():
        raise SystemExit(f"missing reviewed helper directory: {kit_dir}")
    entries = sorted(kit_dir.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise SystemExit("Client kits must not contain symbolic links.")
    files = [path for path in entries if path.is_file()]
    names = {path.relative_to(kit_dir).as_posix() for path in files}
    if names != REQUIRED_FILES:
        missing = sorted(REQUIRED_FILES - names)
        extra = sorted(names - REQUIRED_FILES)
        raise SystemExit(f"client kit file mismatch; missing={missing}, extra={extra}")
    basenames = {path.name for path in files}
    if basenames & FORBIDDEN_NAMES:
        raise SystemExit(f"prohibited distributable content: {sorted(basenames & FORBIDDEN_NAMES)}")
    public_keys = [path for path in files if path.name == "publickey.pem"]
    if any(b"PRIVATE KEY" in public_key.read_bytes() for public_key in public_keys):
        raise SystemExit("publickey.pem unexpectedly contains a private key")
    if any(
        hashlib.sha256(public_key.read_bytes()).hexdigest() != PUBLIC_KEY_SHA256
        for public_key in public_keys
    ):
        raise SystemExit("Client public keys do not match the reviewed binder key.")
    validate_fallback(kit_dir)
    _validate_patch(kit_dir, "dlc_enrollment", "DLC enrollment")
    validate_operation_bases(kit_dir)
    if hashlib.sha256((kit_dir / DEFAULT_PACK_PATH).read_bytes()).hexdigest() != DEFAULT_PACK_SHA256:
        raise SystemExit("Client kit does not contain the reviewed Default map pack.")
    for root in PRESET_ROOTS:
        for name, expected in PRESET_HASHES.items():
            payload = (kit_dir / root / name).read_bytes()
            if len(payload) != 1056 or hashlib.sha256(payload).hexdigest() != expected:
                raise SystemExit(f"Unreviewed Mock preset bytes: {root}/{name}")
    return files


def build_archive(kit_dir: Path = KIT_DIR, output: Path = ARCHIVE_PATH) -> Path:
    files = validate_kit(kit_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, Path(kit_dir.name) / path.relative_to(kit_dir))
    with zipfile.ZipFile(temporary) as archive:
        bad = archive.testzip()
        if bad is not None:
            raise SystemExit(f"client archive integrity check failed: {bad}")
        for path in files:
            name = (Path(kit_dir.name) / path.relative_to(kit_dir)).as_posix()
            if archive.read(name) != path.read_bytes():
                raise SystemExit(f"client archive content check failed: {name}")
    temporary.replace(output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit-dir", type=Path, default=KIT_DIR)
    parser.add_argument("--output", type=Path, default=ARCHIVE_PATH)
    args = parser.parse_args()
    print(build_archive(args.kit_dir, args.output))


if __name__ == "__main__":
    main()
