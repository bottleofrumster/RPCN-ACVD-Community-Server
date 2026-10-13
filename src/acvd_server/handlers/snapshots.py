"""AC, mission and UNAC snapshot request lifecycle.

Keep self-recovery flags on the authenticated session and file lookup/history
in player_storage. Existing recovery policies differ: AC consumes a delivered
preseed on registration, while mission/AI promotion checks semantic equality.
Do not unify those policies or infer full payload validation during cleanup.
"""

from __future__ import annotations

import hashlib
import time
import zlib
from pathlib import Path
from typing import Callable

from ..player_storage import (
    _replace_player_blob,
    _requested_ac_snapshot,
    _requested_ai_snapshot,
    _requested_mission_snapshot,
)
from ..protocol import decode_proto, first_field, proto_bytes, proto_varint
from ..save_codecs import (
    _ac_data_response_payload,
    _ai_info_equivalent,
    _mission_savedata_equivalent,
    _mission_snapshot_summary,
    _own_unac_list_response,
)
from ..telemetry import Telemetry
from ..udp import GameMessage, GameSession


def get_owned_unacs(
    session: GameSession,
    message: GameMessage,
    *,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    prefix = f"{session.player_id}.ai-"
    slots: list[int] = []
    for path in player_data_path.glob(f"{prefix}*.bin"):
        try:
            slots.append(int(path.name[len(prefix) : -4]))
        except ValueError:
            continue
    reply(_own_unac_list_response(slots))
    telemetry.emit(
        "game_owned_unac_list_sent",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        slots=sorted(set(slots)),
    )


def get_ai_extension(
    session: GameSession,
    message: GameMessage,
    *,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    target = player_data_path / f"{session.player_id}.ai-extend.bin"
    ai_extend = target.read_bytes() if target.is_file() else b""
    reply(proto_bytes(1, ai_extend))
    telemetry.emit(
        "game_ai_extend_sent",
        peer=session.peer,
        online_id=session.online_id,
        payload_length=len(ai_extend),
        stored=target.is_file(),
    )


def update_ai_extension(
    session: GameSession,
    message: GameMessage,
    *,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    mode_field = first_field(fields, 1)
    data_field = first_field(fields, 2)
    if (
        mode_field is None
        or not isinstance(mode_field.value, int)
        or data_field is None
        or not isinstance(data_field.value, bytes)
    ):
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason="RequestUpdateAiExtend requires integer field 1 and bytes field 2",
        )
        return
    target = player_data_path / f"{session.player_id}.ai-extend.bin"
    previous_backup = _replace_player_blob(target, data_field.value)
    # The response's required bytes field is the authoritative value.
    reply(proto_bytes(1, data_field.value))
    telemetry.emit(
        "game_ai_extend_updated",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        mode=int(mode_field.value),
        payload_length=len(data_field.value),
        previous_backup=str(previous_backup) if previous_backup is not None else "",
    )


def register_ac_data(
    session: GameSession,
    message: GameMessage,
    *,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    ac_data_field = first_field(fields, 1)
    if ac_data_field is None or not isinstance(ac_data_field.value, bytes):
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            reason="RequestRegisterACData is missing bytes field 1",
        )
        return
    target = player_data_path / f"{session.player_id}.ac-data.bin"
    previous_backup = _replace_player_blob(target, ac_data_field.value)
    applied_preseed = None
    if session.ac_preseed_delivered is not None and session.ac_preseed_delivered.is_file():
        applied_preseed = session.ac_preseed_delivered.with_name(
            f"{session.player_id}.ac-data.preseed.applied-{time.time_ns()}.bin"
        )
        session.ac_preseed_delivered.replace(applied_preseed)
    session.ac_preseed_delivered = None
    reply(b"")
    telemetry.emit(
        "game_ac_data_registered",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        payload_length=len(ac_data_field.value),
        applied_preseed=str(applied_preseed) if applied_preseed is not None else "",
        previous_backup=str(previous_backup) if previous_backup is not None else "",
    )


def get_ac_data(
    session: GameSession,
    message: GameMessage,
    *,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    # RequestGetACDataResponse contains one required bytes/string
    # field. Retail requires the application encoding byte even when
    # no server-hosted AC snapshot exists; a zero-byte string aborts
    # new-player Step 2 with 3050010.
    fields = decode_proto(message.payload)
    player_field = first_field(fields, 1)
    requested_player_id = (
        int(player_field.value)
        if player_field is not None and isinstance(player_field.value, int)
        else session.player_id
    )
    self_request = requested_player_id == session.player_id
    ac_data, preseed_path = _requested_ac_snapshot(
        player_data_path,
        requester_player_id=session.player_id,
        requested_player_id=requested_player_id,
    )
    try:
        response_ac_data = _ac_data_response_payload(ac_data)
    except zlib.error as exc:
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            reason=f"stored AC data is not valid zlib: {exc}",
        )
        return
    reply(proto_bytes(1, response_ac_data))
    if self_request:
        session.ac_preseed_delivered = preseed_path
    telemetry.emit(
        "game_ac_data_sent",
        peer=session.peer,
        online_id=session.online_id,
        player_id=requested_player_id,
        stored_length=len(ac_data),
        payload_length=len(response_ac_data),
        encoding=response_ac_data[:1].hex() if response_ac_data else "empty",
        self_snapshot_suppressed=False,
        source="preseed" if preseed_path is not None else "live",
    )


def get_mission_data(
    session: GameSession,
    message: GameMessage,
    *,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    # The response is one required application-encoded byte string.
    # Encoding 0 is the valid uncompressed form; an empty body means
    # there are no server-side mission snapshots yet, after which the
    # client registers its local mission saves.
    savedata, preseed_path = _requested_mission_snapshot(
        player_data_path,
        player_id=session.player_id,
    )
    savedata = savedata if savedata else b"\x00"
    reply(proto_bytes(1, savedata))
    session.mission_preseed_delivered = preseed_path
    telemetry.emit(
        "game_mission_savedata_sent",
        peer=session.peer,
        online_id=session.online_id,
        payload_length=len(savedata),
        source="preseed" if preseed_path is not None else "live",
        snapshot=_mission_snapshot_summary(savedata),
    )


def upload_mission_data(
    session: GameSession,
    message: GameMessage,
    *,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    savedata_field = first_field(fields, 1)
    if savedata_field is None or not isinstance(savedata_field.value, bytes):
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            reason="RequestUploadMissionSavedata is missing bytes field 1",
        )
        return
    delivered_preseed = session.mission_preseed_delivered
    if delivered_preseed is not None and delivered_preseed.is_file():
        preseed_payload = delivered_preseed.read_bytes()
        if not _mission_savedata_equivalent(preseed_payload, savedata_field.value):
            deferred_target = player_data_path / (
                f"{session.player_id}.mission-savedata.deferred.bin"
            )
            _replace_player_blob(deferred_target, savedata_field.value)
            reply(b"")
            telemetry.emit(
                "game_mission_savedata_upload_deferred",
                peer=session.peer,
                online_id=session.online_id,
                player_id=session.player_id,
                preseed_path=str(delivered_preseed),
                preseed_sha256=hashlib.sha256(preseed_payload).hexdigest(),
                upload_sha256=hashlib.sha256(savedata_field.value).hexdigest(),
                payload_length=len(savedata_field.value),
                snapshot=_mission_snapshot_summary(savedata_field.value, preseed_payload),
            )
            return
    target = player_data_path / f"{session.player_id}.mission-savedata.bin"
    previous_payload = target.read_bytes() if target.is_file() else None
    previous_backup = _replace_player_blob(target, savedata_field.value)
    applied_preseed = None
    delivered_preseed = session.mission_preseed_delivered
    if delivered_preseed is not None and delivered_preseed.is_file():
        applied_preseed = delivered_preseed.with_name(
            f"{session.player_id}.mission-savedata.preseed.applied-{time.time_ns()}.bin"
        )
        delivered_preseed.replace(applied_preseed)
    session.mission_preseed_delivered = None
    reply(b"")
    telemetry.emit(
        "game_mission_savedata_uploaded",
        peer=session.peer,
        online_id=session.online_id,
        payload_length=len(savedata_field.value),
        applied_preseed=str(applied_preseed) if applied_preseed is not None else "",
        previous_backup=str(previous_backup) if previous_backup is not None else "",
        snapshot=_mission_snapshot_summary(savedata_field.value, previous_payload),
    )


def get_ai_info(
    session: GameSession,
    message: GameMessage,
    *,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    player_field = first_field(fields, 1)
    slot_field = first_field(fields, 2)
    player_id = (
        int(player_field.value)
        if player_field is not None and isinstance(player_field.value, int)
        else session.player_id
    )
    slot = slot_field.value if slot_field is not None and isinstance(slot_field.value, int) else 0
    stored_ai, preseed_path = _requested_ai_snapshot(
        player_data_path,
        requester_player_id=session.player_id,
        requested_player_id=player_id,
        slot=slot,
    )
    response_proto = (
        stored_ai
        if stored_ai
        else (
            proto_varint(1, slot)
            + proto_bytes(2, b"")
            + proto_varint(3, 0)
            + proto_bytes(4, b"\x00")
        )
    )
    reply(response_proto)
    if player_id == session.player_id:
        if preseed_path is not None:
            session.ai_preseeds_delivered[int(slot)] = preseed_path
        else:
            session.ai_preseeds_delivered.pop(int(slot), None)
    telemetry.emit(
        "game_ai_info_sent",
        peer=session.peer,
        online_id=session.online_id,
        player_id=player_id,
        slot=slot,
        stored=bool(stored_ai),
        source="preseed" if preseed_path is not None else "live",
    )


def upload_ai_info(
    session: GameSession,
    message: GameMessage,
    *,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    slot_field = first_field(fields, 1)
    if slot_field is None or not isinstance(slot_field.value, int):
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            reason="RequestUploadAiInfo is missing scalar field 1",
        )
        return
    slot = int(slot_field.value)
    delivered_preseed = session.ai_preseeds_delivered.get(slot)
    if delivered_preseed is not None and delivered_preseed.is_file():
        preseed_payload = delivered_preseed.read_bytes()
        if not _ai_info_equivalent(preseed_payload, message.payload):
            # During legacy reconstruction the client downloads the
            # reviewed UNAC preseed, rebuilds local default slots, and
            # immediately uploads those defaults.  Accepting that
            # upload destroys the only known-good migration source.
            # Acknowledge the retail request so login can finish, but
            # retain both the live record and recoverable preseed for
            # a later matching-account synchronization.
            reply(b"")
            telemetry.emit(
                "game_ai_info_upload_deferred",
                peer=session.peer,
                online_id=session.online_id,
                player_id=session.player_id,
                slot=slot,
                preseed_path=str(delivered_preseed),
                preseed_sha256=hashlib.sha256(preseed_payload).hexdigest(),
                upload_sha256=hashlib.sha256(message.payload).hexdigest(),
                payload_length=len(message.payload),
            )
            return
    target = player_data_path / f"{session.player_id}.ai-{slot}.bin"
    previous_backup = _replace_player_blob(target, message.payload)
    applied_preseed = None
    delivered_preseed = session.ai_preseeds_delivered.pop(slot, None)
    if delivered_preseed is not None and delivered_preseed.is_file():
        applied_preseed = delivered_preseed.with_name(
            f"{session.player_id}.ai-{slot}.preseed.applied-{time.time_ns()}.bin"
        )
        delivered_preseed.replace(applied_preseed)
    reply(b"")
    telemetry.emit(
        "game_ai_info_uploaded",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        slot=slot,
        payload_length=len(message.payload),
        applied_preseed=str(applied_preseed) if applied_preseed is not None else "",
        previous_backup=str(previous_backup) if previous_backup is not None else "",
    )
