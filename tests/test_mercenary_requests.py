"""Mercenary registration and negotiation contracts on temporary team stores."""

import pytest

from acvd_server.handlers import mercenaries
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.teams import TeamStore
from acvd_server.udp import (
    START_MERCENARY_NEGOTIATION,
    GameMessage,
    GameSession,
)


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, event, **fields):
        self.rows.append((event, fields))


def session(name, number):
    return GameSession(b"caller01", bytes(16), name, number)


def invoke(actor, function, payload=b"", opcode=0, **dependencies):
    replies, events = [], Events()
    function(
        actor,
        GameMessage(opcode, b"\0\0\0\1", payload, 1),
        telemetry=events,
        reply=replies.append,
        **dependencies,
    )
    return replies, events


@pytest.fixture
def setup(tmp_path):
    store = TeamStore(tmp_path / "teams")
    first = store.create(
        online_id="Leader",
        player_id=11,
        name="First",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=2,
    )
    store.join_team(online_id="Ally", player_id=22, team_id=first["team_id"])
    for name, number in (("Merc", 33), ("Stale", 44)):
        store.create(
            online_id=name,
            player_id=number,
            name=name,
            operation_base=0,
            language=1,
            play_type=1,
            enlistment_method=2,
        )
    for name, number in (("Ally", 22), ("Merc", 33), ("Stale", 44)):
        store.register_mercenary(
            online_id=name,
            player_id=number,
            ac_data=b"\x01profile-" + name.encode(),
            result_values=[1, 2, 3, 4],
        )
    return store, {"Ally", "Merc"}


def test_registration_updates_shared_set_and_returns_not_ready(tmp_path):
    store = TeamStore(tmp_path / "teams")
    active = set()
    actor = session("Merc", 33)
    replies, events = invoke(
        actor,
        mercenaries.register,
        proto_bytes(3, b"profile"),
        team_store=store,
        active_mercenaries=active,
    )
    assert active == {"Merc"} and store.mercenary_for_player_id(33) is not None
    assert first_field(decode_proto(replies[0]), 1).value == 0
    assert events.rows[-1][1]["job_ready"] is False


@pytest.mark.parametrize("flag", [0, 1])
def test_unregister_removes_listing_even_when_client_field_is_false(setup, flag):
    store, active = setup
    replies, _ = invoke(
        session("Merc", 33),
        mercenaries.unregister,
        proto_varint(1, flag),
        team_store=store,
        active_mercenaries=active,
    )
    assert "Merc" not in active and store.mercenary_for_player_id(33) is None
    assert replies == [b""]


def test_continuation_does_not_invent_missing_registration(setup):
    store, active = setup
    replies, events = invoke(
        session("Missing", 99),
        mercenaries.continue_registration,
        team_store=store,
        active_mercenaries=active,
    )
    assert "Missing" not in active and store.mercenary_for_player_id(99) is None
    assert replies == [b""] and events.rows[-1][1]["continued"] is False


def test_search_excludes_same_team_and_stale_durable_listings(setup):
    store, active = setup
    replies, events = invoke(
        session("Leader", 11),
        mercenaries.search,
        team_store=store,
        live_mercenary_online_ids=lambda: active,
    )
    rows = [decode_proto(f.value) for f in decode_proto(replies[0])]
    assert [first_field(r, 1).value for r in rows] == [33]
    assert events.rows[-1][1]["ignored_same_team_online_ids"] == ["Ally"]
    assert events.rows[-1][1]["ignored_stale_count"] == 1


@pytest.mark.parametrize("live", [True, False])
def test_profile_read_requires_live_availability_and_preserves_registration_blob(setup, live):
    store, active = setup
    replies, _ = invoke(
        session("Leader", 11),
        mercenaries.get_data,
        proto_varint(1, 33),
        team_store=store,
        live_mercenary_online_ids=lambda: active if live else set(),
    )
    assert first_field(decode_proto(replies[0]), 1).value == (b"\x01profile-Merc" if live else b"")


def test_start_negotiation_targets_the_candidate(setup):
    store, active = setup
    payload = proto_varint(1, 33)
    replies, events = invoke(
        session("Leader", 11),
        mercenaries.queue_negotiation,
        payload,
        START_MERCENARY_NEGOTIATION,
        team_store=store,
        live_mercenary_online_ids=lambda: active,
    )
    messages = store.mercenary_messages_for("Merc")
    assert len(messages) == 1 and messages[0]["employer_online_id"] == "Leader"
    assert replies == [b""] and events.rows[-1][1]["queued_count"] == 1


@pytest.mark.parametrize("target", [22, 44, 999])
def test_same_team_stale_or_unknown_target_receives_no_offer(setup, target):
    store, active = setup
    replies, events = invoke(
        session("Leader", 11),
        mercenaries.queue_negotiation,
        proto_varint(1, target),
        START_MERCENARY_NEGOTIATION,
        team_store=store,
        live_mercenary_online_ids=lambda: active,
    )
    assert not any(store.mercenary_messages_for(name) for name in ("Ally", "Merc", "Stale"))
    assert replies == [b""] and events.rows[-1][1]["queued_count"] == 0


def test_cancellation_is_scoped_to_the_employer(setup):
    store, active = setup
    for name, number in (("Leader", 11), ("Stale", 44)):
        store.queue_mercenary_message(
            target_player_id=33,
            employer_online_id=name,
            employer_player_id=number,
            employer_team_name=name,
        )
    invoke(session("Leader", 11), mercenaries.cancel_negotiation, team_store=store)
    messages = store.mercenary_messages_for("Merc")
    assert len(messages) == 1 and messages[0]["employer_online_id"] == "Stale"


def test_inactive_recipient_cannot_fetch_durable_inbox(setup):
    store, active = setup
    store.queue_mercenary_message(
        target_player_id=33,
        employer_online_id="Leader",
        employer_player_id=11,
        employer_team_name="First",
    )
    replies, _ = invoke(
        session("Merc", 33),
        mercenaries.get_messages,
        team_store=store,
        live_mercenary_online_ids=lambda: set(),
    )
    assert replies == [b""] and len(store.mercenary_messages_for("Merc")) == 1
