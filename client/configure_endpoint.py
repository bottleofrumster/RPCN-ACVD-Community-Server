"""Set the client kit's login and regulation IPv4 address. Keys are unchanged."""

import argparse
import ipaddress
import re
from pathlib import Path


def configure(text: str, host: str, port: int = 8080) -> str:
    host = str(ipaddress.IPv4Address(host))
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    address = host.encode("utf-16-be").ljust(32, b"\0")
    for offset in range(0, 32, 8):
        location = 0x01A8DE58 + offset
        pattern = rf"(\[\s*be64,\s*0x{location:08x},\s*)0x[0-9a-f]+"
        word = int.from_bytes(address[offset:offset + 8], "big")
        text, count = re.subn(pattern, lambda m: f"{m[1]}0x{word:016x}", text, flags=re.I)
        if count != 1:
            raise ValueError("Expected one reviewed login address block")
    url = f"http://{host}:{port}/regulation/contents_101.bin"
    text, count = re.subn(
        r'(\[\s*cutf8,\s*0x01a8e2b0,\s*)"[^"\n]*"',
        lambda m: f'{m[1]}"{url}"', text, flags=re.I,
    )
    if count != 1:
        raise ValueError("Expected one reviewed regulation URL")
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="Server IPv4 address supplied by the operator")
    parser.add_argument("--port", type=int, default=8080, help="Regulation HTTP port")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    patch = root / "client user patch files/RPCS3/patches/BLUS31194_patch.yml"
    try:
        updated = configure(patch.read_text(), args.host, args.port)
        patch.write_text(updated)
    except (OSError, ValueError) as error:
        parser.exit(2, f"Endpoint not configured: {error}\n")
    print("Client endpoint configured. Public keys are unchanged.")


if __name__ == "__main__":
    main()
