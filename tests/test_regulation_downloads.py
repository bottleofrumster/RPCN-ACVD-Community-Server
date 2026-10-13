"""Native signed manifest/body downloads; existing stored seeds stay unchanged."""

import hashlib
import hmac
import os
import struct

import pytest
from test_regulation_check import archive, signing_key as signing_key, source as source

from acvd_server.crypto import load_private_key
from acvd_server.regulation import (
    MANIFEST_PATH,
    REGULATION_HMAC_KEY,
    REGULATION_PATH,
    RegulationDownloads,
    _verified_header,
    build_container,
    read_container,
)


def downloads(source, signing_key, host="cdn.example", port=8080):
    return RegulationDownloads(source, load_private_key(signing_key[0]), host, port)


def manifest(files, source):
    clear = read_container(files.get(MANIFEST_PATH), source.public_key, byte_order="little")
    return clear, dict(line.split("=", 1) for line in clear.decode("ascii").splitlines())


def test_manifest_lists_separately_authenticated_body_without_rewriting_seed(source, signing_key):
    before = source.path.read_bytes()
    files = downloads(source, signing_key)
    clear, entries = manifest(files, source)
    payload = read_container(files.get(REGULATION_PATH), source.public_key, byte_order="little")
    assert payload == read_container(before, source.public_key) == source.load().payload
    assert entries == {
        "Patch.List.Count": "1",
        "Patch.List.File0.DIGEST": hmac.new(REGULATION_HMAC_KEY, payload, hashlib.sha1).hexdigest().upper(),
        "Patch.List.File0.Dir": "",
        "Patch.List.File0.Name": "regulation.bnd",
        "Patch.List.File0.SizeOrg": str(len(payload)),
        "Patch.List.File0.Path": "http://cdn.example:8080/regulation/regulation.bnd",
    }
    # Native decoder19d8824 swaps these fields; legacy seed tests remain BE.
    header = _verified_header(files.get(MANIFEST_PATH)[:256], source.public_key)
    assert struct.unpack("<IIQ", header[:16]) == (0, 0, len(clear))
    assert header[48:68] == hmac.new(REGULATION_HMAC_KEY, clear, hashlib.sha1).digest()
    assert source.path.read_bytes() == before


def test_each_generation_is_stable_and_same_mtime_replacement_changes_both_files(source, signing_key):
    files = downloads(source, signing_key)
    old = {path: files.get(path) for path in (MANIFEST_PATH, REGULATION_PATH)}
    assert all(files.get(path) is value for path, value in old.items())
    stat = source.path.stat()
    source.path.write_bytes(build_container(archive(104) + bytes(256), signing_key[0]))
    os.utime(source.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    new = {path: files.get(path) for path in old}
    assert all(new[path] != old[path] for path in old)
    assert source.load().version == 104
    clear, entries = manifest(files, source)
    body = read_container(new[REGULATION_PATH], source.public_key, byte_order="little")
    assert entries["Patch.List.File0.DIGEST"] == hmac.new(REGULATION_HMAC_KEY, body, hashlib.sha1).hexdigest().upper()


@pytest.mark.parametrize("path", [MANIFEST_PATH, REGULATION_PATH])
@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_cached_downloads_never_hide_source_failure(source, signing_key, path, failure):
    files = downloads(source, signing_key)
    files.get(path)
    if failure == "missing":
        source.path.unlink()
    else:
        source.path.write_bytes(b"broken")
    with pytest.raises((OSError, ValueError)):
        files.get(path)


@pytest.mark.parametrize("path", [MANIFEST_PATH, REGULATION_PATH])
@pytest.mark.parametrize("offset", [0, -17])
def test_generated_responses_remain_authenticated(source, signing_key, path, offset):
    encoded = bytearray(downloads(source, signing_key).get(path))
    encoded[offset] ^= 1
    with pytest.raises(ValueError):
        read_container(bytes(encoded), source.public_key, byte_order="little")


@pytest.mark.parametrize("size", [255999, 256000, 256001])
def test_native_cache_capacity_is_enforced_without_trimming_data(source, signing_key, size):
    body = archive() + bytes(size - len(archive()))
    source.path.write_bytes(build_container(body, signing_key[0]))
    files = downloads(source, signing_key)
    if size > 256000:
        with pytest.raises(ValueError, match="cache capacity"):
            files.get(MANIFEST_PATH)
    else:
        assert read_container(files.get(REGULATION_PATH), source.public_key, byte_order="little") == body


@pytest.mark.parametrize("host,port", [
    ("", 8080), ("host\nPatch.List.Count=2", 8080), ("host/path", 8080),
    ("user@host", 8080), ("host#fragment", 8080), ("host", 0), ("host", True), ("host", 65536),
])
def test_configured_authority_cannot_inject_manifest_fields(source, signing_key, host, port):
    with pytest.raises(ValueError, match="authority"):
        downloads(source, signing_key, host, port)


def test_ipv6_authority_is_bracketed_and_unknown_paths_are_rejected(source, signing_key):
    files = downloads(source, signing_key, "::1", 8080)
    assert manifest(files, source)[1]["Patch.List.File0.Path"] == "http://[::1]:8080/regulation/regulation.bnd"
    with pytest.raises(ValueError, match="path"):
        files.get("/regulation/../keys/server-private.pem")
