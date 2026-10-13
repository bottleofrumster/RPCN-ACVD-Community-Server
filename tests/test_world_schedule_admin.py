"""Exercise the panel's schedule commands on a private administrative socket."""

import asyncio
import json

import pytest

from acvd_server.administration import AdminEndpoint, OperatorSettings
from acvd_server.telemetry import Telemetry
from acvd_server.world_policy import world_state


@pytest.mark.asyncio
async def test_schedule_readback_boundaries_restart_and_cancel(tmp_path):
    clock = [99]
    path = tmp_path / "settings.json"
    settings = OperatorSettings(path)
    settings.update({"login_enabled": False, "operator_notice": "Keep this notice"})
    protected = tmp_path / "player-data.bin"
    protected.write_bytes(b"unchanged player data")

    def create_endpoint():
        return AdminEndpoint(
            tmp_path / "operator.sock",
            lambda: {"settings": settings.values, "world": world_state(settings.values, clock[0])},
            settings,
            Telemetry(tmp_path / "events.jsonl"),
        )

    async def request(command, **fields):
        reader, writer = await asyncio.open_unix_connection(tmp_path / "operator.sock")
        try:
            writer.write(json.dumps({"command": command, **fields}).encode() + b"\n")
            await writer.drain()
            return json.loads(await reader.readline())
        finally:
            writer.close()
            await writer.wait_closed()

    endpoint = create_endpoint()
    await endpoint.start()
    try:
        saved = await request(
            "configure",
            settings={"world_mode": "scheduled", "grace_start_utc": 100, "grace_end_utc": 200},
        )
        assert saved["ok"] and saved["data"]["world"]["phase"] == 2
        before = path.read_bytes()
        assert not (await request("configure", settings={"grace_end_utc": 99}))["ok"]
        assert path.read_bytes() == before
        # Recreate the endpoint and settings without any desktop process.
        await endpoint.close()
        settings = OperatorSettings(path)
        endpoint = create_endpoint()
        await endpoint.start()
        for now, phase, next_change in ((99, 2, 100), (100, 0, 200), (199, 0, 200), (200, 2, None)):
            clock[0] = now
            state = (await request("status"))["data"]["world"]
            assert (state["phase"], state["next_change_utc"]) == (phase, next_change)
        cancelled = await request(
            "configure",
            settings={"world_mode": "active", "grace_start_utc": None, "grace_end_utc": None},
        )
        assert cancelled["ok"] and cancelled["data"]["world"]["phase"] == 2
        assert cancelled["data"]["settings"]["login_enabled"] is False
        assert cancelled["data"]["settings"]["operator_notice"] == "Keep this notice"
        assert protected.read_bytes() == b"unchanged player data"
    finally:
        await endpoint.close()
