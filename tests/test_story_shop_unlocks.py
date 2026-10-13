"""Earned Story parts remain purchasable after sale and cold reconnect."""

import struct
from copy import deepcopy
from dataclasses import replace

import pytest
from test_mission_progress import snapshot
from test_shop_catalogue import PART_STRIDES, archive_blob
from test_shop_requests import configured as configured, entries, request

from acvd_server.crypto import load_private_key
from acvd_server.protocol import decode_proto, proto_varint
from acvd_server.regulation import build_container
from acvd_server.rewards import StoryRewardPlan
from acvd_server.shop_data import ShopCatalogue
from acvd_server.shop_unlocks import STORY_SHOP_PARTS, remember_story_shop_unlocks
from acvd_server.story_item_data import STORY_PART_REWARDS
from acvd_server.story_items import settle, settle_group
from acvd_server.teams import TeamStore
from acvd_server.udp import BUY_PLAYER_SHOP_FOR_PARTS, DAT, GameMessage, GameSession, ReliablePacket


def story_catalogue(config):
    parts = STORY_SHOP_PARTS | {(0, 210), (0, 111), (1, 420)}
    blob = bytearray(0x34)
    for category in range(len(PART_STRIDES)):
        identifiers = sorted(part for cat, part in parts if cat == category)
        struct.pack_into(">H", blob, 4 + 2 * category, len(identifiers))
        for part in identifiers:
            row = bytearray(PART_STRIDES[category])
            struct.pack_into(">HHI", row, 0, part, part, 70_000)
            blob.extend(row)
    config.regulation_path.write_bytes(
        build_container(archive_blob(bytes(blob)), config.private_key_path)
    )
    return ShopCatalogue.load(config, load_private_key(config.private_key_path).public_key())


def setup_stock(configured):
    config, protocol, replies, events = configured
    protocol.shop_catalogue = story_catalogue(config)
    actor = GameSession(b"named001", bytes(16), "Low", 11)
    return config, protocol, replies, events, actor


@pytest.mark.parametrize("mission", range(2000, 2030))
def test_every_named_clear_part_is_added_at_the_regulation_price(configured, mission):
    config, protocol, replies, _, actor = setup_stock(configured)
    expected = STORY_PART_REWARDS[(100, mission, 0)]
    for category, _ in expected:
        request(protocol, actor, category=category)
        assert not any((row[3], row[4]) in expected for row in entries(replies))
    (config.player_data_path / "11.mission-savedata.bin").write_bytes(
        snapshot([(mission, 0, 0, 0)])
    )
    for category, part in expected:
        request(protocol, actor, category=category)
        offered = [row for row in entries(replies) if row[4] == part]
        assert len(offered) == 1 and offered[0][5] == 70_000


def test_completed_snapshot_unlock_survives_disposal_and_restart(configured):
    config, protocol, replies, _, actor = setup_stock(configured)
    path = config.player_data_path / "11.mission-savedata.bin"
    path.write_bytes(snapshot([(2002, 0, 0, 0)]))
    request(protocol, actor, category=3)
    assert [row[4] for row in entries(replies)] == [2610]
    # Both inventory and current mission snapshot can change independently.
    (config.player_data_path / "11.offline-data.bin").write_bytes(b"")
    path.write_bytes(snapshot([]))
    protocol.teams = TeamStore(protocol.teams.root)
    reopened = GameSession(b"named002", bytes(16), "Low", 11)
    request(protocol, reopened, category=3)
    assert [row[4] for row in entries(replies)] == [2610]
    request(protocol, GameSession(b"named003", bytes(16), "High", 22), category=3)
    assert entries(replies) == []


@pytest.mark.parametrize("condition", [key for key in STORY_PART_REWARDS if key[0] == 1000])
def test_committed_pending_subquest_reward_unlocks_only_earned_parts(configured, condition):
    config, protocol, _, _, actor = setup_stock(configured)
    _, mission, quest = condition
    protocol.teams.synchronize_balance("Low", 100_000)
    plan = StoryRewardPlan(mission, 11, 0, 0, 1, subquest_ids=(quest,))
    settle(protocol.teams, actor, plan, b"isolated-result", inventory=None)
    actual = remember_story_shop_unlocks(protocol.teams, actor, config.player_data_path)
    assert actual == frozenset(STORY_PART_REWARDS[condition])
    before = protocol.teams.state_path.read_bytes()
    assert remember_story_shop_unlocks(protocol.teams, actor, config.player_data_path) == actual
    assert protocol.teams.state_path.read_bytes() == before


