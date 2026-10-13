from __future__ import annotations

import zlib

import pytest

from tools.extract_mission_snapshot import extract_mission_snapshot


def test_extract_mission_snapshot_projects_exact_gprog_ranges() -> None:
    gprog = bytes((index * 17) & 0xFF for index in range(0x950))
    stored, metadata = extract_mission_snapshot(gprog)
    raw = zlib.decompress(stored[1:])
    assert stored[:1] == b"\x01"
    assert raw == gprog[0x1C:0x3C] + gprog[0x4D0:0x950]
    assert len(raw) == 1184
    assert metadata["schema"] == "acvd-mission-savedata-preseed-v1"
    assert metadata["raw_length"] == 1184


def test_extract_mission_snapshot_rejects_truncated_gprog() -> None:
    with pytest.raises(ValueError, match="at least 2384"):
        extract_mission_snapshot(bytes(0x94F))
