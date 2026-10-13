"""Keep private runtime directories and linked inputs out of source exports."""

import json
import subprocess
import zipfile

import pytest

from scripts.export_review import (
    REQUIRED_FILES,
    export,
    private_matches,
    selected_files,
    source_names,
)


def test_export_selects_source_not_private_evidence_or_release_data(tmp_path):
    names = [
        "README.txt",
        "readme.txt",
        "src/code.py",
        "data/players/private.json",
        "keys/key.pem",
        "evidence/capture.json",
        "dist/client.zip",
        "tools/screen_templates/personal.png",
    ]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("placeholder")
    assert {str(p.relative_to(tmp_path)) for p in selected_files(tmp_path, names)} == {
        "README.txt",
        "src/code.py",
    }


def test_export_rejects_linked_parent_even_when_it_stays_inside_root(tmp_path):
    private = tmp_path / "data"
    private.mkdir()
    (private / "secret.json").write_text("private")
    (tmp_path / "tools").symlink_to(private, target_is_directory=True)
    with pytest.raises(ValueError, match="linked"):
        selected_files(tmp_path, ["tools/secret.json"])


@pytest.mark.parametrize("name", ["../private.py", "/private.py"])
def test_export_rejects_unsafe_source_paths(tmp_path, name):
    with pytest.raises(ValueError, match="unsafe"):
        selected_files(tmp_path, [name])


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-be", "utf-16-le"])
def test_privacy_scan_checks_plain_and_hex_encoded_identifiers(encoding):
    name = "ExamplePrivateName"
    encoded = name.encode(encoding)
    assert private_matches(encoded, [name])
    assert private_matches(encoded.hex().encode(), [name])
    assert not private_matches(b"ordinary source", [name])


def test_privacy_scan_rejects_actual_private_key_blocks():
    value = b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 80 + b"\n-----END PRIVATE KEY-----"
    assert private_matches(value, ["ExamplePrivateName"])


def test_public_policy_excludes_private_operations_and_admits_reviewed_fixture_names(tmp_path):
    included = {
        "README.txt", "LICENSE", "docs/setup.txt", "client/README.txt", ".github/workflows/tests.yml",
        "tests/fixtures/story-subquest-result.hex", "src/acvd_server/new_handler.py",
        "desktop/acvd_console/resources/FONT-LICENSE",
    }
    excluded = {
        "readme.txt", "docs/setup.md", "tools/ops/deploy.py",
        "tests/test_deploy_approved_gate.py", "tests/test_reviewed_sale_metadata_recovery.py",
        "tools/analysis/ultimate_live_copy_control.py", "tests/fixtures/unreviewed.hex",
    }
    for name in included | excluded:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("placeholder")
    assert {p.relative_to(tmp_path).as_posix() for p in selected_files(tmp_path, sorted(included | excluded))} == included


def test_broken_source_symlink_is_rejected(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/missing.py").symlink_to(tmp_path / "missing.py")
    with pytest.raises(ValueError, match="linked"):
        selected_files(tmp_path, ["src/missing.py"])


def test_source_discovery_includes_new_owned_code_without_untracked_research(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name in ("src/new.py", "tests/test_new.py", "tools/new_probe.gdb", "README.txt"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("placeholder")
    assert set(source_names(tmp_path)) == {"src/new.py", "tests/test_new.py", "README.txt"}
    subprocess.run(["git", "-C", str(tmp_path), "add", "tools/new_probe.gdb"], check=True)
    assert "tools/new_probe.gdb" in source_names(tmp_path)


def test_export_refuses_incomplete_source_before_creating_output(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "README.txt").write_text("Public guide")
    output = tmp_path / "public.zip"
    with pytest.raises(ValueError, match="Incomplete public source"):
        export(root, output, [])
    assert not output.exists()


def test_candidate_archive_has_exact_manifest_and_explicit_unresolved_license(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for name in REQUIRED_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("00" if name.endswith(".hex") else "example")
    (root / "src/new.py").write_text("new source")
    output = tmp_path / "public.zip"
    result = export(root, output, [])
    assert result["files"] == len(REQUIRED_FILES) + 1
    with zipfile.ZipFile(output) as archive:
        manifest = json.loads(archive.read("ACVD-source/review-manifest.json"))
        assert set(manifest["files"]) == REQUIRED_FILES | {"src/new.py"}
        assert manifest["source_checks"]["status"] == "blocked"
        assert {"code": "license_not_selected", "path": "LICENSE"} in manifest["source_checks"]["issues"]
        assert "tools/ops and their deployment/recovery tests" in manifest["excluded"]
        assert archive.getinfo("ACVD-source/scripts/build_native.sh").external_attr >> 16 & 0o111
    with pytest.raises(ValueError, match="existing archives"):
        export(root, output, [])


def test_export_checks_known_private_patterns_before_writing_archive(tmp_path):
    for name in REQUIRED_FILES:
        path = tmp_path / "source" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ExamplePrivateName")
    output = tmp_path / "public.zip"
    with pytest.raises(ValueError, match="Private-pattern") as rejected:
        export(tmp_path / "source", output, ["ExamplePrivateName"])
    assert "ExamplePrivateName" not in str(rejected.value)
    assert not output.exists()
