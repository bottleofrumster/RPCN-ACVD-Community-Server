from __future__ import annotations

import argparse
import zlib
from pathlib import Path

from acvd_server.crypto import load_private_key, rsa_private_encrypt_x931
from acvd_server.regulation import (
    REGULATION_AES_KEY as REGULATION_AES_KEY,
    REGULATION_HMAC_KEY as REGULATION_HMAC_KEY,
    build_container,
)


def build(
    source: Path,
    private_key_path: Path,
    output: Path,
    encoding: str,
    strip_source_prefix: int = 0,
) -> None:
    source_data = source.read_bytes()
    if strip_source_prefix < 0 or strip_source_prefix > len(source_data):
        raise ValueError("source-prefix strip length is outside the source file")
    source_data = source_data[strip_source_prefix:]
    if encoding == "strip4":
        encoded = source_data[4:]
    elif encoding == "zlib":
        compressor = zlib.compressobj(level=9, wbits=13)
        encoded = b"\x01" + compressor.compress(source_data) + compressor.flush()
    elif encoding == "container":
        encoded = build_container(source_data, private_key_path)
    else:
        private_key = load_private_key(private_key_path)
        block_payload_size = (private_key.key_size + 7) // 8 - 3
        encoded = b"".join(
            rsa_private_encrypt_x931(private_key, source_data[offset : offset + block_payload_size])
            for offset in range(0, len(source_data), block_payload_size)
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(output)
    print(f"Encoded {len(source_data)} regulation bytes as {len(encoded)} CDN bytes")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an ACVD regulation CDN payload")
    parser.add_argument("source", type=Path)
    parser.add_argument("private_key", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--encoding",
        choices=("container", "strip4", "zlib", "x931"),
        default="container",
    )
    parser.add_argument(
        "--strip-source-prefix",
        type=int,
        default=0,
        help="remove this many bytes from the source before container encoding",
    )
    args = parser.parse_args()
    build(
        args.source.resolve(),
        args.private_key.resolve(),
        args.output.resolve(),
        args.encoding,
        args.strip_source_prefix,
    )


if __name__ == "__main__":
    main()
