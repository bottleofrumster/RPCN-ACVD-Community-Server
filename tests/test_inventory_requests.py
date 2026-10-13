"""Independent ownership models for the extracted inventory request boundary."""

import random
from collections import Counter

import pytest

from acvd_server.handlers import inventory
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.udp import (
    GET_MY_ITEM_SIMPLE_INFO_LIST_ALT,
    SYNC_OFFLINE_DATA,
    GameMessage,
    GameSession,
)


class Events:
    def emit(self, *args, **kwargs):
        pass


def record(category, instance, tuning=1000):
    return proto_bytes(
        1,
        b"".join(
            proto_varint(n, v)
            for n, v in (
                (1, category),
                (2, 110 + category),
                (3, category),
                (4, instance),
                (5, tuning),
            )
        ),
    )


def invoke(root, session, function, opcode, payload):
    replies = []
    function(
        session,
        GameMessage(opcode, b"\0\0\0\1", payload, 1),
        player_data_path=root,
        telemetry=Events(),
        reply=replies.append,
    )
    assert len(replies) == 1
    return replies[0]


def read(root, session, cursor=0, other_id=99):
    response = invoke(
        root,
        session,
        inventory.get_owned_items,
        GET_MY_ITEM_SIMPLE_INFO_LIST_ALT,
        proto_varint(1, other_id)
        + proto_varint(2, 3000)
        + proto_varint(3, 0)
        + proto_varint(4, cursor),
    )
    rows = Counter()
    for field in decode_proto(response):
        fields = decode_proto(field.value)
        rows[tuple(first_field(fields, n).value for n in (1, 3, 4, 6))] += 1
    return rows


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_interleaved_accounts_keep_exact_category_snapshots(tmp_path, seed):
    rng = random.Random(seed)
    sessions = [
        GameSession(b"owner001", bytes(16), "One", 11),
        GameSession(b"owner002", bytes(16), "Two", 22),
    ]
    expected = [{}, {}]
    for update in range(40):
        owner = rng.randrange(2)
        category = rng.randrange(6)
        rows = [
            (
                100000 * owner + 100 * update + i + 1,
                110 + category,
                category,
                1000 + rng.randrange(4),
            )
            for i in range(rng.randrange(1, 5))
        ]
        expected[owner][category] = rows
        body = b"".join(record(category, row[0], row[3]) for row in rows)
        invoke(tmp_path, sessions[owner], inventory.sync_offline_data, SYNC_OFFLINE_DATA, body)
        for who, session in enumerate(sessions):
            model = Counter(row for group in expected[who].values() for row in group)
            assert read(tmp_path, session, other_id=sessions[1 - who].player_id) == model


def test_uint64_cursor_boundary_never_redelivers_the_last_instance(tmp_path):
    session = GameSession(b"owner001", bytes(16), "One", 11)
    maximum = (1 << 64) - 1
    invoke(
        tmp_path,
        session,
        inventory.sync_offline_data,
        SYNC_OFFLINE_DATA,
        record(1, maximum - 1) + record(1, maximum, 1123),
    )
    assert read(tmp_path, session, maximum - 1) == Counter({(maximum, 111, 1, 1123): 1})
    assert read(tmp_path, session, maximum) == Counter()


def test_promoted_preseed_guard_expires_without_freezing_later_sales(tmp_path, monkeypatch):
    session = GameSession(b"owner001", bytes(16), "One", 11)
    now = [10.0]
    monkeypatch.setattr(inventory.time, "monotonic", lambda: now[0])
    original = record(1, 101) + record(1, 102)
    after_sale = record(1, 102)
    preseed = tmp_path / "11.offline-data.preseed.bin"
    preseed.write_bytes(original)
    invoke(tmp_path, session, inventory.sync_offline_data, SYNC_OFFLINE_DATA, original)
    assert not preseed.exists() and list(tmp_path.glob("*.applied-*.bin"))
    invoke(tmp_path, session, inventory.sync_offline_data, SYNC_OFFLINE_DATA, after_sale)
    assert (tmp_path / "11.offline-data.bin").read_bytes() == original
    assert (tmp_path / "11.offline-data.deferred.bin").read_bytes() == after_sale
    now[0] = 311.0
    invoke(tmp_path, session, inventory.sync_offline_data, SYNC_OFFLINE_DATA, after_sale)
    assert (tmp_path / "11.offline-data.bin").read_bytes() == after_sale
    assert read(tmp_path, session) == Counter({(102, 111, 1, 1000): 1})
