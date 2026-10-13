"""Native Buy Decal catalogue, payment, replay and ownership readback."""

from __future__ import annotations

import struct
from pathlib import Path

from acvd_server.config import ServerConfig
from acvd_server.handlers import inventory as inventory_requests, shop as shop_requests
from acvd_server.key_item_shop import KeyItemShopCatalogue
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.teams import TeamStore
from acvd_server.udp import (
    BUY_PLAYER_SHOP_KEY_ITEM,
    GET_MY_ITEM_SIMPLE_INFO_LIST,
    GET_PLAYER_KEY_ITEM_LIST,
    GameMessage,
    GameSession,
)


class Events:
    def __init__(self) -> None:
        self.rows = []

    def emit(self, event, **fields) -> None:
        self.rows.append((event, fields))


def emblem_parameter(rows: list[tuple[int, int, int, int]]) -> bytes:
    """Build row-id/category/price/init-type prefixes in the supported PARAM form."""
    count = len(rows)
    data_start = 0x30 + count * 12
    result = bytearray(data_start + count * 16)
    result[10:12] = count.to_bytes(2, "big")
    result[12:44] = b"EMBLEM_PIECE_PARAM_ST".ljust(32, b" ")
    result[44:48] = b"\xff\x02\x00\xff"
    for index, (row_id, category, price, initial_type) in enumerate(rows):
        offset = data_start + index * 16
        struct.pack_into(">III", result, 0x30 + index * 12, row_id, offset, 0)
        result[offset] = category
        struct.pack_into(">hI", result, offset + 2, row_id, price)
        result[offset + 8] = initial_type
    return bytes(result)


def configured(tmp_path: Path):
    config = ServerConfig(project_root=tmp_path)
    config.shop_parameter_path.mkdir(parents=True)
    (config.shop_parameter_path / "emblempieceparam.bin").write_bytes(
        emblem_parameter(
            [
                (15, 0, 5000, 1),
                (16, 0, 5000, 1),
                (17, 1, 7500, 1),
                (18, 0, 5000, 0),
            ]
        )
    )
    catalogue = KeyItemShopCatalogue.load(config)
    assert catalogue is not None
    config.player_data_path.mkdir()
    store = TeamStore(config.team_data_path)
    store.remember_player("Fresh", 50627)
    store.synchronize_balance("Fresh", 10_000_000)
    session = GameSession(b"12345678", bytes(16), "Fresh", 50627)
    events = Events()
    replies = []
    errors = []
    return config, catalogue, store, session, events, replies, errors


def list_request(selectors=(0x20000,), offset=0, limit=1024) -> GameMessage:
    payload = b"".join(proto_varint(2, selector) for selector in selectors)
    payload += proto_varint(3, offset) + proto_varint(4, limit)
    return GameMessage(GET_MY_ITEM_SIMPLE_INFO_LIST, bytes(4), payload, 1)


def list_shop(state) -> list[dict[int, int]]:
    config, catalogue, store, session, events, replies, errors = state
    shop_requests.list_key_items(
        session,
        list_request(),
        catalogue=catalogue,
        player_data_path=config.player_data_path,
        teams=store,
        telemetry=events,
        reply=replies.append,
        reply_error=lambda result, detail: errors.append((result, detail)),
    )
    assert not errors
    return [
        {field.number: field.value for field in decode_proto(row.value)}
        for row in decode_proto(replies[-1])
    ]


def test_catalogue_lists_only_requested_shop_rows(tmp_path: Path) -> None:
    state = configured(tmp_path)
    rows = list_shop(state)
    assert rows == [
        {1: 15, 2: 15, 3: 0x20000, 4: 15, 5: 5000},
        {1: 16, 2: 16, 3: 0x20000, 4: 16, 5: 5000},
    ]
    assert set(state[3].key_item_shop_offers) == {15, 16}
    assert state[4].rows[-1][0] == "game_key_item_shop_lineup_sent"


def test_owned_upload_is_not_offered(tmp_path: Path) -> None:
    state = configured(tmp_path)
    state[0].player_data_path.joinpath("50627.offline-data.bin").write_bytes(
        proto_bytes(3, proto_varint(1, 0x20000) + proto_varint(2, 15))
    )
    assert [row[4] for row in list_shop(state)] == [16]


def test_purchase_debits_once_filters_stock_and_reads_back(tmp_path: Path) -> None:
    state = configured(tmp_path)
    config, catalogue, store, session, events, replies, errors = state
    offer = list_shop(state)[0]
    purchase = GameMessage(
        BUY_PLAYER_SHOP_KEY_ITEM,
        b"\0\0\0\x02",
        proto_varint(2, offer[1]) + proto_varint(3, offer[3]) + proto_varint(4, offer[5]),
        2,
    )
    for _ in range(2):
        shop_requests.buy_key_item(
            session,
            purchase,
            teams=store,
            player_data_path=config.player_data_path,
            telemetry=events,
            reply=replies.append,
            reply_error=lambda result, detail: errors.append((result, detail)),
        )
        assert replies[-1] == b""
    assert not errors
    assert store.balance("Fresh") == 9_995_000
    assert len(store.state["key_item_purchases"]) == 1
    assert events.rows[-1][0] == "game_key_item_shop_purchase_replayed"
    assert [row[4] for row in list_shop(state)] == [16]

    owned_replies = []
    inventory_requests.get_key_items(
        session,
        GameMessage(
            GET_PLAYER_KEY_ITEM_LIST,
            b"\0\0\0\x03",
            proto_varint(2, 0x20000) + proto_varint(5, 0) + proto_varint(6, 2048),
            3,
        ),
        teams=store,
        player_data_path=config.player_data_path,
        telemetry=events,
        reply=owned_replies.append,
        reply_error=lambda result, detail: errors.append((result, detail)),
    )
    key = decode_proto(first_field(decode_proto(owned_replies[-1]), 1).value)
    assert {field.number: field.value for field in key} == {1: 0, 2: 0x20000, 3: 15}


def test_purchase_rejects_changed_price_without_debit(tmp_path: Path) -> None:
    state = configured(tmp_path)
    config, _, store, session, events, replies, errors = state
    offer = list_shop(state)[0]
    shop_requests.buy_key_item(
        session,
        GameMessage(
            BUY_PLAYER_SHOP_KEY_ITEM,
            b"\0\0\0\x02",
            proto_varint(2, offer[1])
            + proto_varint(3, offer[3])
            + proto_varint(4, offer[5] + 1),
            2,
        ),
        teams=store,
        player_data_path=config.player_data_path,
        telemetry=events,
        reply=replies.append,
        reply_error=lambda result, detail: errors.append((result, detail)),
    )
    assert errors == [(1, 28)]
    assert store.balance("Fresh") == 10_000_000
    assert not store.state.get("key_item_purchases")
