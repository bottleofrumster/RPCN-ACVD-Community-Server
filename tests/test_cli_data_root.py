"""Committed code can reuse existing host data without creating a new identity."""

import argparse

import pytest

from acvd_server import cli
from acvd_server.config import ServerConfig


def test_default_root_is_unchanged():
    assert cli._config(argparse.Namespace()).project_root == ServerConfig().project_root


@pytest.mark.parametrize("existing", ["server-private.pem", "publickey.pem"])
def test_initialize_rejects_either_partial_identity_without_writing(
    tmp_path, monkeypatch, existing
):
    config = ServerConfig(project_root=tmp_path)
    config.private_key_path.parent.mkdir()
    preserved = config.private_key_path.parent / existing
    preserved.write_bytes(b"preserved")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: pytest.fail("must not build"))
    with pytest.raises(SystemExit, match="partial keypair"):
        cli.initialize(config)
    assert preserved.read_bytes() == b"preserved"
    assert len(list(preserved.parent.iterdir())) == 1


def test_initialize_uses_shell_for_extracted_script_and_preserves_existing_keys(
    tmp_path, monkeypatch
):
    config = ServerConfig(project_root=tmp_path)
    config.private_key_path.parent.mkdir()
    config.private_key_path.write_bytes(b"private")
    config.public_key_path.write_bytes(b"public")
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    cli.initialize(config)
    assert calls == [((["bash", str(tmp_path / "scripts/build_native.sh")],), {"check": True})]
    assert config.private_key_path.read_bytes() == b"private"
    assert config.public_key_path.read_bytes() == b"public"


def test_explicit_root_selects_every_host_storage_path(tmp_path):
    config = cli._config(argparse.Namespace(data_root=tmp_path))
    for path in (
        config.private_key_path,
        config.public_key_path,
        config.native_library_path,
        config.telemetry_path,
        config.player_data_path,
        config.team_data_path,
        config.session_state_path,
        config.regulation_path,
    ):
        assert path.is_relative_to(tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "missing", ["keys/server-private.pem", "keys/publickey.pem", "native/libacvd_cwc.so"]
)
def test_explicit_root_never_auto_generates_missing_identity(tmp_path, monkeypatch, missing):
    for relative in ("keys/server-private.pem", "keys/publickey.pem", "native/libacvd_cwc.so"):
        if relative != missing:
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"preserved")

    def forbidden(*args, **kwargs):
        raise AssertionError("Initialization or service start must not run")

    monkeypatch.setattr(cli, "initialize", forbidden)
    monkeypatch.setattr(cli, "_serve", forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(["serve", "--data-root", str(tmp_path)])
    assert error.value.code == 2
    assert not (tmp_path / missing).exists()


def test_explicit_initialized_root_reaches_serve_with_ports_preserved(tmp_path, monkeypatch):
    for relative in ("keys/server-private.pem", "keys/publickey.pem", "native/libacvd_cwc.so"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"preserved")
    configs = []

    async def serve(config):
        configs.append(config)

    monkeypatch.setattr(cli, "_serve", serve)
    cli.main(
        ["serve", "--data-root", str(tmp_path), "--login-ports", "50011", "--game-port", "52103"]
    )
    assert configs[0].project_root == tmp_path
    assert configs[0].login_ports == [50011] and configs[0].game_port == 52103
