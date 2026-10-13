"""Live team-session membership, presence refresh and availability coordination.

Owns the live directory and active-mercenary set. Authenticated sessions and
persistent teams are borrowed dynamically, never copied or reconstructed here.
Removing a live member does not remove durable team membership. Push encoding
and delivery remain transport responsibilities.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Mapping

from .team_messages import (
    SESSION_MEMBER_JOIN_PUSH,
    SESSION_MEMBER_LEAVE_PUSH,
    _team_session_leave_push,
    _team_session_member_push,
)
from .teams import TeamStore
from .telemetry import Telemetry
from .udp import GameSession, PushSender

TEAM_PEER_RECOVERY_COOLDOWN_SECONDS = 15.0
TEAM_PEER_RECOVERY_BACKOFF_SECONDS = 60.0
TEAM_PEER_RECOVERY_MAX_ATTEMPTS = 2
MAX_RECENT_TEAM_PEER_RECOVERIES = 4096
MAX_PENDING_TEAM_SESSION_PUSHES = 256


@dataclass(frozen=True)
class DeferredRosterUpdate:
    recipient: GameSession
    queued_at: float
    deliveries: tuple[tuple[bytes, int], ...]


def _remove_team_session_member_from_state(
    team_sessions: dict[int, dict],
    online_id: str,
    requested_session_id: int = 0,
) -> tuple[list[int], list[int]]:
    """Remove one identity from selected sessions and delete empty sessions."""
    removed_from: list[int] = []
    deleted: list[int] = []
    for session_id, team_session in list(team_sessions.items()):
        if requested_session_id > 0 and int(session_id) != int(requested_session_id):
            continue
        members = team_session.get("members", {})
        if not isinstance(members, dict) or members.pop(online_id, None) is None:
            continue
        removed_from.append(int(session_id))
        if not members:
            team_sessions.pop(session_id, None)
            deleted.append(int(session_id))
    return removed_from, deleted


class LiveSessionCoordinator:
    def __init__(
        self,
        *,
        get_sessions: Callable[[], Mapping[bytes, GameSession]],
        get_teams: Callable[[], TeamStore],
        send_push: PushSender,
        can_refresh_peer: Callable[[GameSession, GameSession], bool],
        telemetry: Telemetry,
    ) -> None:
        self._get_sessions = get_sessions
        self._get_teams = get_teams
        self._send_game_push = send_push
        self._can_refresh_peer = can_refresh_peer
        self.telemetry = telemetry
        self.active_mercenaries: set[str] = set()
        self.team_sessions: dict[int, dict] = {}
        # A report has no battle generation. Repair only the reporting client,
        # allow one delayed repeat after the observed 17-25 second recurrence,
        # then back off. Do not mutate a healthy reciprocal client or escalate
        # one generation-free report into a whole-team rebuild.
        self._peer_recovery_times: dict[tuple[bytes, int, bytes], float] = {}
        self._peer_recovery_attempts: dict[tuple[bytes, int, bytes], int] = {}
        # Existing teammates may already be composing or waiting inside an
        # RPCN room when somebody logs in or out. Mock has no central prepare
        # request, so mutating that client's peer roster immediately can eject
        # it from the room. Hold the latest change per session/player until
        # a World/session read, and never override known newer match activity.
        # These reads alone do not prove the native client has left Compose.
        self._pending_team_session_pushes: dict[
            bytes, dict[tuple[int, int], DeferredRosterUpdate]
        ] = {}

    @property
    def sessions(self) -> Mapping[bytes, GameSession]:
        return self._get_sessions()

    @property
    def teams(self) -> TeamStore:
        return self._get_teams()

    def recover_reported_peer(
        self,
        reporter: GameSession,
        session_id: int,
        player_id: int,
        *,
        can_recover: Callable[[GameSession, GameSession], bool],
    ) -> bool:
        """Rebuild only the reporter's retained real-team peer connection.

        A two-client native control completed two Special battles after wire9
        LeaveSessionMember followed by wire8 JoinSessionMember. The old wire7
        experiment changed the session host instead. Keep durable membership,
        the peer's own client state, and active battle contexts untouched.
        See evidence/2026-09-14-same-team-peer-recovery.json.
        """
        if (
            type(session_id) is not int
            or not 0 < session_id < 1 << 64
            or type(player_id) is not int
            or not 0 < player_id < 1 << 32
            or player_id == reporter.player_id
            or self.sessions.get(reporter.auth_token) is not reporter
        ):
            return False
        directory = self.team_sessions.get(session_id)
        if not isinstance(directory, dict) or directory.get("session_id") != session_id:
            return False
        members = directory.get("members", {})
        if not isinstance(members, dict):
            return False
        targets = [
            name
            for name, record in members.items()
            if isinstance(record, dict) and record.get("player_id") == player_id
        ]
        if len(targets) != 1 or targets[0] == reporter.online_id:
            return False
        now = time.monotonic()
        pair: list[GameSession] = []
        team_id = directory.get("team_id")
        if type(team_id) is not int or team_id <= 0:
            return False
        for name, expected_id in (
            (reporter.online_id, reporter.player_id),
            (targets[0], player_id),
        ):
            record = members.get(name)
            if not isinstance(record, dict) or record.get("player_id") != expected_id:
                return False
            ordinal = record.get("member_id")
            identity = record.get("np_identity", b"")
            if (
                type(ordinal) is not int
                or not 0 < ordinal < 1 << 32
                or not isinstance(identity, bytes)
                or len(identity) != 37
                or sum(
                    isinstance(r, dict) and r.get("member_id") == ordinal for r in members.values()
                )
                != 1
                or (self.teams.team_for(name) or {}).get("team_id") != team_id
                or self.teams.player_profile(name).get("member_id") != ordinal
            ):
                return False
            live = [
                s
                for s in self.sessions.values()
                if s.online_id == name
                and s.player_id == expected_id
                and self.sessions.get(s.auth_token) is s
                and s.established
                and s.peer is not None
                and s.team_session_ready
                and 0 <= now - s.last_seen_monotonic <= 90
            ]
            if len(live) != 1:
                return False
            pair.append(live[0])
        if pair[0] is not reporter or not can_recover(*pair):
            return False
        # Keep memory and retry traffic bounded. A later real battle can use
        # this path again; this is not a once-per-login workaround.
        self._peer_recovery_times = {
            key: stamp
            for key, stamp in self._peer_recovery_times.items()
            if 0 <= now - stamp < TEAM_PEER_RECOVERY_BACKOFF_SECONDS
        }
        self._peer_recovery_attempts = {
            key: attempts
            for key, attempts in self._peer_recovery_attempts.items()
            if key in self._peer_recovery_times
        }
        key = (reporter.auth_token, session_id, pair[1].auth_token)
        deferred = self._pending_team_session_pushes.get(reporter.auth_token, {}).get(
            (session_id, player_id)
        )
        if (
            deferred
            and deferred.recipient is reporter
            and [subtype for _payload, subtype in deferred.deliveries]
            == [
                SESSION_MEMBER_LEAVE_PUSH,
                SESSION_MEMBER_JOIN_PUSH,
            ]
        ):
            self.telemetry.emit(
                "game_team_peer_recovery_coalesced",
                online_id=reporter.online_id,
                session_id=session_id,
                reported_player_id=player_id,
                reason="A leave/join refresh is already queued for the next safe World read",
                delivery_state="deferred_until_world_read",
                peer_connection_verified=False,
            )
            return True
        previous = self._peer_recovery_times.get(key)
        attempts = self._peer_recovery_attempts.get(key, 0)
        if previous is not None and now - previous < TEAM_PEER_RECOVERY_COOLDOWN_SECONDS:
            return False
        if attempts >= TEAM_PEER_RECOVERY_MAX_ATTEMPTS:
            return False
        if previous is None and len(self._peer_recovery_times) >= MAX_RECENT_TEAM_PEER_RECOVERIES:
            return False
        record = members[targets[0]]
        leave = _team_session_leave_push(session_id, player_id)
        join = _team_session_member_push(session_id, player_id, record["np_identity"], targets[0])
        attempt = attempts + 1
        self._peer_recovery_times[key] = now
        self._peer_recovery_attempts[key] = attempt
        self._send_game_push(reporter, leave, subtype=SESSION_MEMBER_LEAVE_PUSH)
        self._send_game_push(reporter, join, subtype=SESSION_MEMBER_JOIN_PUSH)
        self.telemetry.emit(
            "game_team_peer_recovery_sent",
            online_id=reporter.online_id,
            session_id=session_id,
            reported_player_id=player_id,
            member_id=record["member_id"],
            attempt=attempt,
            previous_attempt_age_seconds=(
                round(now - previous, 3) if previous is not None else None
            ),
            peer_connection_verified=False,
        )
        return True

    def defer_team_session_push(
        self,
        recipient: GameSession,
        payload: bytes,
        *,
        subtype: int,
        session_id: int,
        player_id: int,
    ) -> bool:
        """Coalesce one roster mutation until the recipient returns to World."""
        if (
            self.sessions.get(recipient.auth_token) is not recipient
            or not recipient.established
            or recipient.peer is None
            or subtype not in (SESSION_MEMBER_JOIN_PUSH, SESSION_MEMBER_LEAVE_PUSH)
            or type(session_id) is not int
            or not 0 < session_id < 1 << 64
            or type(player_id) is not int
            or not 0 < player_id < 1 << 32
        ):
            return False
        pending = self._pending_team_session_pushes.setdefault(recipient.auth_token, {})
        key = (session_id, player_id)
        if key not in pending and len(pending) >= MAX_PENDING_TEAM_SESSION_PUSHES:
            self.telemetry.emit(
                "game_team_session_push_deferred_rejected",
                online_id=recipient.online_id,
                reason="recipient_queue_full",
                pending_count=len(pending),
            )
            return False
        self._queue_roster_update(pending, key, recipient, ((bytes(payload), subtype),))
        self.telemetry.emit(
            "game_team_session_push_deferred",
            online_id=recipient.online_id,
            session_id=session_id,
            subject_player_id=player_id,
            subtype=subtype,
            pending_count=len(pending),
        )
        return True

    def defer_post_battle_team_peer_refreshes(self, recipient: GameSession) -> int:
        """Queue proven leave/join recovery pairs for the next safe World read."""
        if (
            self.sessions.get(recipient.auth_token) is not recipient
            or not recipient.established
            or recipient.peer is None
            or not recipient.team_session_ready
        ):
            return 0
        team = self.teams.team_for(recipient.online_id) or {}
        team_id = int(team.get("team_id", 0))
        directory = self.team_sessions.get(team_id)
        members = directory.get("members", {}) if isinstance(directory, dict) else {}
        own = members.get(recipient.online_id) if isinstance(members, dict) else None
        if not isinstance(own, dict) or own.get("player_id") != recipient.player_id:
            return 0
        now = time.monotonic()
        pending = self._pending_team_session_pushes.setdefault(recipient.auth_token, {})
        queued = 0
        for online_id, record in members.items():
            if online_id == recipient.online_id or not isinstance(record, dict):
                continue
            player_id = record.get("player_id")
            identity = record.get("np_identity")
            live = [
                session
                for session in self.sessions.values()
                if session.online_id == online_id
                and session.player_id == player_id
                and self.sessions.get(session.auth_token) is session
                and session.established
                and session.peer is not None
                and session.team_session_ready
                and 0 <= now - session.last_seen_monotonic <= 90
            ]
            if (
                type(player_id) is not int
                or not 0 < player_id < 1 << 32
                or not isinstance(identity, bytes)
                or len(identity) != 37
                or len(live) != 1
            ):
                continue
            key = (team_id, player_id)
            if key not in pending and len(pending) >= MAX_PENDING_TEAM_SESSION_PUSHES:
                continue
            deliveries = (
                (_team_session_leave_push(team_id, player_id), SESSION_MEMBER_LEAVE_PUSH),
                (
                    _team_session_member_push(team_id, player_id, identity, online_id),
                    SESSION_MEMBER_JOIN_PUSH,
                ),
            )
            self._queue_roster_update(pending, key, recipient, deliveries)
            queued += 1
        if queued:
            self.telemetry.emit(
                "game_post_battle_team_peer_refreshes_deferred",
                online_id=recipient.online_id,
                team_id=team_id,
                peer_count=queued,
            )
        elif not pending:
            self._pending_team_session_pushes.pop(recipient.auth_token, None)
        return queued

    def defer_post_battle_team_refreshes(self, team_id: int) -> list[str]:
        """Queue reciprocal peer refreshes for every live durable team member."""
        recipients = []
        now = time.monotonic()
        for candidate in self.sessions.values():
            if (
                self.sessions.get(candidate.auth_token) is not candidate
                or not candidate.established
                or candidate.peer is None
                or not candidate.team_session_ready
                or now - candidate.last_seen_monotonic > 90
                or int((self.teams.team_for(candidate.online_id) or {}).get("team_id", 0))
                != int(team_id)
            ):
                continue
            if self.defer_post_battle_team_peer_refreshes(candidate):
                recipients.append(candidate.online_id)
        if recipients:
            self.telemetry.emit(
                "game_post_battle_team_refreshes_deferred",
                team_id=int(team_id),
                recipient_online_ids=recipients,
            )
        return recipients

    @staticmethod
    def _queue_roster_update(pending, key, recipient, deliveries):
        previous = pending.get(key)
        queued_at = (
            previous.queued_at if previous and previous.recipient is recipient else time.monotonic()
        )
        pending[key] = DeferredRosterUpdate(recipient, queued_at, deliveries)

    def _current_roster_deliveries(self, recipient, key, update, now):
        """Revalidate at delivery; queued bytes are not current membership truth."""
        if update.recipient is not recipient:
            return (), "recipient_replaced"
        session_id, player_id = key
        directory = self.team_sessions.get(session_id)
        members = directory.get("members", {}) if isinstance(directory, dict) else {}
        own = members.get(recipient.online_id) if isinstance(members, dict) else None
        if (
            not isinstance(own, dict)
            or own.get("player_id") != recipient.player_id
            or (self.teams.team_for(recipient.online_id) or {}).get("team_id") != session_id
        ):
            return (), "recipient_left_team_session"
        targets = [
            (name, row)
            for name, row in members.items()
            if isinstance(row, dict) and row.get("player_id") == player_id
        ]
        leaves = tuple(d for d in update.deliveries if d[1] == SESSION_MEMBER_LEAVE_PUSH)
        if not targets:
            return leaves, "peer_departed"
        if len(targets) != 1 or targets[0][0] == recipient.online_id:
            return (), "ambiguous_peer_identity"
        name, row = targets[0]
        if (self.teams.team_for(name) or {}).get("team_id") != session_id:
            return leaves, "peer_left_team"
        live = [
            s
            for s in self.sessions.values()
            if s.online_id == name
            and s.player_id == player_id
            and self.sessions.get(s.auth_token) is s
            and s.established
            and s.peer is not None
            and s.team_session_ready
            and 0 <= now - s.last_seen_monotonic <= 90
        ]
        if not live:
            return leaves, "peer_not_ready"
        if len(live) != 1:
            return (), "ambiguous_peer_transport"
        if not self._can_refresh_peer(recipient, live[0]):
            return None, "peer_in_match_activity"
        if not any(kind == SESSION_MEMBER_JOIN_PUSH for _, kind in update.deliveries):
            return (), "leave_superseded_by_live_peer"
        identity = row.get("np_identity")
        if not isinstance(identity, bytes) or len(identity) != 37:
            return (), "invalid_peer_identity"
        join = (
            _team_session_member_push(session_id, player_id, identity, name),
            SESSION_MEMBER_JOIN_PUSH,
        )
        return (*leaves, join), "current_roster"

    def flush_deferred_team_session_pushes(self, recipient: GameSession, *, trigger: str) -> int:
        """Use existing read triggers without resetting either active participant.

        Blocked entries retain their original age and are revalidated on the
        next read. This protects known central activity, not unobserved native
        Compose/Mock room state.
        """
        if (
            self.sessions.get(recipient.auth_token) is not recipient
            or not recipient.established
            or recipient.peer is None
            or not recipient.team_session_ready
            or not self._can_refresh_peer(recipient, recipient)
        ):
            return 0
        pending = self._pending_team_session_pushes.get(recipient.auth_token)
        if not pending:
            return 0
        now = time.monotonic()
        deliveries, delivered_pairs, decisions = [], [], []
        queued_count = len(pending)
        for key, update in list(pending.items()):
            current, reason = self._current_roster_deliveries(recipient, key, update, now)
            decisions.append(
                {
                    "session_id": key[0],
                    "player_id": key[1],
                    "reason": reason,
                    "queue_age_seconds": round(max(0, now - update.queued_at), 3),
                    "sent_subtypes": [kind for _, kind in current or ()],
                }
            )
            if current is None:
                continue
            pending.pop(key)
            if current:
                delivered_pairs.append(key)
                deliveries.extend(current)
        if not pending:
            self._pending_team_session_pushes.pop(recipient.auth_token, None)
        for payload, subtype in deliveries:
            self._send_game_push(recipient, payload, subtype=subtype)
        self.telemetry.emit(
            "game_team_session_deferred_pushes_flushed",
            online_id=recipient.online_id,
            trigger=trigger,
            count=len(delivered_pairs),
            queued_count=queued_count,
            retained_count=len(pending),
            delivery_decisions=decisions,
            session_peer_pairs=[list(key) for key in delivered_pairs],
            peer_connection_verified=False,
            subtypes=[subtype for _payload, subtype in deliveries],
        )
        # Clear only delivered links. Busy peers and the opposite directed
        # links must retain their own recovery backoff.
        related = {
            key
            for key in self._peer_recovery_times
            if key[0] == recipient.auth_token
            and (peer := self.sessions.get(key[2])) is not None
            and (key[1], peer.player_id) in delivered_pairs
        }
        for key in related:
            self._peer_recovery_times.pop(key, None)
            self._peer_recovery_attempts.pop(key, None)
        return len(delivered_pairs)

    def discard_deferred_team_session_pushes(self, recipient: GameSession) -> int:
        pending = self._pending_team_session_pushes.get(recipient.auth_token)
        if not pending:
            return 0
        owned = [key for key, update in pending.items() if update.recipient is recipient]
        for key in owned:
            pending.pop(key)
        if not pending:
            self._pending_team_session_pushes.pop(recipient.auth_token, None)
        return len(owned)

    def _live_mercenary_online_ids(self) -> set[str]:
        now = time.monotonic()
        connected = {
            candidate.online_id
            for candidate in self.sessions.values()
            if candidate.established
            and candidate.peer is not None
            and now - candidate.last_seen_monotonic <= 90.0
        }
        return self.active_mercenaries & connected

    def _notify_team_profile_updated(
        self,
        team_id: int,
        *,
        reason: str,
        subject_online_id: str,
    ) -> list[str]:
        """Wake each live team client so it refreshes the durable member list.

        Retail typed-push subtype 4 is PushRequestProfileUpdatedMemberList. Its
        protobuf is a repeated-only field-1 list and accepts an empty list; the
        concrete consumer ignores payload fields and invokes the application
        member-list refresh callback at virtual offset 0xb4.
        """
        team = self.teams.team_by_id(int(team_id))
        if team is None:
            return []
        online_ids = {
            str(record.get("online_id", ""))
            for record in team.get("member_records", [])
            if isinstance(record, dict) and str(record.get("online_id", ""))
        }
        now = time.monotonic()
        recipients: list[str] = []
        for candidate in self.sessions.values():
            if (
                candidate.online_id not in online_ids
                or not candidate.established
                or candidate.peer is None
                or now - candidate.last_seen_monotonic > 90.0
            ):
                continue
            self._send_game_push(candidate, b"", subtype=4)
            recipients.append(candidate.online_id)
        if recipients:
            self.telemetry.emit(
                "game_team_profile_update_pushes_sent",
                team_id=int(team_id),
                reason=reason,
                subject_online_id=subject_online_id,
                recipient_online_ids=recipients,
                subtype=4,
            )
        return recipients

    def _remove_team_session_member_and_notify(
        self,
        online_id: str,
        requested_session_id: int = 0,
    ) -> tuple[list[int], list[int], list[str]]:
        """Remove one member and announce the exact departure to live peers."""
        departures: list[tuple[int, int]] = []
        for session_id, team_session in self.team_sessions.items():
            if requested_session_id > 0 and int(session_id) != int(requested_session_id):
                continue
            members = team_session.get("members", {})
            record = members.get(online_id) if isinstance(members, dict) else None
            if not isinstance(record, dict):
                continue
            # Do not turn missing identity data into a departure for player 1.
            player_id = record.get("player_id")
            if type(player_id) is int and 0 < player_id < 1 << 32:
                departures.append((int(session_id), player_id))

        removed, deleted = _remove_team_session_member_from_state(
            self.team_sessions,
            online_id,
            requested_session_id,
        )
        if removed:
            for candidate in self.sessions.values():
                if candidate.online_id == online_id:
                    candidate.team_session_ready = False
        notified: list[str] = []
        now = time.monotonic()
        for session_id, player_id in departures:
            if session_id not in removed or session_id in deleted:
                continue
            team_session = self.team_sessions.get(session_id)
            members = team_session.get("members", {}) if team_session is not None else {}
            if not isinstance(members, dict):
                continue
            payload = _team_session_leave_push(session_id, player_id)
            for remaining_online_id in members:
                remaining_session = next(
                    (
                        candidate
                        for candidate in self.sessions.values()
                        if candidate.online_id == remaining_online_id
                        and candidate.established
                        and candidate.peer is not None
                        and now - candidate.last_seen_monotonic <= 90.0
                    ),
                    None,
                )
                if remaining_session is None:
                    continue
                if self.defer_team_session_push(
                    remaining_session,
                    payload,
                    subtype=SESSION_MEMBER_LEAVE_PUSH,
                    session_id=session_id,
                    player_id=player_id,
                ):
                    notified.append(remaining_online_id)
        return removed, deleted, notified

    def _purge_stale_team_session_members(self) -> None:
        now = time.monotonic()
        active = {
            candidate.online_id
            for candidate in self.sessions.values()
            if candidate.established
            and candidate.peer is not None
            and now - candidate.last_seen_monotonic <= 90.0
        }
        removed: dict[str, list[int]] = {}
        deleted: set[int] = set()
        for team_session in list(self.team_sessions.values()):
            members = team_session.get("members", {})
            if not isinstance(members, dict):
                continue
            for online_id in list(members):
                if online_id in active:
                    continue
                removed_from, deleted_sessions, notified = (
                    self._remove_team_session_member_and_notify(online_id)
                )
                if removed_from:
                    removed[online_id] = removed_from
                deleted.update(deleted_sessions)
                if notified:
                    self.telemetry.emit(
                        "game_team_session_leave_pushes_sent",
                        online_id=online_id,
                        reason="stale",
                        removed_session_ids=removed_from,
                        recipient_online_ids=notified,
                        subtype=SESSION_MEMBER_LEAVE_PUSH,
                    )
        if removed:
            self.telemetry.emit(
                "game_team_session_stale_members_purged",
                removed_members=removed,
                deleted_session_ids=sorted(deleted),
            )
