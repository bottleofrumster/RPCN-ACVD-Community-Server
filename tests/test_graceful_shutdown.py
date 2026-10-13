"""Bounded service shutdown closes owned listeners, streams, timers, and tasks."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from acvd_server.config import ServerConfig
from acvd_server.crypto import generate_rsa_keypair
from acvd_server.server import AcvdServer


def config(tmp_path: Path) -> ServerConfig:
    (tmp_path / "keys").mkdir()
    (tmp_path / "native").mkdir()
    generate_rsa_keypair(tmp_path / "keys/server-private.pem", tmp_path / "keys/publickey.pem")
    shutil.copy2(
        Path(__file__).resolve().parents[1] / "native/libacvd_cwc.so",
        tmp_path / "native/libacvd_cwc.so",
    )
    return ServerConfig(
        project_root=tmp_path,
        bind_host="127.0.0.1",
        advertised_host="127.0.0.1",
        login_ports=[0],
        auth_port=0,
        cdn_port=0,
        game_port=0,
    )


@pytest.mark.asyncio
async def test_close_is_bounded_complete_and_idempotent(tmp_path):
    server = AcvdServer(config(tmp_path))
    await server.start()
    protocol = server.game_protocol
    assert protocol is not None

    login_port = server.tcp_servers[0].sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", login_port)
    await asyncio.sleep(0)
    assert server._stream_writers

    fallback = asyncio.create_task(asyncio.sleep(60))
    room_watch = asyncio.create_task(asyncio.sleep(60))
    protocol.preparations["Waiting"] = {"single_team_fallback_task": fallback}
    protocol.matches[1] = {"room_log_bridge_task": room_watch}
    recovery = asyncio.get_running_loop().call_later(60, lambda: None)
    protocol._reply_retries["pending"] = recovery

    await asyncio.wait_for(server.close(), timeout=2)
    await asyncio.sleep(0)

    assert await asyncio.wait_for(reader.read(), timeout=1) == b""
    assert fallback.cancelled() and room_watch.cancelled() and recovery.cancelled()
    assert not protocol.preparations and not protocol.matches
    assert not protocol._reply_retries and not protocol._request_recovery.pending
    assert protocol.transport is None and protocol.transport_closed.is_set()
    assert server.game_protocol is None and server.udp_transport is None
    assert not server.tcp_servers and not server._stream_writers
    assert not server.admin_endpoint.path.exists()
    assert server.config.session_state_path.is_file()

    await server.close()
    events = [json.loads(line) for line in server.config.telemetry_path.read_text().splitlines()]
    assert sum(event["event"] == "server_stopping" for event in events) == 1
    assert sum(event["event"] == "server_stopped" for event in events) == 1
    assert any(event["event"] == "game_matchmaking_closed" for event in events)
    assert not any(event["event"].endswith("close_timeout") for event in events)

    writer.close()
    await writer.wait_closed()
