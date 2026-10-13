"""Select Story emblem rewards and reconcile known local ownership."""

from __future__ import annotations

from .protocol import ProtocolError, decode_proto
from .rewards import StoryRewardPlan
from .story_emblem_data import STORY_EMBLEM_REWARDS

EMBLEM_NAMESPACE = 0x20000
STORY_EMBLEM_IDENTITY = 0


def reward_specs(plan: StoryRewardPlan) -> tuple[dict, ...]:
    """Return the emblem rows earned by this qualified Story result."""
    conditions = [(100, plan.mission_id, 0)]
    conditions.extend((1000, plan.mission_id, quest) for quest in plan.subquest_ids)
    result = []
    for condition in conditions:
        for category, row_id in STORY_EMBLEM_REWARDS.get(condition, ()):
            if not 0 <= category <= 0xFFFF or not 0 < row_id < 1 << 32:
                raise ValueError("Story emblem category or row ID is invalid")
            result.append(
                {
                    "condition": condition,
                    "category": category,
                    "selector": EMBLEM_NAMESPACE + category,
                    "row_id": row_id,
                }
            )
    return tuple(result)


def owned_keys(inventory: bytes) -> frozenset[tuple[int, int]]:
    """Return authenticated emblem ``(selector, row)`` pairs from an upload.

    SyncOfflineData exports the complete non-DLC emblem collection as field-3
    records with two required scalars. The separate online Story reward uses a
    three-field KeyItem, but its authenticated result converter copies only the
    row ID into the local emblem result. Keep the schemas separate here: this
    function is ownership filtering only and never supplies a reward identity.
    """
    result: set[tuple[int, int]] = set()
    for field in decode_proto(inventory):
        if field.number != 3:
            continue
        if field.wire_type != 2 or not isinstance(field.value, bytes):
            raise ProtocolError("Story emblem ownership record must be nested")
        nested = decode_proto(field.value)
        values = {}
        for number in (1, 2):
            found = [item for item in nested if item.number == number]
            if len(found) != 1 or found[0].wire_type != 0:
                raise ProtocolError("Story emblem ownership requires two scalars")
            values[number] = int(found[0].value)
        selector, row_id = values[1], values[2]
        if not 0 <= selector < 1 << 32 or not 0 <= row_id < 1 << 32:
            raise ProtocolError("Story emblem ownership scalar is outside uint32")
        if selector >> 16 == 2:
            result.add((selector, row_id))
    return frozenset(result)
