"""Native-control contract tests against disposable paths, never the public service."""

import asyncio
import json
import shutil
import time
from pathlib import Path

import pytest

from acvd_server.administration import AdminEndpoint, OperatorSettings
from acvd_server.config import ServerConfig
from acvd_server.crypto import generate_rsa_keypair
from acvd_server.diagnostics import DiagnosticBuffer, redact
from acvd_server.server import AcvdServer
from acvd_server.telemetry import Telemetry
from acvd_server.udp import GameSession


def test_operator_settings_backup_and_validation(tmp_path):
    settings = OperatorSettings(tmp_path / "operator.json")
    assert settings.accepts_login
    assert settings.values["world_normal_base_reward"] == 150_000
    assert settings.values["world_special_base_reward"] == 1_000_000
    assert settings.values["world_boss_base_reward"] == 1_000_000
    assert settings.values["world_small_subtarget_reward"] == 10_000
    assert settings.values["world_large_subtarget_reward"] == 10_000
    assert settings.values["world_mercenary_rank"] == 0
    assert settings.values["cq_map_mode"] == "territory"
    assert settings.values["cq_map_pool"] == []
    settings.update(
        {"cq_map_mode": "locked_pool", "cq_map_pool": [3101, 3410, 3602]}
    )
    assert OperatorSettings(settings.path).values["cq_map_pool"] == [3101, 3410, 3602]
    settings.update({"maintenance": True})
    assert not settings.accepts_login
    before = settings.path.read_bytes()
    settings.update({"maintenance": False, "operator_notice": "Welcome"})
    assert before in [path.read_bytes() for path in tmp_path.glob("operator-settings.*.bak")]
    assert settings.path.stat().st_mode & 0o777 == 0o600
    assert OperatorSettings(settings.path).accepts_login
    for changes in ({"maintenance": "false"}, {"unknown": 1}, {"operator_notice": "x" * 513}):
        with pytest.raises(ValueError):
            settings.update(changes)
    settings.update({"login_enabled": False})
    assert not settings.accepts_login


@pytest.mark.parametrize(
    "changes",
    [
        {"world_normal_base_reward": -1},
        {"world_special_base_reward": 10_000_001},
        {"world_small_subtarget_reward": True},
        {"world_large_subtarget_reward": "120000"},
        {"world_mercenary_rank": -1},
        {"world_mercenary_rank": 7},
    ],
)
def test_invalid_world_reward_controls_cannot_replace_settings(tmp_path, changes):
    settings = OperatorSettings(tmp_path / "operator.json")
    settings.update({"world_normal_base_reward": 600_000})
    before = settings.path.read_bytes()
    with pytest.raises(ValueError):
        settings.update(changes)
    assert settings.path.read_bytes() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"cq_map_mode": "invalid"},
        {"cq_map_mode": "locked_pool", "cq_map_pool": []},
        {"cq_map_mode": "locked_pool", "cq_map_pool": [0]},
        {"cq_map_mode": "locked_pool", "cq_map_pool": [3710, 3710]},
        {"cq_map_mode": "locked_pool", "cq_map_pool": [3700, 3710]},
        {"cq_map_pool": [2, True]},
    ],
)
def test_invalid_cq_map_controls_cannot_replace_settings(tmp_path, changes):
    settings = OperatorSettings(tmp_path / "operator.json")
    settings.update({"cq_map_mode": "territory", "cq_map_pool": [3200, 3410]})
    before = settings.path.read_bytes()
    with pytest.raises(ValueError, match="CQ map"):
        settings.update(changes)
    assert settings.path.read_bytes() == before


@pytest.mark.parametrize("value", [None, 5, "a" * 2049, "é" * 1025, "bad\0text"])
def test_invalid_welcome_cannot_replace_settings(tmp_path, value):
    settings = OperatorSettings(tmp_path / "operator.json")
    settings.update({"operator_notice": "Keep this"})
    before = settings.path.read_bytes()
    with pytest.raises(ValueError):
        settings.update({"welcome_message": value})
    assert settings.path.read_bytes() == before


