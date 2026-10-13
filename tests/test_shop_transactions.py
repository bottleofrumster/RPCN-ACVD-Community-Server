"""Paid ownership and wallet changes share one durable receipt."""

from copy import deepcopy
from dataclasses import replace

import pytest

from acvd_server.handlers import inventory as handlers
from acvd_server.inventory import PART_CATEGORY_FOR_CLASS
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.shop_catalogue import CataloguePart
from acvd_server.shop_transactions import (
    ShopError,
    confirm_inventory,
    pending_inventory,
    purchase,
    replay_purchase,
)
from acvd_server.teams import TeamStore
from acvd_server.udp import (
    GET_MY_ITEM_SIMPLE_INFO_LIST_ALT,
    SYNC_OFFLINE_DATA,
    GameMessage,
    GameSession,
)

OFFER = CataloguePart(1, 0, 0, 210, 54000)


def record(instance=50, part=111, tuning=1000):
    return proto_bytes(
        1,
        b"".join(
            proto_varint(n, v) for n, v in ((1, 0), (2, part), (3, 0), (4, instance), (5, tuning))
        ),
    )


@pytest.fixture
def state(tmp_path):
    teams = TeamStore(tmp_path / "teams")
    teams.remember_player("One", 11)
    teams.remember_player("Two", 22)
    teams.synchronize_balance("One", 165357)
    teams.synchronize_balance("Two", 200000)
    session = GameSession(b"owner001", bytes(16), "One", 11)
    payload = b"".join(
        proto_varint(n, v) for n, v in ((2, 1), (3, 1), (4, 0), (5, 54000), (7, 1000))
    )
    message = GameMessage(0x3DD, b"\0\0\0\1", payload, 1)
    return teams, session, message, tmp_path / "players"


def buy(state, **kwargs):
    teams, session, message, _ = state
    options = dict(
        offer=OFFER,
        shop_key=1,
        verified_level=0,
        inventory=record(),
        reserved_instance_ids=frozenset({50}),
    )
    options.update(kwargs)
    return purchase(teams, session, message, **options)


def test_one_commit_owns_wallet_and_pending_part_and_survives_reload(state):
    teams, session, message, _ = state
    before = deepcopy(teams.state)
    response, duplicate = buy(state)
    instance = first_field(decode_proto(response), 1).value
    # The low legacy fixture ID must not cause a new ordinary ID below 10000.
    assert instance == 10000 and not duplicate
    assert teams.balance("One") == 111357 and teams.balance("Two") == 200000
    assert teams.state["teams"] == before["teams"] and teams.state["members"] == before["members"]
    loaded = TeamStore(teams.root)
    retry = purchase(
        loaded,
        session,
        message,
        offer=None,
        shop_key=0,
        verified_level=None,
        inventory=None,
        reserved_instance_ids=frozenset(),
    )
    assert retry == (response, True)
    assert loaded.balance("One") == 111357 and len(loaded.state["shop_purchases"]) == 1
    assert pending_inventory(loaded, session, record()) == record() + record(instance, 210)


def test_purchase_rejects_a_preexisting_over_cap_wallet_without_charging(state):
    teams, _, _, _ = state
    invalid = deepcopy(teams.state)
    invalid["player_profiles"]["One"]["balance"] = 1_000_000_001
    teams.commit_state(invalid)
    before, disk = deepcopy(teams.state), teams.state_path.read_bytes()
    with pytest.raises(ShopError, match="Known sufficient funds"):
        buy(state)
    assert teams.state == before and teams.state_path.read_bytes() == disk


def test_purchase_keeps_unregistered_rows_and_checks_registered_claim(state):
    from acvd_server.owned_items import authorized_inventory

    teams, session, message, path = state
    path.mkdir()
    body = record(10001) + record(0) * 90
    target = path / "11.offline-data.bin"
    target.write_bytes(body)
    session.owned_parts_claim = proto_varint(1, 1) + proto_varint(2, 10001)
    inventory, reservations = authorized_inventory(teams, session, path)
    assert inventory == body and reservations == frozenset({10001})
    response, replayed = purchase(
        teams,
        session,
        message,
        offer=OFFER,
        shop_key=1,
        verified_level=0,
        inventory=inventory,
        reserved_instance_ids=reservations,
    )
    assert not replayed and first_field(decode_proto(response), 1).value == 10002
    assert teams.balance("One") == 111357
    assert target.read_bytes() == body
    assert pending_inventory(teams, session, body) == body + record(10002, 210)


