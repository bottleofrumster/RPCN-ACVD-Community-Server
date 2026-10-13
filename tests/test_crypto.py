import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from acvd_server.crypto import (
    CryptoError,
    CwcCipher,
    generate_rsa_keypair,
    load_private_key,
    rsa_oaep_decrypt,
    rsa_private_encrypt_x931,
)
from acvd_server.udp import (
    SYN,
    GameMessage,
    ReliablePacket,
    decode_game_message,
    decode_reliable_packet,
    decrypt_client_datagram,
    decrypt_server_datagram,
    encode_game_push,
    encode_game_reply,
    encode_game_reply_fragments,
    encode_game_request,
    encode_reliable_packet,
    encrypt_client_datagram,
    encrypt_server_datagram,
)

ROOT = Path(__file__).resolve().parents[1]


def test_private_key_created_private_even_with_permissive_umask(tmp_path):
    private, public = tmp_path / "keys/private.pem", tmp_path / "keys/public.pem"
    old_umask = os.umask(0)
    try:
        generate_rsa_keypair(private, public)
    finally:
        os.umask(old_umask)
    assert private.stat().st_mode & 0o777 == 0o600
    assert private.parent.stat().st_mode & 0o777 == 0o700
    assert load_private_key(private).key_size == 2048


def test_private_key_refuses_symlink_target(tmp_path):
    target = tmp_path / "preserved"
    target.write_bytes(b"unchanged")
    private = tmp_path / "private.pem"
    private.symlink_to(target)
    with pytest.raises(OSError):
        generate_rsa_keypair(private, tmp_path / "public.pem")
    assert target.read_bytes() == b"unchanged"


def test_cwc_known_ciphertext_and_round_trip() -> None:
    cipher = CwcCipher(
        bytes.fromhex("000102030405060708090a0b0c0d0e0f"), ROOT / "native/libacvd_cwc.so"
    )
    iv = bytes.fromhex("ffeeddccbbaa9988776655")
    plaintext = bytes.fromhex("0001020304050607")
    envelope = cipher.encrypt(plaintext, iv=iv)
    assert envelope[-len(plaintext) :].hex() == "88b8df0628fd51cc"
    assert cipher.decrypt(envelope) == plaintext
    tampered = envelope[:-1] + bytes([envelope[-1] ^ 1])
    with pytest.raises(CryptoError):
        cipher.decrypt(tampered)


def test_rsa_oaep_and_x931(tmp_path: Path) -> None:
    private_path = tmp_path / "private.pem"
    public_path = tmp_path / "public.pem"
    generate_rsa_keypair(private_path, public_path)
    private = load_private_key(private_path)
    public = serialization.load_pem_public_key(public_path.read_bytes())
    plaintext = b"handshake"
    ciphertext = public.encrypt(
        plaintext,
        padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None),
    )
    assert rsa_oaep_decrypt(private, ciphertext) == plaintext

    signed_block = rsa_private_encrypt_x931(private, b"response")
    numbers = private.private_numbers().public_numbers
    recovered = pow(int.from_bytes(signed_block, "big"), numbers.e, numbers.n).to_bytes(
        len(signed_block), "big"
    )
    assert recovered[0] == 0x6B
    assert recovered[-1] == 0xCC
    delimiter = recovered.index(b"\xba")
    assert set(recovered[1:delimiter]) == {0xBB}
    assert recovered[delimiter + 1 : -1] == b"response"


def test_udp_envelopes_and_reliable_header() -> None:
    key = bytes(range(16))
    token = bytes(range(8))
    prefix = b"Pilot".ljust(17, b"\0") + b"\0" + b"Pilot".ljust(17, b"\0")
    packet = ReliablePacket(0xABC, 0x123, SYN, payload=b"12345678", initial_prefix=prefix)
    plaintext = encode_reliable_packet(packet)
    decoded = decode_reliable_packet(plaintext)
    assert decoded == packet

    client = encrypt_client_datagram(
        plaintext,
        key,
        token,
        1,
        ROOT / "native/libacvd_cwc.so",
        iv=bytes.fromhex("00112233445566778899aa"),
    )
    assert decrypt_client_datagram(client, key, ROOT / "native/libacvd_cwc.so") == (
        token,
        1,
        plaintext,
    )
    with pytest.raises(CryptoError):
        decrypt_client_datagram(
            bytes([client[0] ^ 1]) + client[1:], key, ROOT / "native/libacvd_cwc.so"
        )

    server = encrypt_server_datagram(plaintext, key, ROOT / "native/libacvd_cwc.so")
    assert decrypt_server_datagram(server, key, ROOT / "native/libacvd_cwc.so") == plaintext


def test_game_message_reply_layers() -> None:
    proto = b"\x0a\x05Pilot\x10\x01"
    encoded = encode_game_reply(GameMessage(0, b"\x00\x00\x00\x07", proto, 3))
    decoded = decode_game_message(encoded)
    assert decoded.message_type == 0
    assert decoded.message_index == b"\x00\x00\x00\x07"
    assert decoded.payload == bytes.fromhex("00000000010000000000000000000000") + proto
    assert decoded.packet_counter == 3

    pushed = decode_game_message(encode_game_push(b"\x08\x8c\x07", 4))
    assert pushed.message_type == 0x0320
    assert pushed.message_index == b"\xff\xff\xff\xff"
    assert pushed.payload == b"\x08\x8c\x07"
    assert pushed.packet_counter == 4

    request = GameMessage(0x0386, b"\x00\x00\x00\x09", b"\x08\x01", 5)
    assert decode_game_message(encode_game_request(request)) == request


def test_game_reply_fragmentation_reassembles_the_original_message() -> None:
    payload = bytes(range(256)) * 10
    fragments = encode_game_reply_fragments(
        GameMessage(0, b"\x00\x00\x00\x09", payload, 7),
        max_fragment_length=900,
    )
    assert len(fragments) == 3
    assert [fragment[9] for fragment in fragments] == [0, 1, 2]
    assert all(
        int.from_bytes(fragment[3:8], "big") == 12 + 16 + len(payload) for fragment in fragments
    )
    body = b"".join(fragment[12:] for fragment in fragments)
    combined = (
        fragments[0][:3]
        + len(body).to_bytes(5, "big")
        + b"\x00\x00"
        + (len(body) & 0xFFFF).to_bytes(2, "big")
        + body
    )
    decoded = decode_game_message(combined)
    assert decoded.message_index == b"\x00\x00\x00\x09"
    assert decoded.payload[16:] == payload


def test_game_reply_fragmentation_preserves_40_bit_total_length() -> None:
    payload = bytes(range(256)) * 800
    fragments = encode_game_reply_fragments(
        GameMessage(0, b"\x00\x00\x00\x11", payload, 257),
        max_fragment_length=1012,
    )
    total_length = 12 + 16 + len(payload)
    assert total_length > 0xFFFF
    assert int.from_bytes(fragments[0][3:8], "big") == total_length
    assert fragments[0][3:6] != b"\x00\x00\x00"
    assert [fragment[9] for fragment in fragments] == list(range(len(fragments)))

    body = b"".join(fragment[12:] for fragment in fragments)
    combined = (
        fragments[0][:3]
        + len(body).to_bytes(5, "big")
        + b"\x00\x00"
        + (len(body) & 0xFFFF).to_bytes(2, "big")
        + body
    )
    decoded = decode_game_message(combined)
    assert decoded.message_index == b"\x00\x00\x00\x11"
    assert decoded.payload[16:] == payload
