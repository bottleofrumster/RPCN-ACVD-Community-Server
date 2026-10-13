"""Profile request behavior on temporary stores; migration bugs remain separate."""

import base64
from copy import deepcopy

import pytest

from acvd_server.handlers import profiles
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


@pytest.fixture
def setup(tmp_path):
    store = TeamStore(tmp_path / "teams")
    store.remember_player("Pilot", 11)
    store.remember_player("Peer", 22)
    return store, GameSession(b"token123", bytes(16), "Pilot", 11), [], Events()


def invoke(setup, function, payload=b""):
    store, session, replies, events = setup
    function(
        session,
        GameMessage(0, b"\x00\x00\x00\x01", payload, 1),
        teams=store,
        telemetry=events,
        reply=replies.append,
    )
    return replies


def test_initial_no_team_user_info_provisions_native_new_save_money_once(setup):
    store, session, replies, events = setup
    invoke(setup, profiles.get_my_user_info)
    fields = decode_proto(replies[-1])
    assert first_field(fields, 1).value == session.player_id
    assert first_field(fields, 11).value == 100_000
    assert first_field(fields, 14).value == 1 and first_field(fields, 15).value == 0
    assert TeamStore(store.root).balance("Pilot") == 100_000
    assert store.player_profile("Pilot")["cash_enrollment"] == {
        "policy": "native-new-save-v1",
        "starting_balance": 100_000,
    }
    assert session.new_wallet_provisional is True
    assert events.rows[-1][1]["balance_known"] is False
    assert events.rows[-1][1]["balance_sent"] == 100_000
    assert events.rows[-1][1]["wallet_provisioned"] is True

    before = store.state_path.read_bytes()
    invoke(setup, profiles.get_my_user_info)
    assert store.state_path.read_bytes() == before
    assert events.rows[-1][1]["balance_known"] is True
    assert events.rows[-1][1]["wallet_provisioned"] is False


def test_missing_team_member_wallet_is_not_reclassified_as_a_new_save(setup):
    store, session, replies, events = setup
    store.create(
        online_id="Pilot",
        player_id=11,
        name="Existing",
        operation_base=0,
        language=0,
        play_type=0,
        enlistment_method=0,
    )
    before = deepcopy(store.state)
    invoke(setup, profiles.get_my_user_info)
    fields = decode_proto(replies[-1])
    assert first_field(fields, 11).value == 0
    assert store.state == before
    assert session.new_wallet_provisional is False
    assert events.rows[-1][1]["balance_known"] is False
    assert events.rows[-1][1]["wallet_provisioned"] is False


@pytest.mark.parametrize("balance", [0, 165357])
def test_user_info_distinguishes_known_wallet_from_missing_wallet(setup, balance):
    store, session, replies, events = setup
    store.synchronize_balance(session.online_id, balance)
    before = deepcopy(store.state)
    invoke(setup, profiles.get_my_user_info)
    assert first_field(decode_proto(replies[-1]), 11).value == balance
    assert events.rows[-1][1]["balance_known"] is True
    assert events.rows[-1][1]["balance_sent"] == balance
    assert store.state == before


def test_sparse_properties_preserve_unmentioned_fields_and_other_players(setup):
    store, _, replies, _ = setup
    store.update_user_properties("Peer", scalar_fields={6: 2}, byte_fields={2: b"peer-name"})
    peer = store.player_profile("Peer")
    invoke(
        setup, profiles.update_user_property, proto_bytes(2, b"original-name") + proto_varint(6, 1)
    )
    invoke(setup, profiles.update_user_property, proto_bytes(4, b"second-field"))
    profile = TeamStore(store.root).player_profile("Pilot")
    assert base64.b64decode(profile["user_property_bytes_b64"]["2"]) == b"original-name"
    assert base64.b64decode(profile["user_property_bytes_b64"]["4"]) == b"second-field"
    assert profile["language"] == 1
    assert store.player_profile("Peer") == peer and replies == [b"", b""]


def test_personal_selectors_survive_cold_reload_and_use_my_user_info_fields_8_to_10(setup):
    store, session, replies, events = setup
    invoke(
        setup,
        profiles.update_user_property,
        proto_varint(5, 47) + proto_varint(6, 2) + proto_varint(7, 5),
    )

    reloaded = TeamStore(store.root)
    invoke((reloaded, session, replies, events), profiles.get_my_user_info)
    fields = decode_proto(replies[-1])

    assert first_field(fields, 6).value == 0
    assert [first_field(fields, number).value for number in (8, 9, 10)] == [47, 2, 5]
    assert reloaded.player_profile("Pilot")["user_property_scalars"] == {
        "5": 47,
        "6": 2,
        "7": 5,
    }


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        proto_varint(1, 1),
        proto_bytes(1, b""),
        proto_bytes(1, proto_varint(1, 16)),
        proto_bytes(1, proto_varint(1, 16) + proto_bytes(2, b"bad")),
    ],
)
def test_invalid_garage_message_preserves_existing_settings(setup, payload):
    store, _, replies, events = setup
    store.set_garage_custom_info("Pilot", 16, 127)
    before = deepcopy(store.state)
    invoke(setup, profiles.register_garage_custom_info, payload)
    assert store.state == before and replies == []
    assert events.rows[-1][0] == "game_message_rejected"