def test_unregistered_rows_still_count_toward_purchase_capacity(state):
    with pytest.raises(ShopError, match="garage upper bound"):
        buy(
            state,
            inventory=record(10001) + record(0) * 2979,
            reserved_instance_ids=frozenset({10001}),
        )


def test_read_only_replay_survives_reload_and_later_wallet_change(state):
    teams, session, message, _ = state
    assert replay_purchase(teams, session, message) is None
    expected, _ = buy(state)
    teams.synchronize_balance("One", 90000)
    loaded = TeamStore(teams.root)
    before, disk = deepcopy(loaded.state), loaded.state_path.read_bytes()
    assert replay_purchase(loaded, session, message) == expected
    assert loaded.state == before and loaded.state_path.read_bytes() == disk


@pytest.mark.parametrize("change", ["account", "player", "auth", "index", "counter", "payload"])
def test_replay_does_not_match_a_different_request_identity(state, change):
    teams, session, message, _ = state
    buy(state)
    if change == "account":
        session = replace(session, online_id="Two")
    elif change == "player":
        session = replace(session, player_id=22)
    elif change == "auth":
        session = replace(session, auth_token=b"other001")
    elif change == "index":
        message = replace(message, message_index=b"\0\0\0\2")
    elif change == "counter":
        message = replace(message, packet_counter=2)
    else:
        message = replace(message, payload=message.payload[:-2] + b"\xe9\x07")
    before = deepcopy(teams.state)
    assert replay_purchase(teams, session, message) is None
    assert teams.state == before


@pytest.mark.parametrize(
    "key,value",
    [
        ("online_id", "Two"),
        ("player_id", 22),
        ("player_id", "11"),
        ("instance_id", 0),
        ("instance_id", -1),
        ("instance_id", True),
        ("instance_id", 1 << 64),
    ],
)
def test_corrupt_receipt_is_not_returned_as_success(state, key, value):
    teams, session, message, _ = state
    buy(state)
    receipt = next(iter(teams.state["shop_purchases"].values()))
    receipt[key] = value
    before = deepcopy(teams.state)
    with pytest.raises(ShopError, match="receipt|instance"):
        replay_purchase(teams, session, message)
    with pytest.raises(ShopError, match="receipt|instance"):
        buy(state)
    assert teams.state == before


@pytest.mark.parametrize(
    "target,field,value",
    [
        ("message", "message_index", b""),
        ("message", "message_index", b"12345"),
        ("message", "packet_counter", -1),
        ("message", "packet_counter", 65536),
        ("message", "packet_counter", True),
        ("session", "auth_token", b"short"),
        ("session", "player_id", 0),
        ("session", "player_id", -1),
        ("session", "player_id", 1 << 64),
        ("session", "online_id", ""),
    ],
)
def test_replay_rejects_invalid_request_metadata(state, target, field, value):
    teams, session, message, _ = state
    if target == "message":
        message = replace(message, **{field: value})
    else:
        session = replace(session, **{field: value})
    before = deepcopy(teams.state)
    with pytest.raises(ShopError, match="identity"):
        replay_purchase(teams, session, message)
    assert teams.state == before


def test_distinct_intentions_and_other_accounts_get_distinct_receipts(state):
    teams, session, message, root = state
    first = first_field(decode_proto(buy(state)[0]), 1).value
    second = first_field(
        decode_proto(buy((teams, session, replace(message, message_index=b"\0\0\0\2"), root))[0]), 1
    ).value
    peer = GameSession(b"owner002", bytes(16), "Two", 22)
    third = first_field(decode_proto(buy((teams, peer, message, root))[0]), 1).value
    assert len({first, second, third}) == 3
    assert teams.balance("One") == 57357 and teams.balance("Two") == 146000
    one = pending_inventory(teams, session, b"")
    two = pending_inventory(teams, peer, b"")
    assert len(decode_proto(one)) == 2 and len(decode_proto(two)) == 1


