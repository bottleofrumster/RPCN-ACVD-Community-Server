"""Recovered response errors must not alter established success framing."""

import pytest

from acvd_server.udp import GameMessage, encode_game_reply_fragments


def reply(payload=b""):
    return GameMessage(0, b"\x12\x34\x56\x78", payload, 0xABCD)


def test_success_prefix_is_preserved_exactly():
    fragment = encode_game_reply_fragments(reply(b"\x08\x01"))[0]
    assert fragment[:12] == bytes.fromhex("abcd00000000001e0000001e")
    assert fragment[12:] == bytes.fromhex(
        "0000000c0000000012345678000000000100000000000000000000000801"
    )


@pytest.mark.parametrize("fragment_limit", [7, 900])
def test_failure_preserves_envelope_and_encodes_big_endian_status(fragment_limit):
    fragments = encode_game_reply_fragments(
        reply(), max_fragment_length=fragment_limit, error=(1, 28)
    )
    body = b"".join(fragment[12:] for fragment in fragments)
    assert body == bytes.fromhex("0000000c0000000012345678000000010000001c0000000000000000")
    for index, fragment in enumerate(fragments):
        assert fragment[:3] == bytes.fromhex("abcd00")
        assert int.from_bytes(fragment[3:8], "big") == len(body)
        assert fragment[8:10] == bytes((0, index))
        assert int.from_bytes(fragment[10:12], "big") == len(fragment) - 12


@pytest.mark.parametrize(
    "error",
    [(0, 28), (-1, 28), (2**31, 28), (1, -1), (1, 2**32), (True, 28), (1, False)],
)
def test_invalid_error_words_are_rejected(error):
    with pytest.raises(ValueError):
        encode_game_reply_fragments(reply(), error=error)


def test_failure_cannot_carry_a_success_payload():
    with pytest.raises(ValueError, match="payload"):
        encode_game_reply_fragments(reply(b"\x08\x01"), error=(1, 28))
