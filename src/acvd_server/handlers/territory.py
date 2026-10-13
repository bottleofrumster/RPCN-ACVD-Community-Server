"""Team-scoped territory layout requests and compatibility territory metadata.

Layouts remain opaque; missing and stored-empty layouts are different states.
A requested player ID is metadata, not authority to overwrite another team.
Synthetic territory/AP values remain unqualified world-simulation behavior.
"""

from __future__ import annotations

import zlib
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..matchmaking import MatchmakingCoordinator

from ..protocol import decode_proto, first_field
from ..teams import TeamStore
from ..telemetry import Telemetry
from ..udp import GameMessage, GameSession
from ..world_messages import (
    _territory_cannon_layout,
    _territory_info,
    _territory_layout_update_response,
)


def get_layout(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    matchmaking: MatchmakingCoordinator | None = None,
    phase: int = 2,
) -> None:
    fields = decode_proto(message.payload)

    def integer(number: int, default: int = 0) -> int:
        field = first_field(fields, number)
        return field.value if field is not None and isinstance(field.value, int) else default

    season_id = integer(1, team_store.world_season_id())
    player_id = integer(2, session.player_id)
    area_id = integer(
        3, int((team_store.team_for(session.online_id) or {}).get("territory_area_id", 0))
    )
    # Native records are separate from the older team-scoped compatibility data.
    target = team_store.member_by_player_id(player_id)
    own_team = team_store.team_for(session.online_id)
    matched_owner = None
    if (
        matchmaking is not None
        and own_team is not None
        and season_id == team_store.world_season_id()
    ):
        matched_owner = matchmaking.matched_territory_owner(
            session,
            player_id=player_id,
            area_id=area_id,
            team_id=int(own_team["team_id"]),
        )
    match_scoped = (
        target is not None
        and matched_owner is not None
        and matched_owner == (target[0], int(target[1]["team_id"]))
    )
    same_team = (
        target is not None
        and own_team is not None
        and target[1].get("team_id") == own_team.get("team_id")
    )
    # The native grace notice disables territory download and upload. Preserve
    # only a layout already frozen into a battle that began during active war.
    if phase == 0 and not (
        match_scoped
        and matchmaking is not None
        and matchmaking.accepted_world_phase(session) == 2
    ):
        telemetry.emit(
            "game_message_rejected",
            online_id=session.online_id,
            message_type=message.message_type,
            reason="Territory download is disabled during the grace period",
        )
        return
    if target is not None and own_team is not None and (same_team or match_scoped):
        record = team_store.load_native_territory_layout(
            target[0],
            player_id=player_id,
            season_id=season_id,
            area_id=area_id,
        )
        if record is not None:
            layout, version = record
            reply(_territory_cannon_layout(player_id, layout, layout_version=version))
            telemetry.emit(
                "game_native_territory_layout_sent",
                online_id=session.online_id,
                player_id=player_id,
                season_id=season_id,
                area_id=area_id,
                layout_version=version,
                layout_length=len(layout),
                match_scoped=match_scoped,
            )
            return
    layout = team_store.load_territory_layout(
        session.online_id,
        season_id=season_id,
        area_id=area_id,
    )
    reply(_territory_cannon_layout(player_id, layout))
    telemetry.emit(
        "game_territory_cannon_layout_sent",
        peer=session.peer,
        online_id=session.online_id,
        season_id=season_id,
        player_id=player_id,
        area_id=area_id,
        layout_length=len(layout) if layout is not None else 0,
        persisted=layout is not None,
    )


def _validate_native_layout(payload: bytes, version: int) -> None:
    """Check the captured layout format without altering its encoded bytes."""
    if version != 7 or not payload or len(payload) > 8192:
        raise ValueError("Unsupported territory layout format")
    if payload[0] == 0:
        raw = payload[1:]
    elif payload[0] == 1:
        reader = zlib.decompressobj()
        raw = reader.decompress(payload[1:], 0x421)
        if not reader.eof or reader.unused_data or reader.unconsumed_tail:
            raise ValueError("Invalid compressed territory layout")
    else:
        raise ValueError("Unknown territory layout encoding")
    if len(raw) != 0x420 or raw[0] != version:
        raise ValueError("Territory layout size or version does not match")


