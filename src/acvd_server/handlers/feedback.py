"""Route accepted-hire short messages to recipient-owned personal notices."""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..hire_bindings import HireBindings, HireFeedbackContext
from ..mercenary_feedback import FEEDBACK_TEXT, decode_mercenary_feedback
from ..notice_messages import PersonalNotice
from ..notice_storage import PersonalNoticeStore
from ..protocol import ProtocolError
from ..teams import TeamStore
from ..telemetry import Telemetry
from ..udp import SEND_MERCENARY_MESSAGE, GameMessage, GameSession


@dataclass(frozen=True)
class _FeedbackDelivery:
    """Validated recipient and retry state, before any notice is written."""

    recipient: int
    text_id: int
    request_key: str
    body: str
    receipt: PersonalNotice | None
    context: HireFeedbackContext | None


def _request_key(session: GameSession, message: GameMessage, recipient: int) -> str:
    raw = (
        b"acvd-mercenary-feedback-v1\0"
        + session.auth_token
        + session.online_id.encode("utf-8")
        + b"\0"
        + session.player_id.to_bytes(4, "big")
        + recipient.to_bytes(4, "big")
        + message.message_index
        + message.packet_counter.to_bytes(2, "big")
        + message.payload
    )
    return "mercenary-feedback:" + hashlib.sha256(raw).hexdigest()


def send_feedback(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    hire_bindings: HireBindings,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    reply_error: Callable[[int, int], None],
) -> None:
    """Validate all targets first; commit each notice before success.

    A later recipient's I/O failure can leave a committed prefix. Its durable
    request keys make an exact retry finish without duplicating that prefix.
    This is not an atomic multi-recipient transaction or combat attestation.
    """
    store = PersonalNoticeStore(player_data_path)
    committed = []
    try:
        if message.message_type != SEND_MERCENARY_MESSAGE:
            raise ProtocolError("Only SendMercenaryMessage can submit contract feedback")
        targets = decode_mercenary_feedback(message.payload)
        if targets and not hire_bindings.feedback_sender_is_current(session):
            raise ValueError("Feedback sender is not a current authenticated team member")
        title = f"Feedback from {session.online_id}"
        plan: list[_FeedbackDelivery] = []
        for recipient, code in targets:
            identity = team_store.member_by_player_id(recipient)
            if recipient == session.player_id or identity is None:
                raise ValueError("Feedback recipient is not a known non-self player")
            key = _request_key(session, message, recipient)
            body = FEEDBACK_TEXT[code]
            previous = store.find_event(recipient, key)
            context = None
            if previous is not None:
                if previous.title != title or previous.body != body:
                    raise ValueError("Feedback receipt conflicts with stored content")
            else:
                context = hire_bindings.feedback_for(session, recipient, key)
                if context is None or context.mercenary_online_id != identity[0]:
                    raise ValueError("No unambiguous accepted hire permits this feedback")
            plan.append(_FeedbackDelivery(recipient, code, key, body, previous, context))
        # Reserve the entire validated batch before the first durable write.
        for delivery in plan:
            if delivery.context is not None:
                hire_bindings.reserve_feedback(delivery.context, delivery.request_key)
        for delivery in plan:
            identifier = (
                delivery.receipt.identifier
                if delivery.receipt is not None
                else store.enqueue(
                    delivery.recipient,
                    event_key=delivery.request_key,
                    title=title,
                    body=delivery.body,
                    created_at=datetime.now(timezone.utc),
                )
            )
            committed.append(
                {
                    "player_id": delivery.recipient,
                    "text_id": delivery.text_id,
                    "notice_id": identifier,
                    "replayed": delivery.receipt is not None,
                }
            )
    except (ProtocolError, ValueError, OSError) as error:
        telemetry.emit(
            "game_mercenary_feedback_rejected",
            online_id=session.online_id,
            reason=str(error),
            committed=committed,
        )
        reply_error(1, 28)
        return
    reply(b"")
    telemetry.emit(
        "game_mercenary_feedback_processed",
        online_id=session.online_id,
        notices=committed,
        count=len(committed),
    )
