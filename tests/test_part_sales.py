"""A sale receipt owns both its payment and its removal; no native claim."""

from copy import deepcopy

import pytest

from acvd_server.owned_items import allocate_instances, authorized_inventory, pending_inventory
from acvd_server.part_sales import (
    MAX_U64,
    replay_sale,
    sale_instance,
    sell,
    trash_page,
    without_sold_items,
)
from acvd_server.protocol import ProtocolError, decode_proto, proto_bytes, proto_varint
from acvd_server.teams import TeamStore
from acvd_server.udp import GameSession


def record(instance, part=120):
    return proto_bytes(
        1,
        b"".join(
            proto_varint(n, v) for n, v in [(1, 0), (2, part), (3, 0), (4, instance), (5, 1000)]
        ),
    )


@pytest.fixture
def state(tmp_path):
    teams = TeamStore(tmp_path / "teams")
    teams.remember_player("One", 11)
    teams.remember_player("Two", 22)
    teams.synchronize_balance("One", 100000)
    teams.synchronize_balance("Two", 200000)
    return teams, GameSession(b"owner001", bytes(16), "One", 11)


def apply_sale(state, **changes):
    options = dict(
        inventory=record(10001) + record(10002),
        credit=25000,
        price_source="modeled-price",
        cursor_floor=100,
    )
    options.update(changes)
    return sell(*state, 10002, **options)


def test_sale_is_atomic_and_replay_survives_reload(state):
    teams, session = state
    before = deepcopy(teams.state)
    reply, repeated = apply_sale(state)
    assert reply == proto_varint(1, 101) and not repeated
    assert teams.balance("One") == 125000 and teams.balance("Two") == 200000
    assert teams.state["teams"] == before["teams"] and teams.state["members"] == before["members"]
    loaded = TeamStore(teams.root)
    assert replay_sale(loaded, session, 10002) == reply
    assert sell(loaded, session, 10002, inventory=b"", credit=999, price_source="changed") == (
        reply,
        True,
    )
    assert loaded.balance("One") == 125000


def test_sold_part_does_not_return_from_snapshot_or_pending_receipt(state):
    teams, session = state
    teams.state["shop_purchases"] = {
        "old": dict(
            online_id="One",
            player_id=11,
            category=0,
            part_id=120,
            part_class=0,
            tuning=1000,
            instance_id=10002,
            in_snapshot=False,
        )
    }
    apply_sale(state)
    body = record(10001) + record(10002) + record(0)
    assert without_sold_items(teams, session, body) == record(10001) + record(0)
    assert pending_inventory(teams, session, body) == record(10001) + record(0)
    assert pending_inventory(teams, session, record(10001)) == record(10001)
    assert allocate_instances(teams, session, record(10001), frozenset({10001}), 1) == (10003,)
    other = GameSession(b"owner002", bytes(16), "Two", 22)
    assert without_sold_items(teams, other, body) == body
    assert replay_sale(teams, other, 10002) is None


@pytest.mark.parametrize("body", [b"", record(10001), record(10002) * 2])
def test_missing_or_duplicate_instance_cannot_pay(state, body):
    teams, _ = state
    before = deepcopy(teams.state)
    with pytest.raises(ProtocolError):
        apply_sale(state, inventory=body)
    assert teams.state == before


def test_failed_commit_cannot_remove_or_pay(state, monkeypatch):
    teams, session = state
    before = deepcopy(teams.state)

    def fail(_):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(teams, "commit_state", fail)
    with pytest.raises(OSError):
        apply_sale(state)
    assert teams.state == before and replay_sale(teams, session, 10002) is None


def test_wallet_overflow_keeps_item(state):
    teams, _ = state
    teams.synchronize_balance("One", 999999999)
    with pytest.raises(ProtocolError, match="wallet bound"):
        apply_sale(state)
    assert "part_sales" not in teams.state and teams.balance("One") == 999999999


def test_sale_credit_reduces_a_known_debt(state):
    teams, _ = state
    teams.synchronize_balance("One", -10_000)
    response, repeated = apply_sale(state)
    assert not repeated and response == proto_varint(1, 101)
    assert teams.balance("One") == 15_000


@pytest.mark.parametrize(
    "body",
    [
        b"",
        proto_varint(1, 0),
        proto_varint(1, 9999),
        proto_varint(1, 10001) * 2,
        proto_varint(2, 10001),
        proto_varint(1, 1 << 64),
    ],
)
def test_invalid_sale_request(body):
    with pytest.raises(ProtocolError):
        sale_instance(body)


def test_native_sale_request_scalar():
    assert sale_instance(proto_varint(1, 10001)) == 10001