def upload_layout(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    phase: int = 2,
) -> None:
    """Persist the observed 0x03BE upload before sending its success ACK."""
    try:
        if phase == 0:
            raise ValueError("Territory upload is disabled during the grace period")
        fields = decode_proto(message.payload)
        values = {}
        for number in range(1, 5):
            matches = [item for item in fields if item.number == number]
            if len(matches) != 1:
                raise ValueError("Missing or duplicate territory upload field")
            item = matches[0]
            if item.wire_type != (2 if number == 4 else 0):
                raise ValueError("Invalid territory upload field type")
            values[number] = item.value
        season_id, area_id, version, payload = (values[n] for n in range(1, 5))
        team = team_store.team_for(session.online_id)
        if team is None or session.player_id <= 0:
            raise ValueError("Territory upload requires team membership")
        assigned_area = int(team.get("territory_area_id", 0))
        if season_id != team_store.world_season_id() or not 1 <= area_id <= 7:
            raise ValueError("Territory upload season or area is not active")
        if area_id != assigned_area:
            raise ValueError("Territory upload does not match the assigned area")
        _validate_native_layout(payload, version)
        saved = team_store.save_native_territory_layout(
            session.online_id,
            player_id=session.player_id,
            season_id=season_id,
            area_id=area_id,
            layout_version=version,
            payload=payload,
        )
        if saved is None:
            raise ValueError("Territory upload has no storage owner")
    except (ValueError, zlib.error, OSError) as error:
        telemetry.emit(
            "game_message_rejected",
            online_id=session.online_id,
            message_type=message.message_type,
            reason=str(error),
        )
        return
    reply(b"")
    telemetry.emit(
        "game_native_territory_layout_saved",
        online_id=session.online_id,
        player_id=session.player_id,
        team_id=int(team["team_id"]),
        season_id=season_id,
        area_id=area_id,
        layout_version=version,
        layout_length=len(payload),
        layout_file=saved.name,
    )


def update_layout(
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

    team = team_store.team_for(session.online_id) or {}
    season_id = integer(1, team_store.world_season_id())
    player_id = integer(2, session.player_id)
    area_id = integer(3, int(team.get("territory_area_id", 0)))
    layout_field = first_field(fields, 4)
    if layout_field is None or not isinstance(layout_field.value, bytes):
        # Some retail call sites wrap the same opaque layout as the
        # only length-delimited field. Preserve that payload without
        # trying to understand its internal cannon-placement schema.
        layout_field = next((field for field in fields if isinstance(field.value, bytes)), None)
    if layout_field is None or not isinstance(layout_field.value, bytes):
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason="RequestUpdateTerritoryCannonLayout has no embedded layout",
        )
        return
    saved_path = team_store.save_territory_layout(
        session.online_id,
        season_id=season_id,
        player_id=player_id,
        area_id=area_id,
        payload=layout_field.value,
    )
    if saved_path is None:
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason="territory layout upload requires team membership",
        )
        return
    reply(_territory_layout_update_response())
    telemetry.emit(
        "game_territory_cannon_layout_saved",
        peer=session.peer,
        online_id=session.online_id,
        team_id=team.get("team_id", 0),
        season_id=season_id,
        player_id=player_id,
        area_id=area_id,
        layout_length=len(layout_field.value),
        layout_file=saved_path.name,
    )


def get_info(
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

    team = team_store.team_for(session.online_id) or {}
    player_id = integer(1, session.player_id)
    season_id = integer(2, team_store.world_season_id())
    area_id = int(team.get("territory_area_id", 0))
    reply(_territory_info(player_id, season_id, area_id))
    telemetry.emit(
        "game_territory_info_sent",
        peer=session.peer,
        online_id=session.online_id,
        player_id=player_id,
        season_id=season_id,
        area_id=area_id,
        current_ap=1_000,
        maximum_ap=1_000,
        power_cap=100_000,
    )
