"""Match preparation, cancellation and room notification request boundaries.

The coordinator owns all transient match state; transport supplies correlated
replies. Retain the bounded handoff without inventing new room, password or
role semantics. Native Normal controls are limited; larger squads need
separate gameplay qualification.
"""

from __future__ import annotations

import time
from typing import Callable

from ..combat_reports import (
    decode_battle_disconnect,
    decode_briefing_end,
    decode_report_disconnected,
)
from ..match_messages import _register_prepare_response
from ..match_policy import same_squad
from ..matchmaking import (
    ACTIVE_SINGLE_TEAM,
    INTERWAR_BOSS,
    MatchmakingCoordinator,
)
from ..protocol import ProtocolError, decode_proto, first_field
from ..rewards import world_pay_policy_for_team
from ..teams import TeamStore
from ..telemetry import Telemetry
from ..udp import GameMessage, GameSession


def register_preparation(
    session: GameSession,
    message: GameMessage,
    *,
    matchmaking: MatchmakingCoordinator,
    team_store: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    reply_error: Callable[[int, int], None] | None = None,
) -> None:
    matchmaking._purge_stale_matchmaking()
    fields = decode_proto(message.payload)
    criteria = {field.number: int(field.value) for field in fields if isinstance(field.value, int)}
    claimed_player_ids: list[int] = []
    claimed_player_roles: list[int | None] = []
    invalid_roster = False
    for field in fields:
        if field.number != 6:
            continue
        if not isinstance(field.value, bytes):
            invalid_roster = True
            continue
        record = decode_proto(field.value)
        ids = [f for f in record if f.number == 1]
        roles = [f for f in record if f.number == 2]
        if (
            len(record) != 2
            or len(ids) != 1
            or len(roles) != 1
            or ids[0].wire_type != 0
            or roles[0].wire_type != 0
        ):
            invalid_roster = True
        claimed = first_field(record, 1)
        if claimed is not None and isinstance(claimed.value, int):
            claimed_player_ids.append(int(claimed.value))
            claimed_player_roles.append(
                roles[0].value if len(roles) == 1 and roles[0].wire_type == 0 else None
            )
    team = team_store.team_for(session.online_id)
    phase = matchmaking.get_world_phase()
    team_id = int(team.get("team_id", 0)) if team is not None else 0
    faction_id = team.get("current_faction_id", 0) if team is not None else 0
    if type(faction_id) is not int or faction_id not in (1, 2, 3):
        faction_id = 0
    squad = None
    try:
        pay_policy = world_pay_policy_for_team(team or {}).value
        if invalid_roster and criteria.get(3) in (1, 2):
            raise ValueError("Squad has an invalid participant record")
        interwar = matchmaking.validate_registration(
            session, criteria, claimed_player_ids, team_id, phase, claimed_player_roles
        )
        if interwar:
            squad = matchmaking.resolve_interwar_squad(
                session, claimed_player_ids, claimed_player_roles, team_id
            )
        elif criteria.get(3) in (1, 2):
            squad = matchmaking.resolve_activity_squad(
                session,
                claimed_player_ids or [session.player_id],
                claimed_player_roles or [0],
                team_id,
            )
    except ValueError as error:
        telemetry.emit(
            "game_message_rejected",
            online_id=session.online_id,
            message_type=message.message_type,
            reason=str(error),
        )
        if reply_error is not None:
            # Every rejected registration needs its correlated error reply;
            # dropping stale-context/roster requests leaves the client waiting.
            reply_error(1, 28)
        return
    matchmaking.prepare_registration(session)
    area_id = team.get("territory_area_id", 0) if team is not None else 0
    # Take the region from the authenticated team, never request scalars.
    # Keep the existing unassigned path for missing or invalid stored regions.
    if isinstance(area_id, bool) or not isinstance(area_id, int) or not 1 <= area_id <= 7:
        area_id = 0
    layout_owner_id = 0
    if area_id and type(session.player_id) is int and 0 < session.player_id < 1 << 32:
        try:
            layout = team_store.load_active_native_territory_layout(
                session.online_id,
                season_id=team_store.world_season_id(),
                area_id=area_id,
            )
            if layout is not None:
                if layout[1] != 7 or not 0 < len(layout[0]) <= 8192:
                    raise ValueError("Stored territory layout format is unsupported")
                layout_owner_id = layout[2]
        except (OSError, ValueError, TypeError, KeyError) as error:
            telemetry.emit(
                "game_prepare_territory_layout_unavailable",
                online_id=session.online_id,
                reason=str(error),
            )
    existing = matchmaking.preparations.get(session.online_id)
    same_search = bool(
        existing is not None
        and int(existing.get("match_id", 0)) == 0
        and existing.get("session") is session
        and existing.get("criteria") == criteria
        and existing.get("team_id") == team_id
        and existing.get("faction_id") == faction_id
        and existing.get("claimed_player_ids") == claimed_player_ids
        and existing.get("claimed_player_roles") == claimed_player_roles
        and existing.get("pay_policy") == pay_policy
        and same_squad(existing.get("squad"), squad)
    )
    if existing is not None and not same_search:
        matchmaking._cancel_single_team_fallback(existing)
    prepare_id = (
        int(existing["prepare_id"]) if existing is not None else matchmaking.next_prepare_id
    )
    if existing is None:
        matchmaking.next_prepare_id += 1
    now = time.monotonic()
    preparation = {
        "prepare_id": prepare_id,
        "session": session,
        "criteria": criteria,
        "world_phase": phase,
        "team_id": team_id,
        "faction_id": faction_id,
        "territory_area_id": area_id,
        "territory_layout_owner_id": layout_owner_id,
        "claimed_player_ids": claimed_player_ids,
        "claimed_player_roles": claimed_player_roles,
        "pay_policy": pay_policy,
        "registered_monotonic": now,
        "search_started_monotonic": (
            float(existing.get("search_started_monotonic", now)) if same_search else now
        ),
        "match_id": int(existing.get("match_id", 0)) if existing is not None else 0,
    }
    if same_search and existing.get("single_team_fallback_task") is not None:
        preparation["single_team_fallback_task"] = existing["single_team_fallback_task"]
    if interwar:
        preparation["interwar_boss"] = True
    if squad is not None:
        preparation["squad"] = squad
    matchmaking.preparations[session.online_id] = preparation
    reply(
        _register_prepare_response(
            prepare_id,
            state=1,
            token=f"{session.online_id}:{prepare_id}",
        ),
    )
    telemetry.emit(
        "game_register_prepare_accepted",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        team_id=team_id,
        faction_id=faction_id,
        territory_area_id=area_id,
        territory_layout_owner_id=layout_owner_id,
        claimed_player_ids=claimed_player_ids,
        claimed_player_roles=claimed_player_roles,
        pay_policy=pay_policy,
        authenticated_player_ids=list(squad) if squad is not None else [],
        claimed_player_matches_session=(claimed_player_ids == [int(session.player_id)]),
        prepare_id=prepare_id,
        request_payload=message.payload,
        criteria=criteria,
    )
    matchmaking._match_preparation(preparation)