def test_garage_edits_persist_and_read_back_for_the_authenticated_player(setup):
    store, _, replies, _ = setup
    store.set_garage_custom_info("Peer", 9, 10)
    for primary, secondary in ((16, 127), (2, 7)):
        invoke(
            setup,
            profiles.register_garage_custom_info,
            proto_bytes(1, proto_varint(1, primary) + proto_varint(2, secondary)),
        )
        assert first_field(decode_proto(replies[-1]), 1).value == 0
        invoke(setup, profiles.get_garage_custom_info)
        info = decode_proto(first_field(decode_proto(replies[-1]), 1).value)
        assert [first_field(info, n).value for n in (1, 2)] == [primary, secondary]
        assert TeamStore(store.root).garage_custom_info("Pilot") == {
            "primary": primary,
            "secondary": secondary,
        }
    assert store.garage_custom_info("Peer") == {"primary": 9, "secondary": 10}


def test_comment_update_is_separate_from_stored_pilot_name(setup):
    store, _, replies, _ = setup
    store.update_user_properties("Pilot", scalar_fields={}, byte_fields={2: b"original-name"})
    invoke(setup, profiles.update_team_member_comment, proto_bytes(1, b"ready"))
    profile = store.player_profile("Pilot")
    assert profile["status"] == "ready"
    assert base64.b64decode(profile["user_property_bytes_b64"]["2"]) == b"original-name"
    assert replies == [b""]


def test_member_profile_update_does_not_change_peer_record(setup):
    store, _, replies, _ = setup
    peer = store.player_profile("Peer")
    invoke(setup, profiles.update_member_team_info, proto_varint(1, 3) + proto_varint(2, 1))
    profile = store.player_profile("Pilot")
    assert profile["member_id"] == 3 and profile["member_team_secondary"] == 1
    assert store.player_profile("Peer") == peer and replies == [b""]


def test_duplicate_member_number_is_rejected_and_live_team_refreshes(tmp_path):
    store = TeamStore(tmp_path / "teams")
    team = store.create(
        online_id="Holder",
        player_id=10,
        name="Numbers",
        operation_base=0,
        language=0,
        play_type=1,
        enlistment_method=2,
    )
    assert store.join_team(online_id="Pilot", player_id=11, team_id=team["team_id"])
    store.update_member_team_info("Holder", member_id=11, secondary=0)
    before = deepcopy(store.state)
    replies = []
    events = Events()
    notifications = []

    def notify(team_id, *, reason, subject_online_id):
        assert replies == [b""] and store.state == before
        notifications.append((team_id, reason, subject_online_id))
        return ["Holder", "Pilot"]

    profiles.update_member_team_info(
        GameSession(b"token123", bytes(16), "Pilot", 11),
        GameMessage(0, bytes(4), proto_varint(1, 11) + proto_varint(2, 0), 1),
        teams=store,
        telemetry=events,
        reply=replies.append,
        notify_team_profile=notify,
    )
    assert store.state == before
    assert notifications == [(team["team_id"], "member_number_conflict", "Pilot")]
    assert events.rows[-1][0] == "game_member_team_info_rejected"
    assert events.rows[-1][1]["current_member_id"] == 2
    assert events.rows[-1][1]["profile_update_recipient_online_ids"] == ["Holder", "Pilot"]


@pytest.mark.parametrize("name", [b"rum", "雷雨".encode(), b""])
def test_stored_pilot_name_is_response_field3_only(setup, name):
    store, session, replies, _ = setup
    invoke(setup, profiles.get_my_user_info)
    original = [(field.number, field.value) for field in decode_proto(replies[-1])]
    invoke(setup, profiles.update_user_property, proto_bytes(2, name))
    before = deepcopy(store.state)
    invoke(setup, profiles.get_my_user_info)
    fields = decode_proto(replies[-1])
    assert first_field(fields, 2).value == session.online_id.encode()
    assert first_field(fields, 3).value == name
    assert [(f.number, f.value) for f in fields if f.number != 3] == [
        pair for pair in original if pair[0] != 3
    ]
    assert store.state == before


def test_pilot_rename_survives_store_reload_and_cannot_target_peer(setup):
    store, session, replies, events = setup
    store.update_user_properties("Peer", scalar_fields={}, byte_fields={2: b"Peer Name"})
    for name in (b"Original", b"New Name"):
        invoke(setup, profiles.update_user_property, proto_bytes(2, name))
        reloaded = TeamStore(store.root)
        before = deepcopy(reloaded.state)
        invoke((reloaded, session, replies, events), profiles.get_my_user_info, proto_varint(1, 22))
        assert first_field(decode_proto(replies[-1]), 3).value == name
        assert reloaded.state == before
        assert (
            base64.b64decode(reloaded.player_profile("Peer")["user_property_bytes_b64"]["2"])
            == b"Peer Name"
        )


@pytest.mark.parametrize(
    "properties",
    [
        None,
        [],
        "bad-container",
        {},
        {"2": None},
        {"2": 17},
        {"2": "not-base64!"},
        {"2": "雷雨"},
        {"2": "/w=="},
    ],
)
def test_missing_or_corrupt_pilot_property_keeps_legacy_fallback_without_mutation(
    setup, properties
):
    store, session, replies, _ = setup
    store.state["player_profiles"][session.online_id]["user_property_bytes_b64"] = properties
    before = deepcopy(store.state)
    invoke(setup, profiles.get_my_user_info)
    assert first_field(decode_proto(replies[-1]), 3).value == session.online_id.encode()
    assert store.state == before
