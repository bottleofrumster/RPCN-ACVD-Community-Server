"""The beginner entry point must not execute advanced probes or change live data."""

import argparse
import subprocess

import pytest

from tools import acvd_tools


def test_decode_explains_wire_fields_without_semantic_claim(capsys):
    assert acvd_tools.main(["decode", "--hex", "08 7b"]) == 0
    text = capsys.readouterr().out
    assert "Field 1; wire type 0; value 123" in text
    assert "does not decrypt" in text


def test_bad_hex_is_a_short_error_not_a_traceback(capsys):
    assert acvd_tools.main(["decode", "--hex", "not-hex"]) == 2
    text = capsys.readouterr().err
    assert "Cannot complete" in text and "Traceback" not in text


def test_noninteractive_empty_command_only_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(acvd_tools.sys.stdin, "isatty", lambda: False)
    assert acvd_tools.main([]) == 0
    assert "catalog" in capsys.readouterr().out


def test_advanced_index_parses_but_does_not_execute(tmp_path, monkeypatch, capsys):
    marker = tmp_path / "executed"
    tool = tmp_path / "probe.py"
    tool.write_text(f'"""A dangerous probe."""\nopen({str(marker)!r}, "w").write("bad")\n')
    monkeypatch.setattr(acvd_tools, "TOOLS", tmp_path)
    assert acvd_tools.main(["describe", "probe.py"]) == 0
    assert "Not executed" in capsys.readouterr().out
    assert not marker.exists()


def test_describe_rejects_outside_tool_and_symlink(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    secret = tmp_path / "private.py"
    secret.write_text('"""Not a tool."""')
    (tools / "link.py").symlink_to(secret)
    monkeypatch.setattr(acvd_tools, "TOOLS", tools)
    assert acvd_tools.main(["describe", "../private.py"]) == 2
    assert acvd_tools.main(["describe", "link.py"]) == 2


@pytest.mark.parametrize("destination", ["data/new.bin", "state/new.bin", "src/new.bin"])
def test_extraction_cannot_write_live_project_paths(tmp_path, monkeypatch, destination):
    source = tmp_path / "GPROG.DAT"
    source.write_bytes(bytes(32))
    monkeypatch.setattr(acvd_tools, "ROOT", tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Extractor must not start"))
    assert acvd_tools.main(["extract", "parts", str(source), str(tmp_path / destination)]) == 2


def test_extraction_refuses_existing_destination(tmp_path, monkeypatch):
    source, target = tmp_path / "input.dat", tmp_path / "output.bin"
    source.write_bytes(b"source")
    target.write_bytes(b"keep")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Extractor must not start"))
    assert acvd_tools.main(["extract", "ac", str(source), str(target)]) == 2
    assert target.read_bytes() == b"keep" and source.read_bytes() == b"source"


def test_extraction_uses_argument_list_without_force_or_apply(tmp_path, monkeypatch):
    source, target = tmp_path / "input.dat", tmp_path / "staged.bin"
    source.write_bytes(bytes(32))
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0, stdout='{"staged":true}\n', stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert acvd_tools.main(["extract", "parts", str(source), str(target)]) == 0
    command, options = calls[0]
    assert command[-2:] == [str(source), str(target)]
    assert "--force" not in command and "--apply" not in command
    assert not options.get("shell")


def test_packet_limit_is_bounded_before_file_access():
    assert acvd_tools.main(["pcap", "/missing/file.pcap", "--max-packets", "0"]) == 2


def test_browse_reports_limits_explicitly(tmp_path, monkeypatch, capsys):
    for i in range(30):
        (tmp_path / f"tool_{i}.py").write_text('"""Example."""')
    monkeypatch.setattr(acvd_tools, "TOOLS", tmp_path)
    acvd_tools.browse(argparse.Namespace(search="", all=False))
    assert "Shown: 25 of 30" in capsys.readouterr().out


def test_ghidra_scripts_are_discoverable_but_not_executed(tmp_path, monkeypatch, capsys):
    tools = tmp_path / "tools"
    tools.mkdir()
    scripts = tmp_path / "ghidra_scripts"
    scripts.mkdir()
    (scripts / "Inspect.java").write_text("Not executable Java in this fixture")
    monkeypatch.setattr(acvd_tools, "TOOLS", tools)
    assert acvd_tools.main(["describe", "ghidra_scripts/Inspect.java"]) == 0
    assert "Ghidra Script Manager" in capsys.readouterr().out
    assert acvd_tools.main(["describe", "ghidra_scripts/../tools/acvd_tools.py"]) == 2
