"""Catalogue parsing and rank filtering; no fabricated ownership or prices."""

import json
import struct
import sys
import zlib

import pytest

from acvd_server.protocol import decode_proto
from acvd_server.shop_catalogue import (
    PART_STRIDES,
    CataloguePart,
    catalogue_page,
    eligible_parts,
    part_prices,
    part_tuning_budgets,
    parts_from_regulation_cache,
    regulation_file,
    sale_multiplier,
    sale_price,
    shop_lineup,
)


def parts_blob():
    data = bytearray(0x34)
    struct.pack_into(">HH", data, 4, 2, 1)
    for category, part_id, model_id, price in (
        (0, 210, 2210, 54000),
        (0, 111, 111, 0),
        (1, 420, 420, 76000),
    ):
        row = bytearray(PART_STRIDES[category])
        struct.pack_into(">HHI", row, 0, part_id, model_id, price)
        data.extend(row)
    return bytes(data)


def lineup_blob():
    data = bytearray(0x30 + 2 * 12 + 2 * 8)
    data[12:28] = b"PARTS_SHOP_LINEUP"
    data[44:48] = b"\xff\x02\x00\xff"
    struct.pack_into(">H", data, 10, 2)
    for index, (level, category, part_id) in enumerate(((0, 0, 210), (1, 1, 420))):
        offset = 0x48 + index * 8
        struct.pack_into(">III", data, 0x30 + 12 * index, index + 1, offset, 0)
        struct.pack_into(">Hb x H", data, offset, level, category, part_id)
    return bytes(data)


def archive_blob(parts=None):
    parts = parts_blob() if parts is None else parts
    compressed = zlib.compress(parts)
    data = bytearray(0x60)
    data[:4] = b"BND3"
    data[12:16] = b"\x24\x01\x01\x00"
    struct.pack_into(">I", data, 16, 1)
    struct.pack_into(">IIIII", data, 32, 0x03000000, len(compressed), 0x60, 0x34, len(parts))
    data[0x34:0x41] = b"acvparts.bin\0"
    return bytes(data) + compressed


def archive_with_system(rate=0.5):
    """Two-entry uncompressed archive for authenticated shop tests."""
    parts = parts_blob()
    system = struct.pack(">f", rate)
    data = bytearray(0x100)
    data[:4] = b"BND3"
    data[12:16] = b"\x24\x01\x01\x00"
    struct.pack_into(">I", data, 16, 2)
    for index, (name, body, offset, name_offset) in enumerate(
        [
            (b"acvparts.bin\0", parts, 0x100, 0x50),
            (b"serversystemparam.bin\0", system, 0x100 + len(parts), 0x68),
        ]
    ):
        struct.pack_into(
            ">IIIII", data, 32 + index * 20, 0x02000000, len(body), offset, name_offset, len(body)
        )
        data[name_offset : name_offset + len(name)] = name
    return bytes(data) + parts + system


def test_named_regulation_reader_and_sale_multiplier():
    archive = archive_with_system()
    assert parts_from_regulation_cache(archive) == parts_blob()
    assert sale_multiplier(regulation_file(archive, b"serversystemparam.bin")) == 0.5
    assert regulation_file(archive_blob(), b"serversystemparam.bin") is None
    # Absence and broken data are different; malformed parameters must fail.
    with pytest.raises(ValueError):
        regulation_file(archive[:-1], b"serversystemparam.bin")


@pytest.mark.parametrize("rate", [-0.5, 1.5, float("nan"), float("inf")])
def test_invalid_sale_multiplier_fails_closed(rate):
    with pytest.raises(ValueError):
        sale_multiplier(struct.pack(">f", rate))


def test_sale_price_uses_native_float_rounding_not_integer_half_shortcut():
    assert sale_price(50000, 0.5) == 25000
    assert sale_price(54000, 0.5) == 27000
    assert sale_price(20000, 0.5) == 10000
    assert sale_price(101, 0.5) == 50
    assert sale_price(33554431, 0.5) == 16777216
    assert sale_price(0, 0.5) == 0
    with pytest.raises(ValueError):
        sale_price(0x7FFFFFFF, 1.0)


