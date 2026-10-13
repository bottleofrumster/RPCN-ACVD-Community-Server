import zlib

from tools.acvd_peer_message_summary import MAX_BODY, complete_frame


def frame(body, compressed=False):
    kind = 0x19D | (0x8000 if compressed else 0)
    if compressed:
        body = len(body).to_bytes(4, "big") + zlib.compress(body, 9)[:-4]
    return (
        (6006).to_bytes(2, "little")
        + bytes(11)
        + kind.to_bytes(4, "big")
        + (len(body) + 8).to_bytes(4, "big")
        + body
    )


def test_complete_plain_and_deflate_without_checksum():
    body = bytes(range(256)) * 8
    assert complete_frame(frame(body)) == (6006, 0x19D, body)
    assert complete_frame(frame(body, True)) == (6006, 0x19D, body)


def test_fragment_and_incorrect_declared_body_are_not_complete():
    raw = frame(bytes(2216), True)
    assert complete_frame(raw[:-1]) is None
    assert complete_frame(raw[:21] + (2215).to_bytes(4, "big") + raw[25:]) is None
    assert complete_frame(raw[:21] + (MAX_BODY + 1).to_bytes(4, "big") + raw[25:]) is None


def test_unrelated_ports_and_truncated_deflate_are_not_accepted():
    raw = frame(bytes(2216), True)
    assert complete_frame(bytes(2) + raw[2:]) is None
    truncated = raw[:17] + (len(raw) - 14).to_bytes(4, "big") + raw[21:-1]
    assert complete_frame(truncated) is None
