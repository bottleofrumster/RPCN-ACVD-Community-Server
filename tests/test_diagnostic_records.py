"""Shared view contracts. These tests use no services or private input files."""

import copy

import pytest

from acvd_server.diagnostic_records import build_catalog, parse_udp_constants, redact_record


def reviewed():
    return {
        "schema_version": 1,
        "overrides": {},
        "extra_entries": [],
        "rejected_interpretations": [],
    }


def test_parse_selected_code_as_data_without_execution(tmp_path):
    marker = tmp_path / "executed"
    source = f"open({str(marker)!r}, 'w').write('bad')\nSYN = 2\nGET_THING = 900\nFLAG = True"
    assert parse_udp_constants(source) == {"SYN": 2, "GET_THING": 900}
    assert not marker.exists()


def test_build_preserves_metadata_overrides_and_inputs():
    source = reviewed()
    source["overrides"]["reliable_udp:0x02"] = {
        "summary": "Reviewed meaning.",
        "evidence": ["test.md"],
    }
    before = copy.deepcopy(source)
    result = build_catalog({"SYN": 2}, source)["entries"][0]
    assert result["id"] == "reliable_udp:0x02"
    assert result["direction"] == "bidirectional"
    assert result["source"] == "src/acvd_server/udp.py"
    assert result["value"] == 2 and result["evidence"] == ["test.md"]
    assert source == before


def test_build_rejects_invalid_and_duplicate_entries():
    source = reviewed()
    source["overrides"]["reliable_udp:0x02"] = {"confidence": "invented"}
    with pytest.raises(ValueError, match="confidence"):
        build_catalog({"SYN": 2}, source)
    with pytest.raises(ValueError, match="duplicate"):
        build_catalog({"GET_A": 900, "GET_B": 900}, reviewed())


def test_framing_exclusion_is_named_not_a_numeric_range_rule():
    entries = build_catalog({"INITIAL_PREFIX_SIZE": 35, "GET_CUSTOM": 35, "SYN": 2}, reviewed())[
        "entries"
    ]
    assert {entry["name"] for entry in entries} == {"GET_CUSTOM", "SYN"}
    assert next(entry for entry in entries if entry["name"] == "GET_CUSTOM")["value"] == 35


@pytest.mark.parametrize("desktop", [False, True])
def test_redaction_is_recursive_and_does_not_mutate_input(desktop):
    source = {
        "event": "example",
        "items": [{"GAME_KEY": "sensitive"}],
        "line": "prefix Password = sensitive",
        "safe": 42,
    }
    before = copy.deepcopy(source)
    result = redact_record(source, desktop_markers=desktop)
    assert "sensitive" not in str(result)
    assert result["safe"] == 42 and source == before


@pytest.mark.parametrize("line", [4, None, False, [], {"safe": "text"}])
def test_non_string_lines_remain_valid_json_values(line):
    assert redact_record({"line": line})["line"] == line


@pytest.mark.parametrize("desktop", [False, True])
def test_auth_payload_and_token_frames_are_not_exposed(desktop):
    result = redact_record(
        {"service": "auth", "payload": "sensitive", "fields": "sensitive"}, desktop_markers=desktop
    )
    assert "sensitive" not in str(result)
    frame = redact_record(
        {"event": "game_datagram_received", "payload": "sensitive"}, desktop_markers=desktop
    )
    assert "sensitive" not in str(frame)
