"""Live team-session request handling and shared member removal.

Durable teams and live signaling sessions are distinct. The caller supplies
one shared directory and live-session view, not copies. Purge precedes lookup;
creation ACK precedes peer announcements, while departure delegates removal/
notifications before ACK. Bootstrap validity is not signaling readiness.
"""

from __future__ import annotations

from typing import Callable, Mapping, Protocol

from ..protocol import decode_proto, first_field, proto_varint
from ..session_lifecycle import _remove_team_session_member_from_state
from ..team_messages import (
    _team_session_join_response,
    _team_session_member_push_pair,
    _team_session_response,
)
from ..teams import TeamStore
from ..telemetry import Telemetry
from ..udp import GameMessage, GameSession, PushSender


class MemberRemover(Protocol):
    def __call__(
        self, online_id: str, requested_session_id: int = 0
    ) -> tuple[list[int], list[int], list[str]]:
        """Remove only this identity and notify surviving live peers."""
        ...


def join_session(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    session_directory: dict[int, dict],
    purge_stale_members: Callable[[], None],
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    purge_stale_members()
    request_fields = decode_proto(message.payload)
    session_field = first_field(request_fields, 1)
    identity_field = first_field(request_fields, 2)
    requested_session_id = (
        int(session_field.value)
        if session_field is not None and isinstance(session_field.value, int)
        else 0
    )
    supplied_identity = (
        bytes(identity_field.value)
        if identity_field is not None and isinstance(identity_field.value, bytes)
        else b""
    )
    team_session = session_directory.get(requested_session_id)
    if team_session is not None:
        profile = team_store.player_profile(session.online_id)
        member = team_session.setdefault("members", {}).setdefault(
            session.online_id,
            {
                "player_id": session.player_id,
                "member_id": max(1, int(profile.get("member_id", 1))),
                "np_identity": supplied_identity,
            },
        )
        if supplied_identity:
            member["np_identity"] = supplied_identity
        response = _team_session_join_response(team_session)
        session.team_session_ready = True
    else:
        response = b""
    reply(response)
    telemetry.emit(
        "game_team_session_joined",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        requested_session_id=requested_session_id,
        session_found=team_session is not None,
        supplied_identity_length=len(supplied_identity),
        response_member_count=(
            len(team_session.get("members", {})) if team_session is not None else 0
        ),
        request_payload=message.payload,
    )


def leave_session(
    session: GameSession,
    message: GameMessage,
    *,
    remove_member: MemberRemover,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    session_field = first_field(fields, 1)
    member_field = first_field(fields, 2)
    reason_field = first_field(fields, 3)
    requested_session_id = (
        int(session_field.value)
        if session_field is not None and isinstance(session_field.value, int)
        else 0
    )
    member_id = (
        int(member_field.value)
        if member_field is not None and isinstance(member_field.value, int)
        else 0
    )
    reason = (
        int(reason_field.value)
        if reason_field is not None and isinstance(reason_field.value, int)
        else 0
    )
    removed_session_ids, deleted_session_ids, notified_online_ids = remove_member(
        session.online_id,
        requested_session_id,
    )
    reply(b"")
    telemetry.emit(
        "game_team_session_left",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        requested_session_id=requested_session_id,
        member_id=member_id,
        reason=reason,
        removed_session_ids=removed_session_ids,
        deleted_session_ids=deleted_session_ids,
        recipient_online_ids=notified_online_ids,
        request_payload=message.payload,
    )


def report_disconnected_member(
    session: GameSession,
    message: GameMessage,
    *,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    retire_overlay: Callable[[GameSession, int, int], bool] | None = None,
    recover_peer: Callable[[GameSession, int, int], bool] | None = None,
) -> None:
    fields = decode_proto(message.payload)
    session_fields = [field for field in fields if field.number == 1]
    player_fields = [field for field in fields if field.number == 2]
    session_field = session_fields[0] if len(session_fields) == 1 else None
    player_field = player_fields[0] if len(player_fields) == 1 else None
    requested_session_id = (
        int(session_field.value)
        if session_field is not None and isinstance(session_field.value, int)
        else 0
    )
    reported_player_id = (
        int(player_field.value)
        if player_field is not None and isinstance(player_field.value, int)
        else 0
    )
    valid_report = (
        len(message.payload) <= 24
        and len(fields) == 2
        and session_field is not None
        and session_field.wire_type == 0
        and player_field is not None
        and player_field.wire_type == 0
        and 0 < requested_session_id < 1 << 64
        and 0 < reported_player_id < 1 << 32
    )
    overlay_retired = bool(
        valid_report
        and retire_overlay
        and retire_overlay(session, requested_session_id, reported_player_id)
    )
    reply(b"")
    peer_recovered = bool(
        valid_report
        and not overlay_retired
        and recover_peer
        and recover_peer(session, requested_session_id, reported_player_id)
    )
    telemetry.emit(
        "game_team_session_member_disconnect_reported",
        peer=session.peer,
        online_id=session.online_id,
        requested_session_id=requested_session_id,
        reported_player_id=reported_player_id,
        foreign_overlay_retired=overlay_retired,
        real_team_peer_recovered=peer_recovered,
        recovery_action_requested=peer_recovered,
        peer_connection_verified=False,
    )


def delete_session(
    session: GameSession,
    message: GameMessage,
    *,
    remove_member: MemberRemover,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    session_field = first_field(fields, 1)
    requested_session_id = (
        int(session_field.value)
        if session_field is not None and isinstance(session_field.value, int)
        else 0
    )
    removed_session_ids, deleted_session_ids, notified_online_ids = remove_member(
        session.online_id,
        requested_session_id,
    )
    reply(b"")
    telemetry.emit(
        "game_team_session_deleted",
        peer=session.peer,
        online_id=session.online_id,
        requested_session_id=requested_session_id,
        removed_session_ids=removed_session_ids,
        deleted_session_ids=deleted_session_ids,
        recipient_online_ids=notified_online_ids,
        request_payload=message.payload,
    )


def check_valid_player(
    session: GameSession,
    message: GameMessage,
    *,
    session_directory: dict[int, dict],
    purge_stale_members: Callable[[], None],
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    purge_stale_members()
    team_session = next(
        (
            candidate
            for candidate in session_directory.values()
            if session.online_id in candidate.get("members", {})
        ),
        None,
    )
    valid = 1 if team_session is not None else 0
    reply(proto_varint(1, valid) + proto_varint(2, valid))
    telemetry.emit(
        "game_player_session_validated",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        valid=bool(valid),
    )


def create_session(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    session_directory: dict[int, dict],
    game_sessions: Mapping[bytes, GameSession],
    purge_stale_members: Callable[[], None],
    send_push: PushSender,
    defer_push: Callable[..., bool] | None = None,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    purge_stale_members()
    request_fields = decode_proto(message.payload)
    ticket_field = first_field(request_fields, 1)
    supplied_identity = (
        bytes(ticket_field.value)
        if ticket_field is not None and isinstance(ticket_field.value, bytes)
        else b""
    )
    team = team_store.team_for(session.online_id) or {}
    profile = team_store.player_profile(session.online_id)
    team_id = max(1, int(team.get("team_id", session.player_id)))
    external_member_id = max(1, int(profile.get("member_id", 1)))
    replaced_session_ids, _ = _remove_team_session_member_from_state(
        session_directory,
        session.online_id,
    )
    team_session = session_directory.setdefault(
        team_id,
        {
            "session_id": team_id,
            "team_id": team_id,
            "members": {},
        },
    )
    existing_members = dict(team_session["members"])
    team_session["members"][session.online_id] = {
        "player_id": session.player_id,
        "member_id": external_member_id,
        "np_identity": supplied_identity,
    }
    session.team_session_ready = True
    reply(_team_session_response(team, profile, session.player_id))
    telemetry.emit(
        "game_team_session_created_or_joined",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        team_id=team_id,
        external_session_id=int(team_session["session_id"]),
        external_member_id=external_member_id,
        replaced_session_ids=replaced_session_ids,
        session_members=sorted(team_session["members"]),
        supplied_identity_length=len(supplied_identity),
        request_payload=message.payload,
    )
    push_recipients: list[str] = []
    for existing_online_id, existing_member in existing_members.items():
        if existing_online_id == session.online_id:
            continue
        existing_identity = bytes(existing_member.get("np_identity", b""))
        if not existing_identity or not supplied_identity:
            continue
        existing_session = next(
            (
                candidate
                for candidate in game_sessions.values()
                if candidate.online_id == existing_online_id
                and candidate.established
                and candidate.peer is not None
                and candidate.team_session_ready
            ),
            None,
        )
        if existing_session is None:
            # A reconnect may already have a UDP transport while its
            # old roster entry survives. Announce only after its own
            # Create/JoinTeamSession, never into signaling context 0.
            telemetry.emit(
                "game_team_session_peer_announcement_deferred",
                online_id=session.online_id,
                counterpart_online_id=existing_online_id,
                session_id=int(team_session["session_id"]),
                reason="counterpart_team_session_not_ready",
            )
            continue
        to_new, to_existing = _team_session_member_push_pair(
            int(team_session["session_id"]),
            existing_online_id=existing_online_id,
            existing_member=existing_member,
            new_online_id=session.online_id,
            new_player_id=session.player_id,
            new_np_identity=supplied_identity,
        )
        if session.established and session.peer is not None:
            # The CreateTeamSession reply establishes the recipient's local
            # signaling context. Queue its peer announcements until the
            # subsequent CheckValidPlayer proves that callback completed;
            # sending reply + joins in one datagram burst can be acknowledged
            # by transport while the game silently discards the early joins.
            deferred_new = bool(
                defer_push
                and defer_push(
                    session,
                    to_new,
                    subtype=9,
                    session_id=int(team_session["session_id"]),
                    player_id=int(existing_member["player_id"]),
                )
            )
            if deferred_new:
                push_recipients.append(session.online_id)
            else:
                send_push(session, to_new, subtype=9)
                push_recipients.append(session.online_id)
        if existing_session is not None:
            deferred = bool(
                defer_push
                and defer_push(
                    existing_session,
                    to_existing,
                    subtype=9,
                    session_id=int(team_session["session_id"]),
                    player_id=session.player_id,
                )
            )
            if deferred:
                push_recipients.append(existing_online_id)
            elif defer_push is None:
                send_push(existing_session, to_existing, subtype=9)
                push_recipients.append(existing_online_id)
    if push_recipients:
        telemetry.emit(
            "game_team_session_member_pushes_sent",
            online_id=session.online_id,
            session_id=int(team_session["session_id"]),
            existing_members=sorted(existing_members),
            recipient_online_ids=push_recipients,
            announced_identity_length=len(supplied_identity),
            subtype=9,
            existing_recipient_delivery="deferred_until_world_read" if defer_push else "immediate",
            new_recipient_delivery="deferred_until_session_validation" if defer_push else "immediate",
        )


def check_bootstrap_player(
    session: GameSession,
    message: GameMessage,
    *,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    reply(proto_varint(1, 1))
    telemetry.emit(
        "game_bootstrap_player_validated",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
    )