def test_tuning_budget_uses_part_flag_to_distinguish_normal_and_weapon_arms():
    data = bytearray(0x34)
    struct.pack_into(">H", data, 4 + 2 * 2, 2)
    for part_id, flag in ((1010, 0), (7510, 1)):
        row = bytearray(PART_STRIDES[2])
        struct.pack_into(">HHI", row, 0, part_id, part_id, 50000)
        row[0x0D] = flag
        data.extend(row)
    assert part_tuning_budgets(bytes(data)) == {(2, 1010): 0, (2, 7510): 3}
    assert part_prices(bytes(data)) == {(2, 1010): 50000, (2, 7510): 50000}
    data[0x34 + 0x0D] = 2
    with pytest.raises(ValueError, match="growth flag"):
        part_tuning_budgets(bytes(data))


def test_regulation_and_lineup_use_part_id_not_model_id():
    archive = archive_blob()
    cache = struct.pack(">I", len(archive)) + archive + bytes(16)
    assert parts_from_regulation_cache(cache) == parts_blob()
    assert parts_from_regulation_cache(archive) == parts_blob()
    prices = part_prices(parts_blob())
    assert prices == {(0, 210): 54000, (0, 111): 0, (1, 420): 76000}
    entries = shop_lineup(lineup_blob(), prices)
    assert entries == (CataloguePart(1, 0, 0, 210, 54000), CataloguePart(2, 1, 1, 420, 76000))
    assert eligible_parts(entries, 0) == entries[:1]
    assert eligible_parts(entries, 1) == entries


@pytest.mark.parametrize("level", [-1, True, 1.0, 65536])
def test_invalid_level_is_not_silently_coerced(level):
    with pytest.raises(ValueError):
        eligible_parts((), level)


@pytest.mark.parametrize("authenticated", [False, True])
def test_catalogue_audit_supports_cache_and_authenticated_cdn(
    tmp_path, monkeypatch, capsys, authenticated
):
    from acvd_server.crypto import generate_rsa_keypair
    from acvd_server.regulation import build_container
    from tools.analysis.shop_catalogue import main

    archive = archive_blob()
    source, lineup = tmp_path / "regulation.bin", tmp_path / "partsshoplineup.bin"
    lineup.write_bytes(lineup_blob())
    arguments = ["shop_catalogue.py", str(source), str(lineup), "--level", "0"]
    if authenticated:
        private, public = tmp_path / "private.pem", tmp_path / "public.pem"
        generate_rsa_keypair(private, public)
        source.write_bytes(build_container(archive, private))
        arguments.extend(["--public-key", str(public)])
    else:
        source.write_bytes(struct.pack(">I", len(archive)) + archive)
    before = source.read_bytes(), lineup.read_bytes()
    monkeypatch.setattr(sys, "argv", arguments)
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["regulation_source"] == (
        "authenticated_cdn" if authenticated else "unverified_cache"
    )
    assert report["selected_rows"] == 1
    assert report["parts"][0]["part_id"] == 210
    assert report["parts"][0]["price"] == 54000
    assert (report["public_key_sha256"] is not None) == authenticated
    assert (source.read_bytes(), lineup.read_bytes()) == before


def test_catalogue_audit_rejects_tampered_cdn_before_printing_stock(tmp_path, monkeypatch, capsys):
    from acvd_server.crypto import CryptoError, generate_rsa_keypair
    from acvd_server.regulation import build_container
    from tools.analysis.shop_catalogue import main

    private, public = tmp_path / "private.pem", tmp_path / "public.pem"
    generate_rsa_keypair(private, public)
    source, lineup = tmp_path / "regulation.bin", tmp_path / "partsshoplineup.bin"
    data = bytearray(build_container(archive_blob(), private))
    data[270] ^= 1
    source.write_bytes(data)
    lineup.write_bytes(lineup_blob())
    monkeypatch.setattr(
        sys, "argv", ["shop_catalogue.py", str(source), str(lineup), "--public-key", str(public)]
    )
    with pytest.raises(CryptoError):
        main()
    assert capsys.readouterr().out == ""


def test_stock_page_filters_rank_and_category_before_offset():
    entries = (
        CataloguePart(1, 0, 0, 210, 54000),
        CataloguePart(2, 5, 0, 211, 55000),
        CataloguePart(3, 0, 1, 410, 76000),
        CataloguePart(4, 0, 0, 110, 50000),
    )
    original = entries
    body, count = catalogue_page(entries, level=0, category=0, offset=1, limit=7, shop_key=9)
    outer = decode_proto(body)
    assert count == len(outer) == 1 and outer[0].number == 1
    assert {f.number: f.value for f in decode_proto(outer[0].value)} == {
        1: 9,
        2: 4,
        3: 0,
        4: 110,
        5: 50000,
    }
    assert entries == original


