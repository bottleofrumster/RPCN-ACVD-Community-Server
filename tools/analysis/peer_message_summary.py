"""Summarize complete observed game frames, not a full transport decoder.

Only the observed 13-byte-prefix framing on ports 6004/6006 is recognized.
Fragments, ACKs and other socket APIs are not reconstructed. Missing groups
therefore do not prove absent sends or failed delivery. Deflate output length
is checked; these observed frames omit an Adler trailer, so no checksum claim
is made. This tool never modifies inputs or contacts a client.
"""

import argparse
import hashlib
import json
import zlib
from collections import Counter, defaultdict
from pathlib import Path

MAX_BODY = 1 << 20


def complete_frame(raw):
    if len(raw) < 21:
        return None
    port = int.from_bytes(raw[:2], "little")
    kind = int.from_bytes(raw[13:17], "big")
    length = int.from_bytes(raw[17:21], "big")
    if port not in (6004, 6006) or length != len(raw) - 13:
        return None
    if (kind & ~0x8000) > 0x400:  # bounded observed type range
        return None
    if kind & 0x8000:
        if len(raw) < 27 or raw[25:27] != b"\x78\xda":
            return None
        expected = int.from_bytes(raw[21:25], "big")
        if expected > MAX_BODY:
            return None
        inflater = zlib.decompressobj(-15)
        try:
            body = inflater.decompress(raw[27:], expected + 1)
        except zlib.error:
            return None
        if (
            len(body) != expected
            or not inflater.eof
            or inflater.unconsumed_tail
            or inflater.unused_data
        ):
            return None
    else:
        body = raw[21:]
        if len(body) > MAX_BODY:
            return None
    return port, kind & ~0x8000, body


def summarize(path):
    groups = defaultdict(list)
    skipped = Counter()
    first = None
    with path.open() as stream:
        for line in stream:
            row = json.loads(line)
            ts = row["unix_seconds"] + row["nanoseconds"] / 1e9
            if first is None:
                first = ts
            raw = bytes.fromhex(row["hex"])
            if row["result"] != len(raw) or row["length"] != len(raw):
                skipped["unsuccessful_or_length_mismatch"] += 1
                continue
            frame = complete_frame(raw)
            if frame is None:
                skipped["unrecognized_or_incomplete"] += 1
                continue
            port, kind, body = frame
            groups[(port, kind)].append((round(ts - first, 6), body))
    return {
        "source": str(path),
        "time_origin": "first logged send in this process, not guest join",
        "limitations": "complete recognized sendto frames only; no delivery, reassembly or checksum proof",
        "skipped": dict(skipped),
        "groups": [
            {
                "port": port,
                "type": hex(kind),
                "count": len(rows),
                "seconds": [t for t, _ in rows],
                "body_lengths": sorted({len(body) for _, body in rows}),
                "body_sha256": sorted({hashlib.sha256(body).hexdigest() for _, body in rows}),
            }
            for (port, kind), rows in sorted(groups.items())
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps([summarize(path) for path in args.logs], indent=2))


if __name__ == "__main__":
    main()
