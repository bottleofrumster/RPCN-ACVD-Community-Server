"""Authenticate regulation data before deriving catalogue prices from it."""

import hashlib
import hmac
import struct

import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from acvd_server.crypto import (
    CryptoError,
    generate_rsa_keypair,
    load_private_key,
    rsa_private_encrypt_x931,
)
from acvd_server.regulation import (
    HEADER_BYTES,
    MAX_PAYLOAD_BYTES,
    REGULATION_AES_KEY,
    REGULATION_HMAC_KEY,
    build_container,
    read_container,
)


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    root = tmp_path_factory.mktemp("regulation-keys")
    private_path = root / "private.pem"
    generate_rsa_keypair(private_path, root / "public.pem")
    private = load_private_key(private_path)
    return private_path, private, private.public_key()


def signed_header(header, ciphertext, private):
    return rsa_private_encrypt_x931(private, header) + ciphertext


def header_for(payload, *, size=None, iv=bytes(16)):
    return (
        struct.pack(">IIQ", 0, 0, len(payload) if size is None else size)
        + iv
        + bytes(16)
        + hmac.new(REGULATION_HMAC_KEY, payload, hashlib.sha1).digest()
        + bytes(44)
    )


def ciphertext_for(clear):
    encryptor = Cipher(algorithms.AES(REGULATION_AES_KEY), modes.CBC(bytes(16))).encryptor()
    return encryptor.update(clear) + encryptor.finalize()


@pytest.mark.parametrize("length", [0, 1, 15, 16, 17, 112, 255, 256, 1025, 256000])
def test_round_trip_retains_original_zero_bytes(keys, length):
    private_path, _, public = keys
    payload = (bytes(range(256)) * ((length + 255) // 256))[:length]
    assert read_container(build_container(payload, private_path), public) == payload


def test_builder_keeps_the_existing_wire_layout(keys, monkeypatch):
    private_path, private, _ = keys
    monkeypatch.setattr("acvd_server.regulation.os.urandom", lambda n: bytes(range(n)))
    payload = b"BND3-test\0\0"
    data = build_container(payload, private_path)
    numbers = private.public_key().public_numbers()
    raw = pow(int.from_bytes(data[:256], "big"), numbers.e, numbers.n).to_bytes(256, "big")
    assert raw[:144] == b"\x6b" + b"\xbb" * 141 + b"\xba" + b"\0"
    assert raw[-1] == 0xCC
    assert raw[143:-1] == header_for(payload, iv=bytes(range(16)))
    decryptor = Cipher(algorithms.AES(REGULATION_AES_KEY), modes.CBC(bytes(range(16)))).decryptor()
    assert decryptor.update(data[256:]) + decryptor.finalize() == payload + bytes(16 - len(payload))


def test_complementary_x931_representative(keys):
    private_path, private, public = keys
    data = build_container(b"verified", private_path)
    modulus = private.public_key().public_numbers().n
    complement = (modulus - int.from_bytes(data[:256], "big")).to_bytes(256, "big")
    assert read_container(complement + data[256:], public) == b"verified"


@pytest.mark.parametrize("position", [0, 31, 128, 255, 256, 271, 280])
def test_tampering_is_rejected(keys, position):
    private_path, _, public = keys
    data = bytearray(build_container(bytes(range(64)), private_path))
    data[position] ^= 1
    with pytest.raises(CryptoError):
        read_container(bytes(data), public)


@pytest.mark.parametrize("length", [0, 1, 255, 257, 271, 273, 288])
def test_truncated_or_extended_ciphertext_is_rejected(keys, length):
    private_path, _, public = keys
    data = build_container(b"x", private_path)
    malformed = data[:length].ljust(length, b"\0")
    with pytest.raises(CryptoError):
        read_container(malformed, public)


@pytest.mark.parametrize("offset", [0, 7, 32, 47, 68, 111])
def test_authenticated_but_unsupported_reserved_fields(keys, offset):
    _, private, public = keys
    header = bytearray(header_for(b""))
    header[offset] = 1
    with pytest.raises(CryptoError, match="reserved"):
        read_container(signed_header(bytes(header), b"", private), public)


def test_length_bound_before_decryption(keys):
    _, private, public = keys
    with pytest.raises(CryptoError, match="source length"):
        read_container(
            signed_header(header_for(b"", size=MAX_PAYLOAD_BYTES + 1), b"", private), public
        )
    with pytest.raises(CryptoError, match="size range"):
        read_container(bytes(256 + MAX_PAYLOAD_BYTES + 1), public)


def test_nonzero_aes_padding_is_rejected_even_with_correct_payload_hmac(keys):
    _, private, public = keys
    with pytest.raises(CryptoError, match="padding"):
        read_container(
            signed_header(header_for(b"x"), ciphertext_for(b"x" + b"y" * 15), private), public
        )


def test_hmac_must_match_the_clear_source(keys):
    _, private, public = keys
    with pytest.raises(CryptoError, match="authentication"):
        read_container(
            signed_header(header_for(b"a"), ciphertext_for(b"b" + bytes(15)), private), public
        )


@pytest.mark.parametrize("offset,value", [(0, 0x6A), (1, 0xBA), (142, 0xBB), (255, 0xBC)])
def test_invalid_rsa_padding_is_rejected(keys, offset, value):
    _, private, public = keys
    block = bytearray(b"\x6b" + b"\xbb" * 141 + b"\xba" + header_for(b"") + b"\xcc")
    assert len(block) == 256 and len(header_for(b"")) == HEADER_BYTES
    block[offset] = value
    numbers = private.private_numbers()
    signature = pow(int.from_bytes(block, "big"), numbers.d, numbers.public_numbers.n).to_bytes(
        256, "big"
    )
    with pytest.raises(CryptoError, match="X9.31"):
        read_container(signature, public)


def test_signature_must_be_below_modulus(keys):
    _, _, public = keys
    with pytest.raises(CryptoError, match="modulus"):
        read_container(public.public_numbers().n.to_bytes(256, "big"), public)


def test_wrong_trusted_key_fails(keys):
    private_path, _, _ = keys
    other = rsa.generate_private_key(public_exponent=3, key_size=2048).public_key()
    with pytest.raises(CryptoError):
        read_container(build_container(b"source", private_path), other)


def test_unsupported_key_types_fail():
    for public in (
        ec.generate_private_key(ec.SECP256R1()).public_key(),
        rsa.generate_private_key(public_exponent=3, key_size=1024).public_key(),
    ):
        with pytest.raises(CryptoError, match="2048-bit RSA"):
            read_container(bytes(256), public)


def test_existing_builder_entry_point_remains_usable(keys, tmp_path):
    from scripts.build_regulation_payload import build

    private_path, _, public = keys
    source, output = tmp_path / "source.bin", tmp_path / "contents_101.bin"
    source.write_bytes(b"BND3-unchanged-script-interface")
    original = source.read_bytes()
    build(source, private_path, output, "container")
    assert source.read_bytes() == original
    assert read_container(output.read_bytes(), public) == original
