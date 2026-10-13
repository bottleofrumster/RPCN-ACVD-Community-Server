"""Operator-controlled active-war Conquest map selection.

MatchingResult field 6 selects one of the seven World area rows.  Extermination
then derives one of eight missions from that area's World progress level.  The
profiles below pair those two native inputs so a pool entry means one exact
Extermination map, rather than merely a region family.

The policy changes only newly formed compatible matches.  It never rewrites a
team's territory assignment, and settings changes are rejected while players
are connected so the login-cached World rows cannot change under a live match.
"""

from __future__ import annotations

from typing import Any

CQ_MAP_MODE_TERRITORY = "territory"
CQ_MAP_MODE_LOCKED_POOL = "locked_pool"
CQ_MAP_MODES = (CQ_MAP_MODE_TERRITORY, CQ_MAP_MODE_LOCKED_POOL)
CQ_AREA_IDS = tuple(range(1, 8))
CQ_AREA_NAMES = {
    1: "North Frontier",
    2: "Far East",
    3: "Middle East",
    4: "South Frontier",
    5: "Mid-Continent",
    6: "South Islands",
    7: "New Frontier",
}
CQ_AREA_MAPS = {
    1: (
        (3700, "Alloy Gate City"),
        (3710, "Windy City"),
        (3711, "Windy City/Night"),
        (3720, "Big D Tunnel"),
        (3721, "Big D Tunnel/Working"),
        (3730, "Brew City"),
        (3740, "Galef Plant"),
        (3750, "Agria/M42"),
    ),
    2: (
        (3600, "Andyr City"),
        (3601, "Pierm Snowfield"),
        (3602, "Yorga Base"),
        (3603, "Yorga Base/Blizzard"),
        (3610, "Yunsk Canyon"),
        (3620, "Miynsky Hills"),
        (3621, "Miynsky Hills/Bombing"),
        (3630, "Kensky Base"),
    ),
    3: (
        (3200, "Ratona Naval Port"),
        (3201, "Old Duman"),
        (3202, "Mazar Tomb"),
        (3203, "Mazar Tomb/Night"),
        (3210, "Samir Canyon"),
        (3220, "Quet Hills"),
        (3230, "ALG742-181"),
        (3231, "ALG742-181/Reboot"),
    ),
    4: (
        (3500, "Opal Cave"),
        (3501, "Opal Cave/Depths"),
        (3510, "Tauraca Cave"),
        (3520, "Calpin Wetlands"),
        (3521, "Old Uriae"),
        (3522, "Emapolis"),
        (3523, "Emapolis/Sunrise"),
        (3530, "Raziania Base"),
    ),
    5: (
        (3400, "Vorka City"),
        (3401, "Magion City"),
        (3402, "Borozniki TS"),
        (3403, "Borozniki TS/Pollution"),
        (3410, "Pask Field"),
        (3411, "Pask Field/Night"),
        (3420, "Zilma Borough"),
        (3430, "Under Tower GF710"),
    ),
    6: (
        (3300, "Victoria City"),
        (3301, "Maldan City"),
        (3302, "Maldan City/Night"),
        (3303, "Eduna Yard"),
        (3310, "Fat Rocks"),
        (3311, "Fat Rocks/Flooded"),
        (3320, "Fort Denis"),
        (3330, "Fort Denis/Inner City"),
    ),
    7: (
        (3100, "Route N64"),
        (3101, "Route R1024"),
        (3103, "Dozur Desert"),
        (3104, "Dozur Desert/Bombing"),
        (3120, "Vosto Base/Duststorm"),
        (3121, "Vosto Base"),
        (3110, "Massif Dolango"),
        (3130, "Karmat Base"),
    ),
}
CQ_LEVEL_PROGRESS = (800_000, 650_000, 550_000, 450_000, 350_000, 250_000, 150_000, 50_000)
CQ_APPROVED_MAP_POOL = (3101, 3200, 3300, 3410, 3500, 3602, 3710)
CQ_APPROVED_MAPS_BY_AREA = {1: 3710, 2: 3602, 3: 3200, 4: 3500, 5: 3410, 6: 3300, 7: 3101}
CQ_MAP_PROFILES = {
    mission_id: {
        "mission_id": mission_id,
        "area_id": area_id,
        "region": CQ_AREA_NAMES[area_id],
        "map_name": map_name,
        "level": level,
        "progress": CQ_LEVEL_PROGRESS[level],
        "lower_load": mission_id in CQ_APPROVED_MAP_POOL,
    }
    for area_id, maps in CQ_AREA_MAPS.items()
    for level, (mission_id, map_name) in enumerate(maps)
}
CQ_MAP_IDS = tuple(sorted(CQ_MAP_PROFILES))
CQ_MAP_DEFAULTS = {
    "cq_map_mode": CQ_MAP_MODE_TERRITORY,
    "cq_map_pool": [],
}


