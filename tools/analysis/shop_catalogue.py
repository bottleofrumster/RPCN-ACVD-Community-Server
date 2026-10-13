"""Audit catalogue data from a saved cache or a trusted server CDN payload."""

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from cryptography.hazmat.primitives import serialization

    from acvd_server.mission_progress import calculate_shop_level
    from acvd_server.regulation import read_container
    from acvd_server.shop_catalogue import (
        MAX_PARAMETER_BYTES,
        eligible_parts,
        part_prices,
        parts_from_regulation_cache,
        shop_lineup,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("regu", type=Path, help="REGU.DAT, or contents_101.bin with --public-key")
    parser.add_argument("lineup", type=Path, help="Owned param/partsshoplineup.bin")
    parser.add_argument(
        "--public-key", type=Path, help="Trusted server public PEM; authenticate the CDN input"
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--level", type=int, help="Filter by a verified numeric level")
    source.add_argument(
        "--mission-snapshot",
        type=Path,
        help="Compute from a preserved mission snapshot; requires explicit extra points and rate",
    )
    parser.add_argument("--additional-points", type=int)
    parser.add_argument("--rate-percent", type=int)
    args = parser.parse_args()
    if args.mission_snapshot and (args.additional_points is None or args.rate_percent is None):
        parser.error("--mission-snapshot requires --additional-points and --rate-percent")
    if not args.mission_snapshot and (
        args.additional_points is not None or args.rate_percent is not None
    ):
        parser.error("Point and rate inputs require --mission-snapshot")
    with args.regu.open("rb") as source:
        input_data = source.read(MAX_PARAMETER_BYTES + (257 if args.public_key else 1))
    source_hash = hashlib.sha256(input_data).hexdigest()
    public_key_hash = None
    if args.public_key:
        with args.public_key.open("rb") as source:
            public_key_data = source.read(16385)
        if len(public_key_data) > 16384:
            raise ValueError("Public key file exceeds the supported bound")
        regu = read_container(input_data, serialization.load_pem_public_key(public_key_data))
        public_key_hash = hashlib.sha256(public_key_data).hexdigest()
    else:
        regu = input_data
    with args.lineup.open("rb") as source:
        lineup = source.read(MAX_PARAMETER_BYTES + 1)
    prices = part_prices(parts_from_regulation_cache(regu))
    entries = shop_lineup(lineup, prices)
    calculation = None
    input_hashes = {}
    level = args.level
    if args.mission_snapshot:
        paths = {
            "mission_snapshot": args.mission_snapshot,
            "mission_menu": args.lineup.parent / "missionmenudataparam.bin",
            "level_table": args.lineup.parent / "playerexp2ratevaluelv.bin",
        }
        inputs = {}
        for name, path in paths.items():
            with path.open("rb") as stream:
                inputs[name] = stream.read(MAX_PARAMETER_BYTES + 1)
            input_hashes[name] = hashlib.sha256(inputs[name]).hexdigest()
        calculation = calculate_shop_level(
            inputs["mission_snapshot"],
            inputs["mission_menu"],
            inputs["level_table"],
            additional_points=args.additional_points,
            rate_percent=args.rate_percent,
        )
        level = calculation.level
    selected = entries if level is None else eligible_parts(entries, level)
    print(
        json.dumps(
            {
                "scope": "Data audit, not player eligibility",
                "regulation_source": "authenticated_cdn" if args.public_key else "unverified_cache",
                "regulation_source_sha256": source_hash,
                "public_key_sha256": public_key_hash,
                "regu_sha256": hashlib.sha256(regu).hexdigest(),
                "lineup_sha256": hashlib.sha256(lineup).hexdigest(),
                "price_records": len(prices),
                "lineup_rows": len(entries),
                "selected_rows": len(selected),
                "level": level,
                "calculation": asdict(calculation) if calculation is not None else None,
                "calculation_input_hashes": input_hashes,
                "parts": [asdict(entry) for entry in selected],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
