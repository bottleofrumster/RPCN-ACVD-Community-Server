"""Exercise the normal listing route with synthetic, authenticated game data.

The existing parser tests own the small binary fixture constructors. Reuse
them here; native stock and purchase evidence remain separate qualifications.
"""

import struct
from copy import deepcopy
from dataclasses import replace

import pytest
from test_mission_progress import levels, menu, parameter, snapshot
from test_shop_catalogue import (
    PART_STRIDES,
    archive_blob,
    archive_with_system,
    lineup_blob,
    parts_blob,
)

from acvd_server.config import ServerConfig
from acvd_server.crypto import CryptoError, generate_rsa_keypair, load_private_key
from acvd_server.protocol import decode_proto, proto_bytes, proto_varint
from acvd_server.regulation import build_container
from acvd_server.server import GameDatagramProtocol
from acvd_server.shop_data import PARAMETER_FILES, ShopCatalogue
from acvd_server.teams import TeamStore
from acvd_server.udp import (
    BUY_PLAYER_SHOP_FOR_PARTS,
    DAT,
    GET_PLAYER_SHOP_LINEUP_FOR_PARTS,
    GET_TRASH_PARTS_LIST,
    SELL_PARTS_ITEM,
    SYNC_OFFLINE_DATA,
    VERIFY_OWN_PARTS_LIST,
    GameMessage,
    GameSession,
    ReliablePacket,
)


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


@pytest.fixture
def configured(tmp_path):
    config = ServerConfig(project_root=tmp_path)
    generate_rsa_keypair(config.private_key_path, config.public_key_path)
    config.regulation_path.parent.mkdir(parents=True)
    config.regulation_path.write_bytes(build_container(archive_blob(), config.private_key_path))
    config.shop_parameter_path.mkdir()
    rate = bytearray(0x16)
    struct.pack_into(">h", rate, 0x14, 100)
    for name, value in zip(
        PARAMETER_FILES,
        (lineup_blob(), menu(), levels(), parameter(b"HARDCORE_MODE_PARAM_ST", [(0, rate)])),
    ):
        (config.shop_parameter_path / name).write_bytes(value)
    shop = ShopCatalogue.load(config, load_private_key(config.private_key_path).public_key())
    config.player_data_path.mkdir()
    (config.player_data_path / "11.mission-savedata.bin").write_bytes(snapshot([]))
    (config.player_data_path / "22.mission-savedata.bin").write_bytes(snapshot([(110, 6, 0, 250)]))
    store = TeamStore(config.team_data_path)
    store.remember_player("Low", 11)
    store.remember_player("High", 22)
    events = Events()
    protocol = GameDatagramProtocol(
        events, {}, config.native_library_path, config.player_data_path, store, shop_catalogue=shop
    )
    replies = []
    protocol._send_game_reply = lambda *args, **kwargs: replies.append((args, kwargs))
    return config, protocol, replies, events


def request(protocol, actor, category=0, offset=0, limit=512, payload=None):
    if payload is None:
        payload = proto_varint(2, category) + proto_varint(3, offset) + proto_varint(4, limit)
    message = GameMessage(GET_PLAYER_SHOP_LINEUP_FOR_PARTS, bytes(4), payload, 1)
    protocol._dispatch_game_message(actor, ReliablePacket(3, 2, DAT), message)
    return message


def entries(replies):
    args, kwargs = replies[-1]
    assert not kwargs
    return [{f.number: f.value for f in decode_proto(row.value)} for row in decode_proto(args[3])]


def ultimate_regulation(config, part_ids=(6010, 110, 310, 610, 810, 1010)):
    """Add the six native final-mission part IDs at their signed game price."""
    parts = bytearray(parts_blob())
    struct.pack_into(">H", parts, 4 + 2 * 11, len(part_ids))
    for part_id in part_ids:
        row = bytearray(PART_STRIDES[11])
        struct.pack_into(">HHI", row, 0, part_id, part_id, 500000)
        parts.extend(row)
    config.regulation_path.write_bytes(
        build_container(archive_blob(bytes(parts)), config.private_key_path)
    )
    return ShopCatalogue.load(config, load_private_key(config.private_key_path).public_key())


def test_incomplete_ultimate_weapon_regulation_fails_closed(configured):
    config, _, _, _ = configured
    with pytest.raises(ValueError, match="Ultimate weapon prices are incomplete"):
        ultimate_regulation(config, part_ids=(6010,))


