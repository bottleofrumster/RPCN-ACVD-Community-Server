"""Team/player emblem requests and imported-UGC validation.

Keep requested team/player identity distinct from the authenticated uploader.
Registration replies carry the assigned ID, not the request-shaped metadata.
Imported IDs are not automatically banned because this server lacks their data.
"""

from __future__ import annotations

from typing import Callable

from ..protocol import decode_proto, first_field, proto_bytes, proto_varint
from ..teams import TeamStore
from ..telemetry import Telemetry
from ..udp import GameMessage, GameSession
from ..ugc import (
    _emblem_response_payload,
    _register_emblem_response,
    _requested_team_for_emblem,
    _ugc_id_validation_response,
)


def get_team_emblem(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    team = _requested_team_for_emblem(
        teams,
        session.online_id,
        message.payload,
    )
    ugc_id = int(team.get("emblem_ugc_id", 0)) if team is not None else 0
    emblem = b""
    if team is not None and team.get("emblem_file"):
        emblem_path = teams.root / str(team["emblem_file"])
        if emblem_path.is_file():
            emblem = emblem_path.read_bytes()
    encoded_emblem = _emblem_response_payload(emblem)
    response = proto_bytes(1, encoded_emblem) + proto_varint(2, ugc_id) + proto_varint(3, 0)
    reply(response)
    telemetry.emit(
        "game_team_emblem_sent",
        peer=session.peer,
        online_id=session.online_id,
        team_id=team["team_id"] if team is not None else 0,
        ugc_id=ugc_id,
        emblem_length=len(encoded_emblem),
        fallback=False,
    )


def set_team_emblem(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    ugc_field = first_field(fields, 1)
    emblem_field = first_field(fields, 2)
    ugc_id = ugc_field.value if ugc_field is not None and isinstance(ugc_field.value, int) else 0
    emblem = (
        emblem_field.value
        if emblem_field is not None and isinstance(emblem_field.value, bytes)
        else b""
    )
    path = teams.save_emblem(session.online_id, ugc_id, emblem)
    reply(b"")
    telemetry.emit(
        "game_team_emblem_saved",
        peer=session.peer,
        online_id=session.online_id,
        ugc_id=ugc_id,
        emblem_length=len(emblem),
        path=path,
    )


def get_player_emblem(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
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
    member = teams.member_by_player_id(requested_player_id)
    requested_online_id = (
        member[0]
        if member is not None
        else (session.online_id if requested_player_id == session.player_id else "")
    )
    requested_team = member[1] if member is not None else teams.team_for(requested_online_id)
    stored = teams.player_emblem(requested_online_id) if requested_online_id else None
    ugc_id = int(stored.get("ugc_id", 0)) if stored is not None else 0
    emblem = bytes(stored.get("emblem", b"")) if stored is not None else b""
    if not emblem and requested_team is not None and requested_team.get("emblem_file"):
        emblem_path = teams.root / str(requested_team["emblem_file"])
        if emblem_path.is_file():
            emblem = emblem_path.read_bytes()
            ugc_id = int(requested_team.get("emblem_ugc_id", 0))
    encoded_emblem = _emblem_response_payload(emblem) if emblem else b""
    response_proto = proto_bytes(1, encoded_emblem) + proto_varint(2, ugc_id) + proto_varint(3, 0)
    reply(response_proto)
    telemetry.emit(
        "game_player_emblem_sent",
        peer=session.peer,
        online_id=session.online_id,
        requested_online_id=requested_online_id,
        player_id=requested_player_id,
        ugc_id=ugc_id,
        emblem_length=len(encoded_emblem),
    )


def register_emblem(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    metadata_field = first_field(fields, 1)
    emblem_field = first_field(fields, 2)
    if (
        metadata_field is None
        or not isinstance(metadata_field.value, bytes)
        or emblem_field is None
        or not isinstance(emblem_field.value, bytes)
    ):
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason="RequestRegisterEmblem requires bytes fields 1 and 2",
        )
        return
    pending = teams.register_pending_emblem(
        session.online_id,
        metadata_field.value,
        emblem_field.value,
    )
    reply(_register_emblem_response(int(pending["ugc_id"])))
    telemetry.emit(
        "game_emblem_registered",
        peer=session.peer,
        online_id=session.online_id,
        ugc_id=pending["ugc_id"],
        metadata_length=len(metadata_field.value),
        emblem_length=len(emblem_field.value),
        emblem_file=pending["emblem_file"],
    )


def set_player_emblem(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    ugc_field = first_field(fields, 1)
    emblem_field = first_field(fields, 2)
    if (
        ugc_field is None
        or not isinstance(ugc_field.value, int)
        or emblem_field is None
        or not isinstance(emblem_field.value, bytes)
        or not emblem_field.value
    ):
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason="RequestSetPlayerEmblemByUGCID requires integer field 1 and non-empty bytes field 2",
        )
        return
    assignment = teams.set_player_emblem_by_ugc_id(
        session.online_id,
        int(ugc_field.value),
        emblem_field.value,
    )
    # The response has one required boolean field copied directly by
    # the client; true completes the save operation.
    reply(proto_varint(1, 1))
    telemetry.emit(
        "game_player_emblem_applied",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        ugc_id=assignment["ugc_id"],
        emblem_length=len(emblem_field.value),
    )


def check_ugc_ids(
    session: GameSession,
    message: GameMessage,
    *,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    requested_ids = [
        int(field.value) for field in fields if field.number == 1 and isinstance(field.value, int)
    ]
    # No moderation/ban database exists yet. Unknown imported IDs are
    # valid; only a future explicit ban list may reject one.
    banned_ids: set[int] = set()
    response = _ugc_id_validation_response(requested_ids, banned_ids=banned_ids)
    reply(response)
    telemetry.emit(
        "game_ugc_id_list_checked",
        peer=session.peer,
        online_id=session.online_id,
        requested_ids=requested_ids,
        valid_ids=[ugc_id for ugc_id in requested_ids if ugc_id > 0 and ugc_id not in banned_ids],
    )
