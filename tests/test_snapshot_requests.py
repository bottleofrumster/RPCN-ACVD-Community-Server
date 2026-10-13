"""Snapshot lifecycle tests on disposable records, not native save qualification."""

import zlib

import pytest

from acvd_server.handlers import snapshots
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.udp import GameMessage, GameSession


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, name, **fields):
        self.rows.append((name, fields))


def invoke(root, session, function, payload=b""):
    replies = []
    function(
        session,
        GameMessage(0, b"\0\0\0\1", payload, 1),
        player_data_path=root,
        telemetry=Events(),
        reply=replies.append,
    )
    return replies


@pytest.fixture
def session():
    return GameSession(b"owner001", bytes(16), "Owner", 11)


def ai_payload(raw, *, name=b"Example", compressed=True):
    app = b"\x01" + zlib.compress(raw) if compressed else b"\x00" + raw
    return proto_varint(1, 2) + proto_bytes(2, name) + proto_varint(3, 7) + proto_bytes(4, app)


def test_mission_conflict_defers_then_semantic_match_promotes(tmp_path, session):
    raw = bytes(1184)
    preseed = tmp_path / "11.mission-savedata.preseed.bin"
    preseed.write_bytes(b"\x01" + zlib.compress(raw))
    target = tmp_path / "11.mission-savedata.bin"
    target.write_bytes(b"old")
    invoke(tmp_path, session, snapshots.get_mission_data)
    assert session.mission_preseed_delivered == preseed
    conflict = b"\x00" + b"\x01" + raw[1:]
    assert invoke(tmp_path, session, snapshots.upload_mission_data, proto_bytes(1, conflict)) == [
        b""
    ]
    assert target.read_bytes() == b"old" and preseed.exists()
    assert (tmp_path / "11.mission-savedata.deferred.bin").read_bytes() == conflict
    exact = b"\x00" + raw
    assert invoke(tmp_path, session, snapshots.upload_mission_data, proto_bytes(1, exact)) == [b""]
    assert target.read_bytes() == exact and not preseed.exists()
    assert session.mission_preseed_delivered is None
    assert list(tmp_path.glob("11.mission-savedata.preseed.applied-*.bin"))


def test_ai_conflict_retains_reviewed_source_then_matching_projection_promotes(tmp_path, session):
    raw = bytes(37672)
    reviewed = ai_payload(raw)
    preseed = tmp_path / "11.ai-2.preseed.bin"
    preseed.write_bytes(reviewed)
    target = tmp_path / "11.ai-2.bin"
    target.write_bytes(b"old")
    invoke(tmp_path, session, snapshots.get_ai_info, proto_varint(1, 11) + proto_varint(2, 2))
    assert session.ai_preseeds_delivered[2] == preseed
    assert invoke(
        tmp_path, session, snapshots.upload_ai_info, ai_payload(raw, name=b"Default")
    ) == [b""]
    assert target.read_bytes() == b"old" and preseed.read_bytes() == reviewed
    equivalent = ai_payload(raw, compressed=False)
    assert invoke(tmp_path, session, snapshots.upload_ai_info, equivalent) == [b""]
    assert target.read_bytes() == equivalent and not preseed.exists()
    assert 2 not in session.ai_preseeds_delivered
    assert list(tmp_path.glob("11.ai-2.preseed.applied-*.bin"))


def test_remote_ac_read_does_not_consume_or_replace_own_recovery_flag(tmp_path, session):
    own = tmp_path / "11.ac-data.preseed.bin"
    own.write_bytes(b"\x00" + bytes(26120))
    peer = tmp_path / "22.ac-data.bin"
    peer.write_bytes(b"\x00" + b"P" * 26120)
    peer_preseed = tmp_path / "22.ac-data.preseed.bin"
    peer_preseed.write_bytes(b"\x00" + b"R" * 26120)
    invoke(tmp_path, session, snapshots.get_ac_data, proto_varint(1, 11))
    reply = invoke(tmp_path, session, snapshots.get_ac_data, proto_varint(1, 22))[0]
    assert first_field(decode_proto(reply), 1).value == peer.read_bytes()
    assert session.ac_preseed_delivered == own and own.exists() and peer_preseed.exists()


@pytest.mark.parametrize(
    "function",
    [
        snapshots.register_ac_data,
        snapshots.upload_mission_data,
        snapshots.upload_ai_info,
        snapshots.update_ai_extension,
    ],
)
def test_missing_required_upload_field_creates_no_files(tmp_path, session, function):
    assert invoke(tmp_path, session, function, b"") == []
    assert not list(tmp_path.iterdir())


def test_owned_unac_list_is_account_scoped_and_ignores_non_slot_filenames(tmp_path, session):
    for name in ("11.ai-1.bin", "11.ai-9.bin", "11.ai-extend.bin", "22.ai-3.bin"):
        (tmp_path / name).write_bytes(b"data")
    response = invoke(tmp_path, session, snapshots.get_owned_unacs)[0]
    assert [field.value for field in decode_proto(response)] == [1, 9]


def test_ai_extension_roundtrip_does_not_change_peer_file(tmp_path, session):
    peer = tmp_path / "22.ai-extend.bin"
    peer.write_bytes(b"peer")
    payload = proto_varint(1, 1) + proto_bytes(2, b"extension-data")
    response = invoke(tmp_path, session, snapshots.update_ai_extension, payload)[0]
    assert first_field(decode_proto(response), 1).value == b"extension-data"
    response = invoke(tmp_path, session, snapshots.get_ai_extension)[0]
    assert first_field(decode_proto(response), 1).value == b"extension-data"
    assert peer.read_bytes() == b"peer"