def test_ultimate_weapon_stock_requires_final_story_clear_and_uses_exact_prices(configured):
    config, protocol, replies, _ = configured
    protocol.shop_catalogue = ultimate_regulation(config)
    actor = GameSession(b"ultim001", bytes(16), "Low", 11)
    request(protocol, actor, category=11)
    assert entries(replies) == []
    (config.player_data_path / "11.mission-savedata.bin").write_bytes(
        snapshot([(1010, 6, 0, 0)])
    )
    request(protocol, actor, category=11)
    stock = entries(replies)
    assert len(stock) == 6
    assert [row[4] for row in stock] == [6010, 110, 310, 610, 810, 1010]
    assert {row[3] for row in stock} == {11}
    assert {row[5] for row in stock} == {500000}
    assert len({row[2] for row in stock}) == 6
    assert all(row[2] > 2 for row in stock)
    request(protocol, GameSession(b"ultim003", bytes(16), "High", 22), category=11)
    assert entries(replies) == []


def test_ultimate_weapon_purchase_is_paid_once_and_rechecks_story_clear(configured):
    config, protocol, replies, events = configured
    protocol.shop_catalogue = ultimate_regulation(config)
    (config.player_data_path / "11.mission-savedata.bin").write_bytes(
        snapshot([(1010, 6, 0, 0)])
    )
    (config.player_data_path / "11.offline-data.bin").write_bytes(b"")
    actor = GameSession(b"ultim002", bytes(16), "Low", 11)
    actor.owned_parts_claim = proto_varint(1, 0) + proto_varint(2, 0)
    protocol.teams.synchronize_balance("Low", 1_500_000)
    request(protocol, actor, category=11)
    offer = entries(replies)[0]
    raw = b"".join(
        proto_varint(number, value)
        for number, value in ((2, offer[1]), (3, offer[2]), (4, 12), (5, 500000), (7, 1000))
    )
    message = GameMessage(BUY_PLAYER_SHOP_FOR_PARTS, b"\0\0\0\1", raw, 2)
    # A previous listing must not authorize sale after the verified clear vanishes.
    (config.player_data_path / "11.mission-savedata.bin").write_bytes(snapshot([]))
    protocol._dispatch_game_message(actor, ReliablePacket(3, 2, DAT), message)
    assert replies[-1][1] == {"error": (1, 28)}
    assert protocol.teams.balance("Low") == 1_500_000
    (config.player_data_path / "11.mission-savedata.bin").write_bytes(
        snapshot([(1010, 6, 0, 0)])
    )
    request(protocol, actor, category=11)
    protocol._dispatch_game_message(actor, ReliablePacket(3, 2, DAT), message)
    assert not replies[-1][1]
    paid_reply = replies[-1][0][3]
    assert protocol.teams.balance("Low") == 1_000_000
    receipt = next(iter(protocol.teams.state["shop_purchases"].values()))
    assert (receipt["category"], receipt["part_id"], receipt["part_class"]) == (11, 6010, 12)
    protocol._dispatch_game_message(actor, ReliablePacket(3, 2, DAT), message)
    assert replies[-1][0][3] == paid_reply
    assert protocol.teams.balance("Low") == 1_000_000
    assert events.rows[-1][0] == "game_shop_purchase_replayed"


def test_normal_sale_uses_signed_regulation_and_replays_without_payment(configured):
    config, protocol, replies, events = configured
    key = load_private_key(config.private_key_path)
    config.regulation_path.write_bytes(
        build_container(archive_with_system(), config.private_key_path)
    )
    protocol.shop_catalogue = ShopCatalogue.load(config, key.public_key())
    actor = GameSession(b"sale0001", bytes(16), "Low", 11)
    protocol.teams.synchronize_balance("Low", 100000)
    body = proto_bytes(
        1,
        b"".join(proto_varint(n, v) for n, v in [(1, 0), (2, 210), (3, 0), (4, 10001), (5, 1000)]),
    )
    path = config.player_data_path / "11.offline-data.bin"
    path.write_bytes(body)
    actor.owned_parts_claim = proto_varint(1, 1) + proto_varint(2, 10001)
    for index in (1, 2):
        message = GameMessage(
            SELL_PARTS_ITEM, index.to_bytes(4, "big"), proto_varint(1, 10001), index
        )
        protocol._dispatch_game_message(actor, ReliablePacket(3, 2, DAT), message)
        assert not replies[-1][1]
        assert replies[-1][0][3] == proto_varint(1, 1)
        assert protocol.teams.balance("Low") == 127000
    assert path.read_bytes() == body
    assert len(protocol.teams.state["part_sales"]) == 1
    assert events.rows[-1][0] == "game_shop_sale_replayed"
    assert protocol.shop_catalogue.sale_credit(0, 111) == 0  # Not a buy-lineup entry.
    config.regulation_path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="regulation changed"):
        protocol.shop_catalogue.sale_credit(0, 210)


