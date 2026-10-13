import hashlib
import zipfile
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from client import dlc_enrollment, free_battle_cache

ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location("build_distribution", ROOT / "scripts/build_distribution.py")
assert SPEC and SPEC.loader
BUILDER = module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


def test_country_repair_accepts_public_only_distribution(kit):
    path = kit / "RPCS3/patches/BLUS31194_patch.yml"
    path.write_text(path.read_text().split('  "ACVD Community Server - LAN":')[0])
    BUILDER.validate_kit(kit)


@pytest.mark.parametrize("old,new", [
    ("0x004b51e4, 0x409e003c", "0x004b51e4, 0x60000000"),
    ("0x004b5264, 0x419eff9c", "0x004b5264, 0x409e0050"),
    ("0x004b5268, 0x4800004c", "0x004b5268, 0x79290020"),
])
def test_country_repair_rejects_legacy_branch_or_removed_crash_guard(kit, old, new):
    path = kit / "RPCS3/patches/BLUS31194_patch.yml"
    path.write_text(path.read_text().replace(old, new))
    with pytest.raises(SystemExit, match="Operation Base branch/guard"):
        BUILDER.validate_kit(kit)


def test_country_repair_rejects_unreviewed_table(kit):
    root = BUILDER.PRESET_ROOTS[0].rsplit('/', 1)[0]
    path = kit / f"{root}/lang/en/param/hometowninfo.bin"
    path.write_bytes(b"Wrong country mapping")
    with pytest.raises(SystemExit, match="Operation Base table"):
        BUILDER.validate_kit(kit)


def test_country_repair_requires_exact_lowercase_filename(kit):
    root = BUILDER.PRESET_ROOTS[0].rsplit('/', 1)[0]
    path = kit / f"{root}/lang/en/param/hometowninfo.bin"
    path.rename(path.with_name("hometownInfo.bin"))
    with pytest.raises(SystemExit, match="missing"):
        BUILDER.validate_kit(kit)


