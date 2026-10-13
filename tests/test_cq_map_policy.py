"""Active-war CQ map-pool validation and deterministic selection."""

import pytest

from acvd_server.cq_map_policy import (
    CQ_APPROVED_MAP_POOL,
    CQ_APPROVED_MAPS_BY_AREA,
    CQ_AREA_MAPS,
    CQ_MAP_PROFILES,
    locked_world_progress,
    select_cq_area,
    select_cq_map,
    validate_cq_map_settings,
)


def test_default_policy_preserves_the_match_area():
    assert select_cq_area({}, 3, 1) == 3
    assert validate_cq_map_settings({}) == {
        "cq_map_mode": "territory",
        "cq_map_pool": [],
    }


def test_locked_pool_rotates_deterministically_and_ignores_unassigned_source():
    settings = {"cq_map_mode": "locked_pool", "cq_map_pool": [3101, 3200, 3710]}
    assert [select_cq_area(settings, 0, match_id) for match_id in range(1, 7)] == [
        7,
        3,
        1,
        7,
        3,
        1,
    ]
    assert [select_cq_map(settings, match_id)["mission_id"] for match_id in range(1, 4)] == [
        3101,
        3200,
        3710,
    ]


def test_approved_pool_profiles_are_exact_named_extermination_missions():
    assert {
        profile["area_id"]: (profile["region"], profile["map_name"], mission_id)
        for mission_id, profile in CQ_MAP_PROFILES.items()
        if mission_id in CQ_APPROVED_MAP_POOL
    } == {
        1: ("North Frontier", "Windy City", 3710),
        2: ("Far East", "Yorga Base", 3602),
        3: ("Middle East", "Ratona Naval Port", 3200),
        4: ("South Frontier", "Opal Cave", 3500),
        5: ("Mid-Continent", "Pask Field", 3410),
        6: ("South Islands", "Victoria City", 3300),
        7: ("New Frontier", "Route R1024", 3101),
    }
    assert len(CQ_MAP_PROFILES) == 56
    assert all(len(maps) == 8 for maps in CQ_AREA_MAPS.values())


def test_locked_profiles_select_the_recovered_retail_levels():
    def retail_level(progress):
        if progress <= 0:
            return 7
        for level, threshold in enumerate(
            (700_000, 600_000, 500_000, 400_000, 300_000, 200_000, 100_000)
        ):
            if threshold < progress:
                return level
        return 7

    progress = locked_world_progress(
        {"cq_map_mode": "locked_pool", "cq_map_pool": list(CQ_APPROVED_MAP_POOL)}
    )
    assert progress is not None
    assert {
        area_id: retail_level(value) for area_id, value in progress.items()
    } == {
        area_id: CQ_MAP_PROFILES[mission_id]["level"]
        for area_id, mission_id in CQ_APPROVED_MAPS_BY_AREA.items()
    }
    assert locked_world_progress({}) is None


def test_any_catalog_map_can_replace_its_regions_cached_level():
    pool = [3130, 3231, 3750]
    progress = locked_world_progress(
        {"cq_map_mode": "locked_pool", "cq_map_pool": pool}
    )
    assert progress is not None
    assert {area_id: progress[area_id] for area_id in (1, 3, 7)} == {
        1: CQ_MAP_PROFILES[3750]["progress"],
        3: CQ_MAP_PROFILES[3231]["progress"],
        7: CQ_MAP_PROFILES[3130]["progress"],
    }


@pytest.mark.parametrize(
    "settings",
    [
        {"cq_map_mode": "random"},
        {"cq_map_mode": "locked_pool", "cq_map_pool": []},
        {"cq_map_mode": "locked_pool", "cq_map_pool": [0]},
        {"cq_map_mode": "locked_pool", "cq_map_pool": [8]},
        {"cq_map_mode": "locked_pool", "cq_map_pool": [3710, 3710]},
        {"cq_map_mode": "locked_pool", "cq_map_pool": [3700, 3710]},
        {"cq_map_mode": "locked_pool", "cq_map_pool": [True]},
        {"cq_map_mode": "locked_pool", "cq_map_pool": "1,2"},
    ],
)
def test_invalid_or_empty_locked_pools_fail_closed(settings):
    with pytest.raises(ValueError, match="CQ map"):
        validate_cq_map_settings(settings)


@pytest.mark.parametrize("rotation", [0, -1, True, "1"])
def test_locked_pool_requires_a_positive_integer_rotation(rotation):
    with pytest.raises(ValueError, match="rotation"):
        select_cq_area(
            {"cq_map_mode": "locked_pool", "cq_map_pool": [3710]},
            3,
            rotation,
        )
