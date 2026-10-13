"""Team/profile views and session-membership message encoding.

This module reads supplied records without mutating membership. Persistent team
policy belongs to TeamStore; live membership and notification ordering belong
to the request lifecycle. Global player IDs and session ordinals are distinct."""

from __future__ import annotations

import base64
import struct
from datetime import datetime

from .protocol import _proto_datetime, encode_varint, proto_bytes, proto_varint
from .team_medals import owned_medals, title_is_owned
from .team_progression import view as team_progress_view
from .team_records import TeamRecord

# One-based server API values. The exact ELF factory uses wire IDs 8 and 9.
# Wire 7 is ChangeSessionHost, NOT LeaveSessionMember. Type-name order is not
# dispatch order. See evidence/2026-09-13-session-push-routing-audit.json.
SESSION_MEMBER_JOIN_PUSH = 9
SESSION_MEMBER_LEAVE_PUSH = 10
# PushRequestJoinSessionMember field 4 is a four-value native peer-state enum,
# not the durable team member number.  The exact client converter accepts
# 1/2/3 and normalizes every other value to 0; the networking consumer treats
# 0 as the inactive path.  Value 1 is the native-proven active state.
SESSION_MEMBER_PEER_ACTIVE = 1


def _team_rank_info(team: dict | None = None) -> bytes:
    """Encode the native rank glyph and current/required rank points."""
    return b"".join(
        proto_varint(number, value)
        for number, value in enumerate(team_progress_view(team or {}).wire_values, 1)
    )


def _team_update_date_info(now: datetime, online_id: str) -> bytes:
    """Retail UpdateDateInfo: scalar, two identities, DateTime, identity."""
    return (
        proto_varint(1, 0)
        + proto_bytes(2, online_id)
        + proto_bytes(3, online_id)
        + proto_bytes(4, _proto_datetime(now))
        + proto_bytes(5, online_id)
    )


def _team_color_argb32() -> bytes:
    """Retail ColorARGB32: four required scalar channels, all clear for a new team."""
    return b"".join(proto_varint(number, 0) for number in range(1, 5))


def _retained_team_property(team: dict, field: int, key: str, default: int = 0) -> int:
    """Recover accepted sparse edits without rewriting historical team records."""
    properties = team.get("changeable_properties", {})
    value = properties.get(str(field)) if isinstance(properties, dict) else None
    return value if type(value) is int and 0 <= value <= 0xFFFFFFFF else int(team.get(key, default))


def _team_info(team: dict, now: datetime) -> bytes:
    online_id = str(team["leader_online_id"])
    # Native title selections are request field13 (e.g. 08036801), not
    # field20. Recover previously accepted selections from the retained sparse
    # request without rewriting public records during a read or deployment.
    properties = team.get("changeable_properties", {})
    retained_title = properties.get("13") if isinstance(properties, dict) else None
    title_id = team.get("title_id", 0)
    if type(retained_title) is int and 0 <= retained_title <= 32:
        title_id = retained_title
    if not title_is_owned(team, title_id):
        title_id = 0
    rank_info = _team_rank_info(team)
    update_date_info = _team_update_date_info(now, online_id)
    try:
        color_info = base64.b64decode(str(team.get("team_color_b64", "")), validate=True)
    except (ValueError, base64.binascii.Error):
        color_info = b""
    if not color_info:
        color_info = _team_color_argb32()
    profile_text = str(team.get("profile_text", "")).encode("utf-8")
    member_records = [
        record for record in team.get("member_records", []) if isinstance(record, dict)
    ] or [
        {
            "online_id": online_id,
            "player_id": int(team["leader_player_id"]),
            "leader": True,
        }
    ]
    members = b"".join(
        proto_bytes(
            28,
            proto_varint(1, int(record.get("player_id", 0)))
            # Retail serializes this as an integer membership-status enum,
            # not a leader boolean.  Team leadership is already identified
            # by TeamInfo field 2; every active roster entry uses status 1.
            + proto_varint(2, int(record.get("member_status", 1)))
            + proto_varint(3, int(record.get("member_team_secondary", 0)))
            + proto_varint(4, int(record.get("member_id", 0))),
        )
        for record in member_records
    )
    return (
        proto_varint(1, int(team["team_id"]))
        + proto_varint(2, int(team["leader_player_id"]))
        # Enrollment is CreateTeam field 7 / leader-update field 1.
        # Return the same policy that admission uses, not the old nationality
        # alias. Native Free=2 and Closed=0 controls establish this pairing.
        + proto_varint(3, int(team.get("enlistment_method", 0)))
        + proto_bytes(4, str(team["leader_online_id"]))
        + proto_bytes(5, str(team["name"]))
        + proto_varint(6, int(team.get("current_faction_id", 0)))
        # Exact consumer: protobuf field7 -> SDK+0xd0 -> team model+0x5a4
        # -> @HopeFaction. A points total here becomes an unrelated FMG ID.
        + proto_varint(7, int(team.get("desired_faction_id", 1)))
        + proto_varint(8, int(team.get("territory_area_id", 0)))
        + proto_varint(9, _retained_team_property(team, 10, "play_type"))
        + proto_varint(10, int(team.get("operation_base", 0)))
        + proto_varint(11, int(team.get("language", 0)))
        # Leader-settings field 3 is zero-based; TeamInfo field 12 reserves
        # zero and reads back the setting with a one-value offset.
        + proto_varint(12, int(team.get("pay_apportionment", 0)) + 1)
        # TeamInfo field 13 is the separate Normal-earned Special counter.
        # TeamRankInfo fields 3/4 above belong to rank-point presentation.
        + proto_varint(13, team_progress_view(team).special_sortie_progress)
        + proto_bytes(14, str(team.get("comment", "")))
        + proto_varint(15, int(team.get("enlistment_method", 0)))
        + proto_bytes(16, update_date_info)
        + proto_bytes(19, color_info)
        + proto_bytes(20, update_date_info)
        + proto_varint(21, int(team.get("soldier_id", 0)))
        + proto_bytes(22, update_date_info)
        # Native edit/readback: request field20 -> Sortie Text in field25.
        # Keep the unrelated compatibility field27 unchanged.
        + proto_varint(25, _retained_team_property(team, 20, "sortie_text_id"))
        + proto_bytes(26, profile_text)
        + proto_varint(27, int(team.get("desired_faction_id", 1)))
        + members
        + proto_varint(29, int(team.get("desired_area_id", 0)))
        + proto_bytes(30, rank_info)
        + proto_varint(31, int(title_id))
    )


