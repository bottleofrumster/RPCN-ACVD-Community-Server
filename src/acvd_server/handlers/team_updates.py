"""Durable team mutations with explicit notification boundaries.

TeamStore owns admission, leadership and atomic membership changes. Handlers
retain reply-before-notification ordering; the dispatcher decides which live
clients receive refresh pushes. No transport or session registry is imported.
"""

from __future__ import annotations

from typing import Callable, Protocol

from ..protocol import ProtocolError, decode_proto, first_field, proto_varint
from ..teams import TeamStore
from ..telemetry import Telemetry
from ..udp import GameMessage, GameSession
from ..world_messages import _canonical_faction_selection_id


class TeamProfileNotifier(Protocol):
    def __call__(self, team_id: int, *, reason: str, subject_online_id: str) -> list[str]:
        """Notify the live members of an already-mutated durable team."""
        ...


def create_team(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)

    def integer(number: int, default: int = 0) -> int:
        field = first_field(fields, number)
        return field.value if field is not None and isinstance(field.value, int) else default

    name_field = first_field(fields, 3)
    name = (
        name_field.value.decode("utf-8", "replace")
        if name_field is not None and isinstance(name_field.value, bytes)
        else ""
    )
    team = team_store.create(
        online_id=session.online_id,
        player_id=session.player_id,
        name=name,
        operation_base=integer(11),
        language=integer(9),
        # The creation UI sends Stance as a zero-based enum, while
        # TeamInfo field 9 reserves zero for No Selection.
        play_type=integer(18) + 1,
        # Native Free/Closed creation controls change field 7 (2/0).
        # Field 22 stayed 1 in both controls and can contain unrelated IDs.
        enlistment_method=integer(7),
        creation_wire_fields={number: integer(number) for number in (7, 9, 11, 18, 21, 22, 23)},
    )
    reply(b"")
    telemetry.emit(
        "game_team_created",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        team=team,
    )


