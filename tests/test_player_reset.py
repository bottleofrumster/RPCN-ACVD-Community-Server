"""Player rebuild boundaries on disposable stores only."""

import asyncio
import json
from copy import deepcopy

import pytest

from acvd_server.administration import AdminEndpoint, OperatorSettings
from acvd_server.player_reset import player_directory, reset_player
from acvd_server.teams import TeamStore
from acvd_server.telemetry import Telemetry


def fixture(tmp_path):
    teams = TeamStore(tmp_path / "teams")
    players = tmp_path / "players"
    players.mkdir()
    for name, player_id in (("Target", 42), ("Other", 84)):
        teams.remember_player(name, player_id)
        teams.synchronize_balance(name, 100_000 + player_id)
    state = deepcopy(teams.state)
    state["members"] = {"Target": 1, "Other": 1}
    state["teams"] = {
        "1": {
            "team_id": 1,
            "name": "Keep Team",
            "leader_online_id": "Target",
            "leader_player_id": 42,
            "member_records": [
                {"online_id": "Target", "player_id": 42, "member_id": 1, "leader": True},
                {"online_id": "Other", "player_id": 84, "member_id": 2, "leader": False},
            ],
        }
    }
    state["player_profiles"]["Target"].update(
        member_id=1,
        member_status=1,
        member_team_secondary=0,
        pilot_name="Old",
        rank=6,
    )
    state["player_profiles"]["Other"].update(member_id=2, pilot_name="Keep")
    state["story_rewards"] = {
        "Target:a": {"online_id": "Target", "player_id": 42},
        "Other:b": {"online_id": "Other", "player_id": 84},
    }
    state["shop_purchases"] = {
        "42:1": {"online_id": "Target", "player_id": 42},
        "84:1": {"online_id": "Other", "player_id": 84},
    }
    state["part_sales"] = {"42:2": {"online_id": "Target", "player_id": 42}}
    state["inventory_registrations"] = {
        "x": {"online_id": "Target", "player_id": 42, "items": []}
    }
    state["pending_emblems"] = {
        "Target": {"ugc_id": 200001, "emblem_file": "ugc-200001-emblem.jpg"}
    }
    state["player_emblems"] = {
        "Target": {"ugc_id": 200001, "emblem_file": "ugc-200001-emblem.jpg"}
    }
    state["mercenaries"] = {"Target": {"online_id": "Target", "player_id": 42}}
    state["story_group_rewards"] = {
        "group": {"participants": [{"online_id": "Target", "player_id": 42}]}
    }
    state["battle_settlements"] = [
        {"online_id": "Target", "player_id": 42, "result_digest": "a"},
        {"online_id": "Other", "player_id": 84, "result_digest": "b"},
    ]
    state["world_battle_settlements"] = [
        {
            "result_digest": "c",
            "participants": [{"online_id": "Target", "player_id": 42}],
        }
    ]
    state["money_transfers"] = [
        {
            "sender_online_id": "Target",
            "recipient_online_id": "Other",
            "recipient_player_id": 84,
        }
    ]
    teams.commit_state(state)

    files = {
        "42.ac-data.bin": b"ac",
        "42.mission-savedata.bin": b"mission",
        "42.ai-1.bin": b"unac-1",
        "42.ai-9.bin": b"unac-9",
        "42.ai-extend.bin": b"extension",
        "42.offline-data.bin": b"inventory",
        "42.personal-notices.json": b"[]",
        "84.offline-data.bin": b"other",
    }
    for name, payload in files.items():
        (players / name).write_bytes(payload)
    for name, payload in (
        ("42.ac-data.bin/old.bin", b"old-ac"),
        ("42.offline-data.bin/old.bin", b"old-inventory"),
    ):
        path = players / ".history" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    (teams.root / "ugc-200001-emblem.jpg").write_bytes(b"emblem")
    return teams, players


