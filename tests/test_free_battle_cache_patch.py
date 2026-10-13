import pytest

from client.free_battle_cache import (
    ANCHOR_WORDS,
    CACHE_CAPACITY,
    CACHE_LENGTH_POINTER,
    GUARD_ADDRESS,
    GUARD_WORDS,
    HOOK_ADDRESS,
    PATCH_NAME,
    PPU_HASH,
    RESUME_ADDRESS,
    WORD_PATCHES,
    activation_block,
    branch_word,
    patch_bytes,
    patch_yaml,
)


def fixture():
    data = bytearray(max(address for address, _ in ANCHOR_WORDS) - 0x10000 + 16)
    for address, word in ANCHOR_WORDS:
        offset = address - 0x10000
        data[offset : offset + 4] = word.to_bytes(4, "big")
    for address, expected, _ in WORD_PATCHES:
        offset = address - 0x10000
        data[offset : offset + 4] = expected
    return bytes(data)


def test_fallback_changes_only_reviewed_words():
    original = fixture()
    result = patch_bytes(original)
    assert patch_bytes(result) == result
    previous = 0
    for address, expected, replacement in WORD_PATCHES:
        offset = address - 0x10000
        assert result[previous:offset] == original[previous:offset]
        assert original[offset : offset + 4] == expected
        assert result[offset : offset + 4] == replacement
        previous = offset + 4
    assert result[previous:] == original[previous:]


@pytest.mark.parametrize("index", [0, len(WORD_PATCHES) - 1])
def test_fallback_rejects_unknown_instruction(index):
    data = bytearray(fixture())
    offset = WORD_PATCHES[index][0] - 0x10000
    data[offset : offset + 4] = b"BAD!"
    with pytest.raises(ValueError, match="Unknown Free Battle"):
        patch_bytes(bytes(data))


def test_fallback_rejects_short_input():
    with pytest.raises(ValueError):
        patch_bytes(b"")


def test_yaml_has_no_routes_keys_or_startup_patch():
    result = patch_yaml()
    assert f"PPU-{PPU_HASH}:" in result
    assert PATCH_NAME in result and PATCH_NAME in activation_block()
    assert "BLUS31194: [ 01.02 ]" in result
    assert result.count("- [ be32,") == len(WORD_PATCHES)
    for address, _, replacement in WORD_PATCHES:
        assert f"0x{address:08x}, 0x{replacement.hex()}" in result
    assert "http://" not in result and "PUBLIC KEY" not in result
    assert "002b65d8" not in result and "0093ab54" not in result


def signed(value, bits=32):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def execute_guard(length, magic=0x424E4433, capacity=256000, pointer=0x100000, local=1):
    """Execute the actual emitted instruction words over a small test memory."""
    registers = {index: 0x100 + index for index in range(32)}
    registers.update({0: 0, 3: 0x200000})
    original = registers.copy()
    memory = {CACHE_LENGTH_POINTER: pointer, CACHE_CAPACITY: capacity, original[3] + 0x94: local}
    if pointer:
        memory[pointer] = length & 0xFFFFFFFF
        memory[pointer + 4] = magic
    pc = GUARD_ADDRESS
    flags = {}
    writes = []
    for _ in range(32):
        if pc == RESUME_ADDRESS:
            break
        assert GUARD_ADDRESS <= pc < GUARD_ADDRESS + len(GUARD_WORDS) * 4
        word = GUARD_WORDS[(pc - GUARD_ADDRESS) // 4]
        op, target, base = word >> 26, (word >> 21) & 31, (word >> 16) & 31
        immediate = word & 0xFFFF
        next_pc = pc + 4
        if op == 15:
            registers[target] = (
                (registers[base] if base else 0) + (signed(immediate, 16) << 16)
            ) & 0xFFFFFFFF
        elif op == 32:
            registers[target] = memory[(registers[base] + signed(immediate, 16)) & 0xFFFFFFFF]
        elif op in (10, 11, 31):
            assert (word >> 23) & 7 == 7
            if op == 31:
                assert word & 0x7FE == 0
                left, right = signed(registers[base]), signed(registers[(word >> 11) & 31])
            elif op == 11:
                left, right = signed(registers[base]), signed(immediate, 16)
            else:
                left, right = registers[base], immediate
            flags = {28: left < right, 29: left > right, 30: left == right}
        elif op == 27:
            registers[base] = registers[target] ^ (immediate << 16)
        elif op == 16:
            assert word & 3 == 0 and target in (4, 12)
            take = flags[base] if target == 12 else not flags[base]
            if take:
                next_pc = pc + signed(word & 0xFFFC, 16)
        elif op == 36:
            address = registers[base] + signed(immediate, 16)
            memory[address] = registers[target]
            writes.append((address, registers[target]))
        elif op == 18:
            assert word & 3 == 0
            next_pc = pc + signed(word & 0x03FFFFFC, 26)
        else:
            pytest.fail(f"Unexpected guard opcode: {word:08x}")
        pc = next_pc
    else:
        pytest.fail("Guard did not return within its bound")
    assert all(
        registers[index] == value for index, value in original.items() if index not in (9, 10, 11)
    )
    assert writes in ([], [(original[3] + 0x94, 0)])
    return memory[original[3] + 0x94]


@pytest.mark.parametrize("length", [32, 91335, 256000])
@pytest.mark.parametrize("local", [0, 1])
def test_guard_preserves_existing_capability_for_bounded_bnd3(length, local):
    assert execute_guard(length, local=local) == local


@pytest.mark.parametrize(
    "values",
    [
        {"pointer": 0},
        {"length": 0},
        {"length": 31},
        {"length": -1},
        {"length": 256001},
        {"capacity": 0},
        {"magic": 0},
        {"magic": 0x424E4434},
    ],
)
def test_guard_keeps_original_clear_for_missing_or_bad_cache(values):
    assert execute_guard(**{"length": 91335, **values}) == 0


def test_branch_words_and_guard_footprint_are_exact():
    assert branch_word(HOOK_ADDRESS, GUARD_ADDRESS) == 0x4905ABFC
    assert GUARD_WORDS[-1] == 0x4AFA53CC
    assert len(GUARD_WORDS) * 4 == 64
    with pytest.raises(ValueError):
        branch_word(0, 1 << 25)
    with pytest.raises(ValueError):
        branch_word(1, 4)
