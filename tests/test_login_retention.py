"""Server persistence regressions; all player records live in pytest temp dirs."""

import json
from pathlib import Path

import pytest

from acvd_server.inventory import _merge_offline_inventory
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.server import GameDatagramProtocol, _item_simple_info_list
from acvd_server.teams import TeamStore
from acvd_server.telemetry import Telemetry
from acvd_server.udp import (
    DAT,
    GET_MY_ITEM_SIMPLE_INFO_LIST,
    GET_MY_ITEM_SIMPLE_INFO_LIST_ALT,
    SYNC_OFFLINE_DATA,
    GameMessage,
    GameSession,
    ReliablePacket,
    encode_game_request_fragments,
)

ROOT = Path(__file__).resolve().parents[1]


def inventory(category=1, instance=123456, part=110):
    return proto_bytes(
        1,
        b"".join(
            proto_varint(n, v)
            for n, v in ((1, category), (2, part), (3, category), (4, instance), (5, 1123))
        ),
    )


def key_item(selector=0x20000, item=11):
    return proto_bytes(3, proto_varint(1, selector) + proto_varint(2, item))


def protocol_fixture(tmp_path, monkeypatch):
    players = tmp_path / "players"
    players.mkdir()
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"),
        {},
        ROOT / "native/libacvd_cwc.so",
        players,
        TeamStore(tmp_path / "teams"),
    )
    replies = []
    monkeypatch.setattr(protocol, "_send_game_reply", lambda *args: replies.append(args[-1]))
    session = GameSession(b"token123", bytes(range(16)), "Pilot", 42)

    def upload(payload, sequence=1, message_type=SYNC_OFFLINE_DATA):
        message = GameMessage(message_type, sequence.to_bytes(4, "big"), payload, sequence)
        for index, fragment in enumerate(encode_game_request_fragments(message)):
            protocol._handle_game_payload(
                session,
                ReliablePacket(
                    (sequence + index) % 4096,
                    0,
                    DAT,
                    payload=fragment,
                ),
            )

    return players, replies, upload


@pytest.mark.parametrize(
    "incoming",
    [
        b"\x0a\x01\x08",  # truncated nested varint
        proto_varint(1, 1),  # wrong top-level wire type
        proto_bytes(1, proto_varint(1, 1)),  # category exists, ownership fields absent
        proto_bytes(1, proto_varint(1, 1) + proto_bytes(2, b"bad")),
        inventory() + proto_varint(3, 1),  # do not partially apply a mixed bad batch
    ],
)
@pytest.mark.parametrize("has_existing", [False, True])
def test_invalid_upload_never_replaces_canonical(tmp_path, monkeypatch, incoming, has_existing):
    players, replies, upload = protocol_fixture(tmp_path, monkeypatch)
    target = players / "42.offline-data.bin"
    previous = inventory() + inventory(4, 999) + key_item()
    if has_existing:
        target.write_bytes(previous)
    upload(incoming)
    if has_existing:
        assert target.read_bytes() == previous
    else:
        assert not target.exists()
    assert (players / "42.offline-data.deferred.bin").read_bytes() == incoming
    assert first_field(decode_proto(replies[-1]), 1).value == 0
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    event = next(e for e in reversed(events) if e["event"] == "game_offline_data_synchronized")
    assert event["deferred_reason"]
    assert event["merged_categories"] == []


@pytest.mark.parametrize(
    "previous",
    [
        b"\x0a\x01\x08",
        inventory() + proto_varint(3, 1),
        inventory() + proto_bytes(1, b"\x08"),
    ],
)
def test_corrupt_existing_record_requires_review_not_silent_replacement(
    tmp_path, monkeypatch, previous
):
    players, _, upload = protocol_fixture(tmp_path, monkeypatch)
    target = players / "42.offline-data.bin"
    target.write_bytes(previous)
    upload(inventory(4, 999))
    assert target.read_bytes() == previous
    assert (players / "42.offline-data.deferred.bin").read_bytes() == inventory(4, 999)


def test_invalid_exact_preseed_is_not_promoted(tmp_path, monkeypatch):
    players, _, upload = protocol_fixture(tmp_path, monkeypatch)
    malformed = proto_bytes(1, proto_varint(1, 1))
    preseed = players / "42.offline-data.preseed.bin"
    preseed.write_bytes(malformed)
    upload(malformed)
    assert preseed.read_bytes() == malformed
    assert not (players / "42.offline-data.bin").exists()
    assert not list(players.glob("*.applied-*.bin"))


