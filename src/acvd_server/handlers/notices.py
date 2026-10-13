"""Read and acknowledge only the authenticated player's personal notices."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..notice_messages import (
    PersonalNotice,
    decode_notice_read_ids,
    encode_personal_notices,
    format_login_welcome,
)
from ..notice_storage import MAX_PENDING_REPLY, PersonalNoticeStore
from ..protocol import ProtocolError
from ..telemetry import Telemetry
from ..udp import GameMessage, GameSession


def handle_personal_notices(
    session: GameSession,
    message: GameMessage,
    *,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    reply_error: Callable[[int, int], None],
    acknowledge: bool,
    welcome_message: str = "",
) -> None:
    store = PersonalNoticeStore(player_data_path)
    try:
        if acknowledge:
            identifiers = decode_notice_read_ids(message.payload)
            welcome_id = session.login_welcome.identifier if session.login_welcome else None
            changed = store.acknowledge(
                session.player_id, [key for key in identifiers if key != welcome_id]
            )
            # Do not change session state until durable acknowledgements succeed.
            if welcome_id in identifiers and not session.login_welcome_read:
                session.login_welcome_read = True
                changed.append(welcome_id)
            payload = b""
        else:
            if message.payload:
                raise ProtocolError("Personal-notice query must be empty")
            # Keep old once-per-version welcome records for audit, but do not
            # show them alongside the new per-login welcome. Feedback is unchanged.
            pending = store.pending(session.player_id, exclude_event_prefixes=("welcome:",))
            if not session.login_welcome_checked:
                if welcome_message.strip():
                    created = datetime.now(timezone.utc)
                    body = format_login_welcome(welcome_message)
                    PersonalNotice(1, "Community server notice", body, created)
                    identifier = store.reserve_identifier(session.player_id)
                    session.login_welcome = PersonalNotice(
                        identifier, "Community server notice", body, created
                    )
                session.login_welcome_checked = True
            if session.login_welcome and not session.login_welcome_read:
                pending = [session.login_welcome, *pending][:MAX_PENDING_REPLY]
            payload = encode_personal_notices(pending)
    except (OSError, ValueError, ProtocolError) as error:
        telemetry.emit(
            "game_personal_notice_rejected",
            online_id=session.online_id,
            request_type=message.message_type,
            reason=str(error),
        )
        # Known failure/data-not-found envelope. Never ACK an uncommitted read.
        reply_error(1, 28)
        return
    reply(payload)
    if acknowledge:
        telemetry.emit(
            "game_personal_announce_marked_read",
            peer=session.peer,
            online_id=session.online_id,
            requested_ids=identifiers,
            changed_ids=changed,
        )
    else:
        telemetry.emit(
            "game_personal_announce_sent",
            peer=session.peer,
            online_id=session.online_id,
            count=len(pending),
            notice_ids=[notice.identifier for notice in pending],
        )
