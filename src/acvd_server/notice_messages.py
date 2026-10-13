"""Personal-notice wire records; no storage, recipient selection, or grants.

See evidence/2026-09-11-personal-notice-ui.md for native display and read keys.
Text and batch bounds are server policy limits, not recovered retail limits.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .protocol import ProtocolError, _proto_datetime, decode_proto, proto_bytes, proto_varint

MAX_ACK_IDS = 128
MAX_TITLE_BYTES = 128
MAX_BODY_BYTES = 2048


def format_login_welcome(text: str) -> str:
    """Wrap the native news card explicitly; the client does not wrap long lines."""
    return "\n".join(
        textwrap.fill(line, width=72, break_on_hyphens=False) if line else ""
        for line in text.splitlines()
    )


@dataclass(frozen=True)
class PersonalNotice:
    identifier: int
    title: str
    body: str
    created_at: datetime

    def __post_init__(self) -> None:
        if type(self.identifier) is not int or not 0 < self.identifier < 1 << 32:
            raise ValueError("Notice ID must be a positive uint32")
        for text, limit in ((self.title, MAX_TITLE_BYTES), (self.body, MAX_BODY_BYTES)):
            if (
                type(text) is not str
                or not text
                or "\0" in text
                or len(text.encode("utf-8")) > limit
            ):
                raise ValueError("Notice text is invalid or too long")
        if not isinstance(self.created_at, datetime) or self.created_at.utcoffset() is None:
            raise ValueError("Notice time requires a time zone")


def encode_personal_notices(notices: Iterable[PersonalNotice]) -> bytes:
    """Encode pending notices with the native-tested acknowledgement flag."""
    return b"".join(
        proto_bytes(
            1,
            proto_varint(1, notice.identifier)
            + proto_bytes(2, notice.title)
            + proto_bytes(3, notice.body)
            + proto_bytes(4, _proto_datetime(notice.created_at))
            + proto_varint(5, 1),
        )
        for notice in notices
    )


def decode_notice_read_ids(payload: bytes) -> list[int]:
    """Reject a malformed batch before any read state changes."""
    fields = decode_proto(payload)
    if len(fields) > MAX_ACK_IDS:
        raise ProtocolError("Too many notice acknowledgement IDs")
    if any(
        field.number != 1 or field.wire_type != 0 or not 0 < field.value < 1 << 32
        for field in fields
    ):
        raise ProtocolError("Notice acknowledgement requires positive uint32 field 1")
    return list(dict.fromkeys(int(field.value) for field in fields))