def test_valid_reduction_and_auxiliary_updates_are_not_frozen(tmp_path, monkeypatch):
    players, _, upload = protocol_fixture(tmp_path, monkeypatch)
    target = players / "42.offline-data.bin"
    other = inventory(4, 444)
    target.write_bytes(inventory() + inventory(instance=222) + other + key_item())
    # A valid category snapshot may reflect a sale; do not union removed items back.
    upload(inventory())
    assert target.read_bytes() == other + inventory() + key_item()
    upload(key_item(item=22), 2)
    assert target.read_bytes() == other + inventory() + key_item(item=22)
    before = target.read_bytes()
    upload(b"", 3)
    assert target.read_bytes() == before
    assert not (players / "42.offline-data.deferred.bin").exists()


@pytest.mark.parametrize("first,second", [(0x20000, 0x30000), (0x30000, 0x20000)])
def test_key_item_upload_preserves_the_other_complete_collection(first, second):
    previous = inventory() + key_item(first, 1) + key_item(first + 1, 2)
    incoming = key_item(second, 3) + key_item(second + 4, 4)
    merged, categories, numbers = _merge_offline_inventory(previous, incoming)
    assert merged == previous + incoming
    assert categories == [] and numbers == [3]
    assert _merge_offline_inventory(merged, incoming)[0] == merged


@pytest.mark.parametrize("namespace,other", [(0x20000, 0x30000), (0x30000, 0x20000)])
def test_key_item_snapshot_removes_absent_selectors_not_other_namespaces(namespace, other):
    retained = key_item(other, 80)
    previous = key_item(namespace, 1) + key_item(namespace + 1, 2) + retained
    incoming = key_item(namespace, 3)
    # The producer walks its full collection, not one selector or a historical union.
    assert _merge_offline_inventory(previous, incoming)[0] == retained + incoming


def test_key_item_mixed_namespaces_replace_both_and_preserve_other_families():
    auxiliary = proto_bytes(2, b"unchanged") + proto_bytes(9, b"unknown-family")
    previous = inventory() + auxiliary + key_item() + key_item(0x30000, 3)
    incoming = key_item(0x20001, 4) + key_item(0x30003, 5)
    assert _merge_offline_inventory(previous, incoming)[0] == (
        inventory() + proto_bytes(2, b"unchanged") + incoming + proto_bytes(9, b"unknown-family")
    )


def test_empty_upload_does_not_clear_key_item_namespaces():
    previous = key_item() + key_item(0x30000, 3)
    assert _merge_offline_inventory(previous, b"")[0] == previous
    assert _merge_offline_inventory(previous, inventory())[0] == inventory() + previous


def test_key_item_nested_extension_and_record_order_are_preserved():
    nested = proto_varint(2, 123) + proto_bytes(7, b"extension") + proto_varint(1, 0x20001)
    incoming = proto_bytes(3, nested) + key_item(0x20000, 11)
    previous = key_item(0x30000, 3)
    assert _merge_offline_inventory(previous, incoming)[0] == previous + incoming


@pytest.mark.parametrize(
    "invalid",
    [
        b"",
        b"\x08",
        proto_varint(1, 0x20000),
        proto_varint(2, 1),
        proto_varint(1, 0x20000) + proto_bytes(2, b"bad"),
        proto_bytes(1, b"bad") + proto_varint(2, 1),
        proto_varint(1, 0x20000) + proto_varint(1, 0x30000) + proto_varint(2, 1),
        proto_varint(1, 0x20000) + proto_varint(2, 1) + proto_varint(2, 2),
        proto_varint(1, 0x40000) + proto_varint(2, 1),
        proto_varint(1, 0x100020000) + proto_varint(2, 1),
    ],
)
@pytest.mark.parametrize("bad_existing", [False, True])
def test_invalid_key_item_batch_is_deferred_without_partial_inventory_write(
    tmp_path, monkeypatch, invalid, bad_existing
):
    players, replies, upload = protocol_fixture(tmp_path, monkeypatch)
    target = players / "42.offline-data.bin"
    good = inventory() + key_item() + key_item(0x30000, 3)
    bad = inventory(instance=999) + proto_bytes(3, invalid)
    previous, incoming = (bad, good) if bad_existing else (good, bad)
    target.write_bytes(previous)
    upload(incoming)
    assert target.read_bytes() == previous
    assert (players / "42.offline-data.deferred.bin").read_bytes() == incoming
    assert first_field(decode_proto(replies[-1]), 1).value == 0