def _user_info(
    player_id: int,
    online_id: str,
    team: dict | None,
    status: str = "",
    member_id: int = 0,
    language: int | None = None,
    penalized: bool = False,
    team_session_member: bool = False,
    *,
    pilot_name: bytes | str | None = None,
    operation_base: int | None = None,
    play_type: int | None = None,
) -> bytes:
    """Encode peer data; explicit field4 name overrides the legacy fourth argument.

    The old ``status`` argument is retained for existing helper callers. Native
    field4 is the pilot name, not Status text; qualified request handlers supply
    the validated target name explicitly. No replacement status field is guessed.
    Request handlers also supply personal selectors, including explicit zero
    for unknown values. None retains the older standalone helper interface.
    The retail converter maps field6 to the team identity and field9 to
    Operation Base. Field10 is Language. These are not the self-response
    field numbers; putting team_id in field9 overwrites the selected country.
    """
    team_id = int(team["team_id"]) if team is not None else 0
    return (
        proto_varint(1, player_id)
        + proto_bytes(2, online_id)
        + proto_bytes(3, online_id)
        + proto_bytes(4, status if pilot_name is None else pilot_name)
        + proto_varint(6, team_id)
        + proto_varint(7, 0)
        + proto_varint(9, int((team or {}).get("operation_base", 0))
                       if operation_base is None else operation_base)
        + proto_varint(10, int(language) if language is not None else 0)
        + proto_varint(11, int((team or {}).get("play_type", 0))
                       if play_type is None else play_type)
        + proto_varint(12, max(0, int(member_id)))
        + proto_varint(13, int(language) if language is not None else 0)
        # Field 14 drives ACVD's @Penalized roster state.  Treating it as an
        # online-presence flag paints every connected member red and prevents
        # Normal/Special Sortie registration with an "active penalty" error.
        + proto_varint(14, 1 if penalized else 0)
        + proto_varint(15, 1 if team_session_member else 0)
    )


def _team_member_comment_list_response(comments: list[tuple[int, bytes]]) -> bytes:
    """Retail TeamMemberComment requires global player ID and string, even empty."""
    return b"".join(
        proto_bytes(1, proto_varint(1, player_id) + proto_bytes(2, comment))
        for player_id, comment in comments
    )


def _team_ranking_info(team: dict, *, rank: int = 0, score: int | None = None) -> bytes:
    if score is None:
        score = int(team.get("team_fame_points", 0))
    return (
        proto_varint(1, max(0, int(team.get("team_id", 0))))
        + proto_bytes(2, str(team["name"]))
        + proto_varint(3, max(0, int(score)))
        + proto_varint(4, max(0, int(rank)))
    )


def _team_ranking_list_response(rows: tuple[dict, ...], season_id: int) -> bytes:
    return proto_varint(1, season_id) + b"".join(
        proto_bytes(
            2,
            _team_ranking_info(
                {"name": row["name"], "team_id": row["team_id"]},
                rank=row["rank"],
                score=row["score"],
            ),
        )
        for row in rows
    )


def _team_id_search_response(team_ids: list[int]) -> bytes:
    """Retail RequestSearchTeamIDListResponse: repeated uint32 field 1."""
    return b"".join(proto_varint(1, team_id) for team_id in team_ids)


def _team_info_list_response(teams: list[dict], now: datetime) -> bytes:
    return b"".join(proto_bytes(1, _team_info(team, now)) for team in teams)


def _team_session_response(team: dict, profile: dict, player_id: int) -> bytes:
    """Return CreateTeamSession's required session and member identifiers."""
    external_session_id = max(1, int(team.get("team_id", player_id)))
    # This is the member's ordinal inside the team session, not their global
    # player ID.  Large player IDs are structurally valid uint32 values but do
    # not describe an NP Matching room member and prevent the client from
    # creating its team room.
    external_member_id = max(1, int(profile.get("member_id", 1)))
    return proto_varint(1, external_session_id) + proto_varint(2, external_member_id)