def test_sparse_native_claim_can_sell_only_positively_stored_items(configured):
    config, protocol, replies, events = configured
    config.regulation_path.write_bytes(
        build_container(archive_with_system(), config.private_key_path)
    )
    protocol.shop_catalogue = ShopCatalogue.load(
        config, load_private_key(config.private_key_path).public_key()
    )
    actor = GameSession(b"salesprs", bytes(16), "Low", 11)
    protocol.teams.synchronize_balance("Low", 100000)

    def item(instance):
        return proto_bytes(
            1,
            b"".join(
                proto_varint(n, v)
                for n, v in ((1, 0), (2, 210), (3, 0), (4, instance), (5, 1000))
            ),
        )

    body = item(10001) + item(10003)
    path = config.player_data_path / "11.offline-data.bin"
    path.write_bytes(body)
    # The authenticated native owner has one extra part the server never saw.
    actor.owned_parts_claim = proto_varint(1, 3) + proto_varint(2, 10001 ^ 10002 ^ 10003)
    actor.owned_parts_max_instance_id = 10003
    actor.owned_parts_evidence_serial = actor.owned_parts_claim_serial = 1

    def request(instance, index):
        protocol._dispatch_game_message(
            actor,
            ReliablePacket(3, 2, DAT),
            GameMessage(SELL_PARTS_ITEM, index.to_bytes(4, "big"), proto_varint(1, instance), index),
        )
        return replies[-1]

    first = request(10001, 1)
    assert not first[1] and protocol.teams.balance("Low") == 127000
    assert events.rows[-1] == (
        "game_shop_sale_committed",
        {
            "online_id": "Low",
            "message_index": b"\0\0\0\1",
            "inventory_authority": "positive_stored_instance",
        },
    )
    assert request(10001, 2)[0][3] == first[0][3]
    assert protocol.teams.balance("Low") == 127000
    assert request(10002, 3)[1] == {"error": (1, 28)}
    assert protocol.teams.balance("Low") == 127000
    history = config.player_data_path / ".history/11.offline-data.bin"
    history.mkdir(parents=True)
    (history / "older.bin").write_bytes(item(10002))
    recovered = request(10002, 30)
    assert not recovered[1] and protocol.teams.balance("Low") == 154000
    assert events.rows[-1][1]["inventory_authority"] == "positive_owner_history_instance"
    updated = deepcopy(protocol.teams.state)
    updated.setdefault("shop_purchases", {})["later-paid"] = {
        "online_id": "Low",
        "player_id": 11,
        "category": 0,
        "part_id": 210,
        "part_class": 0,
        "tuning": 1000,
        "instance_id": 10004,
        "in_snapshot": False,
    }
    protocol.teams.commit_state(updated)
    # The server allocated this item after the native maximum was reported.
    assert not request(10004, 4)[1]
    assert protocol.teams.balance("Low") == 181000
    actor.owned_parts_claim_serial = 0
    assert request(10003, 5)[1] == {"error": (1, 28)}
    assert path.read_bytes() == body
    assert protocol.teams.balance("Low") == 181000


def test_normal_dispatch_uses_each_accounts_real_snapshot(configured):
    config, protocol, replies, events = configured
    before = deepcopy(protocol.teams.state)
    saved = {p.name: p.read_bytes() for p in config.player_data_path.iterdir()}
    low, high = (
        GameSession(b"low", bytes(16), "Low", 11),
        GameSession(b"high", bytes(16), "High", 22),
    )
    message = request(protocol, low)
    assert replies[-1][0][0] is low and replies[-1][0][2] is message
    assert entries(replies) == [{1: protocol.shop_catalogue.shop_key, 2: 1, 3: 0, 4: 210, 5: 54000}]
    assert events.rows[-1][1]["calculation"]["level"] == 0
    request(protocol, low, category=1)
    assert entries(replies) == []
    request(protocol, high, category=1)
    assert entries(replies) == [{1: protocol.shop_catalogue.shop_key, 2: 2, 3: 1, 4: 420, 5: 76000}]
    assert events.rows[-1][1]["calculation"]["level"] == 2
    assert protocol.teams.state == before
    assert saved == {p.name: p.read_bytes() for p in config.player_data_path.iterdir()}


