"""Build the guarded BLUS31194 Free Battle fallback for RPCS3.

The guard preserves existing local-regulation capability only for a bounded
BND3 cache. It leaves startup, save history, network keys, and signatures alone.
The native parser remains responsible for the regulation contents.
"""

PPU_HASH = "546acba4dd056f495ece5908935cc84a5b1b17ae"
PATCH_NAME = "ACVD Free Battle Cache Fallback"
PATCH_VERSION = "0.4.0"
HOOK_ADDRESS = 0x00922BE8
GUARD_ADDRESS = 0x0197D7E4
RESUME_ADDRESS = 0x00922BEC
CACHE_LENGTH_POINTER = 0x01F6E0C4
CACHE_CAPACITY = 0x01F6E0BC


def branch_word(source: int, target: int) -> int:
    """Encode a relative PPC branch without changing LR."""
    delta = target - source
    if source % 4 or target % 4 or not -(1 << 25) <= delta < (1 << 25):
        raise ValueError("PPC branch is outside its aligned range")
    return 0x48000000 | (delta & 0x03FFFFFC)


# r3 is the mode receiver. r0 is zero from the preceding native instruction.
# Only volatile r9-r11 and CR7 change. The stack, LR, TOC, and receiver stay intact.
# The native constructor places the cached body immediately after its length word.
GUARD_WORDS = (
    0x3D2001F7,  # lis r9, 0x1f7
    0x8149E0C4,  # lwz r10, -0x1f3c(r9): cached-length pointer
    0x2F8A0000,  # cmpwi cr7, r10, 0
    0x419E002C,  # beq cr7, clear
    0x816A0000,  # lwz r11, 0(r10): cached length
    0x2F8B001F,  # cmpwi cr7, r11, 31
    0x409D0020,  # ble cr7, clear
    0x8129E0BC,  # lwz r9, -0x1f44(r9): capacity
    0x7F8B4800,  # cmpw cr7, r11, r9
    0x419D0014,  # bgt cr7, clear
    0x814A0004,  # lwz r10, 4(r10): BND3 magic
    0x6D4A424E,  # xoris r10, r10, 0x424e
    0x2B8A4433,  # cmplwi cr7, r10, 0x4433
    0x419E0008,  # beq cr7, keep
    0x90030094,  # clear: original stw r0, 0x94(r3)
    branch_word(GUARD_ADDRESS + 15 * 4, RESUME_ADDRESS),
)
ANCHOR_WORDS = (
    (0x00922BE0, 0x38000000),
    (0x00922BE4, 0x90030098),
    (0x00922BEC, 0x49137659),
    (0x00922BF0, 0xE8410028),
    (0x0197D7E0, 0x48000000),
    (0x0197D824, 0x60000000),
    (0x0197D828, 0x48000000),
    (0x002B65D8, 0x809F0094),
    (0x0093AB54, 0x419E04E8),
    (0x0093AB84, 0x38805A3C),
)
WORD_PATCHES = (
    (
        HOOK_ADDRESS,
        bytes.fromhex("90030094"),
        branch_word(HOOK_ADDRESS, GUARD_ADDRESS).to_bytes(4, "big"),
    ),
    *(
        (GUARD_ADDRESS + i * 4, bytes.fromhex("60000000"), word.to_bytes(4, "big"))
        for i, word in enumerate(GUARD_WORDS)
    ),
)


def patch_bytes(data: bytes) -> bytes:
    """Return a patched copy. Refuse unknown instructions or surrounding code."""
    for address, word in ANCHOR_WORDS:
        offset = address - 0x10000
        if data[offset : offset + 4] != word.to_bytes(4, "big"):
            raise ValueError("Unknown Free Battle fallback anchor")
    result = bytearray(data)
    for address, expected, replacement in WORD_PATCHES:
        offset = address - 0x10000
        current = data[offset : offset + 4]
        if current not in (expected, replacement):
            raise ValueError("Unknown Free Battle fallback instruction")
        result[offset : offset + 4] = replacement
    return bytes(result)


def patch_entry() -> str:
    """Return an independent entry under the matching PPU hash."""
    words = "".join(
        f"      - [ be32, 0x{address:08x}, 0x{replacement.hex()} ]\n"
        for address, _, replacement in WORD_PATCHES
    )
    return f'''  "{PATCH_NAME}":
    Games:
      "Armored Core: Verdict Day":
        BLUS31194: [ 01.02 ]
    Author: "ACVD community server project"
    Patch Version: {PATCH_VERSION}
    Notes: "Keep local regulation when its buffer and BND3 header pass the cache guard. Keep this entry enabled when changing servers. Network signature checks stay active."
    Patch:
{words}'''


def patch_yaml() -> str:
    return f"Version: 1.2\n\nPPU-{PPU_HASH}:\n" + patch_entry()


def activation_block() -> str:
    """Append this block under the existing matching PPU hash."""
    return f'''  "{PATCH_NAME}":
    "Armored Core: Verdict Day":
      BLUS31194:
        01.02:
          Enabled: true
'''
