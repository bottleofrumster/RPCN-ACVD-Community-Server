"""Encode and verify the login ticket fields used by authentication."""

from __future__ import annotations

import struct
from dataclasses import dataclass


class TicketError(ValueError):
    pass


@dataclass(slots=True)
class TicketNode:
    node_id: int
    raw: bytes
    children: list["TicketNode"] | None = None

    @property
    def u32(self) -> int:
        if len(self.raw) != 4:
            raise TicketError("ticket node is not a u32")
        return struct.unpack(">I", self.raw)[0]

    @property
    def u64(self) -> int:
        if len(self.raw) != 8:
            raise TicketError("ticket node is not a u64")
        return struct.unpack(">Q", self.raw)[0]

    @property
    def text(self) -> str:
        return self.raw.split(b"\0", 1)[0].decode("utf-8", "replace")


@dataclass(slots=True)
class TicketInfo:
    version: int
    serial_id: str
    issuer_id: int
    issued_at_ms: int
    expires_at_ms: int
    account_id: int
    online_id: str
    region: str
    domain: str
    service_id: str
    status: int
    cookie: bytes | None
    signature_marker: bytes


def _parse_node(data: bytes, offset: int) -> tuple[TicketNode, int]:
    if offset + 4 > len(data):
        raise TicketError("truncated ticket node header")
    node_id, length = struct.unpack_from(">HH", data, offset)
    start, end = offset + 4, offset + 4 + length
    if end > len(data):
        raise TicketError("truncated ticket node")
    raw = data[start:end]
    children = None
    if node_id & 0x3000 == 0x3000:
        children = []
        child_offset = start
        while child_offset < end:
            child, child_offset = _parse_node(data, child_offset)
            if child_offset > end:
                raise TicketError("ticket child exceeds parent")
            children.append(child)
        if child_offset != end:
            raise TicketError("ticket child alignment mismatch")
    return TicketNode(node_id, raw, children), end


def parse_ticket(data: bytes) -> TicketInfo:
    if len(data) < 8:
        raise TicketError("ticket is shorter than its header")
    version, declared_size = struct.unpack_from(">II", data)
    if version != 0x21010000:
        raise TicketError(f"unsupported ticket version: 0x{version:08x}")
    if declared_size + 8 != len(data):
        raise TicketError("ticket size field does not match payload")
    nodes: list[TicketNode] = []
    offset = 8
    while offset < len(data):
        node, offset = _parse_node(data, offset)
        nodes.append(node)
    if len(nodes) != 2 or nodes[0].node_id != 0x3000 or nodes[1].node_id != 0x3002:
        raise TicketError("ticket does not have expected user/signature blobs")
    user = nodes[0].children or []
    if len(user) < 10:
        raise TicketError("ticket user blob is incomplete")
    cookie = user[10].raw if len(user) > 10 and user[10].node_id == 8 and user[10].raw else None
    signature_children = nodes[1].children or []
    signature_marker = signature_children[0].raw if signature_children else b""
    return TicketInfo(
        version=version,
        serial_id=user[0].text,
        issuer_id=user[1].u32,
        issued_at_ms=user[2].u64,
        expires_at_ms=user[3].u64,
        account_id=user[4].u64,
        online_id=user[5].text,
        region=user[6].text,
        domain=user[7].text,
        service_id=user[8].text,
        status=user[9].u32,
        cookie=cookie,
        signature_marker=signature_marker,
    )
