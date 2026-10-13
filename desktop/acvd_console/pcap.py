"""Bounded classic-PCAP IPv4/UDP reader. No capture privileges or live attachment."""

import struct
from datetime import datetime, timezone


def read_pcap(path, max_packets=1000):
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Use a capture window smaller than 64 MiB")
    events, skipped, total = [], 0, 0
    with path.open("rb") as stream:
        header = stream.read(24)
        if len(header) != 24:
            raise ValueError("Truncated PCAP header")
        formats = {
            b"\xd4\xc3\xb2\xa1": ("<", 1e6),
            b"\xa1\xb2\xc3\xd4": (">", 1e6),
            b"\x4d\x3c\xb2\xa1": ("<", 1e9),
            b"\xa1\xb2\x3c\x4d": (">", 1e9),
        }
        if header[:4] not in formats:
            raise ValueError(
                "Only classic PCAP supported; convert PCAPNG without changing packet data"
            )
        endian, resolution = formats[header[:4]]
        link = struct.unpack_from(endian + "I", header, 20)[0]
        if link not in (1, 113, 276, 101):
            raise ValueError(f"Unsupported link type {link}")
        while total < max_packets:
            record = stream.read(16)
            if not record:
                break
            if len(record) != 16:
                raise ValueError("Truncated packet header")
            seconds, fraction, length, original = struct.unpack(endian + "4I", record)
            if length > 1024 * 1024:
                raise ValueError("Oversized captured packet")
            data = stream.read(length)
            total += 1
            if len(data) != length:
                raise ValueError("Truncated packet data")
            offset = {1: 14, 113: 16, 276: 20, 101: 0}[link]
            if len(data) < offset + 20 or data[offset] >> 4 != 4:
                skipped += 1
                continue
            ihl = (data[offset] & 15) * 4
            if ihl < 20 or len(data) < offset + ihl + 8 or data[offset + 9] != 17:
                skipped += 1
                continue
            fragment = int.from_bytes(data[offset + 6 : offset + 8], "big")
            if fragment & 0x3FFF:
                skipped += 1
                continue
            start = offset + ihl
            source_port, dest_port, udp_length = struct.unpack_from(">HHH", data, start)
            if udp_length < 8:
                skipped += 1
                continue
            payload = data[start + 8 : min(start + udp_length, len(data))]
            source = ".".join(map(str, data[offset + 12 : offset + 16]))
            destination = ".".join(map(str, data[offset + 16 : offset + 20]))
            token_redacted = dest_port == 52103 and len(payload) >= 8
            if token_redacted:
                payload = bytes(8) + payload[8:]
            events.append(
                {
                    "event": "pcap_udp",
                    "timestamp_utc": datetime.fromtimestamp(
                        seconds + fraction / resolution, timezone.utc
                    ).isoformat(),
                    "source_endpoint": f"{source}:{source_port}",
                    "destination_endpoint": f"{destination}:{dest_port}",
                    "payload": {
                        "length": udp_length - 8,
                        "hex": payload.hex(),
                        "truncated": original != length or len(payload) != udp_length - 8,
                    },
                    "nxrv_signature": b"NXRV" in payload,
                    "known_game_token_redacted": token_redacted,
                    "meaning": "UDP payload only. Unknown application format; no decryption, reassembly, delivery or checksum claim.",
                }
            )
        capped = bool(stream.read(1))
    return events, {
        "packets_examined": total,
        "skipped_non_ipv4_udp_or_fragments": skipped,
        "limit_reached": capped,
        "link_type": link,
    }
