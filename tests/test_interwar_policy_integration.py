"""Local operator API and grace-period guards on disposable stores."""

import asyncio
import json
import shutil
import time
from copy import deepcopy
from pathlib import Path

import pytest

from acvd_server.config import ServerConfig
from acvd_server.crypto import generate_rsa_keypair
from acvd_server.handlers import team_updates, territory
from acvd_server.matchmaking import MatchmakingCoordinator
from acvd_server.protocol import decode_proto, first_field, proto_bytes, proto_varint
from acvd_server.server import AcvdServer
from acvd_server.teams import TeamStore
from acvd_server.udp import GET_WORLD_CURRENT_INFO, GameMessage, GameSession, ReliablePacket


class Events:
    def __init__(self):
        self.rows = []

    def emit(self, name, **fields):
        self.rows.append((name, fields))


@pytest.mark.asyncio
async def test_live_operator_api_drives_the_same_wire_policy_without_restart(tmp_path, monkeypatch):
    (tmp_path / "keys").mkdir()
    (tmp_path / "native").mkdir()
    generate_rsa_keypair(tmp_path / "keys/server-private.pem", tmp_path / "keys/publickey.pem")
    shutil.copy2(
        Path(__file__).resolve().parents[1] / "native/libacvd_cwc.so",
        tmp_path / "native/libacvd_cwc.so",
    )
    config = ServerConfig(
        project_root=tmp_path,
        bind_host="127.0.0.1",
        advertised_host="127.0.0.1",
        login_ports=[0],
        auth_port=0,
        cdn_port=0,
        game_port=0,
    )
    server = AcvdServer(config)
    await server.start()

    async def api(value):
        reader, writer = await asyncio.open_unix_connection(server.admin_endpoint.path)
        writer.write(json.dumps(value).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return response

    try:
        actor = GameSession(
            b"test0001", bytes(16), "Tester", 11, established=True, peer=("127.0.0.1", 12345)
        )
        server.game_sessions[actor.auth_token] = actor
        before = deepcopy(server.teams.state)
        baseline = (await api({"command": "status"}))["data"]
        replies = []
        protocol = server.game_protocol
        monkeypatch.setattr(protocol, "_send_game_reply", lambda *a, **k: replies.append(a[3]))
        for mode, expected in (("grace", 0), ("active", 2)):
            changed = await api({"command": "configure", "settings": {"world_mode": mode}})
            assert changed["ok"] and changed["data"]["world"]["phase"] == expected
            assert changed["data"]["pid"] == baseline["pid"]
            assert protocol._matchmaking.get_world_phase() == expected
            protocol._dispatch_game_message(
                actor,
                ReliablePacket(0, 0, 4),
                GameMessage(GET_WORLD_CURRENT_INFO, bytes(4), b"", 1),
            )
            outer = decode_proto(replies[-1])
            info = decode_proto(first_field(outer, 1).value)
            assert first_field(info, 3).value == expected
            assert first_field(info, 2).value == 3
            assert actor.established and server.teams.state == before
            assert server.operator_settings.accepts_login
        assert not (await api({"command": "configure", "settings": {"world_mode": "scheduled"}}))[
            "ok"
        ]
        assert protocol.world_phase() == 2
    finally:
        await server.close()


@pytest.fixture
def owned_layout(tmp_path):
    store, events = TeamStore(tmp_path / "teams"), Events()
    members = []
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        send_push=lambda *a, **k: None,
        telemetry=events,
        room_log_paths=[],
        get_sessions=lambda: [member["session"] for member in members],
        get_team_id=lambda name: int((store.team_for(name) or {}).get("team_id", 0)),
    )
    for name, player in (("Attacker", 11), ("Defender", 22)):
        team = store.create(
            online_id=name,
            player_id=player,
            name=name,
            operation_base=0,
            language=1,
            play_type=1,
            enlistment_method=1,
        )
        team["territory_area_id"] = 3
        actor = GameSession(
            name.encode().ljust(8, b"0"),
            bytes(16),
            name,
            player,
            established=True,
            peer=("127.0.0.1", player),
        )
        member = {"session": actor, "team_id": team["team_id"], "prepare_id": player, "match_id": 1}
        members.append(member)
        coordinator.preparations[name] = member
    match = {
        "match_id": 1,
        "members": members,
        "combat_area_id": 3,
        "territory_layout_owner_id": 22,
        "world_phase": 2,
        "squad": {m["session"].player_id: (m["session"], m["team_id"]) for m in members},
        "created_monotonic": time.monotonic(),
    }
    coordinator.matches[1] = match
    layout = b"\0\7" + bytes(1055)
    store.save_native_territory_layout(
        "Defender", player_id=22, season_id=3, area_id=3, layout_version=7, payload=layout
    )
    return store, events, coordinator, members, match, layout


