"""Offline registration keeps multiplicity and never charges or grants twice."""

from copy import deepcopy

import pytest

from acvd_server.inventory import compare_owned_parts
from acvd_server.inventory_registration import prepare_registration
from acvd_server.owned_items import confirm_inventory, pending_inventory, reserved_instances
from acvd_server.part_sales import sell
from acvd_server.protocol import ProtocolError, decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.teams import TeamStore
from acvd_server.udp import GameSession


def record(identity, part=420, kind=10, category=10, tuning=1300):
    return proto_bytes(
        1,
        b"".join(
            proto_varint(n, v) for n, v in enumerate((category, part, kind, identity, tuning), 1)
        ),
    )


def rows(payload):
    return [
        tuple(first_field(decode_proto(f.value), n).value for n in range(1, 6))
        for f in decode_proto(payload)
        if f.number == 1
    ]


@pytest.fixture
def state(tmp_path):
    teams = TeamStore(tmp_path / "teams")
    teams.remember_player("One", 11)
    teams.remember_player("Two", 22)
    teams.synchronize_balance("One", 999999999)
    path = tmp_path / "players"
    path.mkdir()
    return teams, GameSession(b"owner001", bytes(16), "One", 11), path


def register(state, payload):
    teams, session, path = state
    (path / "11.offline-data.bin").write_bytes(payload)
    return prepare_registration(teams, session, payload, path)


def test_register_duplicate_parts_not_duplicate_ids(state):
    teams, session, path = state
    raw = record(10020) + record(0) * 2 + record(0, kind=11, tuning=1003)
    before = deepcopy(teams.state)
    assert register(state, raw) == 3
    effective = rows(pending_inventory(teams, session, raw))
    assert len(effective) == 4 and len({r[3] for r in effective}) == 4
    assert effective[0] == rows(record(10020))[0]
    assert sorted(r[:3] + r[4:] for r in effective) == sorted(r[:3] + r[4:] for r in rows(raw))
    assert {r[3] for r in effective} == {10020, 10021, 10022, 10023}
    assert (path / "11.offline-data.bin").read_bytes() == raw
    for key in before:
        assert teams.state[key] == before[key]


def test_retry_reordered_rows_auxiliary_and_restart_use_same_ids(state):
    teams, session, path = state
    raw = record(0) + record(10020) + record(0, kind=11)
    assert register(state, raw) == 2
    expected = pending_inventory(teams, session, raw)
    saved = deepcopy(teams.state)
    reordered = record(0, kind=11) + record(0) + record(10020) + proto_bytes(3, b"aux")
    loaded = TeamStore(teams.root)
    assert prepare_registration(loaded, session, reordered, path) == 0
    assert loaded.state == saved
    assert rows(pending_inventory(loaded, session, reordered)) == rows(expected)


def test_confirmation_and_disposal_do_not_resurrect(state):
    teams, session, path = state
    raw = record(10020) + record(0)
    register(state, raw)
    effective = pending_inventory(teams, session, raw)
    assert confirm_inventory(teams, session, effective) == 1
    assert confirm_inventory(teams, session, effective) == 0
    assert pending_inventory(teams, session, record(10020)) == record(10020)
    # A late copy of the original upload is not a second ownership grant.
    assert prepare_registration(teams, session, raw, path) == 0
    assert pending_inventory(teams, session, raw) == record(10020)


def test_pending_registered_part_can_be_removed_without_return(state):
    teams, session, path = state
    raw = record(10020) + record(0)
    register(state, raw)
    effective = pending_inventory(teams, session, raw)
    sell(teams, session, 10021, inventory=effective, credit=0, price_source="model")
    assert pending_inventory(teams, session, raw) == record(10020)
    assert prepare_registration(teams, session, raw, path) == 0


def test_registration_receipts_are_owner_scoped(state):
    teams, session, path = state
    raw = record(10020) + record(0)
    register(state, raw)
    other = GameSession(b"owner002", bytes(16), "Two", 22)
    assert pending_inventory(teams, other, raw) == raw
    (path / "22.offline-data.bin").write_bytes(raw)
    assert prepare_registration(teams, other, raw, path) == 1
    assert rows(pending_inventory(teams, other, raw))[-1][3] == 10022


def test_other_account_and_preseed_ids_are_reserved(state):
    teams, session, path = state
    (path / "22.offline-data.bin").write_bytes(record(10021))
    (path / "22.offline-data.preseed.bin").write_bytes(record(10022))
    raw = record(10020) + record(0)
    register(state, raw)
    assert rows(pending_inventory(teams, session, raw))[-1][3] == 10023
    assert reserved_instances(path) == frozenset({10020, 10021, 10022})


@pytest.mark.parametrize("count", [1, 90, 97, 2980])
def test_import_replaces_slots_instead_of_consuming_additional_capacity(state, count):
    teams, session, _ = state
    raw = record(0) * count
    assert register(state, raw) == count
    effective = rows(pending_inventory(teams, session, raw))
    assert len(effective) == len({r[3] for r in effective}) == count
    assert min(r[3] for r in effective) == 10000