@pytest.fixture
def kit(tmp_path: Path, monkeypatch) -> Path:
    """Use synthetic assets; never require or read a player's release tree."""
    root = tmp_path / BUILDER.KIT_NAME
    for name in BUILDER.REQUIRED_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"Synthetic test asset.\n")
    # Pin synthetic layout hashes in this fixture; do not read game assets.
    hashes = {}
    for name in BUILDER.PRESET_HASHES:
        layout_id = int(name.split("_", 1)[0])
        payload = bytes([7, 0, 0, 0]) + layout_id.to_bytes(2, "big") + bytes(1050)
        hashes[name] = hashlib.sha256(payload).hexdigest()
        for folder in BUILDER.PRESET_ROOTS:
            (root / folder / name).write_bytes(payload)
    monkeypatch.setattr(BUILDER, "PRESET_HASHES", hashes)
    country_hashes = {}
    for language in BUILDER.OPERATION_BASE_HASHES:
        payload = ("Synthetic country table " + language).encode()
        country_hashes[language] = hashlib.sha256(payload).hexdigest()
        for base in BUILDER.PRESET_ROOTS:
            path = root / f"{base.rsplit('/', 1)[0]}/lang/{language}/param/hometowninfo.bin"
            path.write_bytes(payload)
    monkeypatch.setattr(BUILDER, "OPERATION_BASE_HASHES", country_hashes)
    monkeypatch.setattr(
        BUILDER,
        "PUBLIC_KEY_SHA256",
        hashlib.sha256(next(root.rglob("publickey.pem")).read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        BUILDER,
        "DEFAULT_PACK_SHA256",
        hashlib.sha256((root / BUILDER.DEFAULT_PACK_PATH).read_bytes()).hexdigest(),
    )
    (root / "RPCS3/patches/BLUS31194_patch.yml").write_text(
        free_battle_cache.patch_yaml() + dlc_enrollment.patch_entry()
        + "".join(
            f'  "ACVD Community Server - {route}":\n    Patch:\n'
            '      - [ be32, 0x004b51e4, 0x409e003c ]\n'
            '      - [ be32, 0x004b5264, 0x419eff9c ]\n'
            '      - [ be32, 0x004b5268, 0x4800004c ]\n'
            for route in ("Public", "LAN")
        )
    )
    (root / "RPCS3/config/patch_config.yml").write_text(
        f"PPU-{free_battle_cache.PPU_HASH}:\n"
        + free_battle_cache.activation_block()
        + dlc_enrollment.activation_block()
    )
    return root


def test_reviewed_helper_kit_is_exact_and_buildable(tmp_path: Path, kit: Path) -> None:
    files = BUILDER.validate_kit(kit)
    assert {path.relative_to(kit).as_posix() for path in files} == BUILDER.REQUIRED_FILES
    archive_path = BUILDER.build_archive(kit, output=tmp_path / "helper.zip")
    with zipfile.ZipFile(archive_path) as archive:
        entries = {
            Path(name).relative_to(BUILDER.KIT_NAME).as_posix()
            for name in archive.namelist()
            if not name.endswith("/")
        }
    assert entries == BUILDER.REQUIRED_FILES
    assert not {Path(name).name for name in entries} & BUILDER.FORBIDDEN_NAMES


def test_client_kit_contains_the_reviewed_guard(kit):
    BUILDER.validate_fallback(kit)


@pytest.mark.parametrize("folder", BUILDER.PRESET_ROOTS)
def test_distribution_rejects_changed_preset_bytes(kit, folder):
    path = kit / folder / "0011_preset.layout"
    payload = bytearray(path.read_bytes())
    payload[-1] ^= 1
    path.write_bytes(payload)
    with pytest.raises(SystemExit, match="Unreviewed Mock preset"):
        BUILDER.validate_kit(kit)


def test_distribution_rejects_missing_preset(kit):
    (kit / BUILDER.PRESET_ROOTS[0] / "0011_preset.layout").unlink()
    with pytest.raises(SystemExit, match="file mismatch"):
        BUILDER.validate_kit(kit)


def test_distribution_rejects_changed_default_pack(kit):
    (kit / BUILDER.DEFAULT_PACK_PATH).write_bytes(b"Different map pack.\n")
    with pytest.raises(SystemExit, match="reviewed Default map pack"):
        BUILDER.validate_kit(kit)


def test_distribution_rejects_missing_default_pack(kit):
    (kit / BUILDER.DEFAULT_PACK_PATH).unlink()
    with pytest.raises(SystemExit, match="file mismatch"):
        BUILDER.validate_kit(kit)


def test_distribution_rejects_key_mismatch_in_either_game_folder(kit):
    for target in kit.rglob("publickey.pem"):
        original = target.read_bytes()
        target.write_bytes(b"Another server public key.\n")
        with pytest.raises(SystemExit, match="reviewed binder key"):
            BUILDER.validate_kit(kit)
        target.write_bytes(original)


def test_distribution_rejects_matching_but_unreviewed_keys(kit):
    for target in kit.rglob("publickey.pem"):
        target.write_bytes(b"Another server public key.\n")
    with pytest.raises(SystemExit, match="reviewed binder key"):
        BUILDER.validate_kit(kit)


def test_distribution_rejects_a_changed_guard(tmp_path, kit):
    copied = kit
    patch = copied / "RPCS3/patches/BLUS31194_patch.yml"
    data = patch.read_text()
    assert "0x00922be8" in data
    patch.write_text(data.replace("0x00922be8", "0x00922be4"))
    with pytest.raises(SystemExit, match="reviewed fallback patch"):
        BUILDER.build_archive(copied, tmp_path / "invalid.zip")


def test_distribution_rejects_missing_dlc_entry(kit):
    path = kit / "RPCS3/patches/BLUS31194_patch.yml"
    path.write_text(free_battle_cache.patch_yaml())
    with pytest.raises(SystemExit, match="DLC enrollment"):
        BUILDER.validate_kit(kit)


def test_distribution_rejects_changed_dlc_instruction(kit):
    path = kit / "RPCS3/patches/BLUS31194_patch.yml"
    path.write_text(path.read_text().replace("0x00eef814", "0x00eef810"))
    with pytest.raises(SystemExit, match="DLC enrollment"):
        BUILDER.validate_kit(kit)


def test_distribution_rejects_symbolic_link(kit, tmp_path):
    target = kit / "README.txt"
    target.unlink()
    external = tmp_path / "external.txt"
    external.write_text("Unreviewed input.")
    target.symlink_to(external)
    with pytest.raises(SystemExit, match="symbolic links"):
        BUILDER.validate_kit(kit)


def test_distribution_rejects_private_key(kit):
    target = next(kit.rglob("publickey.pem"))
    target.write_text("-----BEGIN PRIVATE KEY-----")
    with pytest.raises(SystemExit, match="private key"):
        BUILDER.validate_kit(kit)


@pytest.mark.parametrize(
    "relative", ["RPCS3/patches/BLUS31194_patch.yml", "RPCS3/config/patch_config.yml"]
)
def test_distribution_rejects_wrong_ppu_section(kit, relative):
    path = kit / relative
    marker = f'  "{dlc_enrollment.PATCH_NAME}":'
    path.write_text(path.read_text().replace(marker, "PPU-deadbeef:\n" + marker))
    with pytest.raises(SystemExit, match="DLC enrollment"):
        BUILDER.validate_kit(kit)


@pytest.mark.parametrize(
    "relative", ["RPCS3/patches/BLUS31194_patch.yml", "RPCS3/config/patch_config.yml"]
)
def test_distribution_rejects_duplicate_definition(kit, relative):
    path = kit / relative
    block = (
        dlc_enrollment.patch_entry()
        if "patches/" in relative
        else dlc_enrollment.activation_block()
    )
    path.write_text(path.read_text() + "\n" + block)
    with pytest.raises(SystemExit, match="Duplicate patch key"):
        BUILDER.validate_kit(kit)


@pytest.mark.parametrize(
    "replacement", ["", dlc_enrollment.activation_block().replace("true", "false")]
)
def test_distribution_rejects_missing_or_disabled_activation(kit, replacement):
    path = kit / "RPCS3/config/patch_config.yml"
    path.write_text(path.read_text().replace(dlc_enrollment.activation_block(), replacement))
    with pytest.raises(SystemExit, match="DLC enrollment"):
        BUILDER.validate_kit(kit)


@pytest.mark.parametrize(
    "relative", ["RPCS3/patches/BLUS31194_patch.yml", "RPCS3/config/patch_config.yml"]
)
@pytest.mark.parametrize("alternate", ["single-quoted", "commented-header"])
def test_distribution_rejects_equivalent_ambiguous_yaml_keys(kit, relative, alternate):
    path = kit / relative
    block = (
        dlc_enrollment.patch_entry()
        if "patches/" in relative
        else dlc_enrollment.activation_block()
    )
    if alternate == "single-quoted":
        block = block.replace(
            f'  "{dlc_enrollment.PATCH_NAME}":', f"  '{dlc_enrollment.PATCH_NAME}':"
        )
    else:
        block = f"PPU-{dlc_enrollment.PPU_HASH}: # Duplicate\n" + block
    path.write_text(path.read_text() + "\n" + block.replace("true", "false"))
    with pytest.raises(SystemExit, match="Invalid reviewed"):
        BUILDER.validate_kit(kit)
