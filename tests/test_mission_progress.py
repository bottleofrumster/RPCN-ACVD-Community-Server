"""Mission score arithmetic, without guessing live extra points or mode rates."""

import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from acvd_server.mission_progress import (
    calculate_shop_level,
    hardcore_rate,
    medal_points,
    mission_records,
)
from acvd_server.parameter_tables import read_rows


def parameter(kind, rows):
    header = bytearray(0x30 + 12 * len(rows))
    header[12 : 12 + len(kind)] = kind
    header[44:48] = b"\xff\x02\x00\xff"
    struct.pack_into(">H", header, 10, len(rows))
    body = bytearray()
    for index, (key, row) in enumerate(rows):
        struct.pack_into(">III", header, 48 + index * 12, key, len(header) + len(body), 0)
        body.extend(row)
    return bytes(header + body)


def snapshot(rows):
    raw = bytearray(0x4A0)
    for index in range(96):
        struct.pack_into(">hb", raw, 32 + index * 12, -1, -1)
    for index, (mission, rank, bits, extra) in enumerate(rows):
        struct.pack_into(">hbBBBHI", raw, 32 + index * 12, mission, rank, 0, 0, 0, bits, extra)
    return b"\x01" + zlib.compress(raw)


def menu():
    row = bytearray(0x60)
    struct.pack_into(">H", row, 0x32, 100)
    struct.pack_into(">7H", row, 0x48, 0, 0, 20, 30, 50, 50, 100)
    return parameter(b"MISSION_MENU_DATA_PARAM", [(110, row)])


def levels(values=(0, 500, 600)):
    return parameter(
        b"PLAYER_EXP2RATE_VALUE_LV", [(i, struct.pack(">I", v)) for i, v in enumerate(values)]
    )


def test_mission_score_has_base_cumulative_rank_and_extra_points():
    result = calculate_shop_level(
        snapshot([(110, 6, 0x3E, 250)]), menu(), levels(), additional_points=0, rate_percent=100
    )
    assert result.mission_points == 600 and result.adjusted_points == 600 and result.level == 2


def test_duplicate_rules_match_native_merge_not_double_credit():
    data = snapshot([(110, 6, 1, 250), (110, 2, 4, 50)])
    record = mission_records(data)[0]
    assert record.rank == 6 and record.subquest_bits == 5 and record.extra_points == 50
    result = calculate_shop_level(data, menu(), levels(), additional_points=0, rate_percent=100)
    assert result.mission_points == 400 and result.level == 0


def test_uncleared_slots_do_not_earn_points_and_missing_parameter_uses_native_zero_terms():
    result = calculate_shop_level(
        snapshot([(110, -1, 0, 999), (777, 0, 0, 50)]),
        menu(),
        levels(),
        additional_points=0,
        rate_percent=100,
    )
    assert result.mission_points == 50


def test_extra_points_and_rate_are_explicit_and_float32():
    result = calculate_shop_level(
        snapshot([]), menu(), levels(), additional_points=16777217, rate_percent=100
    )
    assert result.adjusted_points == 16777216
    assert (
        calculate_shop_level(
            snapshot([]), menu(), levels(), additional_points=400, rate_percent=150
        ).level
        == 2
    )
    assert (
        calculate_shop_level(
            snapshot([]), menu(), levels(), additional_points=600, rate_percent=0
        ).level
        == 0
    )


def medal_table(awards=((1, 100), (2, 250))):
    rows = []
    for key, points in awards:
        row = bytearray(0x20)
        struct.pack_into(">i", row, 0x18, points)
        rows.append((key, row))
    return parameter(b"MEDAL_PARAM_ST", rows)


def rates(values=((0, 100), (1, 300), (2, -1))):
    rows = []
    for key, rate in values:
        row = bytearray(0x20)
        struct.pack_into(">h", row, 0x14, rate)
        rows.append((key, row))
    return parameter(b"HARDCORE_MODE_PARAM_ST", rows)


def test_medal_sum_is_zero_only_for_an_explicit_empty_collection():
    assert medal_points((), medal_table()) == 0
    assert medal_points((1,), medal_table()) == 100
    assert medal_points((1, 2), medal_table()) == 350
    with pytest.raises(ValueError):
        medal_points(None, medal_table())


