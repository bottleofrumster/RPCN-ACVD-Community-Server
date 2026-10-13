"""Story emblem rules and the isolated RewardInfo type-3 encoder."""

from dataclasses import replace
from pathlib import Path

import pytest

from acvd_server.protocol import ProtocolError, decode_proto, proto_bytes, proto_varint
from acvd_server.rewards import _register_battle_result_response, story_reward_plan
from acvd_server.story_emblem_data import STORY_EMBLEM_REWARDS
from acvd_server.story_emblems import owned_keys, reward_specs

RAW = bytes.fromhex((Path(__file__).parent / "fixtures/story-subquest-result.hex").read_text())


def test_all_recovered_emblem_rows_have_the_proven_selector():
    rows = [row for values in STORY_EMBLEM_REWARDS.values() for row in values]
    assert len(rows) == 57
    assert {category for category, _ in rows} == {50}
    assert len({row_id for _, row_id in rows}) == 57


def test_reward_selection_uses_only_the_completed_mission():
    plan = story_reward_plan(RAW, 50627)
    assert reward_specs(plan) == (
        {
            "condition": (100, 2002, 0),
            "category": 50,
            "selector": 0x20032,
            "row_id": 1231,
        },
    )
    assert reward_specs(replace(plan, mission_id=204)) == ()


def test_type3_encoder_requires_and_preserves_all_key_item_fields():
    plan = story_reward_plan(RAW, 50627)
    (spec,) = reward_specs(plan)
    response = _register_battle_result_response(plan, emblems=({**spec, "identity": 77},))
    (battle,) = decode_proto(response)
    quests = [decode_proto(field.value) for field in decode_proto(battle.value) if field.number == 8]
    emblem_quest = next(
        quest for quest in quests if next(field.value for field in quest if field.number == 1) == 100
    )
    rewards = [field for field in emblem_quest if field.number == 6]
    emblem = decode_proto(rewards[0].value)
    assert {field.number: field.value for field in emblem if field.wire_type == 0} == {1: 3}
    key_item = decode_proto(next(field.value for field in emblem if field.number == 3))
    assert {field.number: field.value for field in key_item} == {
        1: 77,
        2: 0x20032,
        3: 1231,
    }


@pytest.mark.parametrize("key, value", [("identity", -1), ("selector", 1 << 32), ("row_id", 0)])
def test_type3_encoder_rejects_invalid_key_item_values(key, value):
    plan = story_reward_plan(RAW, 50627)
    (spec,) = reward_specs(plan)
    with pytest.raises(ValueError, match="KeyItem"):
        _register_battle_result_response(plan, emblems=({**spec, "identity": 1, key: value},))


def test_uploaded_two_field_emblem_ownership_stays_separate_from_reward_key_item():
    snapshot = proto_bytes(3, proto_varint(1, 0x20032) + proto_varint(2, 1231))
    snapshot += proto_bytes(3, proto_varint(1, 0x30051) + proto_varint(2, 44))
    assert owned_keys(snapshot) == {(0x20032, 1231)}


@pytest.mark.parametrize(
    "record",
    [
        proto_varint(1, 0x20032),
        proto_varint(2, 1231),
        proto_varint(1, 0x20032) * 2,
        proto_varint(1, 0x20032) + proto_varint(2, 1 << 32),
    ],
)
def test_malformed_uploaded_emblem_ownership_fails_closed(record):
    with pytest.raises(ProtocolError, match="ownership"):
        owned_keys(proto_bytes(3, record))