def test_stored_medal_readback_and_both_shop_paths_use_the_same_rating_bonus(configured):
    from acvd_server.udp import GET_MY_MEDAL_INFO

    config, protocol, replies, events = configured
    actor = GameSession(b"medal001", bytes(16), "Low", 11)
    date = "2026-09-14T12:00:00+00:00"
    state = deepcopy(protocol.teams.state)
    state["player_profiles"]["Low"]["player_medals"] = {
        "player_id": 11,
        "records": [
            {
                "medal_id": 4,
                "award_count": 9,
                "progress_value": 0,
                "first_date": date,
                "second_date": date,
            }
        ],
    }
    protocol.teams.commit_state(state)
    before = protocol.teams.state_path.read_bytes()
    protocol._dispatch_game_message(
        actor, ReliablePacket(3, 2, DAT), GameMessage(GET_MY_MEDAL_INFO, bytes(4), b"", 1)
    )
    medal = decode_proto(decode_proto(replies[-1][0][3])[0].value)
    assert {f.number: f.value for f in medal if f.wire_type == 0} == {1: 4, 2: 9, 3: 0}
    assert events.rows[-1][1]["medal_count"] == 1 and events.rows[-1][1]["ownership_known"]
    assert protocol.teams.state_path.read_bytes() == before
    request(protocol, actor, category=1)
    assert entries(replies)[0][4] == 420
    assert events.rows[-1][1]["calculation"]["additional_points"] == 500
    # A fully known empty fixture inventory, not a missing real player's save.
    (config.player_data_path / "11.offline-data.bin").write_bytes(b"")
    actor.owned_parts_claim = proto_varint(1, 0) + proto_varint(2, 0)
    protocol.teams.synchronize_balance("Low", 100000)
    offer = next(p for p in protocol.shop_catalogue.entries if p.part_id == 420)
    raw = b"".join(
        proto_varint(n, v)
        for n, v in (
            (2, protocol.shop_catalogue.shop_key),
            (3, offer.lineup_id),
            (4, 1),
            (5, offer.price),
            (7, 1000),
        )
    )
    protocol._dispatch_game_message(
        actor, ReliablePacket(3, 2, DAT), GameMessage(BUY_PLAYER_SHOP_FOR_PARTS, bytes(4), raw, 2)
    )
    assert not replies[-1][1] and protocol.teams.balance("Low") == 24000
    assert events.rows[-1][0] == "game_shop_purchase_committed"


def test_page_offset_and_empty_limit(configured):
    _, protocol, replies, _ = configured
    actor = GameSession(b"low", bytes(16), "Low", 11)
    for offset, limit in ((1, 512), (0, 0), (0xFFFFFFFF, 512)):
        request(protocol, actor, offset=offset, limit=limit)
        assert entries(replies) == []


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x80",
        proto_varint(2, 0),
        proto_varint(2, 0) + proto_varint(2, 1) + proto_varint(3, 0) + proto_varint(4, 1),
        proto_bytes(2, b"0") + proto_varint(3, 0) + proto_varint(4, 1),
        proto_varint(2, 7) + proto_varint(3, 0) + proto_varint(4, 1),
        proto_varint(2, 0) + proto_varint(3, 1 << 32) + proto_varint(4, 1),
        proto_varint(2, 0) + proto_varint(3, 0) + proto_varint(4, 513),
        bytes(33),
    ],
)
def test_bad_page_gets_an_error_not_stock(configured, payload):
    _, protocol, replies, events = configured
    before = deepcopy(protocol.teams.state)
    request(protocol, GameSession(b"low", bytes(16), "Low", 11), payload=payload)
    assert replies[-1][1] == {"error": (1, 28)}
    assert replies[-1][0][3] == b"" and protocol.teams.state == before
    assert events.rows[-1][0] == "game_shop_catalogue_rejected"


def test_missing_snapshot_is_not_zero_rank(configured):
    config, protocol, replies, _ = configured
    (config.player_data_path / "11.mission-savedata.bin").unlink()
    request(protocol, GameSession(b"low", bytes(16), "Low", 11))
    assert replies[-1][1] == {"error": (1, 28)}


def test_wrong_account_cannot_read_another_players_rank(configured):
    _, protocol, replies, _ = configured
    request(protocol, GameSession(b"low", bytes(16), "Low", 22), category=1)
    assert replies[-1][1] == {"error": (1, 28)}


