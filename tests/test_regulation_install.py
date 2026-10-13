"""New hosts distribute identical regulation data without sharing signing keys."""

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from acvd_server import cli
from acvd_server.config import ServerConfig
from acvd_server.crypto import CryptoError, generate_rsa_keypair, load_private_key
from acvd_server.regulation import build_container, install_for_host, read_container


@pytest.fixture
def seed(tmp_path):
    owner = tmp_path / "owner"
    generate_rsa_keypair(owner / "private.pem", owner / "public.pem")
    source = owner / "contents_101.bin"
    source.write_bytes(build_container(b"BND3-example\x00\x00", owner / "private.pem"))
    host = tmp_path / "host"
    generate_rsa_keypair(host / "private.pem", host / "public.pem")
    return source, owner / "public.pem", host / "private.pem", host / "cdn/contents_101.bin"


def test_install_verifies_seed_and_signs_unchanged_payload_for_new_host(seed):
    source, public, private, destination = seed
    before = source.read_bytes()
    assert install_for_host(*seed)
    host_key = load_private_key(private).public_key()
    assert read_container(destination.read_bytes(), host_key) == b"BND3-example\x00\x00"
    assert source.read_bytes() == before
    old_key = serialization.load_pem_public_key(public.read_bytes())
    with pytest.raises(CryptoError):
        read_container(destination.read_bytes(), old_key)
    assert list(destination.parent.iterdir()) == [destination]


def test_existing_regulation_is_preserved_without_reading_seed(seed):
    source, public, private, destination = seed
    destination.parent.mkdir()
    destination.write_bytes(b"operator-selected")
    source.unlink()
    assert not install_for_host(*seed)
    assert destination.read_bytes() == b"operator-selected"


def test_tampered_seed_cannot_install(seed):
    source, public, private, destination = seed
    altered = bytearray(source.read_bytes())
    altered[-1] ^= 1
    source.write_bytes(altered)
    with pytest.raises(CryptoError):
        install_for_host(*seed)
    assert not destination.exists()


def test_wrong_trusted_key_cannot_install(seed, tmp_path):
    source, public, private, destination = seed
    generate_rsa_keypair(tmp_path / "wrong-private.pem", tmp_path / "wrong-public.pem")
    with pytest.raises(CryptoError):
        install_for_host(source, tmp_path / "wrong-public.pem", private, destination)
    assert not destination.exists()


def test_concurrent_existing_destination_is_not_overwritten(seed, monkeypatch):
    from acvd_server import regulation

    source, public, private, destination = seed

    def competing_install(temporary, output):
        output.write_bytes(b"other installer")
        raise FileExistsError

    monkeypatch.setattr(regulation.os, "link", competing_install)
    assert not install_for_host(*seed)
    assert destination.read_bytes() == b"other installer"
    assert list(destination.parent.iterdir()) == [destination]


def test_init_installs_bundled_regulation_without_exporting_host_key(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    source = root / "resources/regulation/contents_101.bin"
    public = root / "resources/regulation/publickey.pem"
    bundled = tmp_path / "resources/regulation"
    bundled.mkdir(parents=True)
    (bundled / source.name).write_bytes(source.read_bytes())
    (bundled / public.name).write_bytes(public.read_bytes())
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: None)
    config = ServerConfig(project_root=tmp_path)
    cli.initialize(config)
    payload = read_container(
        config.regulation_path.read_bytes(), load_private_key(config.private_key_path).public_key()
    )
    expected = read_container(
        source.read_bytes(), serialization.load_pem_public_key(public.read_bytes())
    )
    assert payload == expected and payload.startswith(b"BND3")
    first = config.regulation_path.read_bytes()
    cli.initialize(config)
    assert config.regulation_path.read_bytes() == first


def test_force_init_refuses_to_orphan_installed_regulation(tmp_path, monkeypatch):
    config = ServerConfig(project_root=tmp_path)
    generate_rsa_keypair(config.private_key_path, config.public_key_path)
    config.regulation_path.parent.mkdir(parents=True)
    config.regulation_path.write_bytes(build_container(b"BND3", config.private_key_path))
    paths = (config.private_key_path, config.public_key_path, config.regulation_path)
    before = [p.read_bytes() for p in paths]
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: pytest.fail("must not build"))
    with pytest.raises(SystemExit, match="key rotation"):
        cli.initialize(config, force=True)
    assert [p.read_bytes() for p in paths] == before
