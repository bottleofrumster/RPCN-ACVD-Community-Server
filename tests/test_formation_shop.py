"""Formation shop wire routing, catalogue identity, payment and reconnect."""

import struct
from copy import deepcopy
from types import SimpleNamespace

import pytest

from acvd_server.config import ServerConfig
from acvd_server.handlers import inventory
from acvd_server.inventory import _merge_offline_inventory, key_item_pairs
from acvd_server.key_item_shop import KeyItemShopCatalogue
from acvd_server.protocol import decode_proto, proto_bytes, proto_varint
from acvd_server.routing import dispatch_request
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession

SELECTOR = 0xA0000


def parameter():
    rows = [(11, 1, 0), (12, 2, 500000), (13, 3, 500000)]
    start = 48 + len(rows) * 12
    data = bytearray(start + len(rows) * 32)
    data[10:12] = len(rows).to_bytes(2, "big")
    data[12:44] = b"CUSTOMLAYOUT_AREADATA_ST".ljust(32, b" ")
    data[44:48] = b"\xff\x02\x00\xff"
    for i, (identity, level, price) in enumerate(rows):
        offset = start + i * 32
        struct.pack_into(">III", data, 48 + i * 12, identity, offset, 0)
        struct.pack_into(">HBB", data, offset, 107, level, level)
        struct.pack_into(">I", data, offset + 24, price)
    return bytes(data)


@pytest.fixture
def state(tmp_path):
    config = ServerConfig(project_root=tmp_path)
    config.shop_parameter_path.mkdir(parents=True)
    (config.shop_parameter_path / "customlayoutareadata.bin").write_bytes(parameter())
    config.player_data_path.mkdir()
    store = TeamStore(config.team_data_path)
    store.remember_player("Tester", 42)
    store.synchronize_balance("Tester", 1000000)
    peer = GameSession(b"test0001", bytes(16), "Tester", 42)
    replies, errors, events = [], [], []
    runtime = SimpleNamespace(
        key_item_shop_catalogue=KeyItemShopCatalogue.load(config),
        teams=store,
        player_data_path=config.player_data_path,
        telemetry=SimpleNamespace(emit=lambda event, **data: events.append((event, data))),
        _send_game_reply=lambda *args, **kwargs: (
            errors.append(kwargs["error"]) if kwargs.get("error") else replies.append(args[3])
        ),
    )
    return config, runtime, peer, replies, errors


def listing(state, offset=0, limit=64):
    _, runtime, peer, replies, _ = state
    request = proto_varint(2, SELECTOR) + proto_varint(3, offset) + proto_varint(4, limit)
    dispatch_request(runtime, peer, None, GameMessage(984, b"list", request, 1))
    return [
        {f.number: f.value for f in decode_proto(row.value)} for row in decode_proto(replies[-1])
    ]


def buy(state, selector=SELECTOR, price=500000, index=b"buy1"):
    _, runtime, peer, _, _ = state
    request = proto_varint(2, SELECTOR | 12) + proto_varint(3, selector) + proto_varint(4, price)
    dispatch_request(runtime, peer, None, GameMessage(983, index, request, 2))


def test_reported_984_selector_uses_five_field_catalogue_not_owned_cannon(state):
    assert listing(state) == [
        {1: SELECTOR | row, 2: row, 3: SELECTOR, 4: row, 5: 500000} for row in (12, 13)
    ]
    assert not state[4]
    assert state[1].teams.balance("Tester") == 1000000


def test_catalogue_offset_and_end_are_bounded(state):
    assert [row[4] for row in listing(state, 1, 1)] == [13]
    assert listing(state, 2, 64) == []
    assert listing(state, 0, 0) == []


def test_purchase_debits_once_and_survives_reconnect_and_upload(state):
    config, runtime, peer, replies, errors = state
    listing(state)
    buy(state)
    buy(state)
    assert not errors and replies[-1] == b""
    assert runtime.teams.balance("Tester") == 500000
    assert len(runtime.teams.state["key_item_purchases"]) == 1
    runtime.teams = TeamStore(config.team_data_path)
    peer.key_item_shop_offers.clear()
    buy(state)  # Durable replay does not require a surviving offer cache.
    assert not errors and runtime.teams.balance("Tester") == 500000
    assert [row[4] for row in listing(state)] == [13]
    read = proto_varint(2, SELECTOR) + proto_varint(5, 0) + proto_varint(6, 64)
    inventory.get_key_items(
        peer,
        GameMessage(0x3E2, b"read", read),
        teams=runtime.teams,
        player_data_path=config.player_data_path,
        telemetry=runtime.telemetry,
        reply=replies.append,
        reply_error=lambda *value: errors.append(value),
    )
    row = decode_proto(decode_proto(replies[-1])[0].value)
    assert {f.number: f.value for f in row} == {1: 0, 2: SELECTOR, 3: 12}
    saved = proto_bytes(3, proto_varint(1, 0x20000) + proto_varint(2, 15))
    owned = proto_bytes(3, proto_varint(1, SELECTOR) + proto_varint(2, 12))
    merged, _, _ = _merge_offline_inventory(saved, owned)
    assert key_item_pairs(merged, frozenset((SELECTOR, 0x20000))) == ((0x20000, 15), (SELECTOR, 12))


@pytest.mark.parametrize("selector,price", [(SELECTOR, 1), (0x20000, 500000), (0xB0000, 500000)])
def test_forged_offer_does_not_debit(state, selector, price):
    listing(state)
    before = deepcopy(state[1].teams.state)
    buy(state, selector, price)
    assert state[4] == [(1, 28)]
    assert state[1].teams.state == before


def test_unoffered_or_insufficient_purchase_does_not_debit(state):
    buy(state)
    assert state[4] == [(1, 28)]
    listing(state)
    state[1].teams.synchronize_balance("Tester", 100)
    before = deepcopy(state[1].teams.state)
    buy(state, index=b"buy2")
    assert state[1].teams.state == before


def test_absent_catalogue_returns_empty_without_synthetic_fallback(state):
    state[1].key_item_shop_catalogue = None
    assert listing(state) == [] and not state[4]