def test_changed_cdn_requires_reload_before_more_stock_is_served(configured):
    config, protocol, replies, _ = configured
    config.regulation_path.write_bytes(config.regulation_path.read_bytes() + bytes(16))
    request(protocol, GameSession(b"low", bytes(16), "Low", 11))
    assert replies[-1][1] == {"error": (1, 28)}


def test_unconfigured_stock_is_explicit_in_diagnostics(configured):
    _, protocol, replies, events = configured
    protocol.shop_catalogue = None
    request(protocol, GameSession(b"low", bytes(16), "Low", 11))
    assert entries(replies) == []
    assert events.rows[-1][1]["catalogue_available"] is False
    assert events.rows[-1][1]["reason"] == "shop_inputs_not_loaded"


def test_unconnected_purchase_cannot_return_an_empty_success(configured):
    _, protocol, replies, events = configured
    actor = GameSession(b"low", bytes(16), "Low", 11)
    before = deepcopy(protocol.teams.state)
    message = GameMessage(BUY_PLAYER_SHOP_FOR_PARTS, bytes(4), b"", 1)
    protocol._dispatch_game_message(actor, ReliablePacket(3, 2, DAT), message)
    assert replies[-1][1] == {"error": (1, 28)}
    assert protocol.teams.state == before
    assert events.rows[-1][0] == "game_shop_purchase_rejected"


def test_normal_route_replays_paid_request_without_catalogue_or_snapshot(configured):
    from acvd_server.shop_transactions import purchase

    config, protocol, replies, events = configured
    actor = GameSession(b"paid0001", bytes(16), "Low", 11)
    offer = protocol.shop_catalogue.entries[0]
    protocol.teams.synchronize_balance("Low", 100000)
    message = GameMessage(
        BUY_PLAYER_SHOP_FOR_PARTS,
        b"\x00\x00\x00\x09",
        b"".join(
            proto_varint(n, value)
            for n, value in ((2, 1), (3, offer.lineup_id), (4, 0), (5, offer.price), (7, 1000))
        ),
        9,
    )
    expected, _ = purchase(
        protocol.teams,
        actor,
        message,
        offer=offer,
        shop_key=1,
        verified_level=0,
        inventory=b"",
        reserved_instance_ids=frozenset(),
    )
    protocol.teams = TeamStore(protocol.teams.root)
    protocol.teams.synchronize_balance("Low", 30000)
    protocol.shop_catalogue = None
    (config.player_data_path / "11.mission-savedata.bin").unlink()
    before, disk = deepcopy(protocol.teams.state), protocol.teams.state_path.read_bytes()
    protocol._dispatch_game_message(actor, ReliablePacket(3, 2, DAT), message)
    assert replies[-1][0][3] == expected and not replies[-1][1]
    assert events.rows[-1][0] == "game_shop_purchase_replayed"
    assert protocol.teams.state == before and protocol.teams.state_path.read_bytes() == disk


def test_loader_checks_all_tables_and_cdn(configured):
    config, _, _, _ = configured
    public = load_private_key(config.private_key_path).public_key()
    table = config.shop_parameter_path / "playerexp2ratevaluelv.bin"
    original = table.read_bytes()
    table.write_bytes(levels((0, 500, 400)))
    with pytest.raises(ValueError, match="ordered"):
        ShopCatalogue.load(config, public)
    table.write_bytes(original)
    data = bytearray(config.regulation_path.read_bytes())
    data[256] ^= 1
    config.regulation_path.write_bytes(data)
    with pytest.raises(CryptoError):
        ShopCatalogue.load(config, public)


def test_loader_absent_is_distinct_from_partial_configuration(tmp_path):
    config = ServerConfig(project_root=tmp_path)
    assert ShopCatalogue.load(config, None) is None
    config.shop_parameter_path.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        ShopCatalogue.load(config, None)


def owned_record(instance):
    return proto_bytes(
        1,
        b"".join(
            proto_varint(n, v) for n, v in ((1, 0), (2, 110), (3, 0), (4, instance), (5, 1000))
        ),
    )


def buyer(configured):
    config, protocol, _, _ = configured
    actor = GameSession(b"buyer001", bytes(16), "Low", 11)
    protocol.teams.synchronize_balance("Low", 200000)
    (config.player_data_path / "11.offline-data.bin").write_bytes(b"")
    claim = GameMessage(VERIFY_OWN_PARTS_LIST, bytes(4), proto_varint(1, 0) + proto_varint(2, 0), 1)
    protocol._dispatch_game_message(actor, ReliablePacket(3, 2, DAT), claim)
    request(protocol, actor)
    return actor


