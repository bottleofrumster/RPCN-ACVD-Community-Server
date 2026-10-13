"""Bounded, opt-in operator diagnostics. Never persist full wire payloads here."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, is_dataclass
from typing import Any

from .diagnostic_records import SECRET_KEYS as SECRET_KEYS, redact_record

BUFFER_BYTE_LIMIT = 8 * 1024 * 1024
BUFFER_RECORD_LIMIT = 2000
PAGE_BYTE_LIMIT = 512 * 1024
PAGE_RECORD_LIMIT = 100
PAYLOAD_BYTE_LIMIT = 65536
COLLECTION_ITEM_LIMIT = 500


def redact(record: dict[str, Any]) -> dict[str, Any]:
    """Sanitize the live view with the shared disclosure policy."""
    return redact_record(record)


class DiagnosticBuffer:
    """A sequence-addressable 8 MiB ring; the regular audit log stays unchanged."""

    def __init__(self, max_bytes: int = BUFFER_BYTE_LIMIT):
        self.enabled = False
        self.sequence = 0
        self.max_bytes = max_bytes
        self.bytes_used = 0
        self.records: deque[tuple[int, dict, int]] = deque()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if not enabled:
            self.records.clear()
            self.bytes_used = 0

    def append(self, record: dict) -> None:
        if not self.enabled:
            return
        self.sequence += 1
        record = redact(record)
        size = len(json.dumps(record).encode())
        if size > self.max_bytes:
            record = {"event": "diagnostic_record_omitted", "original_bytes": size}
            size = len(json.dumps(record).encode())
        self.records.append((self.sequence, record, size))
        self.bytes_used += size
        while self.bytes_used > self.max_bytes or len(self.records) > BUFFER_RECORD_LIMIT:
            _, _, removed = self.records.popleft()
            self.bytes_used -= removed

    def read(self, after: int) -> dict:
        rows, used, cursor = [], 0, after
        for sequence, record, size in self.records:
            if sequence <= after:
                continue
            if rows and (used + size > PAGE_BYTE_LIMIT or len(rows) >= PAGE_RECORD_LIMIT):
                break
            rows.append({"cursor": sequence, **record})
            used += size
            cursor = sequence
        first = self.records[0][0] if self.records else self.sequence + 1
        return {
            "records": rows,
            "cursor": cursor,
            "oldest": first,
            "gap": after > 0 and after < first - 1,
            "enabled": self.enabled,
        }


def diagnostic_value(value: Any) -> Any:
    """Bound all wire data; report truncation rather than implying a full capture."""
    if isinstance(value, bytes):
        data = value[:PAYLOAD_BYTE_LIMIT]
        return {"length": len(value), "hex": data.hex(), "truncated": len(data) < len(value)}
    if is_dataclass(value):
        return diagnostic_value(asdict(value))
    if isinstance(value, dict):
        return {str(k): diagnostic_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [diagnostic_value(v) for v in value[:COLLECTION_ITEM_LIMIT]]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
