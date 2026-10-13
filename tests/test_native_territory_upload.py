"""Native upload persistence, bounded decoding, and owner isolation."""

import zlib
from copy import deepcopy
from types import SimpleNamespace

import pytest

from acvd_server.handlers import territory
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.server import GameDatagramProtocol
from acvd_server.teams import TeamStore
from acvd_server.udp import UPLOAD_TERRITORY_CANNON_LAYOUT, GameMessage, GameSession

CAPTURED = bytes.fromhex(
    "08031007180722270158c362670002eeff1880e1b1345fb2e2eec30ca360148c82110b00000000ffff0300882e150f"
)
NONEMPTY_CAPTURED = bytes.fromhex(
    "080310071807222d0158c3626700026e1ef5ff688001060478d4c1b40f432d433ac3281805a360040100000000ffff0300a86e11a9"
)


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


@pytest.fixture
def fixture(tmp_path):
    store = TeamStore(tmp_path / "teams")
    team = store.create(
        online_id="Owner",
        player_id=11,
        name="Test",
        operation_base=1,
        language=2,
        play_type=1,
        enlistment_method=2,
    )
    store.join_team(online_id="Peer", player_id=22, team_id=team["team_id"])
    team["territory_area_id"] = 7
    store._save()
    return store, GameSession(b"owner001", bytes(16), "Owner", 11)


def invoke(store, actor, payload=CAPTURED):
    replies, events = [], Events()
    territory.upload_layout(
        actor,
        GameMessage(UPLOAD_TERRITORY_CANNON_LAYOUT, bytes(4), payload, 1),
        team_store=store,
        telemetry=events,
        reply=replies.append,
    )
    return replies, events.rows


def upload_payload(binary, *, season=3, area=7, version=7):
    return (
        proto_varint(1, season)
        + proto_varint(2, area)
        + proto_varint(3, version)
        + proto_bytes(4, binary)
    )


@pytest.mark.parametrize("payload", [CAPTURED, NONEMPTY_CAPTURED])
def test_captured_upload_persists_before_ack_and_reads_back(fixture, payload):
    store, actor = fixture
    binary = first_field(decode_proto(payload), 4).value
    replies, events = invoke(store, actor, payload)
    assert replies == [b""]
    assert events[-1][0] == "game_native_territory_layout_saved"
    reloaded = TeamStore(store.root)
    assert reloaded.load_native_territory_layout("Owner", player_id=11, season_id=3, area_id=7) == (
        binary,
        7,
    )
    output = []
    territory.get_layout(
        actor,
        GameMessage(
            0x03BB, bytes(4), proto_varint(1, 3) + proto_varint(2, 11) + proto_varint(3, 7), 1
        ),
        team_store=reloaded,
        telemetry=Events(),
        reply=output.append,
    )
    fields = decode_proto(output[0])
    assert [(f.number, f.value) for f in fields] == [(2, 7), (3, binary), (4, 0)]


def test_native_opcode_reaches_persistence_instead_of_stub(fixture):
    store, actor = fixture
    replies, events = [], Events()
    receiver = SimpleNamespace(
        teams=store,
        telemetry=events,
        world_phase=lambda: 2,
        _send_game_reply=lambda *args: replies.append(args[-1]),
    )
    message = GameMessage(0x03BE, bytes(4), CAPTURED, 1)
    GameDatagramProtocol._dispatch_game_message(receiver, actor, None, message)
    assert replies == [b""]
    assert any(name == "game_native_territory_layout_saved" for name, _ in events.rows)
    assert not any(name == "game_request_stubbed" for name, _ in events.rows)


def test_native_upload_dispatch_respects_grace_period(fixture):
    store, actor = fixture
    before = deepcopy(store.state)
    replies, events = [], Events()
    receiver = SimpleNamespace(
        teams=store,
        telemetry=events,
        world_phase=lambda: 0,
        _send_game_reply=lambda *args: replies.append(args[-1]),
    )
    GameDatagramProtocol._dispatch_game_message(
        receiver, actor, None, GameMessage(0x03BE, bytes(4), CAPTURED, 1)
    )
    assert not replies and store.state == before
    assert events.rows[-1][0] == "game_message_rejected"