def report_inventory_cursor(protocol, actor, maximum):
    message = GameMessage(
        GET_TRASH_PARTS_LIST,
        bytes(4),
        proto_varint(1, 512) + proto_varint(2, 0) + proto_varint(3, maximum),
        1,
    )
    protocol._dispatch_game_message(actor, ReliablePacket(3, 2, DAT), message)


def report_inventory_claim(protocol, actor, count, checksum):
    message = GameMessage(
        VERIFY_OWN_PARTS_LIST,
        bytes(4),
        proto_varint(1, count) + proto_varint(2, checksum),
        1,
    )
    protocol._dispatch_game_message(actor, ReliablePacket(3, 2, DAT), message)


def buy_message(protocol, *, index=2, entry=1, category=0, price=54000, tuning=1000, key=None):
    key = protocol.shop_catalogue.shop_key if key is None else key
    return GameMessage(
        BUY_PLAYER_SHOP_FOR_PARTS,
        index.to_bytes(4, "big"),
        b"".join(
            proto_varint(n, v)
            for n, v in ((2, key), (3, entry), (4, category), (5, price), (7, tuning))
        ),
        index,
    )


def test_new_purchase_uses_normal_route_and_retry_never_charges_twice(configured):
    config, protocol, replies, events = configured
    actor = buyer(configured)
    message = buy_message(protocol)
    protocol._dispatch_game_message(actor, ReliablePacket(3, 2, DAT), message)
    first = replies[-1]
    assert not first[1]
    assert decode_proto(first[0][3])[0].value == 10000
    assert protocol.teams.balance("Low") == 146000
    assert events.rows[-1][0] == "game_shop_purchase_committed"
    before = deepcopy(protocol.teams.state)
    actor.shop_offers.clear()
    protocol.shop_catalogue = None
    protocol._dispatch_game_message(actor, ReliablePacket(4, 3, DAT), message)
    assert replies[-1][0][3] == first[0][3] and not replies[-1][1]
    assert protocol.teams.state == before
    assert (config.player_data_path / "11.offline-data.bin").read_bytes() == b""


def test_sparse_returning_inventory_uses_same_login_claim_and_native_cursor(configured):
    config, protocol, replies, events = configured
    actor = GameSession(b"sparse01", bytes(16), "Low", 11)
    protocol.teams.synchronize_balance("Low", 200000)
    snapshot = owned_record(10001)
    path = config.player_data_path / "11.offline-data.bin"
    path.write_bytes(snapshot)
    report_inventory_cursor(protocol, actor, 30000)
    assert actor.owned_parts_evidence_serial == 1
    report_inventory_claim(protocol, actor, 2, 10001 ^ 30000)
    assert actor.owned_parts_claim_serial == actor.owned_parts_evidence_serial
    request(protocol, actor)
    protocol._dispatch_game_message(
        actor, ReliablePacket(4, 3, DAT), buy_message(protocol, index=2)
    )
    assert not replies[-1][1]
    assert decode_proto(replies[-1][0][3])[0].value == 30001
    assert protocol.teams.balance("Low") == 146000
    assert path.read_bytes() == snapshot
    assert events.rows[-1][0] == "game_shop_purchase_committed"
    assert events.rows[-1][1]["inventory_authority"] == "same_login_claim_and_cursor"


def test_sparse_purchase_requires_cursor_then_claim_in_the_same_evidence_generation(configured):
    config, protocol, replies, events = configured
    actor = GameSession(b"sparse02", bytes(16), "Low", 11)
    protocol.teams.synchronize_balance("Low", 200000)
    (config.player_data_path / "11.offline-data.bin").write_bytes(owned_record(10001))
    report_inventory_claim(protocol, actor, 2, 10001 ^ 30000)
    report_inventory_cursor(protocol, actor, 30000)
    request(protocol, actor)
    before = deepcopy(protocol.teams.state)
    protocol._dispatch_game_message(
        actor, ReliablePacket(4, 3, DAT), buy_message(protocol, index=2)
    )
    assert replies[-1][1] == {"error": (1, 28)}
    assert protocol.teams.state == before
    assert events.rows[-1][1]["reason"] == (
        "Same-login ownership claim and instance cursor are required"
    )


