"""Exact locked-map inputs exposed through the retail World area cache."""

from datetime import datetime, timezone

from acvd_server.cq_map_policy import CQ_APPROVED_MAPS_BY_AREA, CQ_MAP_PROFILES
from acvd_server.protocol import decode_proto, first_field
from acvd_server.world_messages import (
    _world_basic_info,
    _world_current_info,
    _world_log,
)

LOCKED = {
    "cq_map_mode": "locked_pool",
    "cq_map_pool": [3101, 3200, 3300, 3410, 3500, 3602, 3710],
}


def repeated_messages(payload, number):
    return [decode_proto(field.value) for field in decode_proto(payload) if field.number == number]


def test_locked_basic_info_creates_all_seven_bounded_area_records():
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    areas = repeated_messages(
        _world_basic_info(now, cq_map_settings=LOCKED),
        4,
    )

    assert [first_field(area, 1).value for area in areas] == list(range(1, 8))
    assert [first_field(area, 2).value for area in areas] == [0] * 7
    assert [first_field(area, 3).value for area in areas] == [0] * 7
    assert all(sum(field.number == 4 for field in area) == 1 for area in areas)


def test_locked_current_info_sets_each_exact_map_progress_band():
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    areas = repeated_messages(
        _world_current_info(now, cq_map_settings=LOCKED),
        4,
    )

    assert {
        first_field(area, 1).value: first_field(area, 2).value for area in areas
    } == {
        area_id: CQ_MAP_PROFILES[mission_id]["progress"]
        for area_id, mission_id in CQ_APPROVED_MAPS_BY_AREA.items()
    }
    assert all(first_field(area, 4).value == 0 for area in areas)
    assert all(len(decode_proto(first_field(area, 3).value)) == 7 for area in areas)


def test_locked_world_log_does_not_fabricate_area_history():
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    areas = repeated_messages(
        _world_log(now, cq_map_settings=LOCKED),
        4,
    )

    assert areas == []


def test_default_world_messages_remain_single_area_compatibility_rows():
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    assert len(repeated_messages(_world_basic_info(now), 4)) == 1
    assert len(repeated_messages(_world_current_info(now), 4)) == 1
    assert len(repeated_messages(_world_log(now), 4)) == 0
