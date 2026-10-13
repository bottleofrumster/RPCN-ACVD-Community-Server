"""Publication checks use synthetic source and never inspect a live host."""

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.check_public_source import FIXTURE_PROVENANCE, check, check_contents, main
from scripts.export_review import PUBLIC_HEX_FIXTURES, REQUIRED_FILES, export, selected_files


def test_export_keeps_user_docs_and_client_patch_but_excludes_research(tmp_path):
    included = {
        "Launch Server UI.cmd", "Launch Server UI.sh", "Launch Server UI.desktop",
        "PENDING.txt", "CHANGES.txt", "client/README.txt",
        "client/rpcs3/README.txt", "client/rpcs3/signaling-context-ownership.patch",
    }
    excluded = {"tools/probe.java", "tools/probe.gdb", "docs/agent-notes.md"}
    for name in included | excluded:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Synthetic fixture")
    selected = selected_files(tmp_path, sorted(included | excluded))
    assert {path.relative_to(tmp_path).as_posix() for path in selected} == included


def candidate():
    contents = {name: b"example" for name in REQUIRED_FILES}
    entries = {}
    for name in PUBLIC_HEX_FIXTURES:
        contents[name] = b"00"
        entries[name.rsplit("/", 1)[1]] = {
            "sha256": hashlib.sha256(contents[name]).hexdigest(),
            "provenance": "Synthetic fixture for this publication-check test.",
        }
    contents[FIXTURE_PROVENANCE] = json.dumps({"schema_version": 1, "files": entries}).encode()
    contents["LICENSE"] = b"Synthetic test license marker; never an actual project license."
    return contents


def write_candidate(root, contents):
    for name, payload in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def test_complete_source_with_provenance_passes_without_claiming_approval():
    report = check_contents(candidate())
    assert report["status"] == "source_checks_passed" and report["issues"] == []
    assert "do not grant publication approval" in report["limits"]


def test_plain_text_license_is_included_and_recognized(tmp_path):
    contents = candidate()
    contents["LICENSE.txt"] = contents.pop("LICENSE")
    write_candidate(tmp_path, contents)
    assert check(tmp_path)["issues"] == []
    assert tmp_path / "LICENSE.txt" in selected_files(tmp_path, list(contents))


@pytest.mark.parametrize("fault", ["missing", "wrong_digest", "empty_basis", "invalid_json"])
def test_fixture_review_is_required_and_bound_to_exact_bytes(fault):
    contents = candidate()
    name = sorted(PUBLIC_HEX_FIXTURES)[0]
    if fault == "missing":
        contents.pop(FIXTURE_PROVENANCE)
    elif fault == "wrong_digest":
        contents[name] = b"01"
    elif fault == "empty_basis":
        entries = json.loads(contents[FIXTURE_PROVENANCE])
        entries["files"][name.rsplit("/", 1)[1]]["provenance"] = ""
        contents[FIXTURE_PROVENANCE] = json.dumps(entries).encode()
    else:
        contents[FIXTURE_PROVENANCE] = b"{"
    report = check_contents(contents)
    assert {"code": "fixture_provenance_unresolved", "path": name} in report["issues"]


def test_missing_license_and_core_native_file_block_readiness():
    contents = candidate()
    contents.pop("LICENSE")
    contents.pop("native/aes_modes/cwc.c")
    assert check_contents(contents)["issues"] == [
        {"code": "missing_required_file", "path": "native/aes_modes/cwc.c"},
        {"code": "license_not_selected", "path": "LICENSE"},
    ]


def test_findings_expose_filenames_never_matched_private_values():
    contents = candidate()
    contents["src/example.py"] = ("/" + "home/PrivatePerson/project/").encode()
    contents["src/token.py"] = ("ghp_" + "A" * 36).encode()
    contents["src/name.py"] = b"PrivateIdentity"
    report = check_contents(contents, ["PrivateIdentity"])
    assert {r["code"] for r in report["issues"]} == {"host_path", "private_material"}
    output = json.dumps(report)
    assert "PrivatePerson" not in output and "PrivateIdentity" not in output
    assert "A" * 36 not in output


