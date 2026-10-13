from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location("patch_cdn_url", ROOT / "client/patch_cdn_url.py")
assert SPEC and SPEC.loader
PATCHER = module_from_spec(SPEC)
SPEC.loader.exec_module(PATCHER)


def compatibility_fixture() -> bytes:
    size = max(offset for offset, *_ in PATCHER.COMPATIBILITY_WORD_PATCHES) + 4
    data = bytearray(size)
    for offset, expected, _, _ in PATCHER.COMPATIBILITY_WORD_PATCHES:
        data[offset : offset + 4] = expected
    return bytes(data)


def test_compatibility_words_are_patched_idempotently() -> None:
    patched = PATCHER.patch_compatibility_words(compatibility_fixture())
    assert PATCHER.patch_compatibility_words(patched) == patched
    for offset, _, replacement, _ in PATCHER.COMPATIBILITY_WORD_PATCHES:
        assert patched[offset : offset + 4] == replacement


def test_compatibility_patch_rejects_an_unknown_executable() -> None:
    data = bytearray(compatibility_fixture())
    offset = PATCHER.COMPATIBILITY_WORD_PATCHES[0][0]
    data[offset : offset + 4] = b"BAD!"
    with pytest.raises(SystemExit, match="unexpected regional operation-base branch word"):
        PATCHER.patch_compatibility_words(bytes(data))


def test_country_branch_accepts_retail_and_migrates_legacy_fallback():
    legacy = compatibility_fixture()
    assert legacy[0x4A51E4:0x4A51E8] == bytes.fromhex("60000000")
    fixed = PATCHER.patch_compatibility_words(legacy)
    assert fixed[0x4A51E4:0x4A51E8] == bytes.fromhex("409e003c")
    assert fixed[0x4A5264:0x4A526C] == bytes.fromhex("419eff9c4800004c")
    retail = bytearray(legacy)
    retail[0x4A51E4:0x4A51E8] = bytes.fromhex("409e003c")
    assert PATCHER.patch_compatibility_words(bytes(retail)) == fixed


def test_missing_stage_fallback_branch_and_cave_are_exact() -> None:
    branch = next(
        replacement
        for offset, _, replacement, _ in PATCHER.COMPATIBILITY_WORD_PATCHES
        if offset == PATCHER.STAGE_GETTER_BRANCH_OFFSET
    )
    instruction = int.from_bytes(branch, "big")
    displacement = instruction & 0x03FFFFFC
    if displacement & 0x02000000:
        displacement -= 0x04000000
    assert PATCHER.STAGE_GETTER_BRANCH_OFFSET + displacement == PATCHER.STAGE_FALLBACK_CAVE_OFFSET

    cave = tuple(
        replacement
        for offset, _, replacement, _ in PATCHER.COMPATIBILITY_WORD_PATCHES
        if PATCHER.STAGE_FALLBACK_CAVE_OFFSET
        <= offset
        < PATCHER.STAGE_FALLBACK_CAVE_OFFSET + len(PATCHER.STAGE_FALLBACK_WORDS) * 4
    )
    assert cave == PATCHER.STAGE_FALLBACK_WORDS
