"""Build the qualified BLUS31194 first-enrollment DLC and name preservation patch.

Load installed regulation before native garage validation. Keep the local pilot
name until native online history exists. Do not grant items or change history.
"""

from client.free_battle_cache import PPU_HASH, branch_word

PATCH_NAME = "ACVD DLC Enrollment Preservation"
PATCH_VERSION = "1.0.0"
HOOK_ADDRESS = 0x00EEF814
GUARD_ADDRESS = 0x0197D474
NATIVE_SETTER = 0x01A6EF64

# The caller restores TOC from stack+0x28 even when we skip the import stub.
# Preserve that store, LR, SP, r3/r4, and all nonvolatile registers.
# Unknown types and nonzero history keep the complete native name setter.
GUARD_WORDS = (
    0xF8410028,  # std r2, 0x28(r1)
    0x81230000,  # lwz r9, 0(r3)
    0x6D2901C7,  # xoris r9, r9, 0x01c7
    0x2B895280,  # cmplwi cr7, r9, 0x5280
    0x409E0028,  # bne cr7, native
    0x8123004C,  # lwz r9, 0x4c(r3)
    0x2F890000,  # cmpwi cr7, r9, 0
    0x419E001C,  # beq cr7, native
    0x81490004,  # lwz r10, 4(r9)
    0x81690008,  # lwz r11, 8(r9)
    0x7D4A5B78,  # or r10, r10, r11
    0x2F8A0000,  # cmpwi cr7, r10, 0
    0x409E0008,  # bne cr7, native
    0x4E800020,  # blr: keep the local first-enrollment name
    branch_word(GUARD_ADDRESS + 14 * 4, NATIVE_SETTER),
)
WORD_PATCHES = (
    (0x002B65FC, bytes.fromhex("809f0090"), bytes.fromhex("809f0094")),
    (
        HOOK_ADDRESS,
        bytes.fromhex("48b7f751"),
        (branch_word(HOOK_ADDRESS, GUARD_ADDRESS) | 1).to_bytes(4, "big"),
    ),
    *(
        (GUARD_ADDRESS + i * 4, bytes.fromhex("60000000"), word.to_bytes(4, "big"))
        for i, word in enumerate(GUARD_WORDS)
    ),
)
ANCHOR_WORDS = (
    (0x002B65D8, 0x809F0094),
    (0x002B6600, 0x49795865),
    (HOOK_ADDRESS + 4, 0xE8410028),
    (0x001C8B68, 0x80630054),
    (GUARD_ADDRESS - 4, 0x4BFFFEC0),
    (GUARD_ADDRESS + 60, 0xF821FF51),
)


def patch_bytes(data: bytes) -> bytes:
    """Return a patched copy. Reject unknown code or changed padding."""
    for address, expected in ANCHOR_WORDS:
        offset = address - 0x10000
        if data[offset : offset + 4] != expected.to_bytes(4, "big"):
            raise ValueError("Unknown DLC enrollment anchor")
    result = bytearray(data)
    for address, expected, replacement in WORD_PATCHES:
        offset = address - 0x10000
        if data[offset : offset + 4] not in (expected, replacement):
            raise ValueError("Unknown DLC enrollment instruction")
        result[offset : offset + 4] = replacement
    return bytes(result)


def patch_entry() -> str:
    """Return the independent RPCS3 entry without server routes or keys."""
    words = "".join(f"      - [ be32, 0x{a:08x}, 0x{b.hex()} ]\n" for a, _, b in WORD_PATCHES)
    return f'''  "{PATCH_NAME}":
    Games:
      "Armored Core: Verdict Day":
        BLUS31194: [ 01.02 ]
    Author: "ACVD community server project"
    Patch Version: {PATCH_VERSION}
    Notes: "Load installed DLC before first-enrollment validation. Preserve the local pilot name until online history exists. Keep native ownership, validation, and signature checks."
    Patch:
{words}'''


def patch_yaml() -> str:
    return f"Version: 1.2\n\nPPU-{PPU_HASH}:\n" + patch_entry()


def activation_block() -> str:
    return f'''  "{PATCH_NAME}":
    "Armored Core: Verdict Day":
      BLUS31194:
        01.02:
          Enabled: true
'''
