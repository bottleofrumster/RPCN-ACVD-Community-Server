"""Team News is a scoped, durable projection, not synthetic login messages."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from acvd_server import udp
from acvd_server.handlers.world import get_notice_list
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.server import GameDatagramProtocol
from acvd_server.team_news import (
    MAX_EVENT_ID,
    MAX_TEAM_NEWS,
    MEMBER_JOINED,
    TEAM_MEDAL_EARNED,
    TEAM_RANK_INCREASED,
    decode_query,
    encode_events,
    record_team_changes,
    stored_events,
    wire_notice_id,
)
from acvd_server.teams import TeamStore
from acvd_server.telemetry import Telemetry
from acvd_server.udp import GameMessage, GameSession

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def create(store, name="Leader", player_id=11):
    return store.create(
        online_id=name,
        player_id=player_id,
        name=name,
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=2,
    )


def test_creation_medal_join_and_retry_are_durable_once(tmp_path):
    store = TeamStore(tmp_path / "teams")
    team = create(store)
    assert [r["type"] for r in stored_events(team)] == [TEAM_MEDAL_EARNED]
    assert store.join_team(online_id="Peer", player_id=22, team_id=team["team_id"])
    rows = deepcopy(stored_events(team))
    assert [r["type"] for r in rows] == [TEAM_MEDAL_EARNED, MEMBER_JOINED]
    store.join_team(online_id="Peer", player_id=22, team_id=team["team_id"])
    create(store)
    assert stored_events(team) == rows
    cold = TeamStore(store.root)
    assert stored_events(cold.team_for("Leader")) == rows


def test_medal_and_rank_changes_share_one_commit_and_no_read_side_effect(tmp_path):
    store = TeamStore(tmp_path / "teams")
    team = create(store)
    updated = deepcopy(store.state)
    target = updated["teams"][str(team["team_id"])]
    target["team_medal_ids"] += [10, 11]
    target["team_rank_score"] = 1500
    store.commit_state(updated)
    rows = deepcopy(stored_events(store.team_for("Leader")))
    assert [r["type"] for r in rows][-2:] == [TEAM_MEDAL_EARNED, TEAM_RANK_INCREASED]
    store.commit_state(deepcopy(store.state))
    assert stored_events(store.team_for("Leader")) == rows


@pytest.mark.parametrize("action", ["create", "join", "medal"])
def test_failed_write_rolls_back_event_ids_and_triggering_action(tmp_path, monkeypatch, action):
    store = TeamStore(tmp_path / "teams")
    if action != "create":
        create(store)
    before = deepcopy(store.state)
    disk = store.state_path.read_bytes() if store.state_path.exists() else None

    def fail():
        raise OSError("test write failure")

    monkeypatch.setattr(store, "_save", fail)
    with pytest.raises(OSError):
        if action == "create":
            create(store)
        elif action == "join":
            store.join_team(online_id="Peer", player_id=22, team_id=1)
        else:
            updated = deepcopy(store.state)
            updated["teams"]["1"]["team_medal_ids"].append(10)
            store.commit_state(updated)
    assert store.state == before
    assert (store.state_path.read_bytes() if store.state_path.exists() else None) == disk


def test_failed_candidate_can_be_retried_without_duplicate_news(tmp_path, monkeypatch):
    store = TeamStore(tmp_path / "teams")
    create(store)
    updated = deepcopy(store.state)
    updated["teams"]["1"]["team_medal_ids"].append(10)
    save = store._save
    monkeypatch.setattr(store, "_save", lambda: (_ for _ in ()).throw(OSError("test")))
    with pytest.raises(OSError):
        store.commit_state(updated)
    monkeypatch.setattr(store, "_save", save)
    store.commit_state(updated)
    assert [r["id"] for r in stored_events(store.team_for("Leader"))] == [1, 2]


def query(store, name, player_id, payload=bytes.fromhex("08321000")):
    replies, errors, events = [], [], []
    get_notice_list(
        GameSession(b"test0001", bytes(16), name, player_id),
        GameMessage(0x3A6, bytes(4), payload),
        teams=store,
        telemetry=SimpleNamespace(emit=lambda event, **kw: events.append((event, kw))),
        reply=replies.append,
        reply_error=lambda *args: errors.append(args),
    )
    return replies, errors, events


def test_native_query_is_team_scoped_read_only_and_stable_across_refreshes(tmp_path):
    store = TeamStore(tmp_path / "teams")
    first = create(store)
    second = create(store, "Other", 33)
    store.join_team(online_id="Peer", player_id=22, team_id=first["team_id"])
    before, disk = deepcopy(store.state), store.state_path.read_bytes()
    replies, errors, events = query(store, "Leader", 11)
    assert not errors and events[-1][1]["count"] == 2
    types = [first_field(decode_proto(f.value), 2).value for f in decode_proto(replies[0])]
    assert types == [MEMBER_JOINED, TEAM_MEDAL_EARNED]
    assert query(store, "Peer", 22)[0] == replies
    other, errors, events = query(store, "Other", 33)
    assert events[-1][1]["count"] == 1 and events[-1][1]["team_id"] == second["team_id"]
    assert query(store, "Leader", 11)[0] == replies
    assert store.state == before and store.state_path.read_bytes() == disk
    assert query(store, "Leader", 33)[1] == [(1, 28)]
    assert query(store, "Nobody", 99)[0] == [b""]


def test_transfer_news_belongs_only_to_destination_and_same_team_retry_is_quiet(tmp_path):
    store = TeamStore(tmp_path / "teams")
    first, second = create(store), create(store, "Other", 33)
    store.join_team(online_id="Peer", player_id=22, team_id=first["team_id"])
    old_rows = deepcopy(stored_events(first))
    assert store.join_team(
        online_id="Peer", player_id=22, team_id=second["team_id"], replace_existing=True
    )
    assert stored_events(first) == old_rows
    assert stored_events(second)[-1]["type"] == MEMBER_JOINED
    assert query(store, "Peer", 22)[2][-1][1]["team_id"] == second["team_id"]


def test_bounded_retention_ids_and_dates_do_not_renew_on_reads():
    state = {
        "teams": {"1": {"team_id": 1, "team_medal_ids": [], "team_rank_score": 0}},
        "members": {},
    }
    for n in range(MAX_TEAM_NEWS + 20):
        updated = deepcopy(state)
        updated["members"][f"Pilot{n}"] = 1
        record_team_changes(state, updated, NOW)
        state = updated
    rows = stored_events(state["teams"]["1"])
    assert len(rows) == MAX_TEAM_NEWS and rows[0]["id"] == 21 and rows[-1]["id"] == 70
    assert all(r["created_at"] == NOW.isoformat() for r in rows)
    assert encode_events(rows) == encode_events(rows)


def test_empty_old_teams_do_not_get_fabricated_history():
    state = {
        "teams": {"1": {"team_id": 1, "team_medal_ids": [1, 2], "team_rank_score": 4000}},
        "members": {},
    }
    updated = deepcopy(state)
    record_team_changes(state, updated, NOW)
    assert updated == state and stored_events(updated["teams"]["1"]) == []


def test_notice_wire_required_fields_and_date():
    payload = encode_events([{"id": 9, "type": TEAM_MEDAL_EARNED, "created_at": NOW.isoformat()}])
    notice = decode_proto(decode_proto(payload)[0].value)
    assert [f.number for f in notice] == [1, 2, 5]
    assert first_field(notice, 1).value == wire_notice_id(9)
    assert first_field(notice, 2).value == 1100
    assert [f.value for f in decode_proto(first_field(notice, 5).value)] == [
        2026,
        10,
        5,
        12,
        0,
        0,
        0,
    ]


def test_reported_type_zero_is_fixed_at_the_event_field():
    payload = encode_events([{"id": 9, "type": TEAM_MEDAL_EARNED, "created_at": NOW.isoformat()}])
    fields = decode_proto(decode_proto(payload)[0].value)
    # SDK Notice word1 becomes the application's localized event code.
    assert first_field(fields, 2).value == TEAM_MEDAL_EARNED
    assert first_field(fields, 1).value != TEAM_MEDAL_EARNED


def test_live_followup_after_old_rank_notice_is_not_rejected(tmp_path):
    store = TeamStore(tmp_path / "teams")
    create(store)
    # Exact live diagnostic request: limit50, last notice ID1101.
    replies, errors, _ = query(store, "Leader", 11, bytes.fromhex("083210cd08"))
    assert not errors and len(replies) == 1


def news_ids_and_types(payload):
    notices = [decode_proto(field.value) for field in decode_proto(payload)]
    return [(first_field(row, 1).value, first_field(row, 2).value) for row in notices]


@pytest.mark.parametrize("old_cursor", [0, 1001, 1100, 1101])
def test_legacy_cursor_gets_correct_wire_ids_and_incremental_followups(tmp_path, old_cursor):
    store = TeamStore(tmp_path / "teams")
    create(store)
    updated = deepcopy(store.state)
    updated["teams"]["1"]["team_rank_score"] = 1500
    store.commit_state(updated)
    before, disk = deepcopy(store.state), store.state_path.read_bytes()

    def since(cursor):
        replies, errors, _ = query(store, "Leader", 11, proto_varint(1, 50) + proto_varint(2, cursor))
        assert not errors
        return replies[0]

    rows = news_ids_and_types(since(old_cursor))
    assert rows == [(1103, TEAM_RANK_INCREASED), (1102, TEAM_MEDAL_EARNED)]
    assert rows == news_ids_and_types(since(old_cursor))  # retry is identical
    # A client that accepts these rows advances by maxID. This server test does
    # not emulate the separate native date cutoff or erase old Type0 cache rows.
    cursor = max(identifier for identifier, _ in rows)
    assert since(cursor) == b""
    assert store.state == before and store.state_path.read_bytes() == disk
    store.join_team(online_id="Peer", player_id=22, team_id=1)
    new = news_ids_and_types(since(cursor))
    assert new == [(1104, MEMBER_JOINED)]
    assert since(new[0][0]) == b""


def test_incremental_queries_stay_team_scoped(tmp_path):
    store = TeamStore(tmp_path / "teams")
    first, second = create(store), create(store, "Other", 33)
    store.join_team(online_id="Peer", player_id=22, team_id=first["team_id"])
    first_cursor = wire_notice_id(stored_events(first)[0]["id"])
    payload = proto_varint(1, 50) + proto_varint(2, first_cursor)
    rows = news_ids_and_types(query(store, "Leader", 11, payload)[0][0])
    assert rows == [(wire_notice_id(stored_events(first)[-1]["id"]), MEMBER_JOINED)]
    other_ids = {wire_notice_id(row["id"]) for row in stored_events(second)}
    assert not other_ids.intersection(identifier for identifier, _ in rows)


@pytest.mark.parametrize("cursor", [0, 1, 1101, (1 << 32) - 1])
def test_cursor_is_a_uint32_watermark_not_a_team_or_event_type(cursor):
    decoded = decode_query(proto_varint(1, 50) + proto_varint(2, cursor))
    assert decoded.limit == 50 and decoded.after_id == cursor


@pytest.mark.parametrize("payload", [
    proto_varint(1, 1) + proto_bytes(2, b"0"),
    proto_bytes(1, b"1") + proto_varint(2, 0),
    proto_varint(1, 1) + proto_varint(1, 2) + proto_varint(2, 0),
    proto_varint(1, 1) + proto_varint(2, -1),
    proto_varint(1, 1) + proto_varint(2, 0) + proto_varint(3, 1),
])
def test_cursor_support_does_not_accept_malformed_queries(payload):
    with pytest.raises(ValueError):
        decode_query(payload)


def test_new_wire_identity_retains_history_and_survives_reload(tmp_path):
    store = TeamStore(tmp_path / "teams")
    created = create(store)
    before = deepcopy(stored_events(created))
    encoded = encode_events(before)
    assert stored_events(TeamStore(store.root).team_for("Leader")) == before
    assert encode_events(before) == encoded
    fields = decode_proto(decode_proto(encoded)[0].value)
    assert first_field(fields, 1).value == 1102
    assert first_field(fields, 2).value == TEAM_MEDAL_EARNED
    assert before[0]["id"] == 1  # no stored ID or date migration


def test_rebased_ids_are_bounded_and_allocator_never_wraps():
    assert wire_notice_id(MAX_EVENT_ID) == (1 << 32) - 1
    for invalid in (0, -1, True, MAX_EVENT_ID + 1):
        with pytest.raises(ValueError):
            wire_notice_id(invalid)
    state = {"teams": {"1": {"team_id": 1, "team_medal_ids": [], "team_rank_score": 0}},
             "members": {}, "next_team_news_id": MAX_EVENT_ID}
    updated = deepcopy(state)
    updated["members"]["Last"] = 1
    record_team_changes(state, updated, NOW)
    assert updated["teams"]["1"]["team_news"][0]["id"] == MAX_EVENT_ID
    exhausted = deepcopy(updated)
    exhausted["members"]["Overflow"] = 1
    before = deepcopy(exhausted)
    with pytest.raises(ValueError, match="capacity"):
        record_team_changes(updated, exhausted, NOW)
    assert exhausted == before


def test_high_cursor_and_zero_limit_are_successful_empty_feeds(tmp_path):
    store = TeamStore(tmp_path / "teams")
    create(store)
    for limit, cursor in ((50, (1 << 32) - 1), (0, 1101)):
        replies, errors, _ = query(store, "Leader", 11, proto_varint(1, limit) + proto_varint(2, cursor))
        assert replies == [b""] and not errors


def test_encrypted_followup_preserves_reply_identity_without_retry_errors(tmp_path):
    store = TeamStore(tmp_path / "teams")
    create(store)
    session = GameSession(b"news0001", bytes(range(16)), "Leader", 11, established=True)
    library = Path(__file__).resolve().parents[1] / "native/libacvd_cwc.so"
    protocol = GameDatagramProtocol(
        Telemetry(tmp_path / "events.jsonl"), {session.auth_token: session}, library,
        tmp_path / "players", store,
    )
    packets = []
    protocol.connection_made(SimpleNamespace(sendto=lambda raw, peer: packets.append(raw)))
    before = store.state_path.read_bytes()
    try:
        for sequence, cursor in enumerate((1101, 1102, 1102), 100):
            message = GameMessage(
                udp.GET_NOTICE_LIST, sequence.to_bytes(4, "big"),
                proto_varint(1, 50) + proto_varint(2, cursor), sequence,
            )
            wire = udp.encrypt_client_datagram(
                udp.encode_reliable_packet(udp.ReliablePacket(
                    sequence, 0, udp.DAT, payload=udp.encode_game_request(message),
                )), session.game_key, session.auth_token, 0, library,
            )
            packets.clear()
            protocol.datagram_received(wire, ("127.0.0.1", 40001))
            replies = [udp.decode_reliable_packet(udp.decrypt_server_datagram(
                raw, session.game_key, library,
            )) for raw in packets]
            assert [packet.opcode for packet in replies] == [udp.ACK, udp.DAT_ACK]
            response = udp.decode_game_message(replies[-1].payload)
            assert response.message_index == message.message_index
            assert replies[-1].remote_sequence == sequence
            assert response.payload[:16] == bytes.fromhex("00000000010000000000000000000000")
            assert news_ids_and_types(response.payload[16:]) == (
                [(1102, TEAM_MEDAL_EARNED)] if cursor == 1101 else []
            )
        assert store.state_path.read_bytes() == before
        assert not (tmp_path / "players").exists()
    finally:
        protocol.connection_lost(None)


@pytest.mark.parametrize(
    "payload",
    [b"\xff", proto_varint(1, 1), proto_varint(1, 1) + proto_varint(2, 1 << 32),
     proto_varint(1, 1 << 32) + proto_varint(2, 0), bytes.fromhex("080110001000")],
)
def test_unknown_queries_fail_explicitly(payload):
    with pytest.raises(ValueError):
        decode_query(payload)


def test_limit_is_bounded_and_zero_is_empty(tmp_path):
    assert decode_query(bytes.fromhex("08321000")).limit == 50
    assert decode_query(proto_varint(1, 100000) + proto_varint(2, 0)).limit == 50
    store = TeamStore(tmp_path / "teams")
    create(store)
    assert query(store, "Leader", 11, bytes.fromhex("08001000"))[0] == [b""]


def test_settled_battle_medal_and_rank_news_do_not_repeat_after_result_replay(tmp_path):
    store = TeamStore(tmp_path / "teams")
    create(store)
    store.remember_player("Leader", 11)
    store.synchronize_balance("Leader", 100000)
    updated = deepcopy(store.state)
    updated["teams"]["1"]["team_rank_score"] = 2900
    store.commit_state(updated)
    prior_count = len(stored_events(store.team_for("Leader")))
    payments = (
        {
            "online_id": "Leader",
            "player_id": 11,
            "gross_earnings": 100,
            "expenses": 0,
            "response_hex": "0800",
        },
    )
    args = dict(
        policy_version="news-test",
        team_progress=({"team_id": 1, "rank_delta": 200, "special_delta": 1},),
        team_medal_progress=(
            {
                "team_id": 1,
                "ac_kills_delta": 0,
                "fame_delta": 200,
                "world_sorties_delta": 1,
                "pvp_wins_delta": 0,
            },
        ),
    )
    store.settle_world_battle_money("ad" * 32, payments, **args)
    rows = deepcopy(stored_events(store.team_for("Leader")))
    assert [r["type"] for r in rows[prior_count:]] == [TEAM_MEDAL_EARNED, TEAM_RANK_INCREASED]
    cold = TeamStore(store.root)
    before = deepcopy(cold.state)
    replay = cold.settle_world_battle_money("ad" * 32, payments, **args)
    assert not replay["applied"] and cold.state == before
    assert stored_events(cold.team_for("Leader")) == rows


def test_closed_team_join_does_not_create_news_or_change_membership(tmp_path):
    store = TeamStore(tmp_path / "teams")
    first, second = create(store), create(store, "Other", 33)
    store.join_team(online_id="Peer", player_id=22, team_id=first["team_id"])
    updated = deepcopy(store.state)
    updated["teams"][str(second["team_id"])]["recruitment_open"] = False
    store.commit_state(updated)
    before = deepcopy(store.state)
    assert (
        store.join_team(
            online_id="Peer", player_id=22, team_id=second["team_id"], replace_existing=True
        )
        is None
    )
    assert store.state == before


@pytest.mark.parametrize("corruption", ["duplicate_id", "unknown_type", "bad_date", "oversize"])
def test_corrupt_news_is_not_returned_or_silently_rewritten(tmp_path, corruption):
    store = TeamStore(tmp_path / "teams")
    team = create(store)
    if corruption == "duplicate_id":
        team["team_news"].append(dict(team["team_news"][0]))
    elif corruption == "unknown_type":
        team["team_news"][0]["type"] = 9999
    elif corruption == "bad_date":
        team["team_news"][0]["created_at"] = "not-a-date"
    else:
        team["team_news"] *= MAX_TEAM_NEWS + 1
    before = deepcopy(store.state)
    replies, errors, _ = query(store, "Leader", 11)
    assert not replies and errors == [(1, 28)] and store.state == before
