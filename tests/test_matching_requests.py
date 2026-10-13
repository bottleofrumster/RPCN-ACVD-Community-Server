"""Match request ordering and authenticated state scope on temporary fixtures."""

import pytest

from acvd_server.handlers import matching
from acvd_server.matchmaking import MatchmakingCoordinator
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
    events = Events()
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        send_push=lambda *a, **k: None,
        telemetry=events,
        room_log_paths=[],
    )
    store = TeamStore(tmp_path / "teams")
    actor = GameSession(
        b"owner001", bytes(16), "Owner", 11, established=True, peer=("127.0.0.1", 1111)
    )
    return coordinator, store, actor, events


def message(payload=b""):
    return GameMessage(0, b"\0\0\0\1", payload, 1)


def test_registration_purges_then_replies_before_matching_and_reuses_id(setup, monkeypatch):
    coordinator, store, actor, events = setup
    order = []
    replies = []
    monkeypatch.setattr(coordinator, "_purge_stale_matchmaking", lambda: order.append("purge"))
    monkeypatch.setattr(coordinator, "_match_preparation", lambda prep: order.append("match"))

    def reply(payload):
        assert "Owner" in coordinator.preparations
        replies.append(payload)
        order.append("reply")

    payload = proto_varint(1, 3) + proto_varint(2, 2) + proto_bytes(6, proto_varint(1, 11))
    for _ in range(2):
        matching.register_preparation(
            actor,
            message(payload),
            matchmaking=coordinator,
            team_store=store,
            telemetry=events,
            reply=reply,
        )
    assert order == ["purge", "reply", "match"] * 2
    assert [first_field(decode_proto(r), 1).value for r in replies] == [1, 1]
    assert coordinator.next_prepare_id == 2
    assert coordinator.preparations["Owner"]["claimed_player_ids"] == [11]


@pytest.mark.parametrize(
    "stored,expected",
    [(3, 3), (7, 7), (0, 0), (-1, 0), (8, 0), ("3", 0), (True, 0)],
)
def test_registration_uses_only_the_authenticated_team_region(setup, monkeypatch, stored, expected):
    coordinator, store, actor, events = setup
    monkeypatch.setattr(
        store,
        "team_for",
        lambda online_id: (
            {"team_id": 5, "territory_area_id": stored} if online_id == actor.online_id else None
        ),
    )
    monkeypatch.setattr(coordinator, "_match_preparation", lambda prep: None)
    matching.register_preparation(
        actor,
        message(proto_varint(6, 2) + proto_varint(8, 6)),
        matchmaking=coordinator,
        team_store=store,
        telemetry=events,
        reply=lambda body: None,
    )
    assert coordinator.preparations["Owner"]["territory_area_id"] == expected
    assert events.rows[-1][1]["territory_area_id"] == expected


@pytest.mark.parametrize(
    "record,expected",
    [(None, 0), ((b"encoded", 7, 99), 99), ((b"", 7, 99), 0), ((b"encoded", 8, 99), 0)],
)
def test_registration_snapshots_the_team_active_current_region_layout(
    setup, monkeypatch, record, expected
):
    coordinator, store, actor, events = setup
    reads = []
    monkeypatch.setattr(store, "team_for", lambda online_id: {"team_id": 5, "territory_area_id": 3})

    def load(online_id, **kwargs):
        reads.append((online_id, kwargs))
        return record

    monkeypatch.setattr(store, "load_active_native_territory_layout", load)
    monkeypatch.setattr(coordinator, "_match_preparation", lambda prep: None)
    matching.register_preparation(
        actor,
        message(proto_varint(7, 99999)),
        matchmaking=coordinator,
        team_store=store,
        telemetry=events,
        reply=lambda body: None,
    )
    assert reads == [("Owner", {"season_id": 3, "area_id": 3})]
    assert coordinator.preparations["Owner"]["territory_layout_owner_id"] == expected
    assert events.rows[-1][1]["territory_layout_owner_id"] == expected


def test_bad_stored_layout_does_not_break_registration_or_invent_owner(setup, monkeypatch):
    coordinator, store, actor, events = setup
    monkeypatch.setattr(store, "team_for", lambda online_id: {"team_id": 5, "territory_area_id": 3})

    def fail(*args, **kwargs):
        raise ValueError("Territory payload integrity check failed")

    monkeypatch.setattr(store, "load_active_native_territory_layout", fail)
    monkeypatch.setattr(coordinator, "_match_preparation", lambda prep: None)
    replies = []
    matching.register_preparation(
        actor,
        message(),
        matchmaking=coordinator,
        team_store=store,
        telemetry=events,
        reply=replies.append,
    )
    assert replies and coordinator.preparations["Owner"]["territory_layout_owner_id"] == 0
    assert events.rows[0][0] == "game_prepare_territory_layout_unavailable"


