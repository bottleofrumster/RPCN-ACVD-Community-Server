"""Regulation compatibility and the existing native download/retry response."""

import asyncio
import hashlib
import os
import struct
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from test_cdn_http import Writer

from acvd_server import udp
from acvd_server.crypto import generate_rsa_keypair, load_private_key
from acvd_server.protocol import proto_bytes, proto_varint
from acvd_server.regulation import (
    RegulationDownloads,
    RegulationSource,
    build_container,
    read_container,
)
from acvd_server.server import AcvdServer, GameDatagramProtocol
from acvd_server.shop_catalogue import regulation_archive_size
from acvd_server.teams import TeamStore
from acvd_server.telemetry import Telemetry


def archive(version=103):
    """One real-sized raw system record; intentional zero bytes at its end."""
    data = bytearray(0x180)
    data[:4] = b"BND3"
    data[12:16] = b"\x24\x01\x01\x00"
    struct.pack_into(">I", data, 16, 1)
    struct.pack_into(">IIIII", data, 32, 0x02000000, 0x80, 0x100, 0x40, 0x80)
    name = b"serversystemparam.bin\0"
    data[0x40:0x40 + len(name)] = name
    struct.pack_into(">I", data, 0x16C, version)
    return bytes(data)


@pytest.fixture(scope="module")
def signing_key(tmp_path_factory):
    root = tmp_path_factory.mktemp("regulation-check-keys")
    private = root / "private.pem"
    generate_rsa_keypair(private, root / "public.pem")
    return private, load_private_key(private).public_key()


@pytest.fixture
def source(tmp_path, signing_key):
    private, public = signing_key
    path = tmp_path / "contents_101.bin"
    path.write_bytes(build_container(archive() + bytes(256), private))
    return RegulationSource(path, public)


def request_payload(digest, version):
    return proto_bytes(1, digest) + proto_varint(2, version)


def dispatcher(tmp_path, source):
    events = []
    telemetry = SimpleNamespace(
        emit=lambda event, **data: events.append((event, data)),
        diagnostics=SimpleNamespace(enabled=False),
    )
    protocol = GameDatagramProtocol(
        telemetry, {}, Path("native/libacvd_cwc.so"), tmp_path / "players",
        TeamStore(tmp_path / "teams"), get_regulation=source.load,
    )
    actor = udp.GameSession(b"actor001", bytes(16), "Actor", 11)
    replies = []
    protocol._send = lambda session, packet: replies.append(packet)
    return protocol, actor, replies, events


def dispatch(protocol, actor, payload):
    protocol._dispatch_game_message(
        actor, udp.ReliablePacket(39, 8, udp.DAT),
        udp.GameMessage(udp.CHECK_REGULATION_DATA, b"\x12\x34\x56\x78", payload, 17),
    )


def response(packet):
    message = udp.decode_game_message(packet.payload)
    assert packet.opcode == udp.DAT_ACK
    assert packet.remote_sequence == 39
    assert message.message_index == b"\x12\x34\x56\x78"
    assert len(message.payload) == 16  # no invented version/URL protobuf body
    return struct.unpack(">IIII", message.payload)


@pytest.mark.parametrize("form", [0, 1])
def test_matching_cache_and_fresh_download_have_the_original_success_frame(tmp_path, source, form):
    protocol, actor, replies, events = dispatcher(tmp_path, source)
    current = source.load()
    before = deepcopy(protocol.teams.state)
    dispatch(protocol, actor, request_payload(current.digests[form], current.version))
    assert response(replies[-1]) == (0, 0x01000000, 0, 0)
    assert events[-1][1]["outcome"] == "matched"
    assert protocol.teams.state == before
    assert not (tmp_path / "players").exists()


@pytest.mark.parametrize("version,detail", [(100, 52), (104, 52), (103, 51)])
def test_stale_or_modified_regulation_uses_native_retry_response(tmp_path, source, version, detail):
    protocol, actor, replies, events = dispatcher(tmp_path, source)
    dispatch(protocol, actor, request_payload(bytes(20), version))
    assert response(replies[-1]) == (1, detail, 0, 0)
    assert events[-1][1]["server_version"] == 103


