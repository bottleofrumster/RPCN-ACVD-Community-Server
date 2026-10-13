"""World, season and ranking request handling.

These retain the existing world compatibility defaults and project only
durable, attributable community ranking facts.
Despite its GET name, faction-result handling currently promotes team faction
state before replying; keep that side effect explicit and covered by tests.
Unknown historical ranking data remains unknown rather than becoming zero.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from .. import verdict_war
from ..protocol import ProtocolError, _proto_datetime, decode_proto, first_field, proto_bytes
from ..rankings import (
    decode_page,
    decode_self_season,
    defeat_rows,
    own_row,
    page,
    player_rows,
    team_rows,
)
from ..team_medals import owned_medals
from ..team_messages import (
    _team_medal_info,
    _team_ranking_info,
    _team_ranking_list_response,
    _team_record,
)
from ..team_news import decode_query, encode_events, stored_events, wire_notice_id
from ..team_records import team_record
from ..teams import TeamStore
from ..telemetry import Telemetry
from ..udp import GameMessage, GameSession
from ..world_messages import (
    CURRENT_SEASON_ID,
    WORLD_PHASE_ACTIVE,
    _defeat_ranking_list_response,
    _empty_ranking_list_response,
    _faction_selection,
    _faction_struggle_results,
    _my_defeat_ranking_response,
    _my_player_ranking_response,
    _player_ranking_list_response,
    _world_basic_info,
    _world_current_info,
    _world_log,
)


def get_server_datetime(
    session: GameSession,
    message: GameMessage,
    *,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    now = datetime.now(timezone.utc)
    reply(proto_bytes(1, _proto_datetime(now)))
    telemetry.emit(
        "game_server_datetime_sent",
        peer=session.peer,
        online_id=session.online_id,
        timestamp_utc=now.isoformat(),
    )


def get_world_basic_info(
    session: GameSession,
    message: GameMessage,
    *,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    phase: int = WORLD_PHASE_ACTIVE,
    cq_map_settings: dict | None = None,
    war: dict | None = None,
) -> None:
    if war is not None:
        phase = war["phase"]
    now = datetime.now(timezone.utc)
    reply(
        proto_bytes(
            1,
            _world_basic_info(
                now,
                phase=phase,
                cq_map_settings=cq_map_settings,
                war=war,
            ),
        )
    )
    telemetry.emit(
        "game_world_basic_info_sent",
        peer=session.peer,
        online_id=session.online_id,
        world_id=1,
        season_id=war["season"] if war else CURRENT_SEASON_ID,
        phase=phase,
        cq_map_mode="test_war" if war else (cq_map_settings or {}).get("cq_map_mode", "territory"),
        timestamp_utc=now.isoformat(),
    )


def get_faction_selection(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    team = teams.team_for(session.online_id)
    selected_faction_id = int(team.get("faction_id", 0)) if team is not None else 0
    reply(_faction_selection(selected_faction_id))
    telemetry.emit(
        "game_faction_selection_sent",
        peer=session.peer,
        online_id=session.online_id,
        faction_ids=[1, 2, 3],
        selected_faction_id=selected_faction_id,
    )


def get_faction_struggle_result(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    promoted_team = teams.promote_faction(session.online_id)
    war = teams.test_war_snapshot()
    reply(_faction_struggle_results(war["last_result"] if war else None))
    telemetry.emit(
        "game_faction_struggle_result_sent",
        peer=session.peer,
        online_id=session.online_id,
        world_id=1,
        season_id=war["last_result_season"] if war else CURRENT_SEASON_ID,
        faction_ids=[1, 2, 3],
        current_faction_id=(promoted_team or {}).get("current_faction_id", 0),
        territory_area_id=(promoted_team or {}).get("territory_area_id", 0),
    )


def get_world_current_info(
    session: GameSession,
    message: GameMessage,
    *,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    phase: int = WORLD_PHASE_ACTIVE,
    cq_map_settings: dict | None = None,
    war: dict | None = None,
) -> None:
    if war is not None:
        phase = war["phase"]
    now = datetime.now(timezone.utc)
    reply(
        proto_bytes(
            1,
            _world_current_info(
                now,
                phase=phase,
                cq_map_settings=cq_map_settings,
                war=war,
            ),
        )
    )
    if war is not None:
        session.test_war_view = {
            "generation": war["generation"],
            "season": war["season"],
            "levels": {
                area["area_id"]: verdict_war.level(area, war["rules"]) for area in war["areas"]
            },
        }
    telemetry.emit(
        "game_world_current_info_sent",
        peer=session.peer,
        online_id=session.online_id,
        world_id=1,
        season_id=war["season"] if war else CURRENT_SEASON_ID,
        phase=phase,
        cq_map_mode="test_war" if war else (cq_map_settings or {}).get("cq_map_mode", "territory"),
        timestamp_utc=now.isoformat(),
    )


def get_notice_list(
    session: GameSession,
    message: GameMessage,
    *,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    teams: TeamStore,
    reply_error: Callable[[int, int], None],
) -> None:
    try:
        query = decode_query(message.payload)
        team = teams.team_for(session.online_id)
        rows = []
        if team is not None:
            owner = teams.member_by_player_id(session.player_id)
            if owner is None or owner[0] != session.online_id:
                raise ValueError("Team News account identity changed")
            pending = [row for row in stored_events(team)
                       if wire_notice_id(row["id"]) > query.after_id]
            rows = pending[-query.limit:] if query.limit else []
        payload = encode_events(rows)
    except (ValueError, TypeError) as error:
        telemetry.emit("game_notice_list_rejected", online_id=session.online_id, reason=str(error))
        reply_error(1, 28)
        return
    reply(payload)
    telemetry.emit(
        "game_notice_list_sent",
        peer=session.peer,
        online_id=session.online_id,
        count=len(rows),
        team_id=(team or {}).get("team_id", 0),
        after_event_id=query.after_id,
        requested_limit=query.limit,
        event_ids=[wire_notice_id(row["id"]) for row in rows],
        event_types=[row["type"] for row in rows],
    )


def get_world_log(
    session: GameSession,
    message: GameMessage,
    *,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    phase: int = WORLD_PHASE_ACTIVE,
    cq_map_settings: dict | None = None,
    war: dict | None = None,
) -> None:
    if war is not None:
        phase = war["phase"]
    now = datetime.now(timezone.utc)
    request_fields = decode_proto(message.payload)
    phase_field = first_field(request_fields, 2)
    requested_phase = (
        int(phase_field.value)
        if phase_field is not None and not isinstance(phase_field.value, bytes)
        else 0
    )
    has_current_entry = requested_phase < phase
    reply(
        _world_log(
            now,
            requested_phase,
            phase=phase,
            cq_map_settings=cq_map_settings,
            war=war,
        )
    )
    telemetry.emit(
        "game_world_log_sent",
        peer=session.peer,
        online_id=session.online_id,
        world_id=1,
        season_id=war["season"] if war else CURRENT_SEASON_ID,
        requested_phase=requested_phase,
        world_phase_count=int(has_current_entry),
        area_phase_count=0,
        cq_map_mode="test_war" if war else (cq_map_settings or {}).get("cq_map_mode", "territory"),
        timestamp_utc=now.isoformat(),
    )


def get_team_record(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    reply_error: Callable[[int, int], None] | None = None,
) -> None:
    try:
        fields = decode_proto(message.payload)
        if fields and (len(fields) != 1 or fields[0].number != 1
                       or fields[0].wire_type != 0 or not 0 < fields[0].value < 1 << 32):
            raise ValueError("Team Record query requires one team ID")
        team_id = (fields[0].value if fields else
                   int((teams.team_for(session.online_id) or {}).get("team_id", 0)))
        record = team_record(teams.state, team_id)
    except (TypeError, ValueError) as error:
        telemetry.emit("game_team_record_rejected", online_id=session.online_id, reason=str(error))
        if reply_error is not None:
            reply_error(1, 28)
        else:
            reply(_team_record())
        return
    reply(_team_record(record))
    telemetry.emit(
        "game_team_record_sent",
        peer=session.peer,
        online_id=session.online_id,
        team_id=team_id,
        sorties=record.sorties,
        wins=record.wins,
        pvp_wins=record.pvp_wins,
        wins_complete=record.wins_complete,
        versus_count=len(record.history),
    )


def get_team_medal_info(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    team = teams.refresh_team_medal_state(session.online_id)
    medals = owned_medals(team)
    reply(_team_medal_info(team))
    telemetry.emit(
        "game_my_team_medal_info_sent",
        peer=session.peer,
        online_id=session.online_id,
        team_id=(team or {}).get("team_id", 0),
        medal_count=len(medals),
        team_ac_kills=(team or {}).get("team_ac_kills", 0),
        team_fame_points=(team or {}).get("team_fame_points", 0),
        team_player_medals=(team or {}).get("team_player_medals", 0),
        team_world_sorties=(team or {}).get("team_world_sorties", 0),
        team_pvp_wins=(team or {}).get("team_pvp_wins", 0),
        team_best_ranking=(team or {}).get("team_best_ranking", 0),
    )


def get_team_ranking_list(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    try:
        request = decode_page(message.payload)
        rows = team_rows(teams) if request.season_id == teams.world_season_id() else ()
    except (ProtocolError, ValueError) as error:
        reply(_team_ranking_list_response((), 0))
        telemetry.emit(
            "game_ranking_rejected",
            peer=session.peer,
            online_id=session.online_id,
            message_type=message.message_type,
            reason=str(error),
        )
        return
    selected = page(rows, request)
    reply(_team_ranking_list_response(selected, request.season_id))
    telemetry.emit(
        "game_team_ranking_list_sent",
        peer=session.peer,
        online_id=session.online_id,
        season_id=request.season_id,
        offset=request.offset,
        count=len(selected),
        total=len(rows),
    )


def get_empty_ranking_list(
    session: GameSession,
    message: GameMessage,
    *,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    reply(_empty_ranking_list_response())
    telemetry.emit(
        "game_empty_ranking_list_sent",
        peer=session.peer,
        online_id=session.online_id,
        message_type=message.message_type,
        count=0,
    )


def get_player_ranking_list(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    try:
        request = decode_page(message.payload)
        rows = player_rows(teams) if request.season_id == teams.world_season_id() else ()
    except (ProtocolError, ValueError) as error:
        reply(_player_ranking_list_response((), 0))
        telemetry.emit(
            "game_ranking_rejected",
            peer=session.peer,
            online_id=session.online_id,
            message_type=message.message_type,
            reason=str(error),
        )
        return
    selected = page(rows, request)
    reply(_player_ranking_list_response(selected, request.season_id))
    telemetry.emit(
        "game_player_ranking_list_sent",
        peer=session.peer,
        online_id=session.online_id,
        season_id=request.season_id,
        offset=request.offset,
        count=len(selected),
        total=len(rows),
    )


def get_defeat_ranking_list(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    try:
        request = decode_page(message.payload)
        rows = defeat_rows(teams) if request.season_id == teams.world_season_id() else ()
    except (ProtocolError, ValueError) as error:
        reply(_defeat_ranking_list_response((), 0))
        telemetry.emit(
            "game_ranking_rejected",
            peer=session.peer,
            online_id=session.online_id,
            message_type=message.message_type,
            reason=str(error),
        )
        return
    selected = page(rows, request)
    reply(_defeat_ranking_list_response(selected, request.season_id))
    telemetry.emit(
        "game_defeat_ranking_list_sent",
        peer=session.peer,
        online_id=session.online_id,
        season_id=request.season_id,
        offset=request.offset,
        count=len(selected),
        total=len(rows),
    )


def get_my_player_ranking(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    try:
        season_id = decode_self_season(message.payload)
        rows = player_rows(teams) if season_id == teams.world_season_id() else ()
        row = own_row(rows, session.player_id)
    except (ProtocolError, ValueError) as error:
        reply(_my_player_ranking_response())
        telemetry.emit(
            "game_ranking_rejected",
            peer=session.peer,
            online_id=session.online_id,
            message_type=message.message_type,
            reason=str(error),
        )
        return
    reply(_my_player_ranking_response(row))
    telemetry.emit(
        "game_my_player_ranking_sent",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        season_id=season_id,
        rank=(row or {}).get("rank", 0),
        score=(row or {}).get("score", 0),
    )


def get_my_defeat_ranking(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    try:
        season_id = decode_self_season(message.payload)
        rows = defeat_rows(teams) if season_id == teams.world_season_id() else ()
        row = own_row(rows, session.player_id)
    except (ProtocolError, ValueError) as error:
        reply(b"")
        telemetry.emit(
            "game_ranking_rejected",
            peer=session.peer,
            online_id=session.online_id,
            message_type=message.message_type,
            reason=str(error),
        )
        return
    reply(_my_defeat_ranking_response(row))
    telemetry.emit(
        "game_my_defeat_ranking_sent",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        season_id=season_id,
        count=int(row is not None),
        rank=(row or {}).get("rank", 0),
        score=(row or {}).get("score", 0),
    )


def get_my_team_ranking(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    team = teams.team_for(session.online_id)
    try:
        season_id = decode_self_season(message.payload)
    except ProtocolError as error:
        reply(b"")
        telemetry.emit(
            "game_ranking_rejected",
            peer=session.peer,
            online_id=session.online_id,
            message_type=message.message_type,
            reason=str(error),
        )
        return
    row = None
    if team is not None:
        row = next(
            (
                candidate
                for candidate in (team_rows(teams) if season_id == teams.world_season_id() else ())
                if candidate["team_id"] == team.get("team_id")
            ),
            None,
        )
    if team is None or row is None:
        reply(b"")
        telemetry.emit(
            "game_my_team_ranking_missing",
            peer=session.peer,
            online_id=session.online_id,
            season_id=season_id,
        )
    else:
        reply(
            proto_bytes(
                1,
                _team_ranking_info(team, rank=row["rank"], score=row["score"]),
            )
        )
        telemetry.emit(
            "game_my_team_ranking_sent",
            peer=session.peer,
            online_id=session.online_id,
            team_id=team["team_id"],
            team_name=team["name"],
            season_id=season_id,
            rank=row["rank"],
            score=row["score"],
        )