def cancel_preparation(
    session: GameSession,
    message: GameMessage,
    *,
    matchmaking: MatchmakingCoordinator,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    preparation = matchmaking.preparations.get(session.online_id)
    match_id = int(preparation.get("match_id", 0)) if preparation else 0
    # A matched client leaves central search before the selected host
    # creates and reports its NP room. Retain that bounded handoff;
    # only an unmatched waiting search is actually removed.
    match = matchmaking.matches.get(match_id)
    single_team = match is not None and match.get("kind") in (
        INTERWAR_BOSS,
        ACTIVE_SINGLE_TEAM,
    )
    retained_match = match_id > 0 and match is not None and not single_team
    host_retry_armed = bool(
        retained_match
        and match.get("host_result_sent")
        and not match.get("room_published")
        and not match.get("counterparts_released")
        and not match.get("result_sha256")
        and match.get("members")
        and match["members"][0] is preparation
        and preparation.get("session") is session
    )
    if host_retry_armed:
        # Retail normally leaves central search while the chosen host creates
        # its RPCN room. If it registers the exact search again before a room
        # exists, that later request is evidence that this transition failed.
        match["host_transition_cancelled"] = True
    if single_team:
        matchmaking._remove_matchmaking_state(match_id)
    if not retained_match:
        removed = matchmaking.preparations.pop(session.online_id, None)
        if removed is not None:
            matchmaking._cancel_single_team_fallback(removed)
    reply(b"")
    telemetry.emit(
        "game_cancel_prepare_accepted",
        peer=session.peer,
        online_id=session.online_id,
        prepare_id=int(preparation.get("prepare_id", 0)) if preparation else 0,
        match_id=match_id,
        retained_match=retained_match,
        host_retry_armed=host_retry_armed,
    )


def report_disconnect(
    session: GameSession,
    message: GameMessage,
    *,
    matchmaking: MatchmakingCoordinator,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    try:
        report = decode_battle_disconnect(message.payload)
    except ProtocolError as error:
        telemetry.emit(
            "game_message_rejected",
            online_id=session.online_id,
            message_type=message.message_type,
            reason=str(error),
        )
        return
    match_id, removed_online_ids = matchmaking.remove_reported_battle(session, report)
    reply(b"")
    telemetry.emit(
        "game_battle_session_disconnect_reported",
        peer=session.peer,
        online_id=session.online_id,
        match_id=match_id,
        host_prepare_id=report.host_prepare_id,
        detail32=report.detail32,
        detail64=report.detail64,
        reported_player_ids=[report.player_id],
        removed_online_ids=removed_online_ids,
    )


def report_disconnected(
    session: GameSession,
    message: GameMessage,
    *,
    matchmaking: MatchmakingCoordinator,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    try:
        report = decode_report_disconnected(message.payload)
    except ProtocolError as error:
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            message_type=message.message_type,
            reason=str(error),
        )
        return
    preparation = matchmaking.preparations.get(session.online_id)
    reply(b"")
    telemetry.emit(
        "game_disconnected_reported",
        peer=session.peer,
        online_id=session.online_id,
        reported_id=report.reported_id,
        detail32=report.detail32,
        matches_current_preparation=bool(
            preparation is not None
            and int(preparation.get("prepare_id", 0)) == report.reported_id
            and preparation.get("session") is session
        ),
    )


def report_briefing_end(
    session: GameSession,
    message: GameMessage,
    *,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    try:
        detail64 = decode_briefing_end(message.payload)
    except ProtocolError as error:
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            message_type=message.message_type,
            reason=str(error),
        )
        return
    # The retail response has no registered payload type.  Field 1 varies in
    # native traffic, but no client-side consumer or match-state meaning has
    # been established, so preserve it as telemetry rather than guessing.
    reply(b"")
    telemetry.emit(
        "game_briefing_end_reported",
        peer=session.peer,
        online_id=session.online_id,
        detail64=detail64,
    )


def notify_room(
    session: GameSession,
    message: GameMessage,
    *,
    matchmaking: MatchmakingCoordinator,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    try:
        fields = decode_proto(message.payload)
        keys = [field for field in fields if field.number == 1]
        archives = [field for field in fields if field.number == 2]
        if (
            len(keys) != 1
            or keys[0].wire_type != 0
            or len(archives) != 1
            or archives[0].wire_type != 2
        ):
            raise ValueError("Room notification requires one preparation ID and one archive")
        matchmaking.publish_native_room(
            session,
            prepare_id=keys[0].value,
            encoded=archives[0].value,
            reply=reply,
        )
    except (ProtocolError, ValueError) as error:
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            message_type=message.message_type,
            reason=str(error),
        )