def test_sparse_purchase_conservatively_rejects_native_claim_at_capacity(configured):
    config, protocol, replies, events = configured
    actor = GameSession(b"sparse03", bytes(16), "Low", 11)
    protocol.teams.synchronize_balance("Low", 200000)
    (config.player_data_path / "11.offline-data.bin").write_bytes(b"")
    report_inventory_cursor(protocol, actor, 30000)
    report_inventory_claim(protocol, actor, 2980, 1)
    request(protocol, actor)
    before = deepcopy(protocol.teams.state)
    protocol._dispatch_game_message(
        actor, ReliablePacket(4, 3, DAT), buy_message(protocol, index=2)
    )
    assert replies[-1][1] == {"error": (1, 28)}
    assert protocol.teams.state == before
    assert events.rows[-1][1]["reason"] == "Registered inventory reaches the garage upper bound"


def test_sparse_purchase_does_not_treat_a_missing_snapshot_as_empty_ownership(configured):
    config, protocol, replies, events = configured
    actor = GameSession(b"sparse04", bytes(16), "Low", 11)
    protocol.teams.synchronize_balance("Low", 200000)
    assert not (config.player_data_path / "11.offline-data.bin").exists()
    report_inventory_cursor(protocol, actor, 40000)
    report_inventory_claim(protocol, actor, 3, 123)
    request(protocol, actor)
    protocol._dispatch_game_message(
        actor, ReliablePacket(4, 3, DAT), buy_message(protocol, index=2)
    )
    assert not replies[-1][1]
    assert decode_proto(replies[-1][0][3])[0].value == 40001
    assert events.rows[-1][1]["inventory_authority"] == "same_login_claim_and_cursor"
    assert not (config.player_data_path / "11.offline-data.bin").exists()


def test_inventory_upload_invalidates_sparse_purchase_authority(configured):
    config, protocol, replies, events = configured
    actor = GameSession(b"sparse05", bytes(16), "Low", 11)
    protocol.teams.synchronize_balance("Low", 200000)
    report_inventory_cursor(protocol, actor, 40000)
    report_inventory_claim(protocol, actor, 3, 123)
    upload = GameMessage(SYNC_OFFLINE_DATA, bytes(4), owned_record(10001), 2)
    protocol._dispatch_game_message(actor, ReliablePacket(4, 3, DAT), upload)
    assert actor.owned_parts_claim is None and actor.owned_parts_max_instance_id is None
    request(protocol, actor)
    before = deepcopy(protocol.teams.state)
    protocol._dispatch_game_message(
        actor, ReliablePacket(5, 4, DAT), buy_message(protocol, index=3)
    )
    assert replies[-1][1] == {"error": (1, 28)}
    assert protocol.teams.state == before
    assert events.rows[-1][1]["reason"] == (
        "Same-login ownership claim and instance cursor are required"
    )


def test_pending_additions_allow_distinct_followup_purchases(configured):
    _, protocol, replies, _ = configured
    actor = buyer(configured)
    ids = []
    for index in (2, 3):
        protocol._dispatch_game_message(
            actor, ReliablePacket(index, 0, DAT), buy_message(protocol, index=index)
        )
        assert not replies[-1][1]
        ids.append(decode_proto(replies[-1][0][3])[0].value)
    assert ids == [10000, 10001] and protocol.teams.balance("Low") == 92000


@pytest.mark.parametrize(
    "damage",
    [
        "no_listing",
        "wrong_version",
        "no_claim",
        "no_snapshot",
        "mismatch",
        "duplicates",
        "wrong_price",
        "wrong_key",
        "unoffered_item",
        "tuning",
    ],
)
def test_unqualified_new_purchase_preserves_wallet_and_receipts(configured, damage):
    config, protocol, replies, _ = configured
    actor = buyer(configured)
    options = {}
    if damage == "no_listing":
        actor.shop_offers.clear()
    elif damage == "wrong_version":
        actor.shop_catalogue_version = "old"
    elif damage == "no_claim":
        actor.owned_parts_claim = None
    elif damage == "no_snapshot":
        (config.player_data_path / "11.offline-data.bin").unlink()
    elif damage == "mismatch":
        (config.player_data_path / "11.offline-data.bin").write_bytes(owned_record(70))
    elif damage == "duplicates":
        (config.player_data_path / "11.offline-data.bin").write_bytes(owned_record(70) * 2)
    elif damage == "wrong_price":
        options["price"] = 1
    elif damage == "wrong_key":
        options["key"] = 1 if protocol.shop_catalogue.shop_key != 1 else 2
    elif damage == "unoffered_item":
        options.update(entry=2, category=1, price=76000)
    elif damage == "tuning":
        options["tuning"] = 1003
    before = deepcopy(protocol.teams.state)
    protocol._dispatch_game_message(
        actor, ReliablePacket(3, 2, DAT), buy_message(protocol, **options)
    )
    assert replies[-1][1] == {"error": (1, 28)} and protocol.teams.state == before


