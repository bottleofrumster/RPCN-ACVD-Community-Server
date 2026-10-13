from __future__ import annotations

import zlib

import pytest

from tools.extract_ac_snapshot import AC_BODY_OFFSET, AC_BODY_SIZE, extract_snapshot


def test_extract_snapshot_uses_design_body_and_reports_name() -> None:
    design = bytearray(AC_BODY_OFFSET + AC_BODY_SIZE + 10)
    design[8 : 8 + len("Crawler".encode("utf-16-be"))] = "Crawler".encode("utf-16-be")
    body = bytes((index * 17) & 0xFF for index in range(AC_BODY_SIZE))
    design[AC_BODY_OFFSET : AC_BODY_OFFSET + AC_BODY_SIZE] = body

    stored, metadata = extract_snapshot(bytes(design))

    assert stored[:1] == b"\x01"
    assert zlib.decompress(stored[1:]) == body
    assert metadata["ac_name"] == "Crawler"
    assert metadata["body_length"] == AC_BODY_SIZE


def test_extract_snapshot_rejects_truncated_design() -> None:
    with pytest.raises(ValueError, match="at least"):
        extract_snapshot(b"\x00" * (AC_BODY_OFFSET + AC_BODY_SIZE - 1))