def test_extracted_archive_checks_manifest_and_detects_modified_source(tmp_path):
    root = tmp_path / "source"
    write_candidate(root, candidate())
    archive = tmp_path / "public.zip"
    export(root, archive, [])
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(tmp_path / "extracted")
    extracted = tmp_path / "extracted/ACVD-source"
    assert check(extracted)["status"] == "source_checks_passed"
    (extracted / "src/acvd_server/server.py").write_text("changed")
    assert {"code": "manifest_mismatch", "path": "src/acvd_server/server.py"} in check(extracted)["issues"]


def test_cli_returns_blocked_for_unlicensed_source(tmp_path, capsys):
    contents = candidate()
    contents.pop("LICENSE")
    write_candidate(tmp_path, contents)
    assert main(["--root", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"


def test_cli_errors_do_not_echo_private_os_paths(tmp_path, capsys):
    assert main(["--root", str(tmp_path / "PrivatePerson")]) == 2
    assert "PrivatePerson" not in capsys.readouterr().out


@pytest.fixture
def artifact(tmp_path):
    root = tmp_path / "source"
    write_candidate(root, candidate())
    archive = tmp_path / "public.zip"
    export(root, archive, [])
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(tmp_path / "extracted")
    return tmp_path / "extracted/ACVD-source"


def test_pristine_artifact_verification_accepts_exact_inventory(artifact):
    assert check(artifact, verify_artifact=True)["status"] == "source_checks_passed"


def test_artifact_verification_rejects_unlisted_private_file_without_reading_it(
    artifact, monkeypatch
):
    private = artifact / "keys/private.pem"
    private.parent.mkdir()
    private.write_text("PrivateValueMustNotAppear")
    original = type(private).read_bytes

    def no_private_read(path):
        if path == private:
            pytest.fail("Unexpected private files must never be read")
        return original(path)

    monkeypatch.setattr(type(private), "read_bytes", no_private_read)
    report = check(artifact, verify_artifact=True)
    assert {"code": "unexpected_artifact_file", "path": "keys/private.pem"} in report["issues"]
    assert report["status"] == "blocked" and report["files_checked"] == 0
    assert "PrivateValueMustNotAppear" not in json.dumps(report)


@pytest.mark.parametrize("directory", [False, True])
def test_artifact_verification_rejects_extra_symlinks(artifact, tmp_path, directory):
    outside = tmp_path / "outside"
    if directory:
        outside.mkdir()
        (outside / "private.txt").write_text("PrivateValueMustNotAppear")
    else:
        outside.write_text("PrivateValueMustNotAppear")
    (artifact / "extra").symlink_to(outside, target_is_directory=directory)
    report = check(artifact, verify_artifact=True)
    assert {"code": "artifact_symlink", "path": "extra"} in report["issues"]
    assert "PrivateValueMustNotAppear" not in json.dumps(report)
    assert not any(row["path"].startswith("extra/") for row in report["issues"])


def test_artifact_mode_is_explicit_before_generated_build_files(artifact):
    (artifact / "native/libacvd_cwc.so").write_bytes(b"synthetic build output")
    assert check(artifact)["status"] == "source_checks_passed"
    report = check(artifact, verify_artifact=True)
    assert {"code": "unexpected_artifact_file", "path": "native/libacvd_cwc.so"} in report["issues"]


def test_artifact_verification_requires_manifest(tmp_path):
    write_candidate(tmp_path, candidate())
    with pytest.raises(ValueError, match="review manifest"):
        check(tmp_path, verify_artifact=True)


def test_artifact_own_cli_does_not_create_import_caches(tmp_path):
    contents = candidate()
    project = Path(__file__).resolve().parents[1]
    for name in ("scripts/check_public_source.py", "scripts/export_review.py"):
        contents[name] = (project / name).read_bytes()
    root = tmp_path / "source"
    write_candidate(root, contents)
    archive = tmp_path / "candidate.zip"
    export(root, archive, [])
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(tmp_path / "extracted")
    extracted = tmp_path / "extracted/ACVD-source"
    environment = dict(os.environ)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    result = subprocess.run(
        [sys.executable, "scripts/check_public_source.py", "--verify-artifact"],
        cwd=extracted, env=environment, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "source_checks_passed"
    assert not list(extracted.rglob("__pycache__"))