def test_grace_read_guard_preserves_a_frozen_active_war_handoff(owned_layout):
    store, events, coordinator, members, match, layout = owned_layout
    request = GameMessage(
        0x3BB, bytes(4), proto_varint(1, 3) + proto_varint(2, 22) + proto_varint(3, 3)
    )
    before, replies = deepcopy(store.state), []
    territory.get_layout(
        members[0]["session"],
        request,
        team_store=store,
        telemetry=events,
        reply=replies.append,
        matchmaking=coordinator,
        phase=0,
    )
    assert first_field(decode_proto(replies[0]), 3).value == layout
    match["world_phase"] = 0
    replies.clear()
    territory.get_layout(
        members[0]["session"],
        request,
        team_store=store,
        telemetry=events,
        reply=replies.append,
        matchmaking=coordinator,
        phase=0,
    )
    assert not replies and store.state == before
    coordinator.matches.clear()
    replies.clear()
    territory.get_layout(
        members[1]["session"],
        request,
        team_store=store,
        telemetry=events,
        reply=replies.append,
        matchmaking=coordinator,
        phase=0,
    )
    assert not replies
    assert events.rows[-1][0] == "game_message_rejected"
    assert store.state == before


def test_grace_blocks_upload_without_changing_the_stored_layout(owned_layout):
    store, events, _, members, _, layout = owned_layout
    before = {str(p): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    request = GameMessage(
        0x3BE,
        bytes(4),
        proto_varint(1, 3)
        + proto_varint(2, 3)
        + proto_varint(3, 7)
        + proto_bytes(4, layout[:-1] + b"\1"),
    )
    replies = []
    territory.upload_layout(
        members[1]["session"],
        request,
        team_store=store,
        telemetry=events,
        reply=replies.append,
        phase=0,
    )
    assert not replies and events.rows[-1][0] == "game_message_rejected"
    assert {str(p): p.read_bytes() for p in store.root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("field,value", [(26, 2), (27, 7)])
def test_grace_rejects_known_next_season_changes_atomically(owned_layout, field, value):
    store, events, _, members, _, _ = owned_layout
    before, replies = deepcopy(store.state), []
    team_updates.update_team_properties(
        members[1]["session"],
        GameMessage(0, bytes(4), proto_varint(field, value) + proto_bytes(24, "Do not apply")),
        team_store=store,
        telemetry=events,
        reply=replies.append,
        phase=0,
    )
    assert not replies and store.state == before


def test_grace_allows_unrelated_team_comment_with_unchanged_season_fields(owned_layout):
    store, events, _, members, _, _ = owned_layout
    replies = []
    team_updates.update_team_properties(
        members[1]["session"],
        GameMessage(
            0, bytes(4), proto_varint(26, 1) + proto_varint(27, 0) + proto_bytes(24, "Comment")
        ),
        team_store=store,
        telemetry=events,
        reply=replies.append,
        phase=0,
    )
    assert replies == [b""] and store.team_for("Defender")["comment"] == "Comment"
    assert store.team_for("Defender")["territory_area_id"] == 3