def test_welcome_settings_survive_restart_and_respect_file_bound(tmp_path):
    path = tmp_path / "operator.json"
    settings = OperatorSettings(path)
    settings.update({"welcome_message": "Welcome\nContributor credits"})
    assert OperatorSettings(path).values["welcome_message"] == "Welcome\nContributor credits"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="file-size"):
        settings.update({"operator_notice": "😀" * 512, "welcome_message": "😀" * 300})
    assert path.read_bytes() == before


def test_wrapped_welcome_must_fit_the_wire_body_bound(tmp_path):
    settings = OperatorSettings(tmp_path / "operator.json")
    with pytest.raises(ValueError, match="Wrapped"):
        settings.update({"welcome_message": "a" * 2048})
    assert not settings.path.exists()


def test_debug_ring_bounded_and_redacted(tmp_path):
    telemetry = Telemetry(tmp_path / "events.jsonl")
    telemetry.set_diagnostics(True)
    telemetry.emit(
        "message_received", service="auth", plaintext_payload=b"secret", encrypted_payload=b"cipher"
    )
    row = telemetry.read_diagnostics(0)["records"][0]
    assert "redacted" in row["plaintext_payload"]
    assert row["encrypted_payload"]["hex"] == b"cipher".hex()
    telemetry.emit("game_reply_observed", payload=b"a" * 70000)
    assert telemetry.read_diagnostics(0)["records"][-1]["payload"]["truncated"]
    assert telemetry.read_diagnostics(0)["records"][-1]["payload"]["length"] == 70000
    telemetry.set_diagnostics(False)
    assert telemetry.read_diagnostics(0)["records"] == []
    ring = DiagnosticBuffer(200)
    ring.set_enabled(True)
    for i in range(20):
        ring.append({"event": "test", "value": "x" * 40})
    assert ring.bytes_used <= 200 and ring.read(1)["gap"]
    assert redact({"game_key": {"hex": "secret"}})["game_key"] == "[credential redacted]"