def test_rebuild_removes_only_proven_client_republishable_mirrors(tmp_path):
    teams, players = fixture(tmp_path)
    state_before = deepcopy(teams.state)

    result = reset_player(teams, players, online_id="Target", player_id=42)

    assert result == {
        "online_id": "Target",
        "player_id": 42,
        "deleted_republishable_paths": 5,
        "team_membership_retained": True,
        "wallet_retained": True,
        "inventory_retained": True,
        "emblem_retained": True,
        "transaction_history_retained": True,
    }
    assert teams.state == state_before
    assert not (players / "42.ac-data.bin").exists()
    assert not (players / "42.mission-savedata.bin").exists()
    assert not (players / "42.ai-1.bin").exists()
    assert not (players / "42.ai-9.bin").exists()
    assert not (players / ".history/42.ac-data.bin").exists()
    assert (players / "42.offline-data.bin").read_bytes() == b"inventory"
    assert (players / "42.ai-extend.bin").read_bytes() == b"extension"
    assert (players / "42.personal-notices.json").read_bytes() == b"[]"
    assert (players / ".history/42.offline-data.bin/old.bin").read_bytes() == b"old-inventory"
    assert (players / "84.offline-data.bin").read_bytes() == b"other"
    assert (teams.root / "ugc-200001-emblem.jpg").read_bytes() == b"emblem"
    target = next(row for row in player_directory(teams, players) if row["online_id"] == "Target")
    assert target["has_saved_data"] and target["team_name"] == "Keep Team"
    assert not target["has_republishable_data"]


def test_player_directory_returns_bounded_server_data_summary(tmp_path):
    teams, players = fixture(tmp_path)
    target = next(row for row in player_directory(teams, players) if row["online_id"] == "Target")
    assert target == {
        "online_id": "Target",
        "player_id": 42,
        "team_id": 1,
        "team_name": "Keep Team",
        "has_saved_data": True,
        "has_republishable_data": True,
        "wallet_balance": 100_042,
        "member_id": 1,
        "member_status": 1,
        "member_team_secondary": 0,
        "ac_data": True,
        "story_data": True,
        "unac_count": 2,
        "inventory_data": True,
        "notice_data": True,
        "emblem_data": True,
        "pending_emblem": True,
        "mercenary_registered": True,
        "server_file_count": 7,
        "server_bytes": 41,
        "settlement_count": 2,
        "purchase_count": 4,
        "transfer_count": 1,
        "data_categories": [
            "Rebuildable save",
            "AC data",
            "Story progress",
            "UNAC data",
            "Inventory",
            "Emblem",
            "Notices",
            "Wallet",
            "History",
            "Mercenary",
        ],
    }


def test_stale_identity_and_empty_repeat_are_rejected(tmp_path):
    teams, players = fixture(tmp_path)
    with pytest.raises(ValueError, match="identity changed"):
        reset_player(teams, players, online_id="Target", player_id=999)
    reset_player(teams, players, online_id="Target", player_id=42)
    with pytest.raises(ValueError, match="no client-republishable"):
        reset_player(teams, players, online_id="Target", player_id=42)


def test_failed_state_commit_restores_every_player_file(tmp_path, monkeypatch):
    teams, players = fixture(tmp_path)
    before = deepcopy(teams.state)
    paths = {
        path.relative_to(players): path.read_bytes()
        for path in players.rglob("*")
        if path.is_file()
    }

    def fail(_state):
        raise OSError("injected rebuild failure")

    monkeypatch.setattr(teams, "commit_state", fail)
    with pytest.raises(OSError, match="injected"):
        reset_player(teams, players, online_id="Target", player_id=42)
    assert teams.state == before
    assert {
        path.relative_to(players): path.read_bytes()
        for path in players.rglob("*")
        if path.is_file()
    } == paths


@pytest.mark.asyncio
async def test_confirmed_admin_control_rebuilds_disposable_save_end_to_end(tmp_path):
    teams, players = fixture(tmp_path)
    endpoint = AdminEndpoint(
        tmp_path / "operator.sock",
        lambda: {"player_directory": player_directory(teams, players)},
        OperatorSettings(tmp_path / "settings.json"),
        Telemetry(tmp_path / "events.jsonl"),
        delete_player=lambda online_id, player_id: reset_player(
            teams, players, online_id=online_id, player_id=player_id
        ),
    )
    await endpoint.start()
    try:
        reader, writer = await asyncio.open_unix_connection(endpoint.path)
        writer.write(
            json.dumps(
                {
                    "command": "delete_player",
                    "online_id": "Target",
                    "player_id": 42,
                    "confirmed": True,
                    "confirmation": "Target",
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
    finally:
        await endpoint.close()
    assert response["ok"]
    assert response["data"]["deleted_republishable_paths"] == 5
    assert (players / "42.offline-data.bin").read_bytes() == b"inventory"
    assert not (players / "42.mission-savedata.bin").exists()