def test_reservations_include_other_current_and_reviewed_inventories(configured):
    config, protocol, replies, _ = configured
    actor = buyer(configured)
    peer = config.player_data_path / "22.offline-data.bin"
    preseed = config.player_data_path / "22.offline-data.preseed.bin"
    peer.write_bytes(owned_record(10000) * 2)
    preseed.write_bytes(owned_record(10001))
    protocol._dispatch_game_message(actor, ReliablePacket(3, 2, DAT), buy_message(protocol))
    assert not replies[-1][1] and decode_proto(replies[-1][0][3])[0].value == 10002
    assert peer.read_bytes() == owned_record(10000) * 2 and preseed.read_bytes() == owned_record(
        10001
    )


def test_same_price_changed_part_cannot_use_an_old_offer(configured):
    _, protocol, replies, _ = configured
    actor = buyer(configured)
    catalogue = protocol.shop_catalogue
    changed = replace(catalogue.entries[0], part_id=111)
    protocol.shop_catalogue = replace(catalogue, entries=(changed,) + catalogue.entries[1:])
    before = deepcopy(protocol.teams.state)
    protocol._dispatch_game_message(actor, ReliablePacket(3, 2, DAT), buy_message(protocol))
    assert replies[-1][1] == {"error": (1, 28)} and protocol.teams.state == before


def test_new_version_clears_old_partial_offers_and_changes_wire_key(configured):
    _, protocol, replies, _ = configured
    actor = buyer(configured)
    old_key = protocol.shop_catalogue.shop_key
    protocol.shop_catalogue = replace(
        protocol.shop_catalogue, input_hashes=(("new-input", "a" * 64),)
    )
    assert protocol.shop_catalogue.shop_key != old_key
    request(protocol, actor, category=1)
    assert not actor.shop_offers  # The new low-rank core page is empty.
    before = deepcopy(protocol.teams.state)
    protocol._dispatch_game_message(
        actor, ReliablePacket(3, 2, DAT), buy_message(protocol, key=old_key)
    )
    assert replies[-1][1] == {"error": (1, 28)} and protocol.teams.state == before


def test_rank_is_rechecked_after_the_listing(configured):
    config, protocol, replies, _ = configured
    actor = buyer(configured)
    path = config.player_data_path / "11.mission-savedata.bin"
    path.write_bytes(snapshot([(110, 6, 0, 250)]))
    request(protocol, actor, category=1)
    assert 2 in actor.shop_offers
    path.write_bytes(snapshot([]))
    before = deepcopy(protocol.teams.state)
    protocol._dispatch_game_message(
        actor, ReliablePacket(3, 2, DAT), buy_message(protocol, entry=2, category=1, price=76000)
    )
    assert replies[-1][1] == {"error": (1, 28)} and protocol.teams.state == before


def test_invalid_inventory_claim_cannot_reuse_previous_check(configured):
    _, protocol, replies, _ = configured
    actor = buyer(configured)
    assert actor.owned_parts_claim is not None
    protocol._dispatch_game_message(
        actor, ReliablePacket(3, 2, DAT), GameMessage(VERIFY_OWN_PARTS_LIST, bytes(4), b"\x80", 1)
    )
    assert actor.owned_parts_claim is None
    before = deepcopy(protocol.teams.state)
    protocol._dispatch_game_message(actor, ReliablePacket(4, 3, DAT), buy_message(protocol))
    assert replies[-1][1] == {"error": (1, 28)} and protocol.teams.state == before


def test_new_sale_authority_is_not_restored_from_transport_state(configured):
    from acvd_server.session_state import _load_game_sessions, _persist_game_sessions

    config, _, _, _ = configured
    actor = buyer(configured)
    path = config.session_state_path
    actor.verified_inventory_source = bytes(32)
    actor.verified_inventory_claim = actor.owned_parts_claim
    actor.owned_parts_max_instance_id = 90000
    actor.owned_parts_evidence_serial = 2
    actor.owned_parts_claim_serial = 2
    _persist_game_sessions(path, {actor.auth_token: actor})
    restored = _load_game_sessions(path)[actor.auth_token]
    assert restored.shop_catalogue_version is None
    assert restored.shop_offers == {} and restored.owned_parts_claim is None
    assert restored.verified_inventory_source is None and restored.verified_inventory_claim is None
    assert restored.owned_parts_max_instance_id is None
    assert restored.owned_parts_evidence_serial == 0 and restored.owned_parts_claim_serial == -1
