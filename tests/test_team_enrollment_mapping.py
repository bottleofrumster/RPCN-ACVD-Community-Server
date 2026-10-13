"""Native creation field mapping and consistent admission/readback policy."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from acvd_server.handlers import team_updates
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.team_messages import _team_info
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession


class Events:
    def emit(self, *args, **kwargs):
        pass


def create(store, payload):
    actor = GameSession(b"owner001", bytes(16), "Owner", 42)
    replies = []
    team_updates.create_team(
        actor, GameMessage(0x390, bytes(4), payload), team_store=store,
        telemetry=Events(), reply=replies.append,
    )
    assert replies == [b""]
    return actor, store.team_for("Owner")


@pytest.mark.parametrize("method", [0, 1, 2, 3])
@pytest.mark.parametrize("unrelated", [0, 1, 2, 13, 99])
def test_creation_admission_and_readback_use_field7_not_field22(tmp_path, method, unrelated):
    store = TeamStore(tmp_path)
    _, team = create(
        store, proto_bytes(3, "Fixture") + proto_varint(7, method)
        + proto_varint(9, 2) + proto_varint(11, 1) + proto_varint(18, 0)
        + proto_varint(22, unrelated),
    )
    assert team["enlistment_method"] == method
    assert team["recruitment_open"] is (method == 2)
    team["nationality_id"] = 99  # A stale legacy alias cannot change the display.
    fields = decode_proto(_team_info(team, datetime.now(timezone.utc)))
    assert first_field(fields, 3).value == method
    assert first_field(fields, 10).value == 1
    before = deepcopy(store.state)
    joined = store.join_team(online_id="Guest", player_id=43, team_id=team["team_id"])
    assert (joined is not None) is (method == 2)
    if method != 2:
        assert store.state == before


@pytest.mark.parametrize("payload,expected", [
    ("1a01313802420048025801900100a80100b00101b80100", 2),
    ("1a0231313800420048035801900100a80100b00101b80100", 0),
])
def test_exact_native_creation_requests_preserve_free_and_closed(tmp_path, payload, expected):
    _, team = create(TeamStore(tmp_path), bytes.fromhex(payload))
    assert team["enlistment_method"] == expected
    assert team["creation_wire_fields"]["22"] == 1
    assert team["recruitment_open"] is (expected == 2)


@pytest.mark.parametrize("method", [0, 1, 2, 3])
def test_later_leader_choice_controls_both_admission_and_team_info(tmp_path, method):
    store = TeamStore(tmp_path)
    actor, team = create(store, proto_bytes(3, "Fixture") + proto_varint(7, 2))
    team["nationality_id"] = 2
    replies = []
    team_updates.update_leader_settings(
        actor, GameMessage(0x39D, bytes(4), proto_varint(1, method) + proto_bytes(2, "Owner")),
        team_store=store, telemetry=Events(), reply=replies.append,
    )
    assert replies == [b""]
    reloaded = TeamStore(tmp_path).team_for("Owner")
    assert reloaded["enlistment_method"] == method
    assert reloaded["recruitment_open"] is (method == 2)
    assert first_field(decode_proto(_team_info(reloaded, datetime.now(timezone.utc))), 3).value == method
    assert reloaded["leader_update_identity"] == "Owner"
