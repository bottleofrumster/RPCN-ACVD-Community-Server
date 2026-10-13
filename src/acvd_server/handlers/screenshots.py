"""Authenticated screenshot draft, publication and retrieval requests.

A new draft must not replace the published image until Apply succeeds. Owner
validation precedes storage; each replaced blob retains its own history.
Full-image IDs are fixed64 on the wire, not ordinary varints or player IDs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..player_storage import _replace_player_blob
from ..protocol import ProtocolError, decode_proto, proto_bytes, proto_varint
from ..screenshots import (
    _required_screenshot_registration,
    _world_top_screenshot,
    _world_top_screenshot_id,
    _world_top_thumbnail_info,
)
from ..teams import TeamStore
from ..telemetry import Telemetry
from ..udp import GameMessage, GameSession


def register_screenshot(
    session: GameSession,
    message: GameMessage,
    *,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = _required_screenshot_registration(message.payload)
    if fields is None or int(fields[1].value) != session.player_id:
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason=(
                "RequestRegisterPlayerScreenShot requires fields 1-4 as scalars "
                "and fields 5-8 as bytes, with the authenticated player ID in field 1"
            ),
        )
        return
    target = player_data_path / f"{session.player_id}.player-screenshot.bin"
    previous_backup = _replace_player_blob(target, message.payload)
    reply(b"")
    telemetry.emit(
        "game_player_screenshot_registered",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        payload_length=len(message.payload),
        byte_field_lengths={number: len(fields[number].value) for number in range(5, 9)},
        previous_backup=str(previous_backup) if previous_backup is not None else "",
    )


def apply_screenshot(
    session: GameSession,
    message: GameMessage,
    *,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    try:
        fields = decode_proto(message.payload)
    except ProtocolError:
        fields = []
    token_fields = [field for field in fields if field.number == 1 and field.wire_type == 2]
    registration_path = player_data_path / f"{session.player_id}.player-screenshot.bin"
    if len(token_fields) != 1 or not registration_path.is_file():
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason=(
                "RequestApplyWorldTopScreenShot requires byte field 1 and a "
                "completed screenshot registration for the authenticated player"
            ),
        )
        return
    registration = registration_path.read_bytes()
    registration_fields = _required_screenshot_registration(registration)
    if registration_fields is None or int(registration_fields[1].value) != session.player_id:
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason="stored screenshot registration is malformed or belongs to another player",
        )
        return
    application_path = player_data_path / f"{session.player_id}.world-top-application.bin"
    world_top_path = player_data_path / f"{session.player_id}.world-top-screenshot.bin"
    application_backup = _replace_player_blob(application_path, message.payload)
    screenshot_backup = _replace_player_blob(world_top_path, registration)
    screenshot_id = _world_top_screenshot_id(session.player_id, registration)
    reply(b"")
    telemetry.emit(
        "game_world_top_screenshot_applied",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        screenshot_id=screenshot_id,
        application_token_length=len(token_fields[0].value),
        application_previous_backup=(
            str(application_backup) if application_backup is not None else ""
        ),
        screenshot_previous_backup=(
            str(screenshot_backup) if screenshot_backup is not None else ""
        ),
    )


def get_thumbnails(
    session: GameSession,
    message: GameMessage,
    *,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    try:
        fields = decode_proto(message.payload)
    except ProtocolError:
        fields = []
    limit_fields = [field for field in fields if field.number == 1 and field.wire_type == 0]
    if len(limit_fields) != 1:
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason="RequestGetWorldTopSSThumbnailList requires scalar field 1",
        )
        return
    limit = min(max(0, int(limit_fields[0].value)), 32)
    candidates = (
        sorted(
            player_data_path.glob("*.world-top-screenshot.bin"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        if limit
        else []
    )
    entries = []
    screenshot_ids = []
    for target in candidates:
        registration = target.read_bytes()
        info = _world_top_thumbnail_info(registration)
        if info is None:
            continue
        registration_fields = _required_screenshot_registration(registration)
        player_id = int(registration_fields[1].value)
        entries.append(proto_bytes(1, info))
        screenshot_ids.append(_world_top_screenshot_id(player_id, registration))
        if len(entries) >= limit:
            break
    reply(b"".join(entries))
    telemetry.emit(
        "game_world_top_ss_thumbnail_list_sent",
        peer=session.peer,
        online_id=session.online_id,
        requested_limit=int(limit_fields[0].value),
        applied_limit=limit,
        count=len(entries),
        screenshot_ids=screenshot_ids,
    )


def get_screenshot(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    try:
        fields = decode_proto(message.payload)
    except ProtocolError:
        fields = []
    id_fields = [field for field in fields if field.number == 1 and field.wire_type == 1]
    if len(id_fields) != 1:
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason="RequestGetWorldTopSS requires fixed64 field 1",
        )
        return
    requested_id = int.from_bytes(id_fields[0].value, "little")
    response = None
    owner_online_id = ""
    owner_player_id = 0
    for target in player_data_path.glob("*.world-top-screenshot.bin"):
        registration = target.read_bytes()
        registration_fields = _required_screenshot_registration(registration)
        if registration_fields is None:
            continue
        player_id = int(registration_fields[1].value)
        if _world_top_screenshot_id(player_id, registration) != requested_id:
            continue
        owner_player_id = player_id
        owner = teams.member_by_player_id(player_id)
        owner_online_id = owner[0] if owner is not None else str(player_id)
        response = _world_top_screenshot(registration, owner_online_id)
        break
    if response is None:
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason="RequestGetWorldTopSS references an unknown screenshot ID",
            screenshot_id=requested_id,
        )
        return
    reply(response)
    telemetry.emit(
        "game_world_top_screenshot_sent",
        peer=session.peer,
        online_id=session.online_id,
        screenshot_id=requested_id,
        owner_online_id=owner_online_id,
        owner_player_id=owner_player_id,
        payload_length=len(response),
    )


def get_wait_time(
    session: GameSession,
    message: GameMessage,
    *,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    # RequestGetWaitTimeForApplyWorldTopSSResponse requires one
    # scalar field. Zero means no server-side submission cooldown.
    reply(proto_varint(1, 0))
    telemetry.emit(
        "game_world_top_ss_wait_time_sent",
        peer=session.peer,
        online_id=session.online_id,
        wait_seconds=0,
    )
