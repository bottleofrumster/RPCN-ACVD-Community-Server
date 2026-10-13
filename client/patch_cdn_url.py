#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

ORIGINAL_URL = b"http://acvd-ps3ww-cdn.fromsoftware.jp/regulation/contents_101.bin"
ROOTLESS_URL = b"http://127.0.0.1:8080/regulation/contents_101.bin"
PEM_BEGIN = b"-----BEGIN RSA PUBLIC KEY-----"
PEM_END = b"-----END RSA PUBLIC KEY-----"
STAGE_GETTER_BRANCH_OFFSET = 0xE84318
STAGE_FALLBACK_CAVE_OFFSET = 0x1350A00
STAGE_FALLBACK_WORDS = (
    bytes.fromhex("8124000c"),  # lwz r9, 0x0c(r4)
    bytes.fromhex("786b0020"),  # rldicl r11, r3, 0, 32
    bytes.fromhex("3d290004"),  # addis r9, r9, 4
    bytes.fromhex("8009043c"),  # lwz r0, 0x043c(r9)
    bytes.fromhex("2f80ffff"),  # cmpwi cr7, r0, -1
    bytes.fromhex("409e0008"),  # bne cr7, keep_selector
    bytes.fromhex("38000001"),  # li r0, 1
    bytes.fromhex("900b0000"),  # keep_selector: stw r0, 0(r11)
    bytes.fromhex("4e800020"),  # blr
)
COMPATIBILITY_WORD_PATCHES = (
    # Undo the legacy forced Japanese list. Overseas clients must use their
    # localized HometownInfo table. Keep the separate null guard below.
    (0x4A51E4, bytes.fromhex("60000000"), bytes.fromhex("409e003c"), "regional operation-base branch"),
    # Retail code dereferences address zero when that same lookup misses.
    # Return zero for a null result and retain the normal non-null path.
    (0x4A5264, bytes.fromhex("409e0050"), bytes.fromhex("419eff9c"), "null lookup return"),
    (0x4A5268, bytes.fromhex("79290020"), bytes.fromhex("4800004c"), "non-null lookup path"),
    # Retail Normal Sortie can reach MatchBriefing with battlefield selector
    # -1 when the retired World Mode service does not assign an active area.
    # Branch to an unreachable NOP pad and substitute the first shipped map
    # selector only for -1. Mock, Story, and future server-supplied selectors
    # pass through unchanged.
    (
        STAGE_GETTER_BRANCH_OFFSET,
        bytes.fromhex("8124000c"),
        bytes.fromhex("484cc6e8"),
        "missing-stage fallback branch",
    ),
    *tuple(
        (
            STAGE_FALLBACK_CAVE_OFFSET + index * 4,
            bytes.fromhex("60000000"),
            replacement,
            f"missing-stage fallback cave word {index}",
        )
        for index, replacement in enumerate(STAGE_FALLBACK_WORDS)
    ),
)


def patch_compatibility_words(data: bytes) -> bytes:
    patched = bytearray(data)
    for offset, expected, replacement, label in COMPATIBILITY_WORD_PATCHES:
        current = bytes(patched[offset : offset + 4])
        if current == replacement:
            continue
        if current != expected:
            raise SystemExit(
                f"unexpected {label} word at file offset 0x{offset:x}: "
                f"found {current.hex()}, expected {expected.hex()}"
            )
        patched[offset : offset + 4] = replacement
    return bytes(patched)


def replace_embedded_public_key(data: bytes, public_key: bytes) -> bytes:
    starts = []
    offset = 0
    while True:
        offset = data.find(PEM_BEGIN, offset)
        if offset < 0:
            break
        starts.append(offset)
        offset += len(PEM_BEGIN)
    if len(starts) != 1:
        raise SystemExit(
            f"expected exactly one embedded regulation public key, found {len(starts)}"
        )
    start = starts[0]
    end = data.find(PEM_END, start)
    if end < 0:
        raise SystemExit("embedded regulation public key has no PEM end marker")
    end += len(PEM_END)
    if data[end : end + 2] == b"\r\n":
        end += 2
    elif data[end : end + 1] == b"\n":
        end += 1
    normalized = public_key.replace(b"\r\n", b"\n")
    if not normalized.endswith(b"\n"):
        normalized += b"\n"
    if len(normalized) != end - start:
        raise SystemExit(
            f"replacement regulation key is {len(normalized)} bytes; executable field is {end - start} bytes"
        )
    return data[:start] + normalized + data[end:]


def patch_elf(source: Path, output: Path, public_key_path: Path | None = None) -> None:
    data = source.read_bytes()
    if len(ROOTLESS_URL) > len(ORIGINAL_URL):
        raise SystemExit("replacement URL is longer than the executable field")
    if data.count(ORIGINAL_URL) == 1:
        replacement = ROOTLESS_URL.ljust(len(ORIGINAL_URL), b"\0")
        patched = data.replace(ORIGINAL_URL, replacement, 1)
    elif data.count(ROOTLESS_URL) == 1:
        patched = data
    else:
        raise SystemExit(
            "expected exactly one retail or already-patched CDN URL in the ACVD executable"
        )
    if public_key_path is not None:
        patched = replace_embedded_public_key(patched, public_key_path.read_bytes())
    patched = patch_compatibility_words(patched)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(patched)
    temporary.replace(output)
    print(f"Patched CDN URL, regulation key, and ACVD compatibility fixes in {output}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Patch a user-decrypted ACVD 1.02 executable to use the rootless local CDN on port 8080"
    )
    parser.add_argument("source", type=Path, help="decrypted BLUS31194 v1.02 EBOOT ELF")
    parser.add_argument("output", type=Path, help="output path for the patched ELF")
    parser.add_argument(
        "--public-key",
        type=Path,
        help="replacement RSA public key for the embedded regulation-container verifier",
    )
    args = parser.parse_args()
    patch_elf(
        args.source.expanduser().resolve(),
        args.output.expanduser().resolve(),
        args.public_key.expanduser().resolve() if args.public_key else None,
    )


if __name__ == "__main__":
    main()
