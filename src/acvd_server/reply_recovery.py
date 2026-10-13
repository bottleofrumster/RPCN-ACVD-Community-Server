"""Bounded recovery of unacknowledged fragmented reply tails.

Owns timers, not the UDP socket or session registry. Dynamic callbacks preserve
transport/session replacement behavior without retaining a stale connection.
The limits and intervals are the existing qualified policy:32 attempts,
128 active groups,8 packets per burst,0.5s first delay and0.25s retry delay.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Sequence

from .telemetry import Telemetry
from .udp import GameSession, ReliablePacket


class FragmentReplyRecovery:
    def __init__(
        self,
        *,
        transport_available: Callable[[], bool],
        is_current_session: Callable[[GameSession], bool],
        send_packet: Callable[[GameSession, ReliablePacket], None],
        telemetry: Telemetry,
    ) -> None:
        self.transport_available = transport_available
        self.is_current_session = is_current_session
        self.send_packet = send_packet
        self.telemetry = telemetry
        self.timers: dict[object, asyncio.TimerHandle] = {}

    def close(self) -> None:
        """Cancel all currently owned timers; transport lifecycle stays outside."""
        for handle in self.timers.values():
            handle.cancel()
        self.timers.clear()

    def schedule(
        self,
        session: GameSession,
        packets: Sequence[ReliablePacket],
        attempt: int = 0,
        key: object | None = None,
        peer: tuple[str, int] | None = None,
    ) -> None:
        if (
            not self.transport_available()
            or not session.established
            or not self.is_current_session(session)
        ):
            return
        peer = session.peer if peer is None else peer
        if peer is None or session.peer != peer:
            return
        pending = tuple(p for p in packets if session.outbound_reliable.get(p.local_sequence) is p)
        if not pending:
            return
        if attempt >= 32 or (key is None and len(self.timers) >= 128):
            self.telemetry.emit(
                "game_reply_recovery_exhausted",
                online_id=session.online_id,
                pending_count=len(pending),
                attempts=attempt,
            )
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # Synchronous tooling has no live transport event loop.
        key = object() if key is None else key
        self.timers[key] = loop.call_later(
            0.5 if attempt == 0 else 0.25,
            self.recover,
            session,
            pending,
            attempt,
            key,
            peer,
        )

    def recover(self, session, packets, attempt, key, peer) -> None:
        self.timers.pop(key, None)
        if (
            not self.transport_available()
            or not session.established
            or not self.is_current_session(session)
            or session.peer != peer
        ):
            return
        # Identity comparison rejects a reused 12-bit sequence after wrap.
        pending = tuple(p for p in packets if session.outbound_reliable.get(p.local_sequence) is p)
        if not pending:
            return
        retried = pending[:8]
        for packet in retried:
            self.send_packet(session, packet)
        self.telemetry.emit(
            "game_reply_fragments_recovered",
            online_id=session.online_id,
            local_sequences=[p.local_sequence for p in retried],
            attempt=attempt + 1,
        )
        self.schedule(session, pending, attempt + 1, key, peer)
