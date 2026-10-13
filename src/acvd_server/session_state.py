"""Private restart snapshots for reliable UDP and team membership.

Credentials require owner-only permissions. UDP continuity is not seamless
restart support: TCP connections do not survive process replacement. Active
matchmaking and in-progress migration guards are not resurrected here.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .udp import DAT, DAT_ACK, SYN_ACK, GameSession, ReliablePacket

SESSION_STATE_MAX_AGE_SECONDS = 600.0


def _persist_game_sessions(
    path: Path,
    sessions: dict[bytes, GameSession],
    team_sessions: dict[int, dict] | None = None,
) -> None:
    """Atomically retain the minimum state needed to resume encrypted UDP.

    Tokens and negotiated CWC keys are credentials, so the state directory and
    file are owner-only.  Transient fragment buffers and migration-preseed
    guards are deliberately not serialized.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    records = []
    for token, session in sorted(sessions.items(), key=lambda item: item[0]):
        peer = None
        if session.peer is not None:
            peer = [str(session.peer[0]), int(session.peer[1])]
        records.append(
            {
                "auth_token_hex": bytes(token).hex(),
                "game_key_hex": bytes(session.game_key).hex(),
                "online_id": str(session.online_id),
                "player_id": int(session.player_id),
                "server_sequence": int(session.server_sequence),
                "remote_sequence": int(session.remote_sequence),
                "receive_sequence_initialized": session.receive_sequence_initialized,
                "fragment_counter": int(session.fragment_counter),
                "established": bool(session.established),
                "team_session_ready": bool(session.team_session_ready),
                "peer": peer,
                "last_seen_unix": float(session.last_seen_unix),
                "outbound_reliable": [
                    {
                        "local_sequence": int(packet.local_sequence),
                        "remote_sequence": int(packet.remote_sequence),
                        "opcode": int(packet.opcode),
                        "unknown": int(packet.unknown),
                        "payload_hex": bytes(packet.payload).hex(),
                        "initial_prefix_hex": bytes(packet.initial_prefix).hex(),
                    }
                    for packet in session.outbound_reliable.values()
                ],
            }
        )
    team_session_records = []
    for session_id, team_session in sorted((team_sessions or {}).items()):
        members = []
        for online_id, member in sorted(team_session.get("members", {}).items()):
            members.append(
                {
                    "online_id": str(online_id),
                    "player_id": int(member.get("player_id", 0)),
                    "member_id": int(member.get("member_id", 0)),
                    "np_identity_hex": bytes(member.get("np_identity", b"")).hex(),
                }
            )
        team_session_records.append(
            {
                "session_id": int(session_id),
                "team_id": int(team_session.get("team_id", session_id)),
                "members": members,
            }
        )
    payload = json.dumps(
        {
            "schema_version": 1,
            "sessions": records,
            "team_sessions": team_session_records,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _load_game_sessions(
    path: Path,
    *,
    now_unix: float | None = None,
    max_age_seconds: float = SESSION_STATE_MAX_AGE_SECONDS,
) -> dict[bytes, GameSession]:
    """Load fresh, structurally valid session credentials; ignore bad rows."""
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        return {}
    rows = document.get("sessions")
    if not isinstance(rows, list):
        return {}
    now_unix = time.time() if now_unix is None else float(now_unix)
    restored: dict[bytes, GameSession] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            token = bytes.fromhex(str(row["auth_token_hex"]))
            game_key = bytes.fromhex(str(row["game_key_hex"]))
            online_id = str(row["online_id"])
            player_id = int(row["player_id"])
            server_sequence = int(row["server_sequence"])
            remote_sequence = int(row["remote_sequence"])
            fragment_counter = int(row["fragment_counter"])
            last_seen_unix = float(row["last_seen_unix"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        age = max(0.0, now_unix - last_seen_unix)
        if (
            len(token) != 8
            or len(game_key) != 16
            or not online_id
            or player_id <= 0
            or not 0 <= server_sequence < 4096
            or not 0 <= remote_sequence < 4096
            or not 0 <= fragment_counter <= 0xFFFF
            or age > float(max_age_seconds)
            or token in restored
        ):
            continue
        older_tokens = [
            existing_token
            for existing_token, existing in restored.items()
            if existing.online_id == online_id and existing.last_seen_unix < last_seen_unix
        ]
        if any(
            existing.online_id == online_id and existing.last_seen_unix >= last_seen_unix
            for existing in restored.values()
        ):
            continue
        for older_token in older_tokens:
            restored.pop(older_token, None)
        peer_value = row.get("peer")
        peer = None
        if isinstance(peer_value, list) and len(peer_value) == 2 and isinstance(peer_value[0], str):
            try:
                peer_port = int(peer_value[1])
                if 0 < peer_port <= 0xFFFF:
                    peer = (peer_value[0], peer_port)
            except (TypeError, ValueError):
                pass
        outbound_reliable: dict[int, ReliablePacket] = {}
        history_rows = row.get("outbound_reliable", [])
        if isinstance(history_rows, list) and len(history_rows) <= 512:
            for history_row in history_rows:
                if not isinstance(history_row, dict):
                    outbound_reliable = {}
                    break
                try:
                    local = int(history_row["local_sequence"])
                    remote = int(history_row["remote_sequence"])
                    opcode = int(history_row["opcode"])
                    unknown = int(history_row.get("unknown", 0xFF))
                    payload_hex = str(history_row.get("payload_hex", ""))
                    initial_prefix_hex = str(history_row.get("initial_prefix_hex", ""))
                    if len(payload_hex) > 8192 or len(initial_prefix_hex) > 70:
                        raise ValueError("persisted reliable packet is too large")
                    payload = bytes.fromhex(payload_hex)
                    initial_prefix = bytes.fromhex(initial_prefix_hex)
                except (KeyError, TypeError, ValueError, OverflowError):
                    outbound_reliable = {}
                    break
                if (
                    not 0 <= local < 4096
                    or not 0 <= remote < 4096
                    or opcode not in (SYN_ACK, DAT, DAT_ACK)
                    or not 0 <= unknown <= 0xFF
                    or len(initial_prefix) not in (0, 35)
                    or local in outbound_reliable
                ):
                    outbound_reliable = {}
                    break
                outbound_reliable[local] = ReliablePacket(
                    local,
                    remote,
                    opcode,
                    unknown,
                    payload,
                    initial_prefix,
                )
        restored[token] = GameSession(
            token,
            game_key,
            online_id,
            player_id,
            server_sequence=server_sequence,
            remote_sequence=remote_sequence,
            receive_sequence_initialized=row.get("receive_sequence_initialized") is True,
            established=bool(row.get("established", False)),
            team_session_ready=bool(row.get("team_session_ready", False)),
            peer=peer,
            fragment_counter=fragment_counter,
            last_seen_monotonic=time.monotonic() - age,
            last_seen_unix=last_seen_unix,
            outbound_reliable=outbound_reliable,
        )
    return restored


def _load_team_sessions(
    path: Path,
    sessions: dict[bytes, GameSession],
) -> dict[int, dict]:
    """Restore only team-session members backed by a fresh game session."""
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    rows = document.get("team_sessions") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or not isinstance(rows, list)
    ):
        return {}
    live_by_online_id = {
        session.online_id: session for session in sessions.values() if session.established
    }
    # Older snapshots predate the readiness flag. Their validated retained
    # membership is the migration evidence; an explicit False in a new
    # snapshot must remain False for a freshly authenticated client.
    snapshot_sessions = document.get("sessions", [])
    if not isinstance(snapshot_sessions, list):
        snapshot_sessions = []
    legacy_ready_ids = {
        str(row.get("online_id", ""))
        for row in snapshot_sessions
        if isinstance(row, dict) and "team_session_ready" not in row
    }
    restored: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            session_id = int(row["session_id"])
            team_id = int(row["team_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if session_id <= 0 or team_id <= 0 or session_id in restored:
            continue
        members = {}
        member_rows = row.get("members")
        if not isinstance(member_rows, list):
            continue
        for member in member_rows:
            if not isinstance(member, dict):
                continue
            try:
                online_id = str(member["online_id"])
                player_id = int(member["player_id"])
                member_id = int(member["member_id"])
                np_identity = bytes.fromhex(str(member["np_identity_hex"]))
            except (KeyError, TypeError, ValueError):
                continue
            live = live_by_online_id.get(online_id)
            if (
                live is None
                or live.player_id != player_id
                or player_id <= 0
                or member_id <= 0
                or not 0 < len(np_identity) <= 256
            ):
                continue
            members[online_id] = {
                "player_id": player_id,
                "member_id": member_id,
                "np_identity": np_identity,
            }
            if online_id in legacy_ready_ids:
                live.team_session_ready = True
        if members:
            restored[session_id] = {
                "session_id": session_id,
                "team_id": team_id,
                "members": members,
            }
    return restored
