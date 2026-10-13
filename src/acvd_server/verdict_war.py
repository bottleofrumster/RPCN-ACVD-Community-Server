"""Explicit replacement TEST WAR rules, not recovered retail server balance.

Pure transitions operate on a copied canonical record. TeamStore owns the
atomic write alongside battle receipts; no separate state store or result ledger
is introduced. Enabling a war requires an explicit offline operator action.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy

POLICY = "configurable-test-war-v1"
DEFAULT_RULES = {
    "base_ap": 100_000,
    "normal_win_damage": 75_000,
    "boss_win_damage": 200_000,
    "war_seconds": 1800,
    "grace_seconds": 120,
    "verdict_day_threshold": 25_000,
    "verdict_day_seconds": 120,
}


def rules(values: dict) -> dict[str, int]:
    if not isinstance(values, dict) or values.keys() - DEFAULT_RULES.keys():
        raise ValueError("Unknown test-war rule")
    result = DEFAULT_RULES | values
    _integer(result["base_ap"], 1, 10_000_000, "base_ap")
    bounds = {
        "base_ap": (1, 10_000_000),
        "normal_win_damage": (0, 80_000_000),
        "boss_win_damage": (0, 80_000_000),
        "war_seconds": (60, 604800),
        "grace_seconds": (30, 86400),
        "verdict_day_threshold": (0, result["base_ap"]),
        "verdict_day_seconds": (10, 86400),
    }
    for key, value in result.items():
        low, high = bounds[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Invalid test-war {key}")
    return result


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"Invalid test-war {label}")
    return value


def new(*, season: int, generation: str, now: int, settings: dict, owners: list[int]) -> dict:
    _integer(season, 1, 0xFFFFFFFE, "season")
    _integer(now, 0, 253402000000, "time")
    if not isinstance(generation, str) or len(generation) != 32:
        raise ValueError("Test war requires a unique generation")
    try:
        if bytes.fromhex(generation).hex() != generation:
            raise ValueError("Noncanonical generation")
    except ValueError as error:
        raise ValueError("Test war requires a canonical generation") from error
    if (
        not isinstance(owners, list)
        or len(owners) != 7
        or any(type(value) is not int or value not in (1, 2, 3) for value in owners)
        or sorted(Counter(owners).values()) != [2, 2, 3]
    ):
        raise ValueError("Test war must start with a 2/2/3 distribution over seven areas")
    policy = rules(settings)
    return {
        "schema_version": 1,
        "policy": POLICY,
        "generation": generation,
        "season": season,
        "phase": 2,
        "started_at": now,
        "ends_at": now + policy["war_seconds"],
        "rules": policy,
        "initial_owners": list(owners),
        "revision": 1,
        "last_result": [],
        "last_result_season": None,
        "areas": [
            {
                "area_id": index + 1,
                "faction_id": owner,
                "ap": policy["base_ap"] * 8,
                "epoch": 0,
                "changed_at": now,
                "last_attacker": 0,
                "verdict_deadline": None,
            }
            for index, owner in enumerate(owners)
        ],
    }


def validate(state: dict) -> None:
    if (
        not isinstance(state, dict)
        or type(state.get("schema_version")) is not int
        or state.get("schema_version") != 1
        or state.get("policy") != POLICY
    ):
        raise ValueError("Unsupported test-war state")
    policy = rules(state["rules"])
    if policy != state["rules"]:
        raise ValueError("Stored test-war rules must be explicit")
    # Validate the immutable seed and scalar domains without writing/migrating it.
    new(
        season=state["season"],
        generation=state["generation"],
        now=state["started_at"],
        settings=policy,
        owners=state["initial_owners"],
    )
    _integer(state["phase"], 0, 2, "phase")
    if state["phase"] not in (0, 2):
        raise ValueError("Unsupported test-war phase")
    _integer(state["ends_at"], state["started_at"], 253402100000, "end time")
    _integer(state["revision"], 1, 0x7FFFFFFF, "revision")
    if not isinstance(state.get("last_result"), list) or len(state["last_result"]) not in (0, 3):
        raise ValueError("Invalid test-war standings")
    if state["last_result"]:
        _integer(state.get("last_result_season"), 1, state["season"], "result season")
        if any(not isinstance(r, dict) for r in state["last_result"]):
            raise ValueError("Invalid test-war standing record")
        if {r.get("faction_id") for r in state["last_result"]} != {1, 2, 3} or {
            r.get("rank") for r in state["last_result"]
        } != {1, 2, 3}:
            raise ValueError("Invalid test-war standings identities")
        for row in state["last_result"]:
            _integer(row["faction_id"], 1, 3, "standing faction")
            _integer(row["rank"], 1, 3, "standing rank")
            _integer(row["areas"], 0, 7, "standing area count")
            _integer(row["ap"], 0, policy["base_ap"] * 56, "standing AP")
    elif state.get("last_result_season") is not None:
        raise ValueError("Result season requires completed standings")
    if not isinstance(state["areas"], list) or len(state["areas"]) != 7:
        raise ValueError("Test war requires seven areas")
    for index, area in enumerate(state["areas"], 1):
        if (
            not isinstance(area, dict)
            or type(area.get("area_id")) is not int
            or area["area_id"] != index
        ):
            raise ValueError("Invalid test-war area order")
        _integer(area["faction_id"], 1, 3, "owner")
        _integer(area["ap"], 1, policy["base_ap"] * 8, "AP")
        _integer(area["epoch"], 0, 0x7FFFFFFF, "area epoch")
        _integer(area["changed_at"], state["started_at"], 253402100000, "area time")
        _integer(area["last_attacker"], 0, 3, "attacker")
        if area["verdict_deadline"] is not None:
            _integer(area["verdict_deadline"], area["changed_at"], 253402100000, "deadline")
            if area["last_attacker"] in (0, area["faction_id"]):
                raise ValueError("Verdict Day requires a recorded opposing attacker")


def level(area: dict, policy: dict) -> int:
    """Native lower-bound equality advances to the next one of eight bases."""
    return min(7, 8 - (area["ap"] + policy["base_ap"] - 1) // policy["base_ap"])


def standings(state: dict) -> list[dict[str, int]]:
    scores = [
        {
            "faction_id": faction,
            "areas": sum(a["faction_id"] == faction for a in state["areas"]),
            "ap": sum(a["ap"] for a in state["areas"] if a["faction_id"] == faction),
        }
        for faction in (1, 2, 3)
    ]
    scores.sort(key=lambda s: (-s["areas"], -s["ap"], s["faction_id"]))
    return [dict(score, rank=index) for index, score in enumerate(scores, 1)]


def _capture(state, area, now):
    area.update(
        faction_id=area["last_attacker"],
        ap=state["rules"]["base_ap"] * 8,
        epoch=area["epoch"] + 1,
        changed_at=now,
        last_attacker=0,
        verdict_deadline=None,
    )


def _finish_if_due(state, now):
    if state["phase"] == 2 and (
        now >= state["ends_at"] or len({a["faction_id"] for a in state["areas"]}) == 1
    ):
        state["last_result"] = standings(state)
        state["last_result_season"] = state["season"]
        state["phase"] = 0
        state["ends_at"] = now + state["rules"]["grace_seconds"]
        state["revision"] += 1


def advance(state: dict, now: int) -> dict:
    """Reconcile deadlines once on demand; downtime cannot simulate fake battles."""
    validate(state)
    _integer(now, state["started_at"], 253402000000, "time")
    result = deepcopy(state)
    if result["phase"] == 0:
        if now >= result["ends_at"]:
            season = result["season"] + 1
            # Rotating the explicit seed shares its extra area across factions.
            owners = [owner % 3 + 1 for owner in result["initial_owners"]]
            result = new(
                season=season,
                generation=result["generation"],
                now=now,
                settings=result["rules"],
                owners=owners,
            )
            result["last_result"] = deepcopy(state["last_result"])
            result["last_result_season"] = state["last_result_season"]
        return result
    for area in result["areas"]:
        deadline = area["verdict_deadline"]
        if deadline is not None and deadline <= min(now, result["ends_at"]):
            # TEST policy: a deadline loses the current base. Only the final
            # base transfers the area; no ownership is inferred from a timer.
            area["ap"] = max(
                0, (area["ap"] - 1) // result["rules"]["base_ap"] * result["rules"]["base_ap"]
            )
            area.update(verdict_deadline=None, changed_at=now)
            if area["ap"] == 0:
                _capture(result, area, now)
            result["revision"] += 1
    _finish_if_due(result, now)
    return result


def impact(
    state: dict, context: dict, *, winning_faction: int, boss: bool, now: int
) -> tuple[dict, dict]:
    """Apply one already-authenticated result inside its durable receipt commit.

    No idempotency ledger lives here: callers must use the existing shared
    battle settlement transaction. An old season/area cannot damage a new one.
    """
    result = advance(state, now)
    audit = {"applied": False, "reason": "stale_or_ineligible_context"}
    if not isinstance(context, dict) or type(boss) is not bool:
        raise ValueError("Invalid test-war battle context")
    for key, low, high in (
        ("season", 1, 0xFFFFFFFE),
        ("area_epoch", 0, 0x7FFFFFFF),
        ("attacker_faction", 1, 3),
        ("defender_faction", 1, 3),
    ):
        _integer(context.get(key), low, high, key)
    _integer(winning_faction, 0, 3, "winner")
    area_id = _integer(context.get("area_id"), 1, 7, "battle area")
    area = result["areas"][area_id - 1]
    if (
        result["phase"] != 2
        or any(context.get(key) != result[key] for key in ("generation", "season"))
        or context.get("area_epoch") != area["epoch"]
        or context.get("defender_faction") != area["faction_id"]
    ):
        return result, audit
    attacker = context.get("attacker_faction")
    if (
        type(attacker) is not int
        or attacker not in (1, 2, 3)
        or attacker == area["faction_id"]
        or winning_faction != attacker
    ):
        return result, audit
    damage = result["rules"]["boss_win_damage" if boss else "normal_win_damage"]
    before, old_owner = area["ap"], area["faction_id"]
    previous_level = level(area, result["rules"])
    if not damage:
        return result, {"applied": False, "reason": "damage_disabled"}
    area.update(ap=max(0, before - damage), last_attacker=attacker, changed_at=now)
    if area["ap"] == 0:
        _capture(result, area, now)
    else:
        if level(area, result["rules"]) != previous_level:
            area["verdict_deadline"] = None
        remaining = (area["ap"] - 1) % result["rules"]["base_ap"] + 1
        if remaining <= result["rules"]["verdict_day_threshold"]:
            if area["verdict_deadline"] is None:
                area["verdict_deadline"] = now + result["rules"]["verdict_day_seconds"]
        else:
            area["verdict_deadline"] = None
    result["revision"] += 1
    _finish_if_due(result, now)
    return result, {
        "applied": True,
        "area_id": area_id,
        "damage": min(before, damage),
        "previous_ap": before,
        "ap": area["ap"],
        "previous_faction": old_owner,
        "faction_id": area["faction_id"],
        "map_level": level(area, result["rules"]),
    }