@pytest.mark.parametrize("payload", [
    b"", b"\x0a\x14x", b"\x00", proto_varint(1, 1) + proto_varint(2, 103),
    proto_bytes(1, b"not-a-digest") + proto_varint(2, 103),
    proto_bytes(1, bytes(20)), proto_varint(2, 103),
    proto_bytes(1, bytes(20)) + proto_bytes(2, b"103"),
    request_payload(bytes(20), 1 << 32),
    request_payload(bytes(20), 103) + proto_bytes(1, bytes(20)),
    request_payload(bytes(20), 103) + proto_varint(2, 103),
])
def test_malformed_check_never_acknowledges(tmp_path, source, payload):
    protocol, actor, replies, events = dispatcher(tmp_path, source)
    dispatch(protocol, actor, payload)
    assert response(replies[-1]) == (1, 51, 0, 0)
    assert events[-1][1]["outcome"] == "invalid_request"


def test_unknown_protobuf_field_is_forward_compatible(tmp_path, source):
    protocol, actor, replies, _ = dispatcher(tmp_path, source)
    current = source.load()
    dispatch(protocol, actor, request_payload(current.digests[0], 103) + proto_varint(9, 42))
    assert response(replies[-1])[0] == 0


def test_current_version_with_empty_cache_still_requires_retrieval(tmp_path, source):
    protocol, actor, replies, _ = dispatcher(tmp_path, source)
    dispatch(protocol, actor, request_payload(hashlib.sha1(b"").digest(), 103))
    assert response(replies[-1]) == (1, 51, 0, 0)


def test_digest_is_not_container_hash_or_arbitrary_zero_stripped_prefix(tmp_path, source):
    protocol, actor, replies, _ = dispatcher(tmp_path, source)
    current = source.load()
    assert current.digests == (
        hashlib.sha1(archive()).digest(), hashlib.sha1(archive() + bytes(256)).digest(),
    )
    for body in (current.container, archive().rstrip(b"\0"), archive() + bytes(1)):
        dispatch(protocol, actor, request_payload(hashlib.sha1(body).digest(), 103))
        assert response(replies[-1]) == (1, 51, 0, 0)


@pytest.mark.parametrize("failure", ["missing", "corrupt", "empty"])
def test_unavailable_source_does_not_ack_or_blame_the_client(tmp_path, source, failure):
    protocol, actor, replies, events = dispatcher(tmp_path, source)
    current = source.load()
    if failure == "missing":
        source.path.unlink()
    else:
        source.path.write_bytes(current.container[:-1] + b"x" if failure == "corrupt" else b"")
    dispatch(protocol, actor, request_payload(current.digests[0], 103))
    assert response(replies[-1]) == (1, 28, 0, 0)
    assert events[-1][0] == "game_regulation_data_unavailable"


