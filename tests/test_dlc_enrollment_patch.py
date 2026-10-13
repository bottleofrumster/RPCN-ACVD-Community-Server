import pytest

from client import dlc_enrollment as dlc, free_battle_cache as free


def fixture():
    result = bytearray(dlc.GUARD_ADDRESS + 64 - 0x10000)
    for address, word in dlc.ANCHOR_WORDS:
        result[address - 0x10000 : address - 0x10000 + 4] = word.to_bytes(4, "big")
    for address, expected, _ in dlc.WORD_PATCHES:
        result[address - 0x10000 : address - 0x10000 + 4] = expected
    return bytes(result)


def test_exact_changes_and_idempotence():
    original = fixture()
    patched = dlc.patch_bytes(original)
    assert dlc.patch_bytes(patched) == patched
    expected = bytearray(original)
    for address, _, value in dlc.WORD_PATCHES:
        expected[address - 0x10000 : address - 0x10000 + 4] = value
    assert patched == bytes(expected)
    assert len(dlc.WORD_PATCHES) == 17
    assert len(dlc.GUARD_WORDS) * 4 == 60
    assert dlc.WORD_PATCHES[1][2].hex() == "48a8dc61"
    assert dlc.GUARD_WORDS[-1] == 0x480F1AB8


@pytest.mark.parametrize(
    "address", [0x002B65FC, dlc.HOOK_ADDRESS, dlc.GUARD_ADDRESS, dlc.GUARD_ADDRESS - 4]
)
def test_rejects_unknown_code(address):
    data = bytearray(fixture())
    data[address - 0x10000 : address - 0x10000 + 4] = b"BAD!"
    with pytest.raises(ValueError, match="Unknown DLC enrollment"):
        dlc.patch_bytes(bytes(data))


def test_rejects_short_input():
    with pytest.raises(ValueError):
        dlc.patch_bytes(b"")


def test_independent_entries_and_disjoint_words():
    assert not {a for a, _, _ in dlc.WORD_PATCHES} & {a for a, _, _ in free.WORD_PATCHES}
    assert dlc.PATCH_NAME in dlc.patch_yaml() and dlc.PATCH_NAME in dlc.activation_block()
    assert "http://" not in dlc.patch_yaml() and "PUBLIC KEY" not in dlc.patch_yaml()
    assert dlc.patch_yaml().count("- [ be32,") == 17


@pytest.mark.parametrize(
    "kind,pointer,high,low,expected",
    [
        (0x01C75280, 0x200000, 0, 0, "keep"),
        (0x01C75280, 0x200000, 1, 0, "native"),
        (0x01C75280, 0x200000, 0, 1, "native"),
        (0x01C75280, 0x200000, 0x80000000, 0, "native"),
        (0x01C75078, 0x200000, 0, 0, "native"),
        (0x01C75280, 0, 0, 0, "native"),
    ],
)
def test_execute_actual_guard_words(kind, pointer, high, low, expected):
    registers = {i: 0x100 + i for i in range(32)}
    registers.update({1: 0x300000, 2: 0x01DCCE40, 3: 0x100000, 4: 0x400000})
    original = dict(registers)
    memory = {0x100000: kind, 0x10004C: pointer, 0x200004: high, 0x200008: low}
    old_memory = dict(memory)
    pc = dlc.GUARD_ADDRESS
    equal = False
    for _ in range(20):
        word = dlc.GUARD_WORDS[(pc - dlc.GUARD_ADDRESS) // 4]
        op, rt, ra = word >> 26, (word >> 21) & 31, (word >> 16) & 31
        immediate = word & 0xFFFF
        next_pc = pc + 4
        if op == 62:
            assert word == 0xF8410028
            memory[registers[ra] + immediate] = registers[rt]
        elif op == 32:
            registers[rt] = memory[registers[ra] + immediate] & 0xFFFFFFFF
        elif op == 27:
            registers[ra] = registers[rt] ^ (immediate << 16)
        elif op in (10, 11):
            assert (word >> 23) & 7 == 7
            equal = registers[ra] == immediate
        elif op == 16:
            assert ra == 30 and rt in (4, 12) and word & 3 == 0
            if equal == (rt == 12):
                next_pc = pc + (word & 0xFFFC)
        elif op == 31:
            assert word & 0x7FF == 0x378
            registers[ra] = registers[rt] | registers[(word >> 11) & 31]
        elif word == 0x4E800020:
            result = "keep"
            break
        elif op == 18:
            assert word & 3 == 0
            assert pc + (word & 0x03FFFFFC) == dlc.NATIVE_SETTER
            result = "native"
            break
        else:
            pytest.fail(f"Unexpected instruction {word:08x}")
        pc = next_pc
    else:
        pytest.fail("Guard exceeded its instruction bound")
    assert result == expected
    assert all(registers[i] == v for i, v in original.items() if i not in (9, 10, 11))
    assert memory == {**old_memory, original[1] + 0x28: original[2]}