def test_medal_rank_bonus_is_not_medal_score_or_completion_threshold():
    # Native MedalParam row1: PeaceNum5, MedalPoint300, PlayerRatingBonus100.
    row = bytearray(32)
    struct.pack_into(">i", row, 8, 5)
    struct.pack_into(">i", row, 12, 300)
    struct.pack_into(">i", row, 24, 100)
    struct.pack_into(">i", row, 28, 97750)
    table = parameter(b"MEDAL_PARAM_ST", [(1, row)])
    assert medal_points((1,), table) == 100


@pytest.mark.parametrize("ids", [(0,), (True,), (-1,), (1 << 32,), (1, 1), tuple(range(1, 50)), [1]])
def test_medal_reader_rejects_ambiguous_or_invalid_collection(ids):
    with pytest.raises(ValueError):
        medal_points(ids, medal_table())


def test_medal_reader_does_not_guess_missing_fallback_or_allow_score_overflow():
    with pytest.raises(ValueError):
        medal_points((3,), medal_table())
    with pytest.raises(ValueError):
        medal_points((1,), medal_table(((1, -1),)))
    with pytest.raises(ValueError):
        medal_points((1, 2), medal_table(((1, 0x7FFFFFFF), (2, 1))))


def test_rate_selector_reads_signed_field_and_does_not_default():
    assert hardcore_rate(rates(), 0) == 100
    assert hardcore_rate(rates(), 1) == 300
    assert hardcore_rate(rates(), 2) == -1
    for selector in (None, True, -1, 3, 1 << 31):
        with pytest.raises(ValueError):
            hardcore_rate(rates(), selector)


def test_verified_medal_and_rate_inputs_change_the_level_not_ownership():
    result = calculate_shop_level(
        snapshot([(110, 6, 0, 0)]), menu(), levels(),
        additional_points=medal_points((2,), medal_table()),
        rate_percent=hardcore_rate(rates(), 0),
    )
    assert result.mission_points == 350
    assert result.additional_points == 250
    assert result.adjusted_points == 600 and result.level == 2


@pytest.mark.parametrize(
    "extra,rate", [(None, 100), (True, 100), (-1, 100), (0, None), (0, 32768), (1 << 31, 100)]
)
def test_unknown_or_invalid_external_terms_are_not_defaulted(extra, rate):
    with pytest.raises(ValueError):
        calculate_shop_level(
            snapshot([]), menu(), levels(), additional_points=extra, rate_percent=rate
        )


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x01bad",
        b"\x01" + zlib.compress(bytes(1185)),
        snapshot([]) + b"junk",
        snapshot([(110, 7, 0, 0)]),
    ],
)
def test_bad_snapshots_are_not_reported_as_zero_progress(data):
    with pytest.raises(ValueError):
        mission_records(data)


def test_thresholds_must_be_ordered_contiguous_and_start_at_zero():
    for table in (
        levels((1, 2)),
        levels((0, 100, 99)),
        parameter(b"PLAYER_EXP2RATE_VALUE_LV", [(0, bytes(4)), (2, bytes(4))]),
    ):
        with pytest.raises(ValueError):
            calculate_shop_level(snapshot([]), menu(), table, additional_points=0, rate_percent=100)


def test_parameter_rows_cannot_read_names_as_data():
    raw = bytearray(menu())
    struct.pack_into(">I", raw, 0, 0x40)
    with pytest.raises(ValueError):
        read_rows(bytes(raw), b"MISSION_MENU_DATA_PARAM", 0x56)


@pytest.mark.parametrize(
    "options",
    [
        ["--mission-snapshot", "missing"],
        ["--additional-points", "0"],
        ["--level", "0", "--mission-snapshot", "missing"],
    ],
)
def test_audit_cli_does_not_silently_supply_external_terms(options):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "tools/analysis/shop_catalogue.py"),
            "missing-regu",
            "missing-lineup",
            *options,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Traceback" not in result.stderr and "error:" in result.stderr
