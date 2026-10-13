#!/usr/bin/env python3
"""Query and validate the evidence-ranked ACVD protocol code catalog.

Application and reliable-transport constants are derived from the production
UDP module so the reference cannot silently drift. Reviewed RPCN/NXR entries,
timing, and rejected interpretations live in protocol_catalog.json.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
UDP_SOURCE = PROJECT_ROOT / "src/acvd_server/udp.py"
# Keep the reviewed catalog at its existing operator/documentation path.
CATALOG_SOURCE = PROJECT_ROOT / "tools/protocol_catalog.json"
# Load only this installed/source tool's own pure data contract.
# Operator-selected repository contents remain text, never imported modules.
import sys

if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from acvd_server.diagnostic_records import (
    TRANSPORT_NAMES as TRANSPORT_NAMES,
    VALID_CONFIDENCE as VALID_CONFIDENCE,
    _hex_code as _hex_code,
    _summary_from_name as _summary_from_name,
    build_catalog,
    parse_udp_constants,
    validate_catalog as validate_catalog,
)


def udp_constants(path: Path = UDP_SOURCE) -> dict[str, int]:
    """Read constants as text through the shared safe parser."""
    return parse_udp_constants(path.read_text(encoding="utf-8"))


def load_catalog() -> dict[str, Any]:
    reviewed = json.loads(CATALOG_SOURCE.read_text(encoding="utf-8"))
    return build_catalog(udp_constants(), reviewed)


def filtered_entries(catalog: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    entries = catalog["entries"]
    if args.code:
        wanted = int(args.code, 0)
        entries = [entry for entry in entries if entry["value"] == wanted]
    if args.layer:
        entries = [entry for entry in entries if entry["layer"] == args.layer]
    if args.confidence:
        entries = [entry for entry in entries if entry["confidence"] == args.confidence]
    if args.search:
        needle = args.search.casefold()
        entries = [
            entry for entry in entries if needle in json.dumps(entry, sort_keys=True).casefold()
        ]
    return entries


def _print_entries(entries: list[dict[str, Any]]) -> None:
    if not entries:
        print("No matching protocol codes.")
        return
    for entry in entries:
        timing = entry.get("timing", "")
        print(f"{entry['layer']:20} {entry['code']:8} {entry['name']:42} {entry['confidence']}")
        print(f"  {entry['summary']}")
        if timing:
            print(f"  timing: {timing}")
        if entry.get("payload"):
            print(f"  payload: {entry['payload']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", help="integer code, for example 0x03D4 or 0xf222")
    parser.add_argument("--layer")
    parser.add_argument("--confidence", choices=sorted(VALID_CONFIDENCE))
    parser.add_argument("--search")
    parser.add_argument(
        "--rejected",
        action="store_true",
        help="show rejected interpretations instead of active entries",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--validate", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    catalog = load_catalog()
    if args.validate:
        print(
            f"valid: {len(catalog['entries'])} codes, "
            f"{len(catalog['rejected_interpretations'])} rejected interpretations"
        )
        return 0
    if args.rejected:
        result = catalog["rejected_interpretations"]
        if args.code:
            wanted = args.code.casefold()
            result = [item for item in result if item["code"].casefold() == wanted]
        if args.search:
            needle = args.search.casefold()
            result = [
                item for item in result if needle in json.dumps(item, sort_keys=True).casefold()
            ]
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            for item in result:
                print(f"{item['code']:20} {item['claim']}")
                print(f"  {item['result']}")
                print(f"  evidence: {item['evidence']}")
        return 0

    entries = filtered_entries(catalog, args)
    if args.json:
        print(json.dumps(entries, indent=2, sort_keys=True))
    else:
        _print_entries(entries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
