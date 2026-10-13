"""Team discovery and member views, with explicit live-presence input.

Search discovery honors the requested enlistment methods; an unfiltered
internal list may still include closed teams. Admission remains a join concern.
Presence comes from fresh established sessions, not durable member metadata.
No query creates a team or rewrites membership.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Mapping

from ..profile_values import personal_selector, pilot_name_bytes
from ..protocol import decode_proto, first_field, proto_bytes
from ..team_messages import (
    _team_id_search_response,
    _team_info,
    _team_info_list_response,
    _team_member_comment_list_response,
    _user_info,
)
from ..teams import TeamStore
from ..telemetry import Telemetry
from ..udp import GameMessage, GameSession


def get_team_info(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    team = team_store.team_for(session.online_id)
    if team is None:
        reply(b"")
        telemetry.emit(
            "game_team_info_missing",
            peer=session.peer,
            online_id=session.online_id,
        )
    else:
        response = proto_bytes(1, _team_info(team, datetime.now(timezone.utc)))
        reply(response)
        telemetry.emit(
            "game_team_info_sent",
            peer=session.peer,
            online_id=session.online_id,
            team_id=team["team_id"],
            team_name=team["name"],
        )


def get_user_info(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    game_sessions: Mapping[bytes, GameSession],
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    player_field = first_field(fields, 1)
    requested_player_id = (
        int(player_field.value)
        if player_field is not None and isinstance(player_field.value, int)
        else session.player_id
    )
    member = team_store.member_by_player_id(requested_player_id)
    if member is None and requested_player_id == session.player_id:
        member = (session.online_id, team_store.team_for(session.online_id))
    online_id, team = member if member is not None else ("", None)
    profile = team_store.player_profile(online_id)
    member_online = any(
        candidate.online_id == online_id
        and candidate.established
        and candidate.peer is not None
        and time.monotonic() - candidate.last_seen_monotonic <= 90.0
        for candidate in game_sessions.values()
    )
    response = proto_bytes(
        1,
        _user_info(
            requested_player_id,
            online_id,
            team,
            str(profile.get("status", "")),
            int(profile.get("member_id", 0)),
            int(profile["language"]) if "language" in profile else None,
            False,
            # UserInfo field 15 is the live team-session/presence
            # state.  The old code computed this value above, then
            # accidentally serialized the unrelated persisted
            # secondary-member scalar instead, rendering every
            # remote member offline even while their encrypted game
            # session was actively ACKing pushes.
            member_online,
            pilot_name=pilot_name_bytes(profile, online_id),
            operation_base=personal_selector(profile, 5),
            play_type=personal_selector(profile, 7),
        ),
    )
    reply(response)
    telemetry.emit(
        "game_user_info_by_player_id_sent",
        peer=session.peer,
        online_id=session.online_id,
        requested_online_id=online_id,
        player_id=requested_player_id,
        team_id=team["team_id"] if team is not None else 0,
        member_online=member_online,
    )


def get_team_member_comments(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    """Read known comments for the caller's durable team without repairing data."""
    team = team_store.team_for(session.online_id)
    owners: dict[int, str] = {}
    ambiguous: set[int] = set()
    records = (team or {}).get("member_records", [])
    skipped = 0
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            skipped += 1
            continue
        owner, player_id = record.get("online_id"), record.get("player_id")
        if (
            not isinstance(owner, str)
            or not owner
            or team_store.team_for(owner) is not team
            or type(player_id) is not int
            or not 0 < player_id <= 0xFFFFFFFF
        ):
            skipped += 1
            continue
        profile_id = team_store.player_profile(owner).get("player_id", player_id)
        if type(profile_id) is not int or profile_id != player_id:
            skipped += 1
            continue
        if player_id in owners and owners[player_id] != owner:
            ambiguous.add(player_id)
        else:
            owners[player_id] = owner
    comments = []
    for player_id, owner in owners.items():
        if player_id in ambiguous:
            continue
        value = team_store.player_profile(owner).get("status")
        if not isinstance(value, str):
            skipped += 1
            continue
        try:
            comment = value.encode("utf-8")
        except UnicodeError:
            skipped += 1
            continue
        # An explicit blank is known data; absence/corruption must not invent a clear.
        comments.append((player_id, comment))
    reply(_team_member_comment_list_response(comments))
    telemetry.emit(
        "game_team_member_comments_sent",
        peer=session.peer,
        online_id=session.online_id,
        team_id=int(team["team_id"]) if team is not None else 0,
        count=len(comments),
        skipped_records=skipped,
        ambiguous_ids=len(ambiguous),
    )


