import struct

from acvd_server.tickets import parse_ticket


def node(node_id: int, raw: bytes) -> bytes:
    return struct.pack(">HH", node_id, len(raw)) + raw


def blob(blob_id: int, children: list[bytes]) -> bytes:
    return node(0x3000 | blob_id, b"".join(children))


def test_parse_rpcn_ticket() -> None:
    online_id = b"Pilot".ljust(0x20, b"\0")
    service_id = b"acvd-test".ljust(0x18, b"\0")
    user = blob(
        0,
        [
            node(8, b"1".ljust(0x14, b"\0")),
            node(1, struct.pack(">I", 0x100)),
            node(7, struct.pack(">Q", 1000)),
            node(7, struct.pack(">Q", 2000)),
            node(2, struct.pack(">Q", 42)),
            node(4, online_id),
            node(8, b"us\0\0"),
            node(4, b"un\0\0"),
            node(8, service_id),
            node(1, struct.pack(">I", 0)),
            node(0, b""),
            node(0, b""),
        ],
    )
    signature = blob(2, [node(8, b"RPCN"), node(8, bytes(56))])
    body = user + signature
    ticket = struct.pack(">II", 0x21010000, len(body)) + body
    parsed = parse_ticket(ticket)
    assert parsed.online_id == "Pilot"
    assert parsed.account_id == 42
    assert parsed.service_id == "acvd-test"
    assert parsed.signature_marker == b"RPCN"