def _team_session_member_push(
    session_id: int,
    player_id: int,
    member_np_identity: bytes,
    online_id: str,
) -> bytes:
    """Retail PushRequestJoinSessionMember for one announced member.

    RequestCreateTeamSession field 1 is the creator's 37-byte serialized NP
    identity. The concrete push handler deserializes its field-5 string as the
    member's NP identity; field 3 is the corresponding online ID. Reusing one
    creator identity in both directions makes an existing
    client initialize signaling against itself. Field 2 is the global player
    ID. Field 4 is a bounded peer-state enum, not the player's persistent team
    number; sending member numbers above 3 makes the client normalize the peer
    to inactive and can hide that player after a recovery cycle.
    """
    if not member_np_identity:
        raise ValueError("team-session member NP identity must not be empty")
    return (
        proto_varint(1, max(1, int(session_id)))
        + proto_varint(2, max(1, int(player_id)))
        + proto_bytes(3, online_id)
        + proto_varint(4, SESSION_MEMBER_PEER_ACTIVE)
        + proto_bytes(5, bytes(member_np_identity))
    )


def _team_session_leave_push(session_id: int, player_id: int) -> bytes:
    """Retail PushRequestLeaveSessionMember (server subtype 10, wire type 9).

    Field 2 is the global player ID, not the team-slot number. The native
    consumer at 0x015ed3ec passes it to 0x015d9108, which searches peer+0xd4.
    Join field 2 supplies that peer field through 0x0184d424 and 0x015eedc4.
    This removes a signaling peer; visible roster cleanup is a separate test.
    """
    return proto_varint(1, max(1, int(session_id))) + proto_varint(2, max(1, int(player_id)))


def _team_session_join_response(team_session: dict) -> bytes:
    """Return RequestJoinTeamSession's repeated SessionMemberInfo list."""
    members = team_session.get("members", {})
    if not isinstance(members, dict):
        return b""
    response = bytearray()
    for index, (online_id, record) in enumerate(sorted(members.items())):
        if not isinstance(record, dict):
            continue
        identity = bytes(record.get("np_identity", b""))
        if not identity:
            continue
        member = (
            proto_varint(1, max(1, int(record.get("player_id", 1))))
            + proto_bytes(2, identity)
            + proto_varint(3, max(1, int(record.get("member_id", 1))))
            + proto_varint(4, 1 if index == 0 else 0)
        )
        response.extend(proto_bytes(1, member))
    return bytes(response)


def _team_session_member_push_pair(
    session_id: int,
    *,
    existing_online_id: str,
    existing_member: dict,
    new_online_id: str,
    new_player_id: int,
    new_np_identity: bytes,
) -> tuple[bytes, bytes]:
    """Return announcements for the new and existing clients, respectively."""
    existing_np_identity = bytes(existing_member.get("np_identity", b""))
    return (
        _team_session_member_push(
            session_id,
            int(existing_member["player_id"]),
            existing_np_identity,
            existing_online_id,
        ),
        _team_session_member_push(
            session_id,
            new_player_id,
            new_np_identity,
            new_online_id,
        ),
    )


def _team_record(record: TeamRecord = TeamRecord()) -> bytes:
    """Populate the proved Team Data scalars and five newest versus rows."""
    # Recovered from RequestGetTeamRecordResponse::MergePartialFromCodedStream
    # and IsInitialized in the retail EBOOT. Fields 1, 2, 3, 5, and 6 are
    # required; field 3 is fixed32 and field 4 is repeated VersusRecord.
    return (
        proto_varint(1, record.sorties)
        + proto_varint(2, record.wins)
        + encode_varint((3 << 3) | 5)
        + struct.pack(">I", 0)
        + b"".join(proto_bytes(4, proto_varint(1, 0 if won else 1)
                               + proto_bytes(2, name) + proto_varint(3, 0))
                   for won, name in record.history)
        + proto_varint(5, 0)  # Unqualified; not one of the three visible counts.
        + proto_varint(6, record.pvp_wins)
    )


def _vote_kick_member_response(member_id: int = 0) -> bytes:
    """Required current vote-kick target; zero means no active target."""
    return proto_varint(1, max(0, member_id))


def _team_medal_info(team: dict | None = None) -> bytes:
    """Initialize the actual 0x03FD summary and its field-7 medal list.

    Name-linked vtable0x01d144a8, writer0x018cc2a8 and IsInitialized0x01897718
    require scalars1..6/8 (presence mask0x7f). The medal vector is field7.
    The earlier repeated-field1 interpretation targeted a different message.
    Keep unqualified summary counters neutral; never guess their order from
    our storage names. Ownership and authoritative backend totals are retained.
    """
    medals = owned_medals(team)
    return (
        b"".join(proto_varint(number, 0) for number in range(1, 7))
        + b"".join(proto_varint(7, medal_id) for medal_id in medals)
        + proto_varint(8, 0)
    )
