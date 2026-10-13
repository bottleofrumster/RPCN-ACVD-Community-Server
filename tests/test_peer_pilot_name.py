"""Peer names use the requested profile, never its status or the viewer's name."""

from copy import deepcopy

import pytest

from acvd_server.handlers import profiles, team_queries
from acvd_server.protocol import decode_proto, first_field, proto_varint
from acvd_server.team_messages import _user_info
from acvd_server.teams import TeamStore
from acvd_server.telemetry import Telemetry
from acvd_server.udp import GameMessage, GameSession


def fixture(root):
    store = TeamStore(root / "teams")
    team = store.create(
        online_id="Viewer",
        player_id=11,
        name="Team",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=2,
    )
    store.join_team(online_id="Target", player_id=22, team_id=team["team_id"])
    store.update_user_properties("Viewer", scalar_fields={}, byte_fields={2: b"Viewer Pilot"})
    store.update_user_properties("Target", scalar_fields={}, byte_fields={2: b"Target Pilot"})
    store.update_member_comment("Target", b"Status is not a name")
    return store


def read_peer(store, root, player_id=22):
    responses = []
    viewer = GameSession(b"viewer01", bytes(16), "Viewer", 11)
    target = GameSession(
        b"target01", bytes(16), "Target", 22, established=True, peer=("127.0.0.1", 40002)
    )
    message = GameMessage(0x395, b"\0\0\0\1", proto_varint(1, player_id), 1)
    team_queries.get_user_info(
        viewer,
        message,
        team_store=store,
        game_sessions={target.auth_token: target},
        telemetry=Telemetry(root / "events.jsonl"),
        reply=responses.append,
    )
    return {
        f.number: f.value for f in decode_proto(first_field(decode_proto(responses[0]), 1).value)
    }


@pytest.mark.parametrize("name", [b"Target Pilot", b"", "雷雨".encode()])
def test_only_peer_name_field_changes_and_readback_is_non_mutating(tmp_path, name):
    store = fixture(tmp_path)
    before = deepcopy(store.state)
    original = read_peer(store, tmp_path)
    assert store.state == before
    store.update_user_properties("Target", scalar_fields={}, byte_fields={2: name})
    before = store.state_path.read_bytes()
    fields = read_peer(TeamStore(store.root), tmp_path)
    assert fields[4] == name
    assert fields[1] == 22 and fields[2] == fields[3] == b"Target"
    assert fields[14] == 0 and fields[15] == 1
    assert {k: v for k, v in fields.items() if k != 4} == {
        k: v for k, v in original.items() if k != 4
    }
    assert store.state_path.read_bytes() == before
    assert store.player_profile("Target")["status"] == "Status is not a name"


@pytest.mark.parametrize(
    "properties",
    [None, [], {}, {"2": None}, {"2": 7}, {"2": "invalid!"}, {"2": "雷雨"}, {"2": "/w=="}],
)
def test_bad_peer_name_falls_back_to_target_identity_not_status_or_viewer(tmp_path, properties):
    store = fixture(tmp_path)
    store.state["player_profiles"]["Target"]["user_property_bytes_b64"] = properties
    store._save()
    before = store.state_path.read_bytes()
    assert read_peer(store, tmp_path)[4] == b"Target"
    assert store.state_path.read_bytes() == before


def test_unknown_target_does_not_leak_viewer_name(tmp_path):
    store = fixture(tmp_path)
    before = store.state_path.read_bytes()
    fields = read_peer(store, tmp_path, 999)
    assert fields[1] == 999 and fields[2] == fields[3] == fields[4] == b""
    assert store.state_path.read_bytes() == before


def test_explicit_name_preserves_legacy_encoder_calls():
    legacy = _user_info(22, "Target", None, status="legacy", member_id=2)
    explicit = _user_info(22, "Target", None, status="legacy", member_id=2, pilot_name=b"Name")
    old = {f.number: f.value for f in decode_proto(legacy)}
    new = {f.number: f.value for f in decode_proto(explicit)}
    assert old[4] == b"legacy" and new[4] == b"Name"
    assert {k: v for k, v in old.items() if k != 4} == {k: v for k, v in new.items() if k != 4}


def test_self_and_peer_share_one_validated_name_decoder():
    from acvd_server.profile_values import pilot_name_bytes

    assert profiles._stored_pilot_name is pilot_name_bytes
    assert team_queries.pilot_name_bytes is pilot_name_bytes