def test_key_item_collections_survive_storage_restart_and_keep_history(tmp_path, monkeypatch):
    players, _, upload = protocol_fixture(tmp_path, monkeypatch)
    emblems = b"".join(key_item(0x20000 + i % 9, i) for i in range(183))
    garages = b"".join(key_item(0x30000 + i % 4, i) for i in range(27))
    upload(emblems)
    target = players / "42.offline-data.bin"
    # A new protocol instance has no old per-session merge state.
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "restart-events.jsonl"),
        {},
        ROOT / "native/libacvd_cwc.so",
        players,
        TeamStore(tmp_path / "teams"),
    )
    monkeypatch.setattr(protocol, "_send_game_reply", lambda *args: None)
    session = GameSession(b"newtoken", bytes(range(16)), "Pilot", 42)
    message = GameMessage(SYNC_OFFLINE_DATA, b"\0\0\0\1", garages, 1)
    for index, fragment in enumerate(encode_game_request_fragments(message)):
        protocol._handle_game_payload(session, ReliablePacket(index + 1, 0, DAT, payload=fragment))
    assert target.read_bytes() == emblems + garages
    assert len(decode_proto(target.read_bytes())) == 210
    assert any(
        p.read_bytes() == emblems for p in (players / ".history/42.offline-data.bin").glob("*.bin")
    )


def test_repeated_invalid_uploads_keep_history_then_valid_upload_recovers(tmp_path, monkeypatch):
    players, replies, upload = protocol_fixture(tmp_path, monkeypatch)
    target = players / "42.offline-data.bin"
    original = inventory() + inventory(4, 444)
    target.write_bytes(original)
    first_bad, second_bad = b"\x0a\x01\x08", proto_varint(1, 1)
    upload(first_bad)
    upload(second_bad, 2)
    assert target.read_bytes() == original
    assert (players / "42.offline-data.deferred.bin").read_bytes() == second_bad
    history = list((players / ".history/42.offline-data.deferred.bin").glob("*.bin"))
    assert len(history) == 1 and history[0].read_bytes() == first_bad
    upload(inventory(1, 777), 3)
    assert target.read_bytes() == inventory(4, 444) + inventory(1, 777)
    originals = list((players / ".history/42.offline-data.bin").glob("*.bin"))
    assert len(originals) == 1 and originals[0].read_bytes() == original
    assert all(first_field(decode_proto(reply), 1).value == 0 for reply in replies)


def test_persistence_validation_does_not_change_existing_readback_policy():
    # This patch protects writes; partial historical readback is a separate
    # policy. Do not accidentally turn one incomplete item into an empty list.
    payload = inventory() + proto_bytes(1, proto_varint(1, 4))
    assert _item_simple_info_list(3000, 0, offline_inventory=payload) == (
        _item_simple_info_list(3000, 0, offline_inventory=inventory())
    )


@pytest.mark.parametrize(
    "message_type", [GET_MY_ITEM_SIMPLE_INFO_LIST, GET_MY_ITEM_SIMPLE_INFO_LIST_ALT]
)
@pytest.mark.parametrize("has_live", [False, True])
def test_reviewed_inventory_readback_survives_deferred_upload(
    tmp_path, monkeypatch, message_type, has_live
):
    players, replies, request = protocol_fixture(tmp_path, monkeypatch)
    target = players / "42.offline-data.bin"
    previous = inventory(instance=111)
    if has_live:
        target.write_bytes(previous)
    reviewed = inventory(instance=777) + inventory(instance=888)
    preseed = players / "42.offline-data.preseed.bin"
    preseed.write_bytes(reviewed)
    # Reconstruction submits a conflicting/default batch then requests items.
    request(inventory(instance=0))
    query = proto_varint(2, 3000) + proto_varint(3, 0) + proto_varint(4, 0)
    request(query, 2, message_type)
    assert replies[-1] == _item_simple_info_list(3000, 0, offline_inventory=reviewed)
    assert preseed.read_bytes() == reviewed  # reads must not consume promotion
    if has_live:
        assert target.read_bytes() == previous
    else:
        assert not target.exists()
    page = proto_varint(2, 3000) + proto_varint(3, 1) + proto_varint(4, 1)
    request(page, 3, message_type)
    assert replies[-1] == _item_simple_info_list(3000, 1, offset=1, offline_inventory=reviewed)
    request(reviewed, 4)
    assert target.read_bytes() == reviewed
    assert not preseed.exists()
    request(query, 5, message_type)
    assert replies[-1] == _item_simple_info_list(3000, 0, offline_inventory=reviewed)