@pytest.mark.parametrize(
    "raw",
    [
        record(0) * 2981,
        record(10020) * 2 + record(0),
        record(0, tuning=1333),
        record(0, tuning=1040),
        record(0, kind=99),
        record(0, part=0),
        record(0, part=65536),
        record(0, category=1),
        record((1 << 64) - 1) + record(0),
    ],
)
def test_invalid_import_cannot_change_ledger(state, raw):
    teams, _, _ = state
    before = deepcopy(teams.state)
    with pytest.raises(ProtocolError):
        register(state, raw)
    assert teams.state == before


def test_failed_commit_has_no_ids_and_retry_is_safe(state, monkeypatch):
    teams, session, path = state
    raw = record(10020) + record(0)
    before = deepcopy(teams.state)
    commit = teams.commit_state

    def fail(_):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(teams, "commit_state", fail)
    with pytest.raises(OSError):
        register(state, raw)
    assert teams.state == before and pending_inventory(teams, session, raw) == raw
    monkeypatch.setattr(teams, "commit_state", commit)
    assert prepare_registration(teams, session, raw, path) == 1


def test_new_partial_batch_reuses_pending_placeholders_and_registers_only_new_parts(state):
    teams, session, path = state
    raw = record(10020) + record(0)
    register(state, raw)
    assert prepare_registration(teams, session, raw + record(0), path) == 1
    effective = rows(pending_inventory(teams, session, raw + record(0)))
    assert [row[3] for row in effective] == [10020, 10021, 10022]
    assert len(list(teams.state["inventory_registrations"].values())) == 2


def test_no_zero_rows_need_no_registration(state):
    teams, session, path = state
    before = deepcopy(teams.state)
    assert prepare_registration(teams, session, record(10020), path) == 0
    assert teams.state == before


def test_native_claim_compares_registered_view(state):
    teams, session, _ = state
    raw = record(10020) + record(0)
    register(state, raw)
    effective = pending_inventory(teams, session, raw)
    claim = proto_varint(1, 2) + proto_varint(2, 10020 ^ 10021)
    assert compare_owned_parts(claim, effective)["comparison_status"] == "match"


def test_wrong_identity_cannot_register(state):
    teams, session, path = state
    session.player_id = 99
    with pytest.raises(ProtocolError, match="identity"):
        prepare_registration(teams, session, record(0), path)


def test_normal_cursor_handler_recovers_stored_upload_after_restart(state):
    from acvd_server.handlers import inventory as handlers
    from acvd_server.udp import GET_MY_ITEM_SIMPLE_INFO_LIST_ALT, SYNC_OFFLINE_DATA, GameMessage

    teams, session, path = state
    raw = record(10020) + record(0)
    # Simulate a stop after storing the upload but before its first readback.
    (path / "11.offline-data.bin").write_bytes(raw)
    loaded = TeamStore(teams.root)
    replies = []

    class Events:
        def emit(self, *args, **kwargs):
            pass

    def read(cursor):
        query = GameMessage(
            GET_MY_ITEM_SIMPLE_INFO_LIST_ALT,
            b"\0\0\0\2",
            proto_varint(2, 3000) + proto_varint(3, 0) + proto_varint(4, cursor),
            2,
        )
        handlers.get_owned_items(
            session,
            query,
            player_data_path=path,
            teams=loaded,
            telemetry=Events(),
            reply=replies.append,
        )
        return replies[-1]

    response = read(10020)
    assert len(decode_proto(response)) == 1
    assert first_field(decode_proto(decode_proto(response)[0].value), 1).value == 10021
    assert read(10020) == response
    assert read(10021) == b""
    assert (path / "11.offline-data.bin").read_bytes() == raw
    effective = pending_inventory(loaded, session, raw)
    upload = GameMessage(SYNC_OFFLINE_DATA, b"\0\0\0\3", effective, 3)
    handlers.sync_offline_data(
        session,
        upload,
        player_data_path=path,
        teams=loaded,
        telemetry=Events(),
        reply=replies.append,
    )
    batch = next(iter(loaded.state["inventory_registrations"].values()))
    assert batch["items"][0]["in_snapshot"]
    assert loaded.balance("One") == 999999999


def test_damaged_receipt_cannot_send_duplicate_parts(state):
    teams, session, _ = state
    raw = record(10020) + record(0)
    register(state, raw)
    batch = next(iter(teams.state["inventory_registrations"].values()))
    batch["items"].append(deepcopy(batch["items"][0]))
    with pytest.raises(ProtocolError, match="repeats"):
        pending_inventory(teams, session, raw)


def test_partial_confirmation_retains_other_registered_categories(state):
    teams, session, path = state
    head = record(0, part=120, kind=0, category=0, tuning=1000)
    raw = record(10020) + record(0) + head
    register(state, raw)
    # The weapon category has now uploaded its registered ID. The head
    # category still has its original zero-ID upload, which must not count twice.
    partial = record(10020) + record(10022) + head
    assert confirm_inventory(teams, session, partial) == 1
    before = deepcopy(teams.state)
    assert prepare_registration(teams, session, partial, path) == 0
    assert teams.state == before
    effective = rows(pending_inventory(teams, session, partial))
    assert len(effective) == 3 and {r[3] for r in effective} == {10020, 10021, 10022}


def test_changed_registered_rows_do_not_reregister_covered_zero_rows(state):
    teams, session, path = state
    raw = record(10020) + record(0)
    register(state, raw)
    updated = raw + record(10025)
    assert prepare_registration(teams, session, updated, path) == 0
    assert len(rows(pending_inventory(teams, session, updated))) == 3
