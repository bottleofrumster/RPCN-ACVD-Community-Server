"""Write structured evidence events; keep raw captures out of source releases."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from .audit_log import DEFAULT_BACKUP_COUNT, DEFAULT_MAX_BYTES, AuditLog
from .diagnostic_records import redact_record
from .diagnostics import DiagnosticBuffer, diagnostic_value

AUDIT_BYTE_PREFIX_LIMIT = 256
LOGGER = logging.getLogger(__name__)


class Telemetry:
    """Serialize audit writes and opt-in diagnostic updates under one lock.

    Audit events remain synchronous. Reopening the path for each append also
    permits external log rotation without retaining a stale file descriptor.
    """

    def __init__(
        self,
        path: Path,
        *,
        capture_payloads: bool = False,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
    ):
        self.path = path
        self.capture_payloads = capture_payloads
        self._audit = AuditLog(path, max_bytes=max_bytes, backup_count=backup_count)
        self.started_ns = time.monotonic_ns()
        self.sequence = 0
        self.lock = Lock()
        self.diagnostics = DiagnosticBuffer()
        self.failed_writes = 0
        self._last_failure_warning_ns: int | None = None

    def set_diagnostics(self, enabled: bool) -> None:
        with self.lock:
            self.diagnostics.set_enabled(enabled)

    def read_diagnostics(self, after: int) -> dict:
        with self.lock:
            return self.diagnostics.read(after)

    def audit_status(self) -> dict:
        with self.lock:
            return {
                "failed_writes": self.failed_writes,
                "capture_payloads": self.capture_payloads,
                "max_bytes": self._audit.max_bytes,
                "backup_count": self._audit.backup_count,
            }

    def emit(self, event: str, **data: Any) -> None:
        with self.lock:
            self.sequence += 1
            record = self._audit_record(event, data)
            if self.diagnostics.enabled:
                self.diagnostics.append(
                    {**record, **{key: diagnostic_value(value) for key, value in data.items()}}
                )
            try:
                self._append_record(record)
            except OSError as error:
                # Logging must not turn a committed game transaction into a failed request.
                # Keep an observable counter and a throttled stderr warning, never recurse.
                self.failed_writes += 1
                now = time.monotonic_ns()
                if (
                    self._last_failure_warning_ns is None
                    or now - self._last_failure_warning_ns >= 60_000_000_000
                ):
                    LOGGER.error(
                        "Audit write failed (%s); failed writes=%d",
                        type(error).__name__,
                        self.failed_writes,
                    )
                    self._last_failure_warning_ns = now

    def _audit_record(self, event: str, data: dict[str, Any]) -> dict[str, Any]:
        return redact_record(
            {
                "schema_version": 1,
                "sequence": self.sequence,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": (time.monotonic_ns() - self.started_ns) // 1_000_000,
                "event": event,
                **{
                    key: self._safe(value, capture_payloads=self.capture_payloads)
                    for key, value in data.items()
                },
            }
        )

    def _append_record(self, record: dict[str, Any]) -> None:
        self._audit.append(record)

    @classmethod
    def _safe(cls, value: Any, *, capture_payloads: bool = False) -> Any:
        """Audit bytes are lengths only unless payload-prefix capture is explicit."""
        if isinstance(value, bytes):
            summary = {"length": len(value)}
            if capture_payloads:
                summary["hex_prefix"] = value[:AUDIT_BYTE_PREFIX_LIMIT].hex()
            return summary
        if is_dataclass(value):
            return cls._safe(asdict(value), capture_payloads=capture_payloads)
        if isinstance(value, dict):
            return {
                str(key): cls._safe(item, capture_payloads=capture_payloads)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls._safe(item, capture_payloads=capture_payloads) for item in value]
        if isinstance(value, Path):
            return str(value)
        return value
