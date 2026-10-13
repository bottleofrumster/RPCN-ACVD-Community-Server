"""Observe inventory mismatches without deleting, granting, or rewriting data."""

from types import SimpleNamespace

import pytest

from acvd_server.handlers.inventory import acknowledge_owned_parts, get_owned_items
from acvd_server.inventory import compare_owned_parts
from acvd_server.player_storage import _requested_offline_snapshot
from acvd_server.protocol import decode_proto, proto_bytes, proto_varint
from acvd_server.udp import GET_MY_ITEM_SIMPLE_INFO_LIST_ALT, GameMessage, GameSession


def record(identity):
    return proto_bytes(
        1,
        b"".join(
            proto_varint(n, v) for n, v in ((1, 0), (2, 210), (3, 0), (4, identity), (5, 1000))
        ),
    )


def claim(count, checksum):
    return proto_varint(1, count) + proto_varint(2, checksum)


def test_match_is_not_purchase_authority_or_authentication():
    for body in (record(1) + record(2), record(4) + record(7)):
        result = compare_owned_parts(claim(2, 3), body)
        assert result["comparison_status"] == "match" and result["comparison_match"]
        assert result["authorizes_purchase"] is False
        assert result["proof_scope"] == "count_and_instance_xor_only"


def test_missing_snapshot_is_not_known_empty_inventory():
    assert compare_owned_parts(claim(0, 0), None)["comparison_status"] == "snapshot_missing"
    assert compare_owned_parts(claim(0, 0), b"")["comparison_status"] == "match"
    assert compare_owned_parts(claim(2, 3), record(1))["comparison_status"] == "mismatch"
    assert compare_owned_parts(claim(1, 2), record(1))["comparison_status"] == "mismatch"


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        proto_varint(1, 1),
        proto_varint(1, 1) * 2,
        proto_bytes(1, b"x") + proto_varint(2, 0),
        claim(1 << 32, 0),
        claim(1, 1 << 64),
        b"\x80" * 33,
    ],
)
def test_invalid_claims_are_not_treated_as_matches(payload):
    assert compare_owned_parts(payload, b"")["comparison_status"] == "invalid_claim"


def test_duplicate_registered_and_invalid_instances_require_review():
    assert (
        compare_owned_parts(claim(2, 0), record(1) * 2)["comparison_status"]
        == "ambiguous_instances"
    )
    for body in (b"bad", proto_varint(2, 0), record(1 << 64), proto_bytes(1, proto_varint(1, 0))):
        assert compare_owned_parts(claim(0, 0), body)["comparison_status"] == "snapshot_invalid"


def test_native_claim_excludes_zero_ids_without_removing_their_records():
    body = record(10001) + record(10002) + record(0) * 90
    original = body
    result = compare_owned_parts(claim(2, 10001 ^ 10002), body)
    assert result["comparison_status"] == "match"
    assert result["snapshot_count"] == 92
    assert result["registered_instance_count"] == 2
    assert result["zero_instances"] == 90
    assert result["duplicate_instances"] == 0
    assert not result["authorizes_purchase"]
    assert body == original
    # Zero rows cannot hide a real duplicate or a wrong registered claim.
    assert compare_owned_parts(claim(92, 3), body)["comparison_status"] == "mismatch"
    assert compare_owned_parts(claim(2, 0), body)["comparison_status"] == "mismatch"
    assert (
        compare_owned_parts(claim(2, 0), record(10001) * 2 + record(0))["comparison_status"]
        == "ambiguous_instances"
    )


def test_only_zero_rows_match_only_the_empty_registered_claim():
    assert compare_owned_parts(claim(0, 0), record(0) * 2)["comparison_status"] == "match"
    assert compare_owned_parts(claim(2, 0), record(0) * 2)["comparison_status"] == "mismatch"


def test_snapshot_loader_preserves_absence_and_uses_reviewed_preseed(tmp_path):
    assert _requested_offline_snapshot(tmp_path, player_id=11).payload is None
    live = tmp_path / "11.offline-data.bin"
    live.write_bytes(record(1))
    preseed = tmp_path / "11.offline-data.preseed.bin"
    preseed.write_bytes(record(2))
    selected = _requested_offline_snapshot(tmp_path, player_id=11)
    assert selected.payload == record(2) and selected.canonical_payload == record(1)
    assert selected.preseed_path == preseed
    preseed.write_bytes(b"bad")
    selected = _requested_offline_snapshot(tmp_path, player_id=11)
    assert selected.payload == record(1) and selected.preseed_ignored_reason
    assert preseed.read_bytes() == b"bad" and live.read_bytes() == record(1)


@pytest.mark.parametrize("body", [None, b"", record(1), record(1) * 2, b"bad"])
def test_ack_is_unchanged_and_no_snapshot_is_written_on_any_outcome(tmp_path, body):
    session = GameSession(b"owner000", bytes(16), "One", 11)
    if body is not None:
        (tmp_path / "11.offline-data.bin").write_bytes(body)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    events = []
    responses = []
    acknowledge_owned_parts(
        session,
        GameMessage(0x41D, b"\0" * 4, claim(1, 1), 1),
        player_data_path=tmp_path,
        telemetry=SimpleNamespace(emit=lambda name, **kw: events.append((name, kw))),
        reply=responses.append,
    )
    assert responses == [b""]
    assert events[0][0] == "game_owned_parts_check_received"
    assert events[0][1]["authorizes_purchase"] is False
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_ack_and_readback_use_the_same_reviewed_inventory(tmp_path):
    session = GameSession(b"owner000", bytes(16), "One", 11)
    (tmp_path / "11.offline-data.bin").write_bytes(record(1))
    (tmp_path / "11.offline-data.preseed.bin").write_bytes(record(2))
    events = []
    responses = []
    telemetry = SimpleNamespace(emit=lambda name, **kw: events.append((name, kw)))
    acknowledge_owned_parts(
        session,
        GameMessage(0x41D, b"\0" * 4, claim(1, 2), 1),
        player_data_path=tmp_path,
        telemetry=telemetry,
        reply=responses.append,
    )
    assert events[-1][1]["comparison_status"] == "match"
    get_owned_items(
        session,
        GameMessage(
            GET_MY_ITEM_SIMPLE_INFO_LIST_ALT,
            b"\0" * 4,
            proto_varint(2, 3000) + proto_varint(3, 0) + proto_varint(4, 0),
            2,
        ),
        player_data_path=tmp_path,
        telemetry=telemetry,
        reply=responses.append,
    )
    row = decode_proto(decode_proto(responses[-1])[0].value)
    assert next(f.value for f in row if f.number == 1) == 2


def test_unreadable_inventory_cannot_break_the_existing_ack(tmp_path, monkeypatch):
    from acvd_server.handlers import inventory as handler

    def unavailable(*args, **kwargs):
        raise OSError("Unavailable snapshot")

    monkeypatch.setattr(handler, "_requested_offline_snapshot", unavailable)
    events = []
    replies = []
    session = GameSession(b"owner000", bytes(16), "One", 11)
    acknowledge_owned_parts(
        session,
        GameMessage(0x41D, bytes(4), claim(1, 1), 1),
        player_data_path=tmp_path,
        telemetry=SimpleNamespace(emit=lambda name, **kw: events.append(kw)),
        reply=replies.append,
    )
    assert replies == [b""]
    assert events[0]["comparison_status"] == "snapshot_unavailable"
    assert list(tmp_path.iterdir()) == []