def test_named_purchase_debits_once_and_revalidates_unlock(configured):
    config, protocol, replies, events, actor = setup_stock(configured)
    protocol.teams.synchronize_balance("Low", 200_000)
    (config.player_data_path / "11.offline-data.bin").write_bytes(b"")
    actor.owned_parts_claim = proto_varint(1, 0) + proto_varint(2, 0)
    settle(protocol.teams, actor, StoryRewardPlan(2002, 11, 0, 0, 1), b"result", inventory=None)
    request(protocol, actor, category=3)
    offer = entries(replies)[0]
    payload = b"".join(
        proto_varint(n, v)
        for n, v in ((2, offer[1]), (3, offer[2]), (4, 3), (5, 70_000), (7, 1000))
    )
    message = GameMessage(BUY_PLAYER_SHOP_FOR_PARTS, b"\0\0\0\x11", payload, 2)
    packet = ReliablePacket(3, 2, DAT)
    protocol._dispatch_game_message(actor, packet, message)
    assert not replies[-1][1] and decode_proto(replies[-1][0][3])
    assert protocol.teams.balance("Low") == 130_000
    protocol._dispatch_game_message(actor, packet, message)
    assert protocol.teams.balance("Low") == 130_000
    assert events.rows[-1][0] == "game_shop_purchase_replayed"
    # Revoking all proof simulates an explicit account reset, not a sale.
    updated = deepcopy(protocol.teams.state)
    updated["story_rewards"] = {}
    updated["player_profiles"]["Low"].pop("story_shop_unlocks")
    protocol.teams.commit_state(updated)
    protocol._dispatch_game_message(actor, packet, replace(message, message_index=b"\0\0\0\x12"))
    assert replies[-1][1] == {"error": (1, 28)}
    assert protocol.teams.balance("Low") == 130_000


def test_coop_receipts_unlock_both_participants(configured):
    config, protocol, _, _, actor = setup_stock(configured)
    guest = GameSession(b"named004", bytes(16), "High", 22)
    for owner in (actor, guest):
        protocol.teams.synchronize_balance(owner.online_id, 100_000)
    plans = tuple(StoryRewardPlan(2002, owner.player_id, 0, 0, 1) for owner in (actor, guest))
    settle_group(
        protocol.teams,
        (actor, guest),
        plans,
        b"co-op-result",
        player_data_path=config.player_data_path,
        request_digest="ab" * 32,
    )
    for owner in (actor, guest):
        assert (3, 2610) in remember_story_shop_unlocks(
            protocol.teams, owner, config.player_data_path
        )


def test_unlock_commit_failure_cannot_change_wallet_or_profile(configured, monkeypatch):
    config, protocol, _, _, actor = setup_stock(configured)
    (config.player_data_path / "11.mission-savedata.bin").write_bytes(snapshot([(2002, 0, 0, 0)]))
    before = deepcopy(protocol.teams.state)

    def fail(_updated):
        raise OSError("disk unavailable")

    monkeypatch.setattr(protocol.teams, "commit_state", fail)
    with pytest.raises(OSError):
        remember_story_shop_unlocks(protocol.teams, actor, config.player_data_path)
    assert protocol.teams.state == before


def test_earned_subquest_part_bypasses_rank_without_duplicate_stock(configured):
    config, protocol, replies, _, actor = setup_stock(configured)
    catalogue = protocol.shop_catalogue
    reward = next(row for row in catalogue.story_entries if (row.category, row.part_id) == (4, 410))
    protocol.shop_catalogue = replace(
        catalogue,
        entries=catalogue.entries + (replace(reward, minimum_level=50),),
        story_entries=tuple(row for row in catalogue.story_entries if row != reward),
    )
    request(protocol, actor, category=4)
    assert entries(replies) == []
    protocol.teams.synchronize_balance("Low", 200_000)
    settle(
        protocol.teams,
        actor,
        StoryRewardPlan(210, 11, 0, 0, 1, subquest_ids=(904,)),
        b"earned-subquest",
        inventory=None,
    )
    request(protocol, actor, category=4)
    stock = entries(replies)
    assert len(stock) == 1 and stock[0][4] == 410
    assert protocol.shop_catalogue.offer(reward.lineup_id).minimum_level == 50
    (config.player_data_path / "11.offline-data.bin").write_bytes(b"")
    actor.owned_parts_claim = proto_varint(1, 0) + proto_varint(2, 0)
    raw = b"".join(
        proto_varint(n, v)
        for n, v in ((2, stock[0][1]), (3, stock[0][2]), (4, 4), (5, 70_000), (7, 1000))
    )
    protocol._dispatch_game_message(
        actor,
        ReliablePacket(3, 2, DAT),
        GameMessage(BUY_PLAYER_SHOP_FOR_PARTS, b"\0\0\0\x13", raw, 2),
    )
    assert not replies[-1][1]
    assert protocol.teams.balance("Low") == 130_000


def test_unearned_named_offer_and_foreign_identity_cannot_unlock(configured):
    config, protocol, _, _, actor = setup_stock(configured)
    named = next(row for row in protocol.shop_catalogue.story_entries if row.part_id == 2610)
    with pytest.raises(ValueError, match="earned Story reward"):
        protocol.shop_catalogue.unlocked_offer(named, frozenset())
    with pytest.raises(ValueError, match="identity"):
        remember_story_shop_unlocks(
            protocol.teams, replace(actor, player_id=22), config.player_data_path
        )


def test_pagination_preserves_unique_keys_with_unlocked_parts(configured):
    config, protocol, replies, _, actor = setup_stock(configured)
    (config.player_data_path / "11.mission-savedata.bin").write_bytes(
        snapshot([(mission, 0, 0, 0) for mission in range(2000, 2030)])
    )
    request(protocol, actor, category=3)
    all_rows = entries(replies)
    paged = []
    for offset in range(0, len(all_rows) + 1, 3):
        request(protocol, actor, category=3, offset=offset, limit=3)
        paged.extend(entries(replies))
    assert paged == all_rows
    assert len({row[2] for row in paged}) == len(paged)
