"""Bounded JSONL storage for a single server process. Caller serializes access."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

DEFAULT_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3


class AuditLog:
    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
    ):
        if type(max_bytes) is not int or max_bytes < 1024:
            raise ValueError("audit log size must be at least 1024 bytes")
        if type(backup_count) is not int or not 1 <= backup_count <= 10:
            raise ValueError("audit backup count must be between 1 and 10")
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def append(self, record: dict) -> None:
        line = self._encode(record)
        if len(line) > self.max_bytes:
            line = self._encode(
                {
                    "schema_version": 1,
                    "event": "audit_record_omitted",
                    "sequence": record["sequence"],
                    "timestamp_utc": record["timestamp_utc"],
                    "elapsed_ms": record["elapsed_ms"],
                    "original_bytes": len(line),
                }
            )
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise OSError("audit destination is not a regular file")
            size = info.st_size
        except FileNotFoundError:
            size = 0
        if size and size + len(line) > self.max_bytes:
            self._rotate()
        # Reopen each append so external rename-based rotation is also respected.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        start = os.fstat(fd).st_size
        try:
            remaining = memoryview(line)
            while remaining:
                written = os.write(fd, remaining)
                if written == 0:
                    raise OSError("audit append made no progress")
                remaining = remaining[written:]
        except OSError:
            # A disk-full short write must not poison the following JSONL record.
            # This single-writer file may have been renamed, so repair this fd.
            os.ftruncate(fd, start)
            raise
        finally:
            os.close(fd)

    @staticmethod
    def _encode(record: dict) -> bytes:
        return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()

    def _rotate(self) -> None:
        for index in range(self.backup_count, 0, -1):
            source = (
                self.path if index == 1 else self.path.with_name(f"{self.path.name}.{index - 1}")
            )
            destination = self.path.with_name(f"{self.path.name}.{index}")
            try:
                source.replace(destination)
            except FileNotFoundError:
                continue