def test_stock_page_reaches_all_rows_without_duplicates_and_handles_empty_pages():
    entries = tuple(CataloguePart(i, 0, 10, i, 20000) for i in range(1, 183))
    ids = []
    for offset in range(0, 190, 7):
        body, count = catalogue_page(
            entries, level=0, category=10, offset=offset, limit=7, shop_key=1
        )
        rows = decode_proto(body)
        assert len(rows) == count
        ids.extend(next(f.value for f in decode_proto(r.value) if f.number == 2) for r in rows)
    assert ids == list(range(1, 183))
    for options in ({"limit": 0}, {"offset": 0xFFFFFFFF}, {"category": 11}):
        request = dict(level=0, category=10, offset=0, limit=512, shop_key=1)
        request.update(options)
        assert catalogue_page(entries, **request) == (b"", 0)


@pytest.mark.parametrize(
    "option,value",
    [
        ("level", None),
        ("category", 9),
        ("category", True),
        ("offset", -1),
        ("offset", 1 << 32),
        ("limit", 513),
        ("limit", True),
        ("shop_key", 0),
        ("shop_key", 1 << 32),
    ],
)
def test_stock_page_rejects_unknown_authority_and_invalid_bounds(option, value):
    request = dict(level=0, category=0, offset=0, limit=512, shop_key=1)
    request[option] = value
    with pytest.raises(ValueError):
        catalogue_page((), **request)


@pytest.mark.parametrize("offset,value", [(12, 0), (13, 0)])
def test_unsupported_archive_flags_are_rejected(offset, value):
    data = bytearray(archive_blob())
    data[offset] = value
    with pytest.raises(ValueError):
        parts_from_regulation_cache(bytes(data))


def test_archive_bounds_and_decompression_are_checked():
    archive = archive_blob()
    for data in (b"", archive[:-1], b"\xff" * 4 + archive):
        with pytest.raises(ValueError):
            parts_from_regulation_cache(data)
    wrong_size = bytearray(archive)
    struct.pack_into(">I", wrong_size, 48, 0xFFFFFFFF)
    with pytest.raises(ValueError):
        parts_from_regulation_cache(bytes(wrong_size))
    bad_offset = bytearray(archive)
    struct.pack_into(">I", bad_offset, 40, 0xFFFFFF00)
    with pytest.raises(ValueError):
        parts_from_regulation_cache(bytes(bad_offset))


def test_part_bank_bounds_and_duplicates_are_checked():
    with pytest.raises(ValueError):
        part_prices(parts_blob()[:-1])
    duplicate = bytearray(parts_blob())
    struct.pack_into(">H", duplicate, 0x34 + PART_STRIDES[0], 210)
    with pytest.raises(ValueError):
        part_prices(bytes(duplicate))
    unsupported = bytearray(parts_blob())
    struct.pack_into(">H", unsupported, 4 + 7 * 2, 1)
    with pytest.raises(ValueError):
        part_prices(bytes(unsupported))


def test_lineup_does_not_guess_missing_prices_or_bad_rows():
    with pytest.raises(ValueError):
        shop_lineup(lineup_blob(), {})
    prices = part_prices(parts_blob())
    for position, fmt, value in ((44, ">B", 0), (0x3C, ">I", 1), (0x34, ">I", 0xFFFFFF00)):
        data = bytearray(lineup_blob())
        struct.pack_into(fmt, data, position, value)
        with pytest.raises(ValueError):
            shop_lineup(bytes(data), prices)


def test_level_filter_is_monotonic_and_keeps_exact_prices():
    entries = tuple(
        CataloguePart(level + 1, level, 0, 100 + level, level * 100) for level in range(26)
    )
    assert eligible_parts(entries, 0)[0].price == 0
    for level in range(26):
        assert eligible_parts(entries, level) == entries[: level + 1]
    assert len(eligible_parts(entries, 65535)) == 26


@pytest.mark.parametrize(
    "values",
    [
        (1, 0, 0, 210, 54000.0),
        (1, 0, True, 210, 54000),
        (1, 0, 0, 0, 54000),
        (1, -1, 0, 210, 54000),
        (1, 0, 0, 210, 1 << 32),
    ],
)
def test_catalogue_value_domains_are_not_coerced(values):
    with pytest.raises(ValueError):
        CataloguePart(*values)
