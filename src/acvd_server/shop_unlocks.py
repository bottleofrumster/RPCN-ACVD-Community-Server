"""Permanent Story shop access, independent of currently owned part copies."""

from __future__ import annotations

from copy import deepcopy

from .mission_progress import mission_records
from .player_storage import _requested_mission_snapshot
from .story_item_data import STORY_PART_REWARDS
from .story_items import SUPPORTED_ITEM_POLICIES

# Ultimate Weapons retain their separately qualified Mission 10 shop path.
STORY_SHOP_PARTS = frozenset(
    part
    for (namespace, _mission, selector), parts in STORY_PART_REWARDS.items()
    if namespace == 1000 or (namespace == 100 and selector == 0)
    for part in parts
    if part[0] != 11
)


def remember_story_shop_unlocks(teams, session, player_data_path) -> frozenset[tuple[int, int]]:
    """Union retained unlocks with completed saves and owner-bound rewards.

    Missing subquest bit semantics are not guessed: those parts require a
    committed reward condition. Mission-clear rows can also be recovered from
    native saved progression, covering clears before the online reward ledger.
    A write failure denies this read/purchase; no wallet or inventory is edited.
    """
    profile = teams.player_profile(session.online_id)
    if profile.get("player_id") != session.player_id:
        raise ValueError("Story shop unlock identity is not established")
    retained = profile.get("story_shop_unlocks")
    unlocked = set()
    if retained is not None:
        if (
            not isinstance(retained, dict)
            or retained.get("player_id") != session.player_id
            or retained.get("version") != 1
            or not isinstance(retained.get("parts"), list)
        ):
            raise ValueError("Stored Story shop unlocks need review")
        for pair in retained["parts"]:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or any(type(value) is not int for value in pair)
                or tuple(pair) not in STORY_SHOP_PARTS
            ):
                raise ValueError("Stored Story shop part needs review")
            unlocked.add(tuple(pair))
    prior = set(unlocked)
    snapshot, _preseed = _requested_mission_snapshot(player_data_path, player_id=session.player_id)
    for record in mission_records(snapshot):
        if record.rank >= 0:
            unlocked.update(STORY_PART_REWARDS.get((100, record.mission_id, 0), ()))
    ledger = teams.state.get("story_rewards", {})
    if not isinstance(ledger, dict):
        raise ValueError("Story reward ledger needs review")
    for receipt in ledger.values():
        if not isinstance(receipt, dict) or (
            receipt.get("online_id"),
            receipt.get("player_id"),
        ) != (session.online_id, session.player_id):
            continue
        if receipt.get("policy_version") not in SUPPORTED_ITEM_POLICIES:
            continue
        settlement = receipt.get("settlement", {})
        if not isinstance(settlement, dict):
            raise ValueError("Story reward settlement needs review")
        mission_id = settlement.get("mission_id")
        if type(mission_id) is not int:
            raise ValueError("Story reward mission needs review")
        for key in ("items", "pending_specs"):
            specs = receipt.get(key, [])
            if not isinstance(specs, list):
                raise ValueError("Story reward specifications need review")
            for spec in specs:
                if not isinstance(spec, dict):
                    raise ValueError("Story reward specification needs review")
                condition = spec.get("condition")
                part = (spec.get("category"), spec.get("part_id"))
                if (
                    not isinstance(condition, (tuple, list))
                    or len(condition) != 3
                    or any(type(value) is not int for value in condition)
                    or condition[1] != mission_id
                    or any(type(value) is not int for value in part)
                    or part not in STORY_PART_REWARDS.get(tuple(condition), ())
                ):
                    raise ValueError("Story reward part disagrees with its condition")
                unlocked.add(part)
    unlocked.intersection_update(STORY_SHOP_PARTS)
    if unlocked != prior:
        updated = deepcopy(teams.state)
        updated["player_profiles"][session.online_id]["story_shop_unlocks"] = {
            "version": 1,
            "player_id": session.player_id,
            "parts": [list(pair) for pair in sorted(unlocked)],
        }
        teams.commit_state(updated)
    return frozenset(unlocked)
