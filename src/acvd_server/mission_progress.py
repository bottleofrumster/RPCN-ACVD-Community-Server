"""Recover shop-level arithmetic from mission data, without assigning authority.

The caller must verify the snapshot, parameter versions, extra points and
hardcore mode. This module does not grant rank or write player data.
Guest functions 0x002b93d0, 0x002b8fc8, 0x001cdf1c and 0x004bc66c define
record merging, mission points, rate adjustment and level thresholds.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from .parameter_tables import read_rows

SNAPSHOT_BYTES = 0x4A0
MISSION_COUNT = 96
MISSION_OFFSET = 0x20
MISSION_STRIDE = 12


@dataclass(frozen=True, slots=True)
class MissionRecord:
    mission_id: int
    rank: int
    subquest_bits: int
    extra_points: int


def mission_records(snapshot: bytes) -> tuple[MissionRecord, ...]:
    """Decode and merge the 96 native slots, including uncleared rank -1."""
    if not snapshot or snapshot[0] != 1 or len(snapshot) > 65536:
        raise ValueError("Unsupported mission snapshot envelope")
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(snapshot[1:], SNAPSHOT_BYTES + 1)
    except zlib.error as exc:
        raise ValueError("Invalid mission compression") from exc
    if (
        len(raw) != SNAPSHOT_BYTES
        or not decoder.eof
        or decoder.unconsumed_tail
        or decoder.unused_data
    ):
        raise ValueError("Mission snapshot has the wrong expanded size")
    merged = {}
    for index in range(MISSION_COUNT):
        offset = MISSION_OFFSET + index * MISSION_STRIDE
        mission_id, rank = struct.unpack_from(">hb", raw, offset)
        if mission_id < 0:
            continue
        bits = int.from_bytes(raw[offset + 6 : offset + 8], "big")
        extra = int.from_bytes(raw[offset + 8 : offset + 12], "big")
        if not -1 <= rank <= 6:
            raise ValueError("Unknown mission-rank value")
        old = merged.get(mission_id)
        # Retail takes the highest rank and ORs subquest bits. Extra points
        # come from the last duplicate record, not their sum or maximum.
        merged[mission_id] = MissionRecord(
            mission_id,
            max(rank, old.rank) if old else rank,
            bits | old.subquest_bits if old else bits,
            extra,
        )
    return tuple(merged.values())


def mission_points(records: tuple[MissionRecord, ...], menu: bytes) -> int:
    """Add base, cumulative rank awards and saved per-mission extra points."""
    rows = read_rows(menu, b"MISSION_MENU_DATA_PARAM", 0x56)
    total = 0
    for record in records:
        if record.rank < 0:
            continue
        row = rows.get(record.mission_id)
        # The retail getters return zero for a missing mission parameter.
        # The saved extra-point term still participates in that native path.
        base = int.from_bytes(row[0x32:0x34], "big") if row else 0
        awards = struct.unpack_from(">7H", row, 0x48) if row else (0,) * 7
        total += base + sum(awards[: record.rank + 1]) + record.extra_points
    if not 0 <= total <= 0x7FFFFFFF:
        raise ValueError("Mission points exceed the supported signed range")
    return total


@dataclass(frozen=True, slots=True)
class ShopLevelResult:
    mission_points: int
    additional_points: int
    rate_percent: int
    adjusted_points: int
    level: int


def _float32(value: float) -> float:
    return struct.unpack(">f", struct.pack(">f", value))[0]


def medal_points(eligible_medal_ids: tuple[int, ...], medal_table: bytes) -> int:
    """Sum parameter awards for an already-filtered, authoritative medal list.

    Retail 0x001c9e00 applies its validity predicate, then sums the signed
    PlayerRatingBonus at MedalParam row+0x18 through 0x00267708, not the
    separate MedalPoint at+0x0c. The result reaches the
    score provider through 0x001bdb90. This reader does not infer date validity
    or grant medals. An unknown collection must not be supplied as an empty one.
    """
    if (
        not isinstance(eligible_medal_ids, tuple)
        or len(eligible_medal_ids) > 48
        or any(
            type(value) is not int or not 1 <= value <= 0xFFFFFFFF for value in eligible_medal_ids
        )
        or len(set(eligible_medal_ids)) != len(eligible_medal_ids)
    ):
        raise ValueError("Supply at most 48 unique, verified medal IDs")
    # The full v1.02 row is32 bytes; this prefix includes the rating bonus
    # and deliberately excludes the unrelated owned-caption ID at+0x1c.
    rows = read_rows(medal_table, b"MEDAL_PARAM_ST", 0x1C)
    total = 0
    for medal_id in eligible_medal_ids:
        row = rows.get(medal_id)
        if row is None:
            # Retail has a separate fallback row. Its value is not qualified.
            raise ValueError("Medal has no verified parameter row")
        points = struct.unpack_from(">i", row, 0x18)[0]
        if points < 0:
            raise ValueError("Negative medal awards need separate review")
        total += points
    if total > 0x7FFFFFFF:
        raise ValueError("Medal points exceed the supported signed range")
    return total


def hardcore_rate(rate_table: bytes, selector: int) -> int:
    """Read the explicit HardcoreModeParam selector's signed percentage.

    Parameter slot 0x5e is named in system/paramlist.xml. Getter 0x009d9b7c
    reads row+0x14. Context types 0/1 select row 0; type 2 uses its context.
    Do not select a context type here or use an unverified fallback row.
    """
    if type(selector) is not int or not 0 <= selector <= 0x7FFFFFFF:
        raise ValueError("Supply a verified, non-negative rate selector")
    rows = read_rows(rate_table, b"HARDCORE_MODE_PARAM_ST", 0x16)
    row = rows.get(selector)
    if row is None:
        raise ValueError("Rate selector has no verified parameter row")
    return struct.unpack_from(">h", row, 0x14)[0]


def level_thresholds(data: bytes) -> tuple[int, ...]:
    """Validate the same threshold sequence for startup and live calculation."""
    rows = read_rows(data, b"PLAYER_EXP2RATE_VALUE_LV", 4)
    if not rows or set(rows) != set(range(len(rows))):
        raise ValueError("Level thresholds must be contiguous from zero")
    thresholds = tuple(int.from_bytes(rows[level], "big") for level in range(len(rows)))
    if thresholds[0] != 0 or any(a > b for a, b in zip(thresholds, thresholds[1:])):
        raise ValueError("Level thresholds are not ordered from zero")
    return thresholds


def calculate_shop_level(
    snapshot: bytes,
    mission_menu: bytes,
    level_table: bytes,
    *,
    additional_points: int,
    rate_percent: int,
) -> ShopLevelResult:
    """Calculate with explicit, verified external terms; never default them."""
    if type(additional_points) is not int or not 0 <= additional_points <= 0x7FFFFFFF:
        raise ValueError("Additional points must be verified non-negative integers")
    if type(rate_percent) is not int or not -32768 <= rate_percent <= 32767:
        raise ValueError("Rate must fit the native signed 16-bit field")
    points = mission_points(mission_records(snapshot), mission_menu)
    total = points + additional_points
    if total > 0x7FFFFFFF:
        raise ValueError("Combined points exceed the supported signed range")
    rate = _float32(_float32(rate_percent) / 100.0) if rate_percent > 0 else 0.0
    adjusted = int(_float32(_float32(total) * rate))
    if adjusted > 0x7FFFFFFF:
        raise ValueError("Adjusted points exceed the supported signed range")
    thresholds = level_thresholds(level_table)
    level = 0
    for index, threshold in enumerate(thresholds[1:], 1):
        if adjusted < threshold:
            break
        level = index
    return ShopLevelResult(points, additional_points, rate_percent, adjusted, level)