def test_verified_login_source_survives_server_sale_without_fabricating_claim(state, tmp_path):
    teams, session = state
    path = tmp_path / "players"
    path.mkdir()
    body = record(10001) + record(10002) + record(10003)
    snapshot = path / "11.offline-data.bin"
    snapshot.write_bytes(body)
    sell(teams, session, 10001, inventory=body, credit=10, price_source="fixture")
    claim = proto_varint(1, 2) + proto_varint(2, 10002 ^ 10003)
    session.owned_parts_claim = claim
    view, _ = authorized_inventory(teams, session, path)
    assert view == record(10002) + record(10003)
    sell(teams, session, 10002, inventory=view, credit=10, price_source="fixture")
    view, _ = authorized_inventory(teams, session, path)
    assert view == record(10003)
    assert session.owned_parts_claim == claim and snapshot.read_bytes() == body
    # A different source cannot inherit a prior source's proof.
    snapshot.write_bytes(record(10001) + record(10002) + record(10003, part=121))
    with pytest.raises(ProtocolError, match="native claim"):
        authorized_inventory(teams, session, path)
    assert session.verified_inventory_source is None


def test_new_native_report_invalidates_the_prior_login_proof(state, tmp_path):
    from types import SimpleNamespace

    from acvd_server.handlers.inventory import acknowledge_owned_parts
    from acvd_server.udp import GameMessage

    teams, session = state
    path = tmp_path / "players"
    path.mkdir()
    body = record(10001) + record(10002) + record(10003)
    (path / "11.offline-data.bin").write_bytes(body)
    sell(teams, session, 10001, inventory=body, credit=10, price_source="fixture")
    claim = proto_varint(1, 2) + proto_varint(2, 10002 ^ 10003)
    session.owned_parts_claim = claim
    view, _ = authorized_inventory(teams, session, path)
    sell(teams, session, 10002, inventory=view, credit=10, price_source="fixture")
    acknowledge_owned_parts(
        session,
        GameMessage(0x41D, bytes(4), claim, 1),
        teams=teams,
        player_data_path=path,
        telemetry=SimpleNamespace(emit=lambda *a, **k: None),
        reply=lambda _: None,
    )
    assert session.verified_inventory_source is None
    with pytest.raises(ProtocolError, match="native claim"):
        authorized_inventory(teams, session, path)


def page_request(after=0, maximum_instance=MAX_U64, limit=512):
    return proto_varint(1, limit) + proto_varint(2, after) + proto_varint(3, maximum_instance)


def test_removal_history_is_owner_scoped_and_advances_page_cursor(state):
    teams, session = state
    for identity in (10001, 10002, 10003):
        sell(
            teams, session, identity, inventory=record(identity), credit=10, price_source="fixture"
        )
    before = deepcopy(teams.state)
    body, cursor, count = trash_page(teams, session, page_request(limit=1))
    assert (cursor, count) == (1, 1)
    assert [(f.number, f.value) for f in decode_proto(body)] == [(1, 10001), (2, 1), (3, 3)]
    assert teams.state == before
    body, cursor, count = trash_page(teams, session, page_request(after=1, limit=2))
    assert (cursor, count) == (3, 2)
    assert [(f.number, f.value) for f in decode_proto(body)] == [
        (1, 10002),
        (1, 10003),
        (2, 3),
        (3, 3),
    ]
    sell(teams, session, 10004, inventory=record(10004), credit=10, price_source="fixture")
    body, cursor, count = trash_page(teams, session, page_request(after=3))
    assert (cursor, count) == (4, 1)
    other = GameSession(b"owner002", bytes(16), "Two", 22)
    assert trash_page(teams, other, page_request()) == (
        proto_varint(2, 0) + proto_varint(3, 0),
        0,
        0,
    )


def test_old_server_cursor_is_not_moved_backwards(state):
    assert trash_page(*state, page_request(after=500)) == (
        proto_varint(2, 500) + proto_varint(3, 500),
        500,
        0,
    )
    response, _ = apply_sale(state, cursor_floor=500)
    assert response == proto_varint(1, 501)


def test_native_inventory_maximum_is_not_a_removal_history_bound(state):
    teams, session = state
    apply_sale(state)
    body, cursor, count = trash_page(
        teams, session, page_request(maximum_instance=63876879755356866)
    )
    assert (cursor, count) == (101, 1)
    assert [(f.number, f.value) for f in decode_proto(body)] == [(1, 10002), (2, 101), (3, 101)]
    body, cursor, count = trash_page(teams, session, page_request(maximum_instance=10001))
    assert (cursor, count) == (101, 0)
    assert body == proto_varint(2, 101) + proto_varint(3, 101)
    assert trash_page(teams, session, page_request(after=500, maximum_instance=9))[1:] == (500, 0)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        page_request(limit=0),
        page_request(limit=513),
        page_request(after=MAX_U64),
        page_request() + proto_varint(1, 1),
        page_request(maximum_instance=1 << 64),
    ],
)
def test_invalid_history_requests_cannot_mutate(state, payload):
    before = deepcopy(state[0].state)
    with pytest.raises(ProtocolError):
        trash_page(*state, payload)
    assert state[0].state == before
