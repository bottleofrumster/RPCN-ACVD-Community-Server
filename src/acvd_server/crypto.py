"""Adapt RSA, AES-CWC, and native-library operations used by the wire protocol."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class CryptoError(ValueError):
    pass


def generate_rsa_keypair(private_path: Path, public_path: Path) -> None:
    private_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = rsa.generate_private_key(public_exponent=3, key_size=2048)
    descriptor = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    public_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.PKCS1,
        )
    )


def load_private_key(path: Path) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise CryptoError("server private key is not RSA")
    return key


def rsa_oaep_decrypt(key: rsa.RSAPrivateKey, ciphertext: bytes) -> bytes:
    try:
        return key.decrypt(
            ciphertext,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA1()),
                algorithm=hashes.SHA1(),
                label=None,
            ),
        )
    except ValueError as exc:
        raise CryptoError("RSA-OAEP decryption failed") from exc


def rsa_private_encrypt_x931(key: rsa.RSAPrivateKey, plaintext: bytes) -> bytes:
    """Equivalent to legacy OpenSSL RSA_private_encrypt(..., RSA_X931_PADDING)."""
    numbers = key.private_numbers()
    size = (key.key_size + 7) // 8
    padding_length = size - len(plaintext) - 3
    if padding_length < 0:
        raise CryptoError("message is too large for RSA X9.31 block")
    encoded = b"\x6b" + (b"\xbb" * padding_length) + b"\xba" + plaintext + b"\xcc"
    value = int.from_bytes(encoded, "big")
    result = pow(value, numbers.d, numbers.public_numbers.n)
    return result.to_bytes(size, "big")


class CwcCipher:
    IV_SIZE = 11
    TAG_SIZE = 16

    def __init__(self, key: bytes, library_path: Path):
        if len(key) not in (16, 24, 32):
            raise CryptoError(f"unsupported AES-CWC key size: {len(key)}")
        self.key = bytes(key)
        self.library_path = library_path
        try:
            self.library = ctypes.CDLL(str(library_path))
        except OSError as exc:
            raise CryptoError(f"cannot load AES-CWC library: {library_path}") from exc
        byte_ptr = ctypes.POINTER(ctypes.c_ubyte)
        args = [
            byte_ptr,
            ctypes.c_size_t,
            byte_ptr,
            ctypes.c_size_t,
            byte_ptr,
            ctypes.c_size_t,
            byte_ptr,
            byte_ptr,
            ctypes.c_size_t,
        ]
        self.library.acvd_cwc_encrypt.argtypes = args
        self.library.acvd_cwc_encrypt.restype = ctypes.c_int
        self.library.acvd_cwc_decrypt.argtypes = args
        self.library.acvd_cwc_decrypt.restype = ctypes.c_int
        aad_args = [
            byte_ptr,
            ctypes.c_size_t,
            byte_ptr,
            ctypes.c_size_t,
            byte_ptr,
            ctypes.c_size_t,
            byte_ptr,
            ctypes.c_size_t,
            byte_ptr,
            byte_ptr,
            ctypes.c_size_t,
        ]
        self.library.acvd_cwc_encrypt_aad.argtypes = aad_args
        self.library.acvd_cwc_encrypt_aad.restype = ctypes.c_int
        self.library.acvd_cwc_decrypt_aad.argtypes = aad_args
        self.library.acvd_cwc_decrypt_aad.restype = ctypes.c_int

    @staticmethod
    def _buffer(value: bytes, *, minimum: int = 1):
        return (ctypes.c_ubyte * max(len(value), minimum))(*value)

    def encrypt(
        self, plaintext: bytes, *, iv: bytes | None = None, aad: bytes | None = None
    ) -> bytes:
        iv = os.urandom(self.IV_SIZE) if iv is None else bytes(iv)
        if len(iv) != self.IV_SIZE:
            raise CryptoError("AES-CWC IV must be 11 bytes")
        aad = iv if aad is None else bytes(aad)
        key_buf = self._buffer(self.key)
        iv_buf = self._buffer(iv)
        aad_buf = self._buffer(aad)
        input_buf = self._buffer(plaintext)
        output_buf = (ctypes.c_ubyte * max(len(plaintext), 1))()
        tag_buf = (ctypes.c_ubyte * self.TAG_SIZE)()
        result = self.library.acvd_cwc_encrypt_aad(
            key_buf,
            len(self.key),
            iv_buf,
            len(iv),
            aad_buf,
            len(aad),
            input_buf,
            len(plaintext),
            output_buf,
            tag_buf,
            self.TAG_SIZE,
        )
        if result != 0:
            raise CryptoError("AES-CWC encryption failed")
        return iv + bytes(tag_buf) + bytes(output_buf[: len(plaintext)])

    def decrypt(self, envelope: bytes, *, aad: bytes | None = None) -> bytes:
        if len(envelope) < self.IV_SIZE + self.TAG_SIZE + 1:
            raise CryptoError("AES-CWC envelope is too short")
        iv = envelope[: self.IV_SIZE]
        tag = envelope[self.IV_SIZE : self.IV_SIZE + self.TAG_SIZE]
        ciphertext = envelope[self.IV_SIZE + self.TAG_SIZE :]
        aad = iv if aad is None else bytes(aad)
        key_buf = self._buffer(self.key)
        iv_buf = self._buffer(iv)
        aad_buf = self._buffer(aad)
        input_buf = self._buffer(ciphertext)
        output_buf = (ctypes.c_ubyte * len(ciphertext))()
        tag_buf = self._buffer(tag)
        result = self.library.acvd_cwc_decrypt_aad(
            key_buf,
            len(self.key),
            iv_buf,
            len(iv),
            aad_buf,
            len(aad),
            input_buf,
            len(ciphertext),
            output_buf,
            tag_buf,
            len(tag),
        )
        if result != 0:
            raise CryptoError("AES-CWC authentication/decryption failed")
        return bytes(output_buf)
