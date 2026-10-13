"""Offline analysis commands retain legacy imports, paths and output contracts."""

import ast
import importlib
import importlib.util
import json
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "old,new",
    [
        ("acvd_protocol_catalog", "protocol_catalog"),
        ("acvd_peer_message_summary", "peer_message_summary"),
    ],
)
def test_normal_imports_have_one_canonical_module(old, new):
    legacy = importlib.import_module(f"tools.{old}")
    canonical = importlib.import_module(f"tools.analysis.{new}")
    assert legacy is canonical


@pytest.mark.parametrize(
    "old,new,member",
    [
        ("acvd_protocol_catalog", "protocol_catalog", "load_catalog"),
        ("acvd_peer_message_summary", "peer_message_summary", "summarize"),
    ],
)
def test_retained_spec_module_can_read_canonical_exports(old, new, member, monkeypatch):
    name = f"analysis_spec_test_{new}"
    # Restore the sys.modules slot after checking this historical import pattern.
    monkeypatch.setitem(sys.modules, name, None)
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{old}.py")
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    canonical = importlib.import_module(f"tools.analysis.{new}")
    assert getattr(wrapper, member) is getattr(canonical, member)
    assert member in dir(wrapper)


def invoke(path, args, cwd):
    return subprocess.run(
        [sys.executable, str(path), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize(
    "args",
    [
        ["--validate"],
        ["--json"],
        ["--code", "0x03d4", "--json"],
        ["--rejected", "--code", "0xf222", "--json"],
        ["--search", "no-such-protocol-entry"],
    ],
)
def test_catalog_cli_matches_from_unrelated_working_directory(tmp_path, args):
    old = invoke(ROOT / "tools/acvd_protocol_catalog.py", args, tmp_path)
    new = invoke(ROOT / "tools/analysis/protocol_catalog.py", args, tmp_path)
    assert old.returncode == new.returncode == 0
    assert old.stdout == new.stdout
    assert old.stderr == new.stderr == ""


def test_catalog_uses_original_authoritative_paths():
    from tools.analysis import protocol_catalog

    assert protocol_catalog.PROJECT_ROOT == ROOT
    assert protocol_catalog.UDP_SOURCE == ROOT / "src/acvd_server/udp.py"
    assert protocol_catalog.CATALOG_SOURCE == ROOT / "tools/protocol_catalog.json"


@pytest.mark.parametrize("compressed", [False, True])
def test_peer_summary_cli_matches_and_never_changes_input(tmp_path, compressed):
    body = bytes(range(256)) * 8
    encoded = len(body).to_bytes(4, "big") + zlib.compress(body, 9)[:-4] if compressed else body
    kind = 0x19D | (0x8000 if compressed else 0)
    raw = (6006).to_bytes(2, "little") + bytes(11) + kind.to_bytes(4, "big")
    raw += (len(encoded) + 8).to_bytes(4, "big") + encoded
    path = tmp_path / "observed.jsonl"
    path.write_text(
        json.dumps(
            {
                "unix_seconds": 100,
                "nanoseconds": 250000000,
                "result": len(raw),
                "length": len(raw),
                "hex": raw.hex(),
            }
        )
        + "\n"
    )
    before = path.read_bytes()
    old = invoke(ROOT / "tools/acvd_peer_message_summary.py", [str(path)], tmp_path)
    new = invoke(ROOT / "tools/analysis/peer_message_summary.py", [str(path)], tmp_path)
    assert old.returncode == new.returncode == 0
    assert old.stdout == new.stdout
    assert old.stderr == new.stderr == ""
    assert json.loads(new.stdout)[0]["groups"][0]["body_lengths"] == [len(body)]
    assert path.read_bytes() == before


@pytest.mark.parametrize("name", ["protocol_catalog", "peer_message_summary"])
def test_analysis_modules_have_no_live_control_or_runtime_state_imports(name):
    tree = ast.parse((ROOT / "tools/analysis" / f"{name}.py").read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    assert all(
        name.split(".")[0] in sys.stdlib_module_names or name == "acvd_server.diagnostic_records"
        for name in imports
    )
    assert not {"socket", "subprocess", "ctypes"}.intersection(imports)


def test_shared_diagnostic_contract_has_only_pure_dependencies():
    path = ROOT / "src/acvd_server/diagnostic_records.py"
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert {alias.name for alias in node.names} <= {"ast", "re"}
        elif isinstance(node, ast.ImportFrom):
            assert node.module in {"__future__", "typing"}
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "exec", "eval", "__import__"}