@pytest.mark.parametrize(
    "options",
    [
        {"offer": None},
        {"shop_key": 2},
        {"verified_level": None},
        {"verified_level": True},
        {"offer": replace(OFFER, minimum_level=1)},
        {"offer": replace(OFFER, price=54001)},
        {"inventory": None},
        {"inventory": record((1 << 64) - 1), "reserved_instance_ids": frozenset({(1 << 64) - 1})},
        {"inventory": record() * 3000},
        {"reserved_instance_ids": frozenset()},
    ],
)
def test_invalid_authority_does_not_charge_or_create_ownership(state, options):
    teams, _, _, _ = state
    before = deepcopy(teams.state)
    stored = teams.state_path.read_bytes()
    with pytest.raises(ShopError):
        buy(state, **options)
    assert teams.state == before and teams.state_path.read_bytes() == stored


@pytest.mark.parametrize("count", [2979, 2980, 2999, 3000])
def test_registered_inventory_upper_bound_matches_native_constant(state, count):
    # This checks the necessary registered-item bound only. Real carry-over
    # entries can make the garage full sooner; no native free-slot claim here.
    teams, _, _, _ = state
    ids = frozenset(range(10000, 10000 + count))
    inventory = b"".join(record(instance) for instance in sorted(ids))
    before = deepcopy(teams.state)
    if count < 2980:
        response, _ = buy(state, inventory=inventory, reserved_instance_ids=ids)
        assert first_field(decode_proto(response), 1).value == max(ids) + 1
    else:
        with pytest.raises(ShopError, match="garage upper bound"):
            buy(state, inventory=inventory, reserved_instance_ids=ids)
        assert teams.state == before


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        proto_varint(2, 1) * 5,
        b"".join(proto_varint(n, v) for n, v in ((2, 1), (3, 1), (4, 0), (5, 12345), (7, 1000))),
        b"".join(proto_varint(n, v) for n, v in ((2, 1), (3, 1), (4, 0), (5, 54000), (7, 1999))),
    ],
)
def test_invalid_request_cannot_change_wallet(state, payload):
    teams, s, m, r = state
    before = deepcopy(teams.state)
    with pytest.raises(ShopError):
        buy((teams, s, replace(m, payload=payload), r))
    assert teams.state == before


def test_missing_wallet_is_not_zero_or_a_grant(state):
    teams, s, m, r = state
    del teams.state["player_profiles"]["One"]["balance"]
    before = deepcopy(teams.state)
    with pytest.raises(ShopError):
        buy(state)
    assert teams.state == before


@pytest.mark.parametrize("part_class", [2, 10, 11])
@pytest.mark.parametrize(
    "power,rapid,accuracy",
    [(p, r, a) for p in range(4) for r in range(4) for a in range(4) if p + r + a <= 3],
)
def test_qualified_tuning_allocation_is_retained_in_receipt_and_readback(
    state, part_class, power, rapid, accuracy
):
    teams, session, message, root = state
    code = 1000 + 100 * power + 10 * rapid + accuracy
    offer = CataloguePart(48, 0, PART_CATEGORY_FOR_CLASS[part_class], 420, 20000)
    payload = b"".join(
        proto_varint(n, v) for n, v in ((2, 1), (3, 48), (4, part_class), (5, 20000), (7, code))
    )
    message = replace(message, payload=payload)
    response, duplicate = buy((teams, session, message, root), offer=offer, tuning_budget=3)
    instance = first_field(decode_proto(response), 1).value
    assert not duplicate and teams.balance("One") == 145357
    loaded = TeamStore(teams.root)
    receipt = next(iter(loaded.state["shop_purchases"].values()))
    assert receipt["tuning"] == code
    row = decode_proto(pending_inventory(loaded, session, b""))[0]
    assert {f.number: f.value for f in decode_proto(row.value)} == {
        1: offer.category,
        2: 420,
        3: part_class,
        4: instance,
        5: code,
    }
    # A committed retry must not lose the part when the caller's capability
    # view later changes or becomes unavailable.
    assert buy((loaded, session, message, root), offer=None, tuning_budget=None) == (response, True)