def test_uploaders_are_isolated_and_retries_are_idempotent(fixture):
    store, owner = fixture
    assert invoke(store, owner)[0] == [b""]
    before = store.state_path.read_bytes()
    assert invoke(store, owner)[0] == [b""]
    assert store.state_path.read_bytes() == before
    peer = GameSession(b"peer0001", bytes(16), "Peer", 22)
    raw = bytes([7]) + bytes(0x41F)
    body = b"\x00" + raw
    assert invoke(store, peer, upload_payload(body))[0] == [b""]
    assert store.load_native_territory_layout("Peer", player_id=22, season_id=3, area_id=7) == (
        body,
        7,
    )
    owner_record = store.load_native_territory_layout("Owner", player_id=11, season_id=3, area_id=7)
    assert owner_record[0] != body
    assert store.load_active_native_territory_layout(
        "Owner", season_id=3, area_id=7
    ) == (body, 7, 22)
    # Re-uploading an existing owner revision deliberately reselects it for
    # the whole team even though its content-addressed file already exists.
    assert invoke(store, owner)[0] == [b""]
    assert store.load_active_native_territory_layout(
        "Peer", season_id=3, area_id=7
    ) == (first_field(decode_proto(CAPTURED), 4).value, 7, 11)
    assert len(list(store.root.glob("*-territory-*.bin"))) == 2


@pytest.mark.parametrize(
    "change", ["area", "season", "version", "duplicate", "type", "truncated", "bomb", "trailing"]
)
def test_invalid_upload_preserves_data_and_has_no_success_ack(fixture, change):
    store, actor = fixture
    assert invoke(store, actor)[0] == [b""]
    before = deepcopy(store.state)
    files = {p.name: p.read_bytes() for p in store.root.iterdir() if p.is_file()}
    binary = first_field(decode_proto(CAPTURED), 4).value
    candidates = {
        "area": upload_payload(binary, area=1),
        "season": upload_payload(binary, season=2),
        "version": upload_payload(binary, version=8),
        "duplicate": CAPTURED + proto_varint(2, 7),
        "type": proto_varint(1, 3)
        + proto_bytes(2, b"7")
        + proto_varint(3, 7)
        + proto_bytes(4, binary),
        "truncated": upload_payload(binary[:-3]),
        "bomb": upload_payload(b"\x01" + zlib.compress(bytes(200000))),
        "trailing": upload_payload(binary + b"extra"),
    }
    replies, events = invoke(store, actor, candidates[change])
    assert not replies and events[-1][0] == "game_message_rejected"
    assert store.state == before
    assert {p.name: p.read_bytes() for p in store.root.iterdir() if p.is_file()} == files


def test_nonmember_cannot_upload(fixture):
    store, _ = fixture
    actor = GameSession(b"other001", bytes(16), "Other", 33)
    before = deepcopy(store.state)
    assert not invoke(store, actor)[0]
    assert store.state == before


def test_failed_metadata_write_does_not_ack_or_replace_prior_record(fixture, monkeypatch):
    store, actor = fixture
    assert invoke(store, actor)[0] == [b""]
    before = deepcopy(store.state)
    persisted = store.state_path.read_bytes()

    def fail():
        raise OSError("Test write failure")

    monkeypatch.setattr(store, "_save", fail)
    body = b"\x00\x07" + bytes(0x41F)
    assert not invoke(store, actor, upload_payload(body))[0]
    assert store.state == before and store.state_path.read_bytes() == persisted


def test_old_payload_remains_recoverable_after_replacement(fixture):
    store, actor = fixture
    assert invoke(store, actor)[0] == [b""]
    old_files = {p.name: p.read_bytes() for p in store.root.glob("*-territory-*.bin")}
    assert invoke(store, actor, upload_payload(b"\x00\x07" + bytes(0x41F)))[0] == [b""]
    assert all((store.root / name).read_bytes() == data for name, data in old_files.items())