def test_replacement_even_with_preserved_size_and_mtime_invalidates_identity(source, signing_key):
    first = source.load()
    assert source.load() is first
    before = source.path.stat()
    source.path.write_bytes(build_container(archive(104) + bytes(256), signing_key[0]))
    os.utime(source.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    second = source.load()
    assert second.version == 104 and second.digests != first.digests
    assert second.sha256 != first.sha256


@pytest.mark.parametrize("mutation", ["name", "extent", "suffix", "magic", "empty", "short"])
def test_signed_but_invalid_archive_is_not_a_compatibility_authority(source, signing_key, mutation):
    data = bytearray(archive())
    if mutation == "name":
        data[0x40] = ord("x")
    elif mutation == "extent":
        struct.pack_into(">I", data, 40, len(data))
    elif mutation == "suffix":
        data.extend(b"hidden")
    elif mutation == "magic":
        data[:4] = b"bad!"
    elif mutation == "empty":
        struct.pack_into(">I", data, 16, 0)
    else:
        struct.pack_into(">I", data, 48, 4)
    source.path.write_bytes(build_container(bytes(data), signing_key[0]))
    with pytest.raises(ValueError):
        source.load()


def test_archive_extent_includes_real_trailing_zero_bytes():
    data = archive()
    assert regulation_archive_size(data + bytes(1024)) == len(data)
    assert len(data.rstrip(b"\0")) < len(data)


async def fetch(source, signing_key, path="/regulation/contents_101.bin"):
    events = []
    server = SimpleNamespace(
        config=SimpleNamespace(regulation_path=source.path),
        regulation_downloads=RegulationDownloads(source, load_private_key(signing_key[0]), "127.0.0.1", 8080),
        _stream_writers=set(),
        telemetry=SimpleNamespace(emit=lambda event, **data: events.append((event, data))),
    )
    reader, writer = asyncio.StreamReader(), Writer()
    reader.feed_data(f"GET {path} HTTP/1.1\r\nHost: untrusted.example\r\n\r\n".encode())
    reader.feed_eof()
    await AcvdServer._handle_cdn(server, reader, writer)
    header, body = bytes(writer.data).split(b"\r\n\r\n", 1)
    assert writer.closed and not server._stream_writers
    return header, body


async def test_outdated_check_http_download_recheck_uses_one_authenticated_source(tmp_path, source, signing_key):
    protocol, actor, replies, _ = dispatcher(tmp_path, source)
    before = source.path.read_bytes()
    dispatch(protocol, actor, request_payload(bytes(20), 100))
    assert response(replies[-1]) == (1, 52, 0, 0)
    header, body = await fetch(source, signing_key)
    assert header.startswith(b"HTTP/1.1 200 OK")
    manifest = dict(line.split("=", 1) for line in read_container(
        body, source.public_key, byte_order="little"
    ).decode("ascii").splitlines())
    assert manifest["Patch.List.File0.Path"] == "http://127.0.0.1:8080/regulation/regulation.bnd"
    header, body = await fetch(source, signing_key, "/regulation/regulation.bnd")
    assert header.startswith(b"HTTP/1.1 200 OK")
    decoded = read_container(body, source.public_key, byte_order="little")
    assert decoded == read_container(before, source.public_key)
    dispatch(protocol, actor, request_payload(hashlib.sha1(decoded).digest(), 103))
    assert response(replies[-1]) == (0, 0x01000000, 0, 0)
    assert source.path.read_bytes() == before


async def test_cdn_refuses_a_corrupted_file_after_a_successful_check(source, signing_key):
    source.load()
    source.path.write_bytes(b"broken")
    header, body = await fetch(source, signing_key)
    assert header.startswith(b"HTTP/1.1 503 Service Unavailable")
    assert body == b"regulation file unavailable\n"


def test_public_seed_matches_the_recovered_retail_checksum_and_version():
    root = Path(__file__).resolve().parents[1] / "resources/regulation"
    public = serialization.load_pem_public_key((root / "publickey.pem").read_bytes())
    current = RegulationSource(root / "contents_101.bin", public).load()
    assert current.version == 103
    assert [digest.hex() for digest in current.digests] == [
        "71e74d62f165ddaea0912c9d20db37edae7bd8f0",
        "48c4e40f110553fc93034651af180e4760eca4ef",
    ]


def test_encrypted_check_preserves_request_identity_and_survives_rejection(tmp_path, source):
    actor = udp.GameSession(b"check001", bytes(range(16)), "Pilot", 42, established=True)
    library = Path(__file__).resolve().parents[1] / "native/libacvd_cwc.so"
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"), {actor.auth_token: actor},
        library, tmp_path / "players", TeamStore(tmp_path / "teams"),
        get_regulation=source.load,
    )
    packets = []
    protocol.connection_made(SimpleNamespace(sendto=lambda data, peer: packets.append((data, peer))))
    current = source.load()
    before = deepcopy(protocol.teams.state)
    try:
        for sequence, (version, digest, expected) in enumerate([
            (100, bytes(20), (1, 52, 0, 0)),
            (103, bytes(20), (1, 51, 0, 0)),
            (103, current.digests[0], (0, 0x01000000, 0, 0)),
            (103, current.digests[1], (0, 0x01000000, 0, 0)),
        ], 100):
            request = udp.GameMessage(
                udp.CHECK_REGULATION_DATA, sequence.to_bytes(4, "big"),
                request_payload(digest, version), sequence,
            )
            wire = udp.encrypt_client_datagram(
                udp.encode_reliable_packet(udp.ReliablePacket(
                    sequence, 0, udp.DAT, payload=udp.encode_game_request(request),
                )), actor.game_key, actor.auth_token, 0, library,
            )
            packets.clear()
            protocol.datagram_received(wire, ("127.0.0.1", 40001))
            decoded = [udp.decode_reliable_packet(udp.decrypt_server_datagram(
                raw, actor.game_key, library,
            )) for raw, _ in packets]
            assert [packet.opcode for packet in decoded] == [udp.ACK, udp.DAT_ACK]
            message = udp.decode_game_message(decoded[-1].payload)
            assert message.message_index == request.message_index
            assert decoded[-1].remote_sequence == sequence
            assert struct.unpack(">IIII", message.payload) == expected
            assert all(peer == ("127.0.0.1", 40001) for _, peer in packets)
        assert actor.established and protocol.teams.state == before
        assert not (tmp_path / "players").exists()
    finally:
        protocol.connection_lost(None)