@pytest.mark.parametrize(
    "code,budget,part_class",
    [
        (1003, 0, 10),
        (1003, 2, 10),
        (1004, 3, 10),
        (1040, 3, 10),
        (1400, 3, 10),
        (1112, 3, 10),
        (999, 3, 10),
        (2000, 3, 10),
        (1001, 3, 0),
        (1000, None, 10),
        (1000, True, 10),
        (1000, 4, 10),
    ],
)
def test_invalid_tuning_never_charges_or_writes_a_receipt(state, code, budget, part_class):
    teams, session, message, root = state
    before = deepcopy(teams.state)
    disk = teams.state_path.read_bytes()
    offer = CataloguePart(48, 0, PART_CATEGORY_FOR_CLASS[part_class], 420, 20000)
    payload = b"".join(
        proto_varint(n, v) for n, v in ((2, 1), (3, 48), (4, part_class), (5, 20000), (7, code))
    )
    with pytest.raises(ShopError):
        buy(
            (teams, session, replace(message, payload=payload), root),
            offer=offer,
            tuning_budget=budget,
        )
    assert teams.state == before and teams.state_path.read_bytes() == disk


def test_write_failure_does_not_apply_in_memory_or_return_success(state, monkeypatch):
    teams, _, _, _ = state
    before = deepcopy(teams.state)
    stored = teams.state_path.read_bytes()

    def fail():
        raise OSError("storage failure")

    monkeypatch.setattr(teams, "_save", fail)
    with pytest.raises(OSError):
        buy(state)
    assert teams.state == before and teams.state_path.read_bytes() == stored


def test_reconciliation_preserves_tuning_and_never_duplicates(state):
    teams, s, _, _ = state
    instance = first_field(decode_proto(buy(state)[0]), 1).value
    snapshot = record() + record(instance, 210, 1123)
    before = deepcopy(teams.state)
    assert pending_inventory(teams, s, snapshot) == snapshot and teams.state == before
    with pytest.raises(ShopError):
        pending_inventory(teams, s, record(instance, 999))
    confirm_inventory(teams, s, snapshot)
    assert pending_inventory(teams, s, snapshot) == snapshot
    # Once an upload owns the record, later removal is not undone by a receipt.
    assert pending_inventory(teams, s, b"") == b""


class Events:
    def emit(self, *args, **kwargs):
        pass


def test_normal_readback_and_upload_integrate_the_receipt(state):
    teams, s, _, root = state
    instance = first_field(decode_proto(buy(state)[0]), 1).value
    replies = []
    query = GameMessage(
        GET_MY_ITEM_SIMPLE_INFO_LIST_ALT,
        b"\0\0\0\2",
        proto_varint(2, 3000) + proto_varint(3, 0) + proto_varint(4, 50),
        2,
    )
    handlers.get_owned_items(
        s, query, player_data_path=root, teams=teams, telemetry=Events(), reply=replies.append
    )
    item = decode_proto(decode_proto(replies[0])[0].value)
    assert first_field(item, 1).value == instance and first_field(item, 3).value == 210
    upload = GameMessage(SYNC_OFFLINE_DATA, b"\0\0\0\3", record() + record(instance, 210), 3)
    handlers.sync_offline_data(
        s, upload, player_data_path=root, teams=teams, telemetry=Events(), reply=replies.append
    )
    assert all(r["in_snapshot"] for r in teams.state["shop_purchases"].values())
    stored = (root / "11.offline-data.bin").read_bytes()
    assert stored == upload.payload and pending_inventory(teams, s, stored) == stored


def test_inventory_class_mapping_is_shared_with_save_extraction():
    from tools.migration.extract_offline_inventory import FIELD1_FOR_CLASS

    assert FIELD1_FOR_CLASS is PART_CATEGORY_FOR_CLASS


def test_purchased_instance_cannot_be_duplicated_before_or_after_confirmation(state):
    teams, session, _, _ = state
    instance = first_field(decode_proto(buy(state)[0]), 1).value
    snapshot = record() + record(instance, 210)
    duplicate = snapshot + record(instance, 210)
    with pytest.raises(ShopError):
        pending_inventory(teams, session, duplicate)
    confirm_inventory(teams, session, snapshot)
    with pytest.raises(ShopError):
        pending_inventory(teams, session, duplicate)


