"""Authenticate the stored regulation and build native two-stage downloads.

The RSA/X9.31 header supplies the AES-CBC IV, original byte count and HMAC.
These legacy algorithms and shared keys are required by the retail protocol;
they are not a recommendation for a new protocol. A caller must supply the
trusted server public key, not a key obtained from the envelope being checked.
No archive names are opened and no player data is read or changed here.
Existing stored seeds use a legacy big-endian length; native download header
integers are little-endian. Keep these contracts explicit, without rewriting
the stored seed or weakening signature/checksum validation.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .crypto import CryptoError, load_private_key, rsa_private_encrypt_x931
from .shop_catalogue import regulation_archive_size, regulation_file

REGULATION_AES_KEY = bytes.fromhex("739c12a4f1a252662850ebb02ddd3402")
REGULATION_HMAC_KEY = bytes.fromhex("9b0e703f14bbda7f2b63efb7b71fa5192bb7727a")
HEADER_BYTES = 0x70
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
MANIFEST_PATH = "/regulation/contents_101.bin"
REGULATION_PATH = "/regulation/regulation.bnd"
DOWNLOAD_PATHS = (MANIFEST_PATH, REGULATION_PATH)
NATIVE_CACHE_BYTES = 256_000


@dataclass(frozen=True)
class RegulationData:
    """The HTTP bytes and check identity derived from one authenticated container."""

    container: bytes
    version: int
    digests: tuple[bytes, ...]
    sha256: str
    payload: bytes


class RegulationSource:
    """Share one verified source between the CDN and CHECK_REGULATION_DATA.

    Re-read on access so removal or replacement cannot leave a stale check
    identity. Cache verification by exact bytes, not mtime; a failed read never
    falls back to the cached file. No player state or private key is required.
    """

    def __init__(self, path: Path, public_key: rsa.RSAPublicKey) -> None:
        self.path = path
        self.public_key = public_key
        self._cached: RegulationData | None = None

    def load(self) -> RegulationData:
        with self.path.open("rb") as stream:
            encoded = stream.read(MAX_PAYLOAD_BYTES + 257)
        if self._cached is not None and self._cached.container == encoded:
            return self._cached
        payload = read_container(encoded, self.public_key)
        size = regulation_archive_size(payload)
        system = regulation_file(payload, b"serversystemparam.bin", minimum_size=0x70)
        if system is None:
            raise ValueError("Regulation has no ServerSystemParam version")
        # Native getter 1f36f4 and request builder 1f4018 use this same word.
        version = int.from_bytes(system[0x6C:0x70], "big")
        # Existing REGU caches hash the BND extent; the download callback copies
        # the complete decoded stream. Accept these two representations only,
        # after proving the difference is exclusively archive padding.
        digests = tuple(dict.fromkeys(
            hashlib.sha1(body).digest() for body in (payload[:size], payload)
        ))
        self._cached = RegulationData(
            encoded, version, digests, hashlib.sha256(encoded).hexdigest(), payload
        )
        return self._cached


def install_for_host(
    source: Path, trusted_public_key: Path, host_private_key: Path, destination: Path
) -> bool:
    """Verify a distributed container, then sign its unchanged payload for a new host.

    Never overwrite existing regulation. The trusted key comes from the reviewed
    distribution, not from the signed envelope. Private keys stay on each host.
    """
    if destination.exists() or destination.is_symlink():
        return False
    key = serialization.load_pem_public_key(trusted_public_key.read_bytes())
    payload = read_container(source.read_bytes(), key)
    encoded = build_container(payload, host_private_key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".regulation-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return False
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def build_container(source_data: bytes, private_key_path: Path) -> bytes:
    """Build the legacy stored-seed format; not a native HTTP response."""
    return _build_container(source_data, load_private_key(private_key_path), ">")


def _build_container(source_data: bytes, private_key: rsa.RSAPrivateKey, order: str) -> bytes:
    iv = os.urandom(16)
    digest = hmac.new(REGULATION_HMAC_KEY, source_data, hashlib.sha1).digest()
    header = struct.pack(f"{order}IIQ", 0, 0, len(source_data)) + iv + bytes(16) + digest + bytes(44)
    padded = source_data + bytes((-len(source_data)) % 16)
    encryptor = Cipher(algorithms.AES(REGULATION_AES_KEY), modes.CBC(iv)).encryptor()
    encrypted_body = encryptor.update(padded) + encryptor.finalize()
    return rsa_private_encrypt_x931(private_key, header) + encrypted_body


def _verified_header(signature: bytes, key: rsa.RSAPublicKey) -> bytes:
    numbers = key.public_numbers()
    value = int.from_bytes(signature, "big")
    if value >= numbers.n:
        raise CryptoError("Regulation signature is outside the RSA modulus")
    decoded = pow(value, numbers.e, numbers.n)
    # X9.31 permits the complementary signature representative. Accept it
    # only when the resulting complete padding block has the required form.
    prefix = b"\x6b" + b"\xbb" * (len(signature) - HEADER_BYTES - 3) + b"\xba"
    for candidate in (decoded, numbers.n - decoded):
        block = candidate.to_bytes(len(signature), "big")
        if block.startswith(prefix) and block[-1:] == b"\xcc":
            return block[len(prefix) : -1]
    raise CryptoError("Regulation RSA/X9.31 header did not verify")


def read_container(
    data: bytes, public_key: rsa.RSAPublicKey, *, byte_order: str = "big"
) -> bytes:
    """Authenticate a stored seed, or a native download with byte_order='little'.

    The output excludes AES padding but retains every authenticated source
    byte, including padding already present inside the original BND archive.
    Parsing a BND or a saved REGU file alone does not provide this verification.
    """
    if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size != 2048:
        raise CryptoError("Regulation requires the trusted 2048-bit RSA public key")
    signature_bytes = public_key.key_size // 8
    if not signature_bytes <= len(data) <= signature_bytes + MAX_PAYLOAD_BYTES:
        raise CryptoError("Regulation container is outside the supported size range")
    header = _verified_header(data[:signature_bytes], public_key)
    if header[:8] != bytes(8) or header[32:48] != bytes(16) or header[68:] != bytes(44):
        raise CryptoError("Regulation header uses unsupported reserved fields")
    size = int.from_bytes(header[8:16], byte_order)
    if size > MAX_PAYLOAD_BYTES:
        raise CryptoError("Regulation source length exceeds the supported bound")
    ciphertext = data[signature_bytes:]
    if len(ciphertext) != (size + 15) // 16 * 16:
        raise CryptoError("Regulation ciphertext length does not match its header")
    decryptor = Cipher(algorithms.AES(REGULATION_AES_KEY), modes.CBC(header[16:32])).decryptor()
    clear = decryptor.update(ciphertext) + decryptor.finalize()
    payload = clear[:size]
    if any(clear[size:]):
        raise CryptoError("Regulation AES padding is not zero")
    digest = hmac.new(REGULATION_HMAC_KEY, payload, hashlib.sha1).digest()
    if not hmac.compare_digest(digest, header[48:68]):
        raise CryptoError("Regulation payload authentication failed")
    return payload


class RegulationDownloads:
    """One authenticated source owns the manifest, body and compatibility check.

    Native contents_101.bin is a key=value manifest. Its listed regulation.bnd
    is separately authenticated, then copied into the game's regulation cache.
    Generated responses are cached by the source's exact-byte identity. Every
    request rechecks the source, so removal/corruption cannot serve stale data.
    """

    def __init__(self, source: RegulationSource, key: rsa.RSAPrivateKey, host: str, port: int):
        self.source = source
        self.key = key
        if (
            not isinstance(host, str) or not host or not host.isascii()
            or any(not (c.isalnum() or c in ".-:") for c in host)
            or type(port) is not int or not 1 <= port <= 65535
        ):
            raise ValueError("Regulation download requires a configured HTTP authority")
        authority = f"[{host}]" if ":" in host else host
        self.body_url = f"http://{authority}:{port}{REGULATION_PATH}"
        self._current: RegulationData | None = None
        self._files: dict[str, bytes] = {}

    def get(self, path: str) -> bytes:
        if path not in DOWNLOAD_PATHS:
            raise ValueError("Unsupported regulation download path")
        current = self.source.load()
        if current is not self._current:
            payload = current.payload
            if len(payload) > NATIVE_CACHE_BYTES:
                raise ValueError("Regulation exceeds the native cache capacity")
            digest = hmac.new(REGULATION_HMAC_KEY, payload, hashlib.sha1).hexdigest().upper()
            manifest = (
                "Patch.List.Count=1\n"
                f"Patch.List.File0.DIGEST={digest}\n"
                "Patch.List.File0.Dir=\n"
                "Patch.List.File0.Name=regulation.bnd\n"
                f"Patch.List.File0.SizeOrg={len(payload)}\n"
                f"Patch.List.File0.Path={self.body_url}\n"
            ).encode("ascii")
            files = {
                MANIFEST_PATH: _build_container(manifest, self.key, "<"),
                REGULATION_PATH: _build_container(payload, self.key, "<"),
            }
            self._files, self._current = files, current
        return self._files[path]