@pytest.mark.asyncio
async def test_unix_admin_controls_are_local_and_scoped(tmp_path):
    settings = OperatorSettings(tmp_path / "settings.json")
    telemetry = Telemetry(tmp_path / "events.jsonl")
    endpoint = AdminEndpoint(
        tmp_path / "operator.sock", lambda: {"settings": settings.values}, settings, telemetry
    )
    await endpoint.start()

    async def request(data):
        reader, writer = await asyncio.open_unix_connection(endpoint.path)
        writer.write(json.dumps(data).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return response

    try:
        assert endpoint.path.stat().st_mode & 0o777 == 0o600
        assert (await request({"command": "status"}))["ok"]
        assert (await request({"command": "configure", "settings": {"maintenance": True}}))["ok"]
        assert not settings.accepts_login
        assert not (await request({"command": "delete_players"}))["ok"]
        assert not (await request({"command": "configure", "settings": {"maintenance": "false"}}))[
            "ok"
        ]
        assert (await request({"command": "debug", "enabled": True}))["ok"]
        telemetry.emit("test", payload=b"wire")
        assert (await request({"command": "events", "after": 0}))["data"]["records"]
    finally:
        await endpoint.close()
    assert not endpoint.path.exists()


@pytest.mark.asyncio
async def test_cq_map_policy_change_requires_an_empty_session_boundary(tmp_path):
    settings = OperatorSettings(tmp_path / "settings.json")
    telemetry = Telemetry(tmp_path / "events.jsonl")
    live = {"players": [{"online_id": "Pilot"}], "matches": 0}
    endpoint = AdminEndpoint(
        tmp_path / "operator.sock",
        lambda: {**live, "settings": settings.values},
        settings,
        telemetry,
    )
    await endpoint.start()

    async def request(data):
        reader, writer = await asyncio.open_unix_connection(endpoint.path)
        writer.write(json.dumps(data).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return response

    change = {
        "command": "configure",
        "settings": {"cq_map_mode": "locked_pool", "cq_map_pool": [3200, 3410]},
    }
    try:
        rejected = await request(change)
        assert not rejected["ok"] and "logged out" in rejected["error"]
        assert settings.values["cq_map_mode"] == "territory"

        live["players"] = []
        live["matches"] = 1
        rejected = await request(change)
        assert not rejected["ok"] and "no match" in rejected["error"]

        live["matches"] = 0
        assert (await request(change))["ok"]
        assert settings.values["cq_map_pool"] == [3200, 3410]
    finally:
        await endpoint.close()


@pytest.mark.asyncio
async def test_permanent_player_reset_requires_exact_confirmation(tmp_path):
    settings = OperatorSettings(tmp_path / "settings.json")
    telemetry = Telemetry(tmp_path / "events.jsonl")
    calls = []
    endpoint = AdminEndpoint(
        tmp_path / "operator.sock",
        lambda: {"settings": settings.values},
        settings,
        telemetry,
        delete_player=lambda online_id, player_id: calls.append((online_id, player_id))
        or {"online_id": online_id, "player_id": player_id, "deleted_paths": 2},
    )
    await endpoint.start()

    async def request(data):
        reader, writer = await asyncio.open_unix_connection(endpoint.path)
        writer.write(json.dumps(data).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return response

    try:
        base = {"command": "delete_player", "online_id": "Target", "player_id": 42}
        assert not (await request(base))["ok"]
        assert not (await request({**base, "confirmed": True, "confirmation": "Other"}))["ok"]
        accepted = await request({**base, "confirmed": True, "confirmation": "Target"})
        assert accepted["ok"] and calls == [("Target", 42)]
    finally:
        await endpoint.close()


@pytest.mark.asyncio
async def test_real_server_maintenance_preserves_established_session(tmp_path):
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

    async def request(value):
        r, w = await asyncio.open_unix_connection(server.admin_endpoint.path)
        w.write(json.dumps(value).encode() + b"\n")
        await w.drain()
        result = json.loads(await r.readline())
        w.close()
        await w.wait_closed()
        return result

    try:
        session = GameSession(b"12345678", bytes(16), "CONSOLE_QA", 123)
        session.peer = ("127.0.0.1", 65432)
        session.established = True
        session.last_seen_monotonic = time.monotonic()
        server.game_sessions[session.auth_token] = session
        status = await request({"command": "status"})
        assert status["ok"] and status["data"]["players"][0]["online_id"] == "CONSOLE_QA"
        assert status["data"]["connection_policy"] == {
            "room_handoff": "native_authenticated_archive",
            "legacy_room_log_bridge_active": False,
            "team_peer_recovery": {
                "scope": "reporter_only",
                "cooldown_seconds": 15.0,
                "max_attempts": 2,
                "backoff_seconds": 60.0,
                "whole_team_escalation": False,
            },
        }
        map_change = await request(
            {
                "command": "configure",
                "settings": {"cq_map_mode": "locked_pool", "cq_map_pool": [3200]},
            }
        )
        assert not map_change["ok"] and "logged out" in map_change["error"]
        await request({"command": "configure", "settings": {"maintenance": True}})
        r, w = await asyncio.open_connection(
            "127.0.0.1", server.tcp_servers[0].sockets[0].getsockname()[1]
        )
        assert await asyncio.wait_for(r.read(1), 1) == b""
        w.close()
        await w.wait_closed()
        assert session.established
        await request({"command": "configure", "settings": {"maintenance": False}})
        assert server.operator_settings.accepts_login
        assert not (await request({"command": "shutdown"}))["ok"]
        assert (await request({"command": "shutdown", "confirmed": True}))["ok"]
        await asyncio.wait_for(server.shutdown_requested.wait(), 1)
    finally:
        await server.close()
