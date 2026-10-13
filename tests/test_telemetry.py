"""Audit and operator diagnostics intentionally have different wire-data policies."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from acvd_server.audit_log import AuditLog
from acvd_server.telemetry import Telemetry


def records(telemetry):
    return [json.loads(line) for line in telemetry.path.read_text().splitlines()]


def test_audit_normalizes_nested_values_without_changing_input(tmp_path):
    @dataclass
    class Detail:
        path: Path
        payload: bytes

    telemetry = Telemetry(tmp_path / "nested/events.jsonl", capture_payloads=True)
    payload = bytes(range(256)) + b"tail"
    data = {7: (Detail(Path("example"), payload), None, True)}
    telemetry.emit("sample", data=data)
    (row,) = records(telemetry)
    assert row["schema_version"] == 1
    assert row["sequence"] == 1 and row["elapsed_ms"] >= 0
    assert row["timestamp_utc"].endswith("+00:00")
    assert row["data"] == {
        "7": [
            {"path": "example", "payload": {"length": 260, "hex_prefix": payload[:256].hex()}},
            None,
            True,
        ]
    }
    assert data[7][0].payload == payload


def test_audit_and_diagnostics_keep_distinct_payload_contracts(tmp_path):
    telemetry = Telemetry(tmp_path / "events.jsonl", capture_payloads=True)
    telemetry.set_diagnostics(True)
    telemetry.emit("sample", payload=b"a" * 70000, items=list(range(510)))
    (audit,) = records(telemetry)
    (diagnostic,) = telemetry.read_diagnostics(0)["records"]
    assert audit["payload"] == {"length": 70000, "hex_prefix": (b"a" * 256).hex()}
    assert len(audit["items"]) == 510
    assert diagnostic["payload"] == {
        "length": 70000,
        "hex": (b"a" * 65536).hex(),
        "truncated": True,
    }
    assert len(diagnostic["items"]) == 500


def test_auth_material_is_redacted_even_with_explicit_capture(tmp_path):
    telemetry = Telemetry(tmp_path / "events.jsonl", capture_payloads=True)
    telemetry.set_diagnostics(True)
    telemetry.emit("message_received", service="auth", payload=b"credential", ticket="secret")
    (diagnostic,) = telemetry.read_diagnostics(0)["records"]
    (audit,) = records(telemetry)
    assert "redacted" in diagnostic["payload"]
    assert diagnostic["ticket"] == "[credential redacted]"
    assert "redacted" in audit["payload"]
    assert audit["ticket"] == "[credential redacted]"


def test_disabling_diagnostics_clears_ring_but_not_audit(tmp_path):
    telemetry = Telemetry(tmp_path / "events.jsonl")
    telemetry.set_diagnostics(True)
    telemetry.emit("first")
    telemetry.set_diagnostics(False)
    telemetry.emit("second")
    assert telemetry.read_diagnostics(0)["records"] == []
    telemetry.set_diagnostics(True)
    telemetry.emit("third")
    (row,) = telemetry.read_diagnostics(0)["records"]
    assert row["cursor"] == 2 and row["sequence"] == 3
    assert [r["event"] for r in records(telemetry)] == ["first", "second", "third"]


def test_concurrent_emit_keeps_complete_lines_in_sequence_order(tmp_path):
    telemetry = Telemetry(tmp_path / "events.jsonl")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: telemetry.emit("sample", value=i), range(64)))
    rows = records(telemetry)
    assert [r["sequence"] for r in rows] == list(range(1, 65))
    assert sorted(r["value"] for r in rows) == list(range(64))


def test_append_reopens_path_after_external_rotation(tmp_path):
    telemetry = Telemetry(tmp_path / "events.jsonl")
    telemetry.emit("first")
    rotated = tmp_path / "events.old.jsonl"
    telemetry.path.rename(rotated)
    telemetry.emit("second")
    assert json.loads(rotated.read_text())["event"] == "first"
    (row,) = records(telemetry)
    assert row["event"] == "second" and row["sequence"] == 2


def test_write_failure_is_observable_without_breaking_requests(tmp_path, monkeypatch, caplog):
    telemetry = Telemetry(tmp_path / "events.jsonl")
    telemetry.set_diagnostics(True)

    def fail_open(*args, **kwargs):
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(telemetry._audit, "append", fail_open)
        telemetry.emit("failed_write")
        telemetry.emit("second_failed_write")
    assert telemetry.sequence == 2 and telemetry.failed_writes == 2
    assert len(caplog.records) == 1
    assert telemetry.read_diagnostics(0)["records"][0]["event"] == "failed_write"
    telemetry.emit("next")
    (row,) = records(telemetry)
    assert row["sequence"] == 3


def test_caller_metadata_precedence_is_preserved(tmp_path):
    telemetry = Telemetry(tmp_path / "events.jsonl")
    telemetry.emit("sample", sequence=42, schema_version=3, elapsed_ms=9)
    (row,) = records(telemetry)
    assert (row["sequence"], row["schema_version"], row["elapsed_ms"]) == (42, 3, 9)
    assert telemetry.sequence == 1


def test_default_audit_keeps_byte_lengths_not_payloads(tmp_path):
    telemetry = Telemetry(tmp_path / "events.jsonl")
    telemetry.emit("sample", nested={"payload": b"private contents", "password": "secret"})
    (row,) = records(telemetry)
    assert row["nested"] == {
        "payload": {"length": 16},
        "password": "[credential redacted]",
    }


def test_rotation_bounds_retention_and_keeps_complete_json_records(tmp_path):
    telemetry = Telemetry(tmp_path / "events.jsonl", max_bytes=1024, backup_count=2)
    for i in range(50):
        telemetry.emit("sample", value=i, text="x" * 100)
    files = list(tmp_path.glob("events.jsonl*"))
    assert len(files) == 3
    assert all(path.stat().st_size <= 1024 for path in files)
    assert all(path.stat().st_mode & 0o077 == 0 for path in files)
    retained = [json.loads(line) for path in files for line in path.read_text().splitlines()]
    assert max(row["sequence"] for row in retained) == 50
    assert len({row["sequence"] for row in retained}) == len(retained)


def test_oversized_audit_event_is_explicitly_omitted(tmp_path):
    telemetry = Telemetry(tmp_path / "events.jsonl", max_bytes=1024)
    telemetry.emit("large", text="x" * 2000)
    (row,) = records(telemetry)
    assert row["event"] == "audit_record_omitted" and row["original_bytes"] > 2000
    assert telemetry.path.stat().st_size <= 1024


@pytest.mark.parametrize(
    "options",
    [
        {"max_bytes": 0},
        {"max_bytes": True},
        {"backup_count": 0},
        {"backup_count": 11},
    ],
)
def test_invalid_audit_limits_are_rejected(tmp_path, options):
    with pytest.raises(ValueError):
        Telemetry(tmp_path / "events.jsonl", **options)


def test_partial_write_is_removed_before_next_append(tmp_path, monkeypatch):
    log = AuditLog(tmp_path / "events.jsonl")
    record = {"sequence": 1, "event": "first"}
    log.append(record)
    before = log.path.read_bytes()
    real_write = os.write
    calls = 0

    def partial_then_fail(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[:4])
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", partial_then_fail)
        with pytest.raises(OSError, match="disk full"):
            log.append({"sequence": 2, "event": "lost"})
    assert log.path.read_bytes() == before
    log.append({"sequence": 3, "event": "last"})
    assert [json.loads(line)["sequence"] for line in log.path.read_text().splitlines()] == [1, 3]


@pytest.mark.parametrize("size", [10, 2048])
def test_audit_rejects_symlink_destination_without_writing_target(tmp_path, size):
    target = tmp_path / "preserved"
    target.write_text("x" * size)
    link = tmp_path / "events.jsonl"
    link.symlink_to(target)
    telemetry = Telemetry(link, max_bytes=1024)
    telemetry.emit("sample")
    assert telemetry.failed_writes == 1
    assert target.read_text() == "x" * size
    assert link.is_symlink() and not (tmp_path / "events.jsonl.1").exists()


def test_dataclass_diagnostics_use_recursive_secret_redaction(tmp_path):
    @dataclass
    class Detail:
        password: str
        count: int

    telemetry = Telemetry(tmp_path / "events.jsonl")
    telemetry.set_diagnostics(True)
    telemetry.emit("sample", details=Detail("secret", 3))
    row = telemetry.read_diagnostics(0)["records"][0]
    assert row["details"] == {"password": "[credential redacted]", "count": 3}
    assert telemetry.audit_status()["failed_writes"] == 0