@pytest.mark.parametrize(
    "stored,expected", [(1, 1), (2, 2), (3, 3), (0, 0), (4, 0), (True, 0), ("2", 0)]
)
def test_registration_uses_only_authenticated_current_faction(setup, monkeypatch, stored, expected):
    coordinator, store, actor, events = setup
    monkeypatch.setattr(
        store,
        "team_for",
        lambda online_id: {
            "team_id": 5,
            "current_faction_id": stored,
            "faction_id": 3,
            "desired_faction_id": 3,
        },
    )
    monkeypatch.setattr(coordinator, "_match_preparation", lambda prep: None)
    matching.register_preparation(
        actor,
        message(proto_varint(9, 999)),
        matchmaking=coordinator,
        team_store=store,
        telemetry=events,
        reply=lambda body: None,
    )
    assert coordinator.preparations["Owner"]["faction_id"] == expected
    assert events.rows[-1][1]["faction_id"] == expected


@pytest.mark.parametrize(
    "stored,expected",
    [(None, "pay_costs_equally"), (0, "pay_costs_equally"), (1, "equal_distribution")],
)
def test_registration_freezes_authenticated_team_pay_policy(setup, monkeypatch, stored, expected):
    coordinator, store, actor, events = setup
    team = {"team_id": 5, "current_faction_id": 1}
    if stored is not None:
        team["pay_apportionment"] = stored
    monkeypatch.setattr(store, "team_for", lambda online_id: team)
    monkeypatch.setattr(coordinator, "_match_preparation", lambda prep: None)
    matching.register_preparation(
        actor,
        message(),
        matchmaking=coordinator,
        team_store=store,
        telemetry=events,
        reply=lambda body: None,
    )
    assert coordinator.preparations["Owner"]["pay_policy"] == expected
    assert events.rows[-1][1]["pay_policy"] == expected


def test_registration_rejects_noncanonical_team_pay_policy(setup, monkeypatch):
    coordinator, store, actor, events = setup
    monkeypatch.setattr(
        store,
        "team_for",
        lambda online_id: {"team_id": 5, "current_faction_id": 1, "pay_apportionment": 2},
    )
    replies = []
    matching.register_preparation(
        actor,
        message(),
        matchmaking=coordinator,
        team_store=store,
        telemetry=events,
        reply=replies.append,
    )
    assert replies == [] and coordinator.preparations == {}
    assert events.rows[-1][0] == "game_message_rejected"


@pytest.mark.parametrize("matched", [False, True])
def test_cancel_retains_only_an_existing_matched_handoff(setup, matched):
    coordinator, _, actor, events = setup
    coordinator.preparations = {
        "Owner": {"prepare_id": 1, "match_id": 1 if matched else 0},
        "Other": {"prepare_id": 2, "match_id": 0},
    }
    if matched:
        coordinator.matches = {1: {"match_id": 1}}
    replies = []
    matching.cancel_preparation(
        actor, message(), matchmaking=coordinator, telemetry=events, reply=replies.append
    )
    assert ("Owner" in coordinator.preparations) == matched
    assert "Other" in coordinator.preparations and replies == [b""]


@pytest.mark.parametrize("member_index,armed", [(0, True), (1, False)])
def test_cancel_arms_recovery_only_for_released_host_before_room(
    setup, member_index, armed
):
    coordinator, _, actor, events = setup
    other = GameSession(
        b"other001",
        bytes(16),
        "Other",
        22,
        established=True,
        peer=("127.0.0.1", 2222),
    )
    owner = {"prepare_id": 1, "match_id": 1, "session": actor}
    counterpart = {"prepare_id": 2, "match_id": 1, "session": other}
    members = [owner, counterpart] if member_index == 0 else [counterpart, owner]
    coordinator.preparations = {"Owner": owner, "Other": counterpart}
    match = {"match_id": 1, "members": members, "host_result_sent": True}
    coordinator.matches = {1: match}

    matching.cancel_preparation(
        actor,
        message(),
        matchmaking=coordinator,
        telemetry=events,
        reply=lambda _body: None,
    )

    assert bool(match.get("host_transition_cancelled")) is armed
    assert events.rows[-1][1]["host_retry_armed"] is armed


