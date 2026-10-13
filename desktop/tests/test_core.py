"""Pure desktop tests: no window, service control or native clients."""

import json
import re
import struct
import subprocess

import pytest
from acvd_console.core import ServiceControl, TailReader, bounded_lines
from acvd_console.language import EVIDENCE, MEANINGS, description_text
from acvd_console.pcap import read_pcap
from acvd_console.traffic import Catalog, decode_record, protobuf_fields, safe_record


def test_tail_partial_rotation_and_truncation(tmp_path):
    path = tmp_path / "events.log"
    path.write_bytes(b"one\npar")
    reader = TailReader(path)
    assert reader.poll() == ["one"]
    with path.open("ab") as out:
        out.write(b"tial\n")
    assert reader.poll() == ["partial"]
    path.write_bytes(b"two\n")
    assert reader.poll() == ["two"]
    path.rename(tmp_path / "old.log")
    path.write_bytes(b"new\n")
    assert reader.poll() == ["new"]
    assert reader.poll() == []


def test_overload_is_counted_not_silently_hidden():
    lines, skipped = bounded_lines(list(range(600)))
    assert lines == list(range(100, 600)) and skipped == 100


def test_live_client_tail_starts_after_existing_records(tmp_path):
    path = tmp_path / "client.log"
    path.write_text("old event\n")
    reader = TailReader(path, 0)
    assert reader.poll() == []
    with path.open("a") as stream:
        stream.write("new event\n")
    assert reader.poll() == ["new event"]


def test_owned_new_observer_file_keeps_first_record(tmp_path):
    path = tmp_path / "observer.jsonl"
    reader = TailReader(path, None)
    assert reader.poll() == []
    path.write_text("first event\n")
    assert reader.poll() == ["first event"]


def test_description_sentence_lengths_are_bounded():
    # This check does not replace the STE dictionary and meaning review.
    for text in [*MEANINGS.values(), *EVIDENCE.values()]:
        assert all(
            len(sentence.split()) <= 25 for sentence in re.split(r"[.!?]\s*", text) if sentence
        )


def test_source_identifiers_remain_in_description():
    text = description_text(
        {"name": "UNKNOWN", "code": 0x7FFE, "layer": "application_service", "confidence": "unknown"}
    )
    assert "UNKNOWN" in text and "0x7ffe" in text and "not known" in text


def test_service_attach_never_starts_duplicate(tmp_path):
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, f"LoadState=loaded\nActiveState=active\nWorkingDirectory={tmp_path}\n", ""
        )

    service = ServiceControl(runner)
    assert "Already running" in service.act("start", tmp_path)
    assert len(calls) == 1 and "show" in calls[0]
    with pytest.raises(ValueError):
        service.act("stop", tmp_path)
    with pytest.raises(ValueError):
        service.act("restart", tmp_path / "other", True)
    with pytest.raises(ValueError):
        service.act("erase", tmp_path, True)


def test_unknown_and_credentials(tmp_path):
    catalog = Catalog(tmp_path)
    result = catalog.describe({"event": "game_message_received", "message_type": 0xFFFF})
    assert result["confidence"] == "unknown"
    record = safe_record(
        {
            "event": "message_sent",
            "service": "auth",
            "plaintext_payload": "secret",
            "game_key": "secret",
        }
    )
    assert "secret" not in json.dumps(record)
    fields = protobuf_fields(bytes.fromhex("08011203616263"))["fields"]
    assert fields[0]["value"] == 1 and fields[1]["value"]["bytes"] == 3
    with pytest.raises(ValueError):
        protobuf_fields(bytes.fromhex("120361"))


def test_non_string_import_event_does_not_break_feed(tmp_path):
    assert decode_record({"event": 5}, Catalog(tmp_path))["direction"] == "• Observe"


def test_catalog_matches_cli_metadata_without_importing_selected_root(tmp_path):
    from pathlib import Path

    from tools.analysis.protocol_catalog import load_catalog

    root = Path(__file__).resolve().parents[2]
    expected = load_catalog()
    catalog = Catalog(root)
    assert catalog.entries == {
        (entry["layer"], entry["value"]): entry for entry in expected["entries"]
    }
    (tmp_path / "src/acvd_server").mkdir(parents=True)
    (tmp_path / "tools").mkdir()
    marker = tmp_path / "must-not-exist"
    (tmp_path / "src/acvd_server/udp.py").write_text(
        f"open({str(marker)!r}, 'w').write('bad')\nSYN = 2\n"
    )
    (tmp_path / "tools/protocol_catalog.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "overrides": {},
                "extra_entries": [],
                "rejected_interpretations": [],
            }
        )
    )
    selected = Catalog(tmp_path)
    assert selected.entries[("reliable_udp", 2)]["name"] == "SYN"
    assert set(selected.entries) == {("reliable_udp", 2)}
    assert not marker.exists()


def test_catalog_bundle_is_only_a_fallback(tmp_path, monkeypatch):
    from acvd_console import traffic

    resources = tmp_path / "bundle"
    resources.mkdir()
    bundled = {
        "id": "application_service:0x1234",
        "layer": "application_service",
        "code": "0x1234",
        "name": "OLD",
        "confidence": "unknown",
    }
    (resources / "protocol-catalog.json").write_text(json.dumps({"entries": [bundled]}))
    monkeypatch.setattr(traffic, "RESOURCES", resources)
    root = tmp_path / "selected"
    assert Catalog(root).entries == {("application_service", 0x1234): bundled}
    (root / "src/acvd_server").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "src/acvd_server/udp.py").write_text("SYN = 2")
    document = {
        "schema_version": 1,
        "overrides": {"reliable_udp:0x02": {"summary": "Current."}},
        "extra_entries": [],
        "rejected_interpretations": [],
    }
    (root / "tools/protocol_catalog.json").write_text(json.dumps(document))
    current = Catalog(root)
    assert set(current.entries) == {("reliable_udp", 2)}
    assert current.entries[("reliable_udp", 2)]["summary"] == "Current."


def test_packaged_catalog_has_no_framing_length_entry(tmp_path):
    assert all(
        entry["name"] != "INITIAL_PREFIX_SIZE" for entry in Catalog(tmp_path).entries.values()
    )


def test_shared_redaction_and_non_string_client_log(tmp_path):
    from acvd_server.diagnostics import redact

    record = {"event": "client_log", "line": "Password: sensitive", "key": "sensitive"}
    assert "sensitive" not in str(redact(record))
    assert "sensitive" not in str(safe_record(record))
    result = decode_record({"event": "client_log", "line": 4}, Catalog(tmp_path))
    assert result["record"]["line"] == 4


def test_pcap_sll2_and_truncation(tmp_path):
    path = tmp_path / "sample.pcap"
    header = b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 276)
    ip = bytearray(20)
    ip[0] = 0x45
    ip[9] = 17
    ip[12:16] = bytes([127, 0, 0, 1])
    ip[16:20] = bytes([127, 0, 0, 2])
    payload = b"NXRV-test"
    packet = bytes(20) + ip + struct.pack(">HHHH", 7004, 7004, 8 + len(payload), 0) + payload
    path.write_bytes(header + struct.pack("<4I", 100, 0, len(packet), len(packet)) + packet)
    rows, summary = read_pcap(path)
    assert rows[0]["nxrv_signature"] and summary["link_type"] == 276
    assert rows[0]["payload"]["hex"] == payload.hex()
    path.write_bytes(header + b"short")
    with pytest.raises(ValueError):
        read_pcap(path)