def validate_cq_map_settings(values: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("CQ map settings must be an object")
    mode = values.get("cq_map_mode", CQ_MAP_MODE_TERRITORY)
    pool = values.get("cq_map_pool", [])
    if mode not in CQ_MAP_MODES:
        raise ValueError("CQ map mode must use team territories or a locked pool")
    if (
        not isinstance(pool, list)
        or any(type(mission_id) is not int or mission_id not in CQ_MAP_IDS for mission_id in pool)
        or pool != sorted(set(pool))
    ):
        raise ValueError("CQ map pool must contain unique sorted Extermination mission IDs")
    areas = [int(CQ_MAP_PROFILES[mission_id]["area_id"]) for mission_id in pool]
    if len(areas) != len(set(areas)):
        raise ValueError("CQ map pool supports only one exact map per region")
    if mode == CQ_MAP_MODE_LOCKED_POOL and not pool:
        raise ValueError("Locked CQ map mode requires at least one selected map")
    return {"cq_map_mode": mode, "cq_map_pool": list(pool)}


def select_cq_area(values: dict[str, Any], original_area_id: int, rotation: int) -> int:
    """Select one frozen active-war area, preserving the default policy when off."""
    settings = validate_cq_map_settings(values)
    if settings["cq_map_mode"] == CQ_MAP_MODE_TERRITORY:
        return original_area_id
    if type(rotation) is not int or rotation <= 0:
        raise ValueError("CQ map rotation requires one positive match identity")
    pool = settings["cq_map_pool"]
    mission_id = pool[(rotation - 1) % len(pool)]
    return int(CQ_MAP_PROFILES[mission_id]["area_id"])


def select_cq_map(values: dict[str, Any], rotation: int) -> dict[str, Any] | None:
    """Return the exact frozen profile for one newly formed locked match."""
    settings = validate_cq_map_settings(values)
    if settings["cq_map_mode"] != CQ_MAP_MODE_LOCKED_POOL:
        return None
    if type(rotation) is not int or rotation <= 0:
        raise ValueError("CQ map rotation requires one positive match identity")
    mission_id = settings["cq_map_pool"][(rotation - 1) % len(settings["cq_map_pool"])]
    return dict(CQ_MAP_PROFILES[mission_id])


def locked_world_progress(values: dict[str, Any]) -> dict[int, int] | None:
    """Return the seven login-cached progress rows needed for exact maps.

    Every area is included because the client creates its bounded area table
    from WorldBasicInfo at login.  The selected pool still controls which area
    can be assigned to a new Extermination match.
    """
    settings = validate_cq_map_settings(values)
    if settings["cq_map_mode"] != CQ_MAP_MODE_LOCKED_POOL:
        return None
    selected_by_area = dict(CQ_APPROVED_MAPS_BY_AREA)
    selected_by_area.update(
        {
            int(CQ_MAP_PROFILES[mission_id]["area_id"]): mission_id
            for mission_id in settings["cq_map_pool"]
        }
    )
    return {
        area_id: int(CQ_MAP_PROFILES[mission_id]["progress"])
        for area_id, mission_id in selected_by_area.items()
    }
