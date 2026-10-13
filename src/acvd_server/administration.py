"""Same-user Unix-socket administration; no network listener or player-data editor."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import socket
import stat
import struct
import tempfile
import time
from pathlib import Path
from typing import Callable

from . import verdict_war
from .cq_map_policy import CQ_AREA_MAPS, CQ_MAP_DEFAULTS, CQ_MAP_PROFILES, validate_cq_map_settings
from .notice_messages import MAX_BODY_BYTES, format_login_welcome
from .player_reset import player_directory
from .session_lifecycle import (
    TEAM_PEER_RECOVERY_BACKOFF_SECONDS,
    TEAM_PEER_RECOVERY_COOLDOWN_SECONDS,
    TEAM_PEER_RECOVERY_MAX_ATTEMPTS,
)
from .team_medals import fame_points as team_fame_points, owned_medals
from .team_progression import SPECIAL_SORTIE_SEGMENTS, view as team_progress_view
from .world_economy import WORLD_REWARD_DEFAULTS, validate_world_reward_settings
from .world_policy import WORLD_DEFAULTS, validate_world_settings, world_state


class OperatorSettings:
    DEFAULTS = {
        "maintenance": False,
        "login_enabled": True,
        "operator_notice": "",
        "welcome_message": "",
        **CQ_MAP_DEFAULTS,
        **WORLD_DEFAULTS,
        **WORLD_REWARD_DEFAULTS,
    }

    def __init__(self, path: Path):
        self.path = path
        self.values = dict(self.DEFAULTS)
        if path.exists():
            if path.is_symlink() or path.stat().st_size > 8192:
                raise ValueError("Unsafe operator settings file")
            self.values = self.validate(json.loads(path.read_text()))

    @classmethod
    def validate(cls, values: dict) -> dict:
        if not isinstance(values, dict) or set(values) - cls.DEFAULTS.keys():
            raise ValueError("Unknown operator setting")
        result = {**cls.DEFAULTS, **values}
        if any(type(result[k]) is not bool for k in ("maintenance", "login_enabled")):
            raise ValueError("Admission settings must be booleans")
        notice = result["operator_notice"]
        if not isinstance(notice, str) or len(notice) > 512 or "\0" in notice:
            raise ValueError("Operator notice must be at most 512 characters")
        welcome = result["welcome_message"]
        if not isinstance(welcome, str) or "\0" in welcome or len(welcome.encode("utf-8")) > 2048:
            raise ValueError("Login welcome must be at most 2048 UTF-8 bytes")
        if len(format_login_welcome(welcome).encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError("Wrapped login welcome must be at most 2048 UTF-8 bytes")
        validate_world_settings(result)
        validate_world_reward_settings(result)
        result.update(validate_cq_map_settings(result))
        if len(json.dumps(result).encode("utf-8")) > 8192:
            raise ValueError("Operator settings exceed the persisted file-size limit")
        return result

    def update(self, changes: dict) -> None:
        values = self.validate({**self.values, **changes})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("Refusing symlink settings")
        if self.path.exists():
            backup = self.path.with_name(f"operator-settings.{time.time_ns()}.bak")
            with backup.open("xb") as out:
                os.chmod(backup, 0o600)
                out.write(self.path.read_bytes())
        fd, temporary = tempfile.mkstemp(prefix=".operator-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(values, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.values = values

    @property
    def accepts_login(self) -> bool:
        return self.values["login_enabled"] and not self.values["maintenance"]


class AdminEndpoint:
    def __init__(
        self,
        path: Path,
        snapshot: Callable[[], dict],
        settings: OperatorSettings,
        telemetry,
        shutdown: Callable[[], None] | None = None,
        delete_player: Callable[[str, int], dict] | None = None,
        validate_settings_change: Callable[[dict], None] | None = None,
        start_test_war: Callable[[dict, list[int]], dict] | None = None,
    ):
        self.path, self.snapshot, self.settings, self.telemetry = (
            path,
            snapshot,
            settings,
            telemetry,
        )
        self.server = None
        self.lock = None
        self.inode = None
        self.shutdown = shutdown
        self.delete_player = delete_player
        self.validate_settings_change = validate_settings_change
        self.start_test_war = start_test_war

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".lock")
        self.lock = lock_path.open("a+")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Refusing to replace a non-owned admin socket")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            try:
                probe.connect(str(self.path))
            except ConnectionRefusedError:
                pass
            else:
                raise ValueError("An administrative listener is already active")
            finally:
                probe.close()
            self.path.unlink()
        self.server = await asyncio.start_unix_server(self.handle, path=self.path, limit=8192)
        os.chmod(self.path, 0o600)
        self.inode = self.path.stat().st_ino

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.inode is not None and self.path.exists() and self.path.stat().st_ino == self.inode:
            self.path.unlink()
        if self.lock:
            self.lock.close()

    async def handle(self, reader, writer) -> None:
        try:
            peer = writer.get_extra_info("socket")
            _, uid, _ = struct.unpack(
                "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if uid != os.getuid():
                raise PermissionError("Only the server owner may administer this socket")
            request = json.loads(await asyncio.wait_for(reader.readline(), 3))
            if not isinstance(request, dict):
                raise ValueError("Expected an object")
            command = request.get("command")
            if command == "status":
                result = self.snapshot()
            elif command == "start_test_war" and self.start_test_war is not None:
                if request.get("confirmed") is not True:
                    raise ValueError("Explicit test-war start confirmation required")
                result = self.start_test_war(
                    request.get("rules", {}), request.get("owners", [1, 1, 2, 2, 3, 3, 3])
                )
            elif command == "configure":
                changes = request.get("settings", {})
                if not isinstance(changes, dict):
                    raise ValueError("Expected settings object")
                if self.validate_settings_change is not None:
                    self.validate_settings_change(changes)
                elif {"cq_map_mode", "cq_map_pool"} & changes.keys():
                    live = self.snapshot()
                    if live.get("players") or live.get("matches"):
                        raise ValueError(
                            "CQ map policy can change only while every player is logged out "
                            "and no match is active"
                        )
                self.settings.update(changes)
                self.telemetry.emit("operator_settings_changed", settings=self.settings.values)
                result = self.snapshot()
            elif command == "debug":
                enabled = request.get("enabled")
                if type(enabled) is not bool:
                    raise ValueError("Expected boolean enabled")
                self.telemetry.set_diagnostics(enabled)
                result = {"enabled": enabled}
            elif command == "events":
                after = request.get("after", 0)
                if type(after) is not int or after < 0:
                    raise ValueError("Invalid event cursor")
                result = self.telemetry.read_diagnostics(after)
            elif command == "shutdown" and self.shutdown is not None:
                if request.get("confirmed") is not True:
                    raise ValueError("Explicit shutdown confirmation required")
                asyncio.get_running_loop().call_later(0.2, self.shutdown)
                result = {"shutdown_requested": True}
            elif command == "delete_player" and self.delete_player is not None:
                online_id = request.get("online_id")
                player_id = request.get("player_id")
                if (
                    request.get("confirmed") is not True
                    or not isinstance(online_id, str)
                    or request.get("confirmation") != online_id
                    or type(player_id) is not int
                ):
                    raise ValueError(
                        "Permanent player reset requires exact identity confirmation"
                    )
                result = self.delete_player(online_id, player_id)
            else:
                raise ValueError("Unknown command")
            response = {"ok": True, "data": result}
        except (ValueError, TypeError, KeyError, OSError, asyncio.TimeoutError) as error:
            response = {"ok": False, "error": str(error)}
        try:
            writer.write(json.dumps(response).encode() + b"\n")
            await asyncio.wait_for(writer.drain(), 3)
        finally:
            writer.close()
            await writer.wait_closed()


def server_snapshot(server, started: float) -> dict:
    """Read canonical live objects; never call storage methods with write defaults."""
    from .world_messages import CURRENT_SEASON_ID

    now = time.monotonic()
    wall_now = time.time()
    war = server.teams.test_war_snapshot()
    cq_ids = (
        [CQ_AREA_MAPS[a["area_id"]][verdict_war.level(a, war["rules"])][0] for a in war["areas"]]
        if war
        else server.operator_settings.values["cq_map_pool"]
    )
    players = []
    for session in list(server.game_sessions.values()):
        if session.peer is None or not session.established:
            continue
        age = max(0, now - session.last_seen_monotonic)
        if age > 90:
            continue
        players.append(
            {
                "online_id": session.online_id,
                "player_id": session.player_id,
                "peer": session.peer,
                "last_seen_seconds": round(age, 1),
            }
        )
    state = server.teams.state
    teams = []
    for team in state.get("teams", {}).values():
        progress = team_progress_view(team)
        teams.append(
            {
                k: team.get(k)
                for k in (
                    "team_id",
                    "name",
                    "leader_online_id",
                    "faction_id",
                    "current_faction_id",
                    "headquarters_id",
                    "member_records",
                    "territory_layouts",
                    "native_territory_layouts",
                    "territory_area_id",
                )
            }
            | {
                "team_rank": progress.rank_label,
                "team_rank_score": progress.score,
                "team_fame_points": team_fame_points(team),
                "team_medal_count": len(owned_medals(team)),
                "team_ac_kills": team.get("team_ac_kills", 0),
                "team_player_medals": team.get("team_player_medals", 0),
                "team_world_sorties": team.get("team_world_sorties", 0),
                "team_pvp_wins": team.get("team_pvp_wins", 0),
                "team_best_ranking": team.get("team_best_ranking", 0),
                "special_sortie_progress": progress.special_sortie_progress,
                "special_sortie_segments": SPECIAL_SORTIE_SEGMENTS,
            }
        )
    protocol = server.game_protocol
    resources = {"disk_free_bytes": shutil.disk_usage(server.config.project_root).free}
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(("VmRSS:", "Threads:")):
                key, value = line.split(":", 1)
                resources[key] = value.strip()
    except OSError:
        pass
    return {
        "api_version": 1,
        "pid": os.getpid(),
        "root": str(server.config.project_root),
        "started_monotonic": started,
        "uptime_seconds": int(now - started),
        "timestamp_unix": wall_now,
        "settings": server.operator_settings.values,
        "debug_enabled": server.telemetry.diagnostics.enabled,
        "audit": server.telemetry.audit_status(),
        "players": players,
        "player_directory": player_directory(server.teams, server.config.player_data_path),
        "teams": teams,
        "settlements": state.get("battle_settlements", [])[-100:],
        "world_settlements": state.get("world_battle_settlements", [])[-100:],
        "world": {
            "season": CURRENT_SEASON_ID,
            **world_state(server.operator_settings.values, wall_now),
            **(
                {
                    "season": war["season"],
                    "phase": war["phase"],
                    "mode": "test_war",
                    "label": "TEST WAR" if war["phase"] == 2 else "TEST WAR grace period",
                    "source": war["policy"],
                    "next_change_utc": war["ends_at"],
                }
                if war
                else {}
            ),
        },
        "test_war": war,
        "cq_maps": {
            "mode": "test_war" if war else server.operator_settings.values["cq_map_mode"],
            "pool": [
                {
                    **CQ_MAP_PROFILES[mission_id],
                    "hardware_profile": (
                        "lower_load"
                        if CQ_MAP_PROFILES[mission_id]["lower_load"]
                        else "unclassified"
                    ),
                }
                for mission_id in cq_ids
            ],
        },
        "connection_policy": {
            "room_handoff": "native_authenticated_archive",
            "legacy_room_log_bridge_active": bool(protocol and protocol.match_room_log_paths),
            "team_peer_recovery": {
                "scope": "reporter_only",
                "cooldown_seconds": TEAM_PEER_RECOVERY_COOLDOWN_SECONDS,
                "max_attempts": TEAM_PEER_RECOVERY_MAX_ATTEMPTS,
                "backoff_seconds": TEAM_PEER_RECOVERY_BACKOFF_SECONDS,
                "whole_team_escalation": False,
            },
        },
        "sessions": len(protocol.team_sessions) if protocol else 0,
        "matches": sum(not match.get("result_sha256") for match in protocol.matches.values())
        if protocol
        else 0,
        "retained_match_contexts": len(protocol.matches) if protocol else 0,
        "ports": {
            "login": server.config.login_ports,
            "auth": server.config.auth_port,
            "udp": server.config.game_port,
            "cdn": server.config.cdn_port,
        },
        "advertised_host": server.config.advertised_host,
        "resources": resources,
        "capabilities": {
            "world_mode_controls": True,
            "world_reward_controls": True,
            "cq_map_pool_controls": True,
            "maintenance": "Admission drain; existing players are not kicked",
            "login_welcome": True,
            "player_reset": True,
            "welcome": "Configured login notice; once per authenticated login",
            "rankings": (
                "Season-scoped durable team, medal, attributed AC-destruction, "
                "and hired-mercenary standings"
            ),
        },
    }