def leave_team(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    notify_team_profile: TeamProfileNotifier,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    result = team_store.leave_team(session.online_id)
    reply(b"")
    profile_update_recipients = (
        notify_team_profile(
            int(result.get("team_id", 0)),
            reason="member_left",
            subject_online_id=session.online_id,
        )
        if result is not None and not bool(result.get("team_deleted", False))
        else []
    )
    telemetry.emit(
        "game_team_left" if result is not None else "game_team_leave_ignored",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        team_id=int(result.get("team_id", 0)) if result is not None else 0,
        team_deleted=bool(result.get("team_deleted", False)) if result is not None else False,
        leader_promoted=str(result.get("leader_promoted", "")) if result is not None else "",
        profile_update_recipient_online_ids=profile_update_recipients,
    )


def join_team_auto(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    notify_team_profile: TeamProfileNotifier,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    def nested_integer_values(payload: bytes, depth: int = 0) -> list[int]:
        values: list[int] = []
        try:
            fields = decode_proto(payload)
        except ProtocolError:
            return values
        for field in fields:
            if isinstance(field.value, int):
                values.append(int(field.value))
            elif isinstance(field.value, bytes) and depth < 2:
                values.extend(nested_integer_values(field.value, depth + 1))
        return values

    previous_team = team_store.team_for(session.online_id)
    previous_team_id = int(previous_team.get("team_id", 0)) if previous_team else 0
    candidates = nested_integer_values(message.payload)
    team_id = next(
        (value for value in candidates if team_store.team_by_id(value) is not None),
        0,
    )
    team = team_store.join_team(
        online_id=session.online_id,
        player_id=session.player_id,
        team_id=team_id,
        replace_existing=True,
    )
    reply(b"")
    profile_update_recipients: dict[int, list[str]] = {}
    if team is not None:
        joined_team_id = int(team["team_id"])
        if previous_team_id > 0 and previous_team_id != joined_team_id:
            profile_update_recipients[previous_team_id] = notify_team_profile(
                previous_team_id,
                reason="member_transferred_out",
                subject_online_id=session.online_id,
            )
        profile_update_recipients[joined_team_id] = notify_team_profile(
            joined_team_id,
            reason="member_joined",
            subject_online_id=session.online_id,
        )
    telemetry.emit(
        "game_team_auto_joined" if team is not None else "game_team_auto_join_rejected",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        requested_candidates=candidates,
        team_id=team_id,
        previous_team_id=previous_team_id,
        profile_update_recipients=profile_update_recipients,
    )


def join_faction(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    faction_field = first_field(fields, 1)
    context_field = first_field(fields, 2)
    requested_faction_id = (
        faction_field.value
        if faction_field is not None and isinstance(faction_field.value, int)
        else 0
    )
    selection_context = (
        context_field.value
        if context_field is not None and isinstance(context_field.value, int)
        else 0
    )
    # Genuine visual selection proves field 1 is the canonical row:
    # Sirius=1, Venide=2, EGF=3. Field 2 is the current season/context
    # (observed 3), not a reverse-coded faction and not a world ID.
    war = team_store.test_war_snapshot()
    valid_contexts = (war["season"],) if war else (1, 2, 3)
    if requested_faction_id not in (1, 2, 3) or selection_context not in valid_contexts:
        telemetry.emit(
            "game_faction_join_rejected",
            peer=session.peer,
            online_id=session.online_id,
            faction_id=requested_faction_id,
            selection_context=selection_context,
        )
        return
    faction_id = _canonical_faction_selection_id(requested_faction_id)
    world_id = 1
    team = team_store.set_faction(session.online_id, world_id, faction_id)
    reply(proto_varint(1, faction_id) + proto_varint(2, selection_context))
    telemetry.emit(
        "game_faction_joined",
        peer=session.peer,
        online_id=session.online_id,
        team_id=team["team_id"] if team is not None else 0,
        world_id=world_id,
        faction_id=faction_id,
        requested_faction_id=requested_faction_id,
        selection_context=selection_context,
        territory_area_id=(team or {}).get("territory_area_id", 0),
    )


def update_team_properties(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    phase: int = 2,
    reply_error: Callable[[int, int], None] | None = None,
) -> None:
    fields = decode_proto(message.payload)
    scalar_fields = {
        field.number: int(field.value)
        for field in fields
        if field.number in (8, 10, 13, 14, 20, 23, 25, 26, 27) and isinstance(field.value, int)
    }
    if phase == 0:
        current = team_store.team_for(session.online_id) or {}
        protected = {26: ("desired_faction_id", 1), 27: ("desired_area_id", 0)}
        if any(
            number in scalar_fields and max(0, scalar_fields[number]) != current.get(key, default)
            for number, (key, default) in protected.items()
        ):
            telemetry.emit(
                "game_message_rejected",
                online_id=session.online_id,
                message_type=message.message_type,
                reason="Next-season settings are disabled during the grace period",
            )
            return
    color_field = first_field(fields, 11)
    comment_field = first_field(fields, 24)
    color = (
        color_field.value
        if color_field is not None and isinstance(color_field.value, bytes)
        else None
    )
    comment = (
        comment_field.value
        if comment_field is not None and isinstance(comment_field.value, bytes)
        else None
    )
    try:
        team = team_store.update_team_properties(
            session.online_id,
            scalar_fields=scalar_fields,
            color=color,
            comment=comment,
        )
    except ValueError as error:
        telemetry.emit(
            "game_message_rejected", online_id=session.online_id,
            message_type=message.message_type, reason=str(error),
        )
        if reply_error is not None:
            reply_error(1, 28)
        return
    reply(b"")
    telemetry.emit(
        "game_team_changeable_properties_updated",
        peer=session.peer,
        online_id=session.online_id,
        team_id=team["team_id"] if team is not None else 0,
        scalar_fields=scalar_fields,
        color_length=len(color or b""),
        comment_length=len(comment or b""),
    )


def update_leader_settings(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)

    def integer(number: int) -> int:
        field = first_field(fields, number)
        return field.value if field is not None and isinstance(field.value, int) else 0

    def binary(number: int) -> bytes:
        field = first_field(fields, number)
        return field.value if field is not None and isinstance(field.value, bytes) else b""

    team = team_store.update_leader_settings(
        session.online_id,
        pay_apportionment=integer(3),
        leader_identity=binary(2),
        enlistment_method=integer(1),
        profile_text=binary(4),
    )
    if team is None:
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason="leader team settings update requires team leadership",
        )
        return
    reply(b"")
    telemetry.emit(
        "game_team_leader_settings_updated",
        peer=session.peer,
        online_id=session.online_id,
        team_id=team["team_id"],
        pay_apportionment=team["pay_apportionment"],
        leader_identity=team.get("leader_update_identity", ""),
        enlistment_method=team["enlistment_method"],
        profile_text_length=len(binary(4)),
    )