def test_reviewed_preseed_keeps_only_purchases_still_owned_in_canonical_snapshot(state):
    teams, session, _, root = state
    instance = first_field(decode_proto(buy(state)[0]), 1).value
    snapshot = record() + record(instance, 210, 1123)
    root.mkdir()
    (root / "11.offline-data.bin").write_bytes(snapshot)
    (root / "11.offline-data.preseed.bin").write_bytes(record())
    confirm_inventory(teams, session, snapshot)
    replies = []
    query = GameMessage(
        GET_MY_ITEM_SIMPLE_INFO_LIST_ALT,
        b"\0\0\0\7",
        proto_varint(2, 3000) + proto_varint(4, 50),
        7,
    )
    handlers.get_owned_items(
        session, query, player_data_path=root, teams=teams, telemetry=Events(), reply=replies.append
    )
    item = decode_proto(decode_proto(replies[-1])[0].value)
    assert first_field(item, 1).value == instance and first_field(item, 6).value == 1123
    (root / "11.offline-data.bin").write_bytes(record())
    handlers.get_owned_items(
        session, query, player_data_path=root, teams=teams, telemetry=Events(), reply=replies.append
    )
    assert replies[-1] == b""  # Do not resurrect a removed, confirmed purchase.


def test_allocation_skips_reserved_ids_and_is_not_blocked_by_another_high_cursor(state):
    teams, s, m, root = state
    peer = GameSession(b"owner002", bytes(16), "Two", 22)
    high = (1 << 64) - 2
    buy((teams, peer, m, root), inventory=record(high), reserved_instance_ids=frozenset({high, 50}))
    response, _ = buy(state, reserved_instance_ids=frozenset({50, 10000, 10001, high}))
    assert first_field(decode_proto(response), 1).value == 10002


@pytest.mark.parametrize(
    "existing,expected", [(None, 10000), (0, 10000), (9999, 10000), (10000, 10001)]
)
def test_first_purchase_never_uses_the_native_special_id_range(state, existing, expected):
    inventory = b"" if existing is None else record(existing)
    reserved = frozenset() if existing is None else frozenset({existing})
    response, duplicate = buy(state, inventory=inventory, reserved_instance_ids=reserved)
    assert not duplicate and first_field(decode_proto(response), 1).value == expected
    teams, session, message, _ = state
    loaded = TeamStore(teams.root)
    replay = purchase(
        loaded,
        session,
        message,
        offer=None,
        shop_key=0,
        verified_level=None,
        inventory=None,
        reserved_instance_ids=frozenset(),
    )
    assert replay == (response, True)
    assert loaded.balance("One") == 111357


def test_conflicting_upload_is_deferred_and_old_upload_does_not_erase_purchase(state):
    teams, s, _, root = state
    instance = first_field(decode_proto(buy(state)[0]), 1).value
    root.mkdir()
    target = root / "11.offline-data.bin"
    target.write_bytes(record())
    bad = GameMessage(SYNC_OFFLINE_DATA, b"\0\0\0\5", record(instance, 999), 5)
    handlers.sync_offline_data(
        s, bad, player_data_path=root, teams=teams, telemetry=Events(), reply=lambda _: None
    )
    assert target.read_bytes() == record()
    assert (root / "11.offline-data.deferred.bin").read_bytes() == bad.payload
    assert not any(r["in_snapshot"] for r in teams.state["shop_purchases"].values())
    old = replace(bad, payload=record())
    handlers.sync_offline_data(
        s, old, player_data_path=root, teams=teams, telemetry=Events(), reply=lambda _: None
    )
    assert pending_inventory(teams, s, target.read_bytes()) == record() + record(instance, 210)


def test_snapshot_write_before_receipt_confirmation_is_recoverable(state, monkeypatch):
    teams, s, _, root = state
    instance = first_field(decode_proto(buy(state)[0]), 1).value
    upload = GameMessage(SYNC_OFFLINE_DATA, b"\0\0\0\6", record() + record(instance, 210), 6)
    replies = []
    original = teams._save

    def fail():
        raise OSError("receipt confirmation failed")

    monkeypatch.setattr(teams, "_save", fail)
    with pytest.raises(OSError):
        handlers.sync_offline_data(
            s, upload, player_data_path=root, teams=teams, telemetry=Events(), reply=replies.append
        )
    assert replies == []
    assert (root / "11.offline-data.bin").read_bytes() == upload.payload
    assert pending_inventory(teams, s, upload.payload) == upload.payload
    assert teams.balance("One") == 111357
    monkeypatch.setattr(teams, "_save", original)
    handlers.sync_offline_data(
        s, upload, player_data_path=root, teams=teams, telemetry=Events(), reply=replies.append
    )
    assert len(replies) == 1 and all(
        r["in_snapshot"] for r in teams.state["shop_purchases"].values()
    )