def search_team_ids(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    requested_name = ""
    enlistment_methods: set[int] = set()
    for criterion in (
        field for field in fields if field.number == 3 and isinstance(field.value, bytes)
    ):
        nested = decode_proto(criterion.value)
        selector = first_field(nested, 1)
        if selector is None or not isinstance(selector.value, int):
            # Preserve the older recovered name-only form while preferring the
            # now-captured selector1 request emitted by the retail search UI.
            name_field = first_field(nested, 2)
            if name_field is not None and isinstance(name_field.value, bytes):
                requested_name = name_field.value.decode("utf-8", "replace")
            continue
        if selector.value == 1:
            name_field = first_field(nested, 2)
            if name_field is not None and isinstance(name_field.value, bytes):
                requested_name = name_field.value.decode("utf-8", "replace")
        elif selector.value == 7:
            method_field = first_field(nested, 8)
            if method_field is not None and isinstance(method_field.value, int):
                enlistment_methods.add(int(method_field.value))

    def scalar(number: int, default: int) -> int:
        field = first_field(fields, number)
        return int(field.value) if field is not None and isinstance(field.value, int) else default

    # Captured retail search uses field1=32 as its result bound, field2 as the
    # page offset and field4=1 for the default newest-first order. TeamSearchItem
    # selector7 carries the one-based Screening/Free/Password method in field8.
    # Closed method0 is deliberately absent: the original Tip103 calls it
    # unsearchable. Apply these before pagination so a new Free team is not
    # hidden behind older closed/legacy rows in the client's five-row fetch.
    limit = max(1, min(32, scalar(1, 32)))
    offset = max(0, min(1_000_000, scalar(2, 0)))
    sort_order = scalar(4, 1)
    team_ids = team_store.search_team_ids(name=requested_name, open_only=False)
    if enlistment_methods:
        team_ids = [
            team_id
            for team_id in team_ids
            if int((team_store.team_by_id(team_id) or {}).get("enlistment_method", 0))
            in enlistment_methods
        ]
    if sort_order == 1:
        # Team IDs are monotonic creation IDs. Existing records predate durable
        # per-team update timestamps, so this is the only authoritative recency
        # order shared by old and new rows.
        team_ids.reverse()
    team_ids = team_ids[offset : offset + limit]
    reply(_team_id_search_response(team_ids))
    telemetry.emit(
        "game_team_id_search_sent",
        peer=session.peer,
        online_id=session.online_id,
        requested_name=requested_name,
        enlistment_methods=sorted(enlistment_methods),
        limit=limit,
        offset=offset,
        sort_order=sort_order,
        team_ids=team_ids,
        count=len(team_ids),
    )


def get_team_info_list(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    requested_ids = [
        int(field.value) for field in fields if field.number == 1 and isinstance(field.value, int)
    ]
    # RequestGetTeamInfoList is normally an empty request.  The
    # previous implementation treated that as "return no teams",
    # which made every successful ID search render an empty result.
    if not requested_ids:
        requested_ids = team_store.search_team_ids(open_only=False)
    teams = [
        team for team_id in requested_ids if (team := team_store.team_by_id(team_id)) is not None
    ]
    reply(_team_info_list_response(teams, datetime.now(timezone.utc)))
    telemetry.emit(
        "game_team_info_list_sent",
        peer=session.peer,
        online_id=session.online_id,
        requested_team_ids=requested_ids,
        returned_team_ids=[team["team_id"] for team in teams],
        count=len(teams),
    )


def get_vote_status(
    session: GameSession,
    message: GameMessage,
    *,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    reply(b"")
    telemetry.emit(
        "game_team_vote_status_sent",
        peer=session.peer,
        online_id=session.online_id,
        count=0,
    )


def get_joining_status(
    session: GameSession,
    message: GameMessage,
    *,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    page_field = first_field(fields, 1)
    offset = 0
    limit = 0
    if page_field is not None and isinstance(page_field.value, bytes):
        page = decode_proto(page_field.value)
        offset_field = first_field(page, 1)
        limit_field = first_field(page, 2)
        offset = (
            int(offset_field.value)
            if offset_field is not None and isinstance(offset_field.value, int)
            else 0
        )
        limit = (
            int(limit_field.value)
            if limit_field is not None and isinstance(limit_field.value, int)
            else 0
        )
    # Response field 1 is repeated pending TeamStatus; an empty list
    # means this account has no vote-based join currently in flight.
    reply(b"")
    telemetry.emit(
        "game_joining_team_status_sent",
        peer=session.peer,
        online_id=session.online_id,
        offset=max(0, offset),
        limit=max(0, limit),
        count=0,
    )
