"""Read ACVD v1.02 catalogue inputs without inventing prices or unlock levels.

These functions read data only. They do not grant ownership or determine a
player's rank. The caller must select the correct regulation and obtain the
player's level from verified progression data before offering parts.

The part-bank layout is recovered from guest code 0x004857c8..0x0048602c.
See evidence/2026-09-13-shop-paid-native.json for provenance and limits.
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass

from .parameter_tables import MAX_PARAMETER_BYTES, MAX_ROWS, bounded_data as _bounded, read_rows
from .protocol import proto_bytes, proto_varint

PART_HEADER_SIZE = 0x34
LINEUP_TYPE = b"PARTS_SHOP_LINEUP"

# Category order is the file's bank order. Counts are BE uint16 at 4 + 2*index.
# Empty banks 7..9 are aliases in the retail address builder. Reject non-empty
# forms until their separate layout is qualified. No current input uses them.
PART_STRIDES = (
    0x144,
    0x144,
    0x148,
    0x174,
    0x148,
    0x144,
    0x150,
    0x148,
    0x148,
    0x16C,
    0x1A0,
    0x1AC,
    0x1C4,
    0x134,
)
SHOP_CATEGORIES = frozenset((*range(7), 10, 11, 12, 13))


@dataclass(frozen=True, slots=True)
class CataloguePart:
    lineup_id: int
    minimum_level: int
    category: int
    part_id: int
    price: int

    def __post_init__(self) -> None:
        bounds = (
            (self.lineup_id, 0, 0xFFFFFFFF),
            (self.minimum_level, 0, 0xFFFF),
            (self.part_id, 1, 0xFFFF),
            (self.price, 0, 0xFFFFFFFF),
        )
        if any(
            type(value) is not int or not lower <= value <= upper for value, lower, upper in bounds
        ):
            raise ValueError("Catalogue fields must fit their integer domains")
        if type(self.category) is not int or self.category not in SHOP_CATEGORIES:
            raise ValueError("Unsupported catalogue category")


def parts_from_regulation_cache(data: bytes) -> bytes:
    """Extract acvparts.bin from a saved REGU.DAT or a clear BND3 archive.

    This checks structure, compression and lengths, not CDN authentication.
    Never treat a successful parse as proof that an input came from the server.
    Names are compared in memory; no archive entry is written to the filesystem.
    """
    found = regulation_file(data, b"acvparts.bin", minimum_size=PART_HEADER_SIZE)
    if found is None:
        raise ValueError("No acvparts.bin in this regulation archive")
    return found


def _regulation_entries(data: bytes) -> tuple[bytes, list[tuple[bytes, int, int, int, int]]]:
    """Normalize a cache/BND and validate its bounded entry table, without inflation."""
    _bounded(data, 0x20)
    if data[:4] != b"BND3":
        size = int.from_bytes(data[:4], "big")
        if data[4:8] != b"BND3" or not 0x20 <= size <= len(data) - 4:
            raise ValueError("Invalid REGU cache prefix")
        data = data[4 : 4 + size]
    if data[12] not in (0x24, 0x26) or data[13:16] != b"\x01\x01\x00":
        raise ValueError("Unsupported BND3 format or byte order")
    has_ids = bool(data[12] & 2)
    stride = 24 if has_ids else 20
    count = int.from_bytes(data[16:20], "big")
    header_end = 0x20 + count * stride
    if count > 256 or header_end > len(data):
        raise ValueError("Invalid BND3 entry count")
    entries = []
    for index in range(count):
        start = 0x20 + index * stride
        flags, compressed_size, offset = struct.unpack_from(">III", data, start)
        name_offset, expanded_size = struct.unpack_from(
            ">II", data, start + (16 if has_ids else 12)
        )
        if not header_end <= name_offset < len(data):
            raise ValueError("BND3 name offset is outside the archive")
        end = data.find(b"\0", name_offset, min(len(data), name_offset + 512))
        if end < 0:
            raise ValueError("BND3 name has no bounded terminator")
        if flags not in (0x02000000, 0x03000000):
            raise ValueError("Unsupported part entry flags")
        if not header_end <= offset <= len(data) or compressed_size > len(data) - offset:
            raise ValueError("Part entry extends outside the archive")
        if not 1 <= expanded_size <= MAX_PARAMETER_BYTES:
            raise ValueError("Expanded part entry is outside the supported bounds")
        entries.append((data[name_offset:end], flags, compressed_size, offset, expanded_size))
    return data, entries


def regulation_archive_size(data: bytes) -> int:
    """Return the BND extent, excluding only zero padding after the last entry.

    Do not strip trailing zeros from a file: a real entry may end in zero bytes.
    The checksum used by existing retail caches stops at the last encoded entry.
    """
    if data[:4] != b"BND3":
        raise ValueError("Expected a clear BND3 regulation archive")
    data, entries = _regulation_entries(data)
    if not entries:
        raise ValueError("Empty regulation archive")
    size = max(offset + compressed_size for _, _, compressed_size, offset, _ in entries)
    if any(data[size:]):
        raise ValueError("Nonzero data after the last regulation entry")
    return size


def regulation_file(data: bytes, name: bytes, *, minimum_size: int = 4) -> bytes | None:
    """Read one bounded BND3 entry. None means absent, never malformed.

    Callers must authenticate the outer regulation before using it as server
    authority. This parser does not extract files to disk.
    """
    if not isinstance(name, bytes) or not 1 <= len(name) <= 128 or b"\0" in name:
        raise ValueError("Invalid regulation entry name")
    if type(minimum_size) is not int or not 1 <= minimum_size <= MAX_PARAMETER_BYTES:
        raise ValueError("Invalid regulation entry size bound")
    data, entries = _regulation_entries(data)
    found = None
    for entry_name, flags, compressed_size, offset, expanded_size in entries:
        if entry_name.lower() != name.lower():
            continue
        if found is not None:
            raise ValueError("Duplicate regulation entries")
        if expanded_size < minimum_size:
            raise ValueError("Expanded part entry is outside the supported bounds")
        body = data[offset : offset + compressed_size]
        if flags == 0x03000000:
            decoder = zlib.decompressobj()
            try:
                body = decoder.decompress(body, expanded_size + 1)
            except zlib.error as exc:
                raise ValueError("Invalid compressed part entry") from exc
            if not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
                raise ValueError("Part compression length does not match its entry")
        if len(body) != expanded_size:
            raise ValueError("Expanded part length does not match its header")
        found = body
    return found


def sale_multiplier(system_parameter: bytes) -> float:
    """Read ServerSystemParam+0, used by native GetPrice in sale context4."""
    _bounded(system_parameter, 4)
    value = struct.unpack_from(">f", system_parameter)[0]
    # Conservative service policy: do not enable a negative or inflationary
    # sell rate. The native stock rate is0.5, not a constant in our encoder.
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Sale multiplier is outside the supported range")
    return value


def sale_price(base_price: int, multiplier: float) -> int:
    """Match the client's single-precision multiply and truncate to int32.

    GetPrice0x00446ab0 loads acvparts price+4. In sale context4 it obtains
    ServerSystemParam through0x001faa5c(index3), reads float+0, then performs
    frsp/fmuls/fctiwz. Tuning does not enter this calculation.
    """
    if type(base_price) is not int or not 0 <= base_price <= 0x7FFFFFFF:
        raise ValueError("Sale base price must be a non-negative int32")
    if (
        not isinstance(multiplier, float)
        or not math.isfinite(multiplier)
        or not 0 <= multiplier <= 1
    ):
        raise ValueError("Sale multiplier is outside the supported range")

    def single(value):
        return struct.unpack(">f", struct.pack(">f", value))[0]

    value = single(single(base_price) * single(multiplier))
    if not 0 <= value < 1 << 31:
        raise ValueError("Sale price exceeds the native int32 result")
    return int(value)


def part_prices(data: bytes) -> dict[tuple[int, int], int]:
    """Read IDs and prices from supported part banks; keep zero prices exact."""
    return {key: struct.unpack_from(">I", body, 4)[0] for key, body in _part_rows(data)}


def part_tuning_budgets(data: bytes) -> dict[tuple[int, int], int]:
    """Use each part's native growth flag, never its broad class alone.

    Scr_IsGrowEnable0x00429614 reads parameter byte+0x0d. The allocation UI
    at0x005db608 computes remaining points as3 minus the three allocations.
    """
    result = {}
    for key, body in _part_rows(data):
        flag = body[0x0D]
        if flag not in (0, 1):
            raise ValueError("Unsupported part growth flag")
        result[key] = 3 if flag else 0
    return result


def _part_rows(data: bytes):
    """Share bank bounds and identity checks for price and growth readers."""
    _bounded(data, PART_HEADER_SIZE)
    counts = struct.unpack_from(">24H", data, 4)
    if any(counts[index] for index in range(24) if index not in SHOP_CATEGORIES):
        raise ValueError("A non-empty unsupported part bank needs review")
    seen = set()
    offset = PART_HEADER_SIZE
    for category, stride in enumerate(PART_STRIDES):
        count = counts[category]
        if count > MAX_ROWS or count * stride > len(data) - offset:
            raise ValueError("Part bank exceeds its input bounds")
        for index in range(count):
            body = data[offset + index * stride : offset + (index + 1) * stride]
            part_id = struct.unpack_from(">H", body)[0]
            key = (category, part_id)
            if part_id == 0 or key in seen:
                raise ValueError("Zero or duplicate part identity")
            seen.add(key)
            yield key, body
        offset += count * stride
    # Current files contain a trailing region. It is not another price bank.
    # Do not truncate it or claim that this reader describes the entire format.


def shop_lineup(data: bytes, prices: dict[tuple[int, int], int]) -> tuple[CataloguePart, ...]:
    """Join eight-byte level/category/part rows to exact regulation prices."""
    result = []
    for row_id, body in read_rows(data, LINEUP_TYPE, 8).items():
        level, category, part_id = struct.unpack_from(">Hb x H", body)
        key = (category, part_id)
        if category not in SHOP_CATEGORIES or key not in prices:
            raise ValueError("Lineup part has no supported regulation price")
        result.append(CataloguePart(row_id, level, category, part_id, prices[key]))
    return tuple(result)


def eligible_parts(entries: tuple[CataloguePart, ...], level: int) -> tuple[CataloguePart, ...]:
    """Filter by an already-verified level; do not infer it from displayed rank."""
    if type(level) is not int or not 0 <= level <= 0xFFFF:
        raise ValueError("A verified non-negative shop level is required")
    return tuple(entry for entry in entries if entry.minimum_level <= level)


def catalogue_rows(
    entries: tuple[CataloguePart, ...],
    *,
    level: int,
    category: int,
    offset: int,
    limit: int,
) -> tuple[CataloguePart, ...]:
    """Select one page once for both its wire reply and its offered-item set."""
    if type(category) is not int or category not in SHOP_CATEGORIES:
        raise ValueError("Unsupported shop category")
    for value, lower, upper in ((offset, 0, 0xFFFFFFFF), (limit, 0, 512)):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError("Invalid catalogue page bounds or shop key")
    filtered = [row for row in eligible_parts(entries, level) if row.category == category]
    return tuple(filtered[offset : offset + limit])


def encode_catalogue_rows(rows: tuple[CataloguePart, ...], shop_key: int) -> bytes:
    """Encode listing keys and prices, not owned-part instance IDs."""
    if type(shop_key) is not int or not 1 <= shop_key <= 0xFFFFFFFF:
        raise ValueError("Invalid catalogue shop key")
    return b"".join(
        proto_bytes(
            1,
            b"".join(
                proto_varint(number, value)
                for number, value in (
                    (1, shop_key),
                    (2, row.lineup_id),
                    (3, row.category),
                    (4, row.part_id),
                    (5, row.price),
                )
            ),
        )
        for row in rows
    )


def catalogue_page(
    entries: tuple[CataloguePart, ...],
    *,
    level: int,
    category: int,
    offset: int,
    limit: int,
    shop_key: int,
) -> tuple[bytes, int]:
    """Preserve the existing page API while sharing its exact row selection."""
    rows = catalogue_rows(entries, level=level, category=category, offset=offset, limit=limit)
    return encode_catalogue_rows(rows, shop_key), len(rows)