def test_other_players_preseed_is_never_used_for_owned_items(tmp_path, monkeypatch):
    players, replies, request = protocol_fixture(tmp_path, monkeypatch)
    (players / "77.offline-data.preseed.bin").write_bytes(inventory(instance=777))
    query = proto_varint(1, 77) + proto_varint(2, 3000) + proto_varint(4, 0)
    request(query, 1, GET_MY_ITEM_SIMPLE_INFO_LIST_ALT)
    assert replies[-1] == b""


def test_invalid_preseed_does_not_hide_valid_canonical_readback(tmp_path, monkeypatch):
    players, replies, request = protocol_fixture(tmp_path, monkeypatch)
    target = players / "42.offline-data.bin"
    target.write_bytes(inventory())
    preseed = players / "42.offline-data.preseed.bin"
    preseed.write_bytes(b"\x0a\x01\x08")
    query = proto_varint(2, 3000) + proto_varint(4, 0)
    request(query, 1, GET_MY_ITEM_SIMPLE_INFO_LIST_ALT)
    assert replies[-1] == _item_simple_info_list(3000, 0, offline_inventory=inventory())
    assert preseed.read_bytes() == b"\x0a\x01\x08"
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    event = next(e for e in reversed(events) if e["event"] == "game_item_simple_info_list_sent")
    assert event["preseed_ignored_reason"]


def test_large_fragmented_inventory_preserves_every_instance(tmp_path, monkeypatch):
    players, replies, request = protocol_fixture(tmp_path, monkeypatch)
    ids = {(1 << 48) + index for index in range(1593)}
    payload = b"".join(inventory(index % 11, (1 << 48) + index) for index in range(1593))
    request(payload)
    assert len(replies) == 1
    assert (players / "42.offline-data.bin").read_bytes() == payload
    request(proto_varint(2, 3000) + proto_varint(4, 0), 100, GET_MY_ITEM_SIMPLE_INFO_LIST_ALT)
    rows = [decode_proto(field.value) for field in decode_proto(replies[-1]) if field.number == 1]
    assert len(rows) == 1593
    assert {first_field(row, 1).value for row in rows} == ids
    assert all(first_field(row, 6).value == 1123 for row in rows)


def test_native_reconnect_cursor_does_not_redeliver_owned_instances(tmp_path, monkeypatch):
    players, replies, request = protocol_fixture(tmp_path, monkeypatch)
    # Native 0x03e4 first enrollment sends field4=0, but reconnect sends the
    # largest local uint64 instance ID. It is not a requested row limit.
    cursor = 63876879755356865
    existing = inventory(instance=cursor - 1) + inventory(instance=cursor)
    target = players / "42.offline-data.bin"
    target.write_bytes(existing)
    query = proto_varint(2, 3000) + proto_varint(3, 0)
    request(query + proto_varint(4, 0), 1, GET_MY_ITEM_SIMPLE_INFO_LIST_ALT)
    assert len(decode_proto(replies[-1])) == 2
    request(query + proto_varint(4, cursor), 2, GET_MY_ITEM_SIMPLE_INFO_LIST_ALT)
    assert replies[-1] == b""
    assert target.read_bytes() == existing
    target.write_bytes(existing + inventory(instance=cursor + 1))
    request(query + proto_varint(4, cursor), 3, GET_MY_ITEM_SIMPLE_INFO_LIST_ALT)
    rows = [decode_proto(f.value) for f in decode_proto(replies[-1])]
    assert len(rows) == 1 and first_field(rows[0], 1).value == cursor + 1


def test_other_owned_item_alias_keeps_its_existing_limit_behavior(tmp_path, monkeypatch):
    players, replies, request = protocol_fixture(tmp_path, monkeypatch)
    (players / "42.offline-data.bin").write_bytes(inventory(instance=101) + inventory(instance=102))
    request(proto_varint(2, 3000) + proto_varint(4, 1), 1, GET_MY_ITEM_SIMPLE_INFO_LIST)
    assert len(decode_proto(replies[-1])) == 1