def test_unmatched_disconnect_cannot_clear_a_new_search_or_other_player(setup):
    coordinator, _, actor, events = setup
    coordinator.preparations = {
        "Owner": {"prepare_id": 1, "match_id": 0},
        "Other": {"prepare_id": 2, "match_id": 0},
    }
    coordinator.signaling_ready_pairs = {
        frozenset({"Owner", "Peer"}),
        frozenset({"Other", "Third"}),
    }
    replies = []
    matching.report_disconnect(
        actor,
        message(proto_varint(1, 1) + proto_varint(2, 0) + proto_varint(3, 0) + proto_varint(4, 99)),
        matchmaking=coordinator,
        telemetry=events,
        reply=replies.append,
    )
    assert set(coordinator.preparations) == {"Owner", "Other"}
    assert coordinator.signaling_ready_pairs == {
        frozenset({"Owner", "Peer"}),
        frozenset({"Other", "Third"}),
    }
    assert events.rows[-1][1]["removed_online_ids"] == [] and replies == [b""]


def test_report_disconnected_acknowledges_current_preparation_without_removing_it(setup):
    coordinator, _, actor, events = setup
    preparation = {"prepare_id": 53, "match_id": 7, "session": actor}
    coordinator.preparations = {"Owner": preparation}
    coordinator.matches = {7: {"match_id": 7}}
    replies = []
    matching.report_disconnected(
        actor,
        message(bytes.fromhex("08351000")),
        matchmaking=coordinator,
        telemetry=events,
        reply=replies.append,
    )
    assert replies == [b""]
    assert coordinator.preparations == {"Owner": preparation}
    assert coordinator.matches == {7: {"match_id": 7}}
    assert events.rows[-1] == (
        "game_disconnected_reported",
        {
            "peer": actor.peer,
            "online_id": "Owner",
            "reported_id": 53,
            "detail32": 0,
            "matches_current_preparation": True,
        },
    )


def test_report_disconnected_preserves_late_or_foreign_key_as_telemetry(setup):
    coordinator, _, actor, events = setup
    coordinator.preparations = {"Owner": {"prepare_id": 54, "session": actor}}
    replies = []
    matching.report_disconnected(
        actor,
        message(bytes.fromhex("08351000")),
        matchmaking=coordinator,
        telemetry=events,
        reply=replies.append,
    )
    assert replies == [b""] and events.rows[-1][1]["matches_current_preparation"] is False


def test_briefing_end_acknowledges_without_mutating_match_state(setup):
    coordinator, _, actor, events = setup
    coordinator.preparations = {"Owner": {"prepare_id": 1, "match_id": 7}}
    coordinator.matches = {7: {"match_id": 7}}
    preparations = dict(coordinator.preparations)
    matches = dict(coordinator.matches)
    replies = []
    matching.report_briefing_end(
        actor,
        message(proto_varint(1, 64)),
        telemetry=events,
        reply=replies.append,
    )
    assert replies == [b""]
    assert coordinator.preparations == preparations and coordinator.matches == matches
    assert events.rows[-1] == (
        "game_briefing_end_reported",
        {"peer": actor.peer, "online_id": "Owner", "detail64": 64},
    )


@pytest.mark.parametrize("payload", [b"", proto_varint(2, 1), proto_varint(1, 1 << 64)])
def test_invalid_briefing_end_is_rejected_without_acknowledgement(setup, payload):
    _, _, actor, events = setup
    replies = []
    matching.report_briefing_end(
        actor, message(payload), telemetry=events, reply=replies.append
    )
    assert replies == [] and events.rows[-1][0] == "game_message_rejected"


def test_valid_room_notification_acknowledges_before_publication(setup, monkeypatch):
    coordinator, _, actor, events = setup
    order = []

    def publish(who, **kwargs):
        assert order == ["reply"] and who is actor
        assert kwargs["prepare_id"] == 77
        assert kwargs["encoded"] == b"archive"
        order.append("publish")

    def accept(who, **kwargs):
        kwargs.pop("reply")(b"")
        publish(who, **kwargs)

    monkeypatch.setattr(coordinator, "publish_native_room", accept)
    matching.notify_room(
        actor,
        message(proto_varint(1, 77) + proto_bytes(2, b"archive")),
        matchmaking=coordinator,
        telemetry=events,
        reply=lambda payload: order.append("reply"),
    )
    assert order == ["reply", "publish"]


@pytest.mark.parametrize(
    "payload",
    [
        proto_varint(1, 77),
        proto_varint(1, 77) + proto_varint(2, 1),
        proto_varint(1, 77) + proto_varint(1, 78) + proto_bytes(2, b"archive"),
    ],
)
def test_invalid_room_notification_does_not_acknowledge_or_publish(setup, monkeypatch, payload):
    coordinator, _, actor, events = setup
    calls = []
    replies = []
    monkeypatch.setattr(coordinator, "_publish_match_room", lambda *a, **k: calls.append(a))
    matching.notify_room(
        actor, message(payload), matchmaking=coordinator, telemetry=events, reply=replies.append
    )
    assert not calls and not replies and events.rows[-1][0] == "game_message_rejected"
