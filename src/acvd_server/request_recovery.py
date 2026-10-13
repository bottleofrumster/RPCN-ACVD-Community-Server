"""Ordered request delivery and bounded recovery of missing client packets.

A native control proves opcode3 requests the packet after the cumulative ACK.
Do not acknowledge a gap or dispatch a duplicate ordinary request. Fragments
of an already-started large request may be cached out of reliable order; the
native Voice of War uploader interleaves distant fragments and otherwise
times out while the server requests each gap serially.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable

from .telemetry import Telemetry
from .udp import GameSession, ReliablePacket

RECEIVE_WINDOW = 512  # Less than half the 12-bit sequence space.
FRAGMENT_REORDER_WINDOW = 64
MAX_RECOVERY_SESSIONS = 128
MAX_ATTEMPTS = 32


@dataclass
class _Pending:
    session: GameSession
    peer: tuple[str, int]
    sequence: int
    attempts: int = 0
    timer: asyncio.TimerHandle | None = None


class RequestRecovery:
    def __init__(
        self,
        *,
        is_current: Callable[[GameSession], bool],
        request_next: Callable[[GameSession], None],
        telemetry: Telemetry,
    ) -> None:
        self.is_current = is_current
        self.request_next = request_next
        self.telemetry = telemetry
        self.pending: dict[bytes, _Pending] = {}
        self.out_of_order: dict[bytes, tuple[GameSession, set[int]]] = {}

    def discard(self, session: GameSession) -> None:
        buffered = self.out_of_order.get(session.auth_token)
        if buffered is not None and buffered[0] is session:
            self.out_of_order.pop(session.auth_token, None)
        state = self.pending.get(session.auth_token)
        if state is not None and state.session is session:
            self.pending.pop(session.auth_token)
            if state.timer is not None:
                state.timer.cancel()

    def close(self) -> None:
        for state in list(self.pending.values()):
            self.discard(state.session)
        self.out_of_order.clear()

    def _live(self, session: GameSession) -> bool:
        return (
            session.established
            and session.peer is not None
            and self.is_current(session)
            and 0 <= time.monotonic() - session.last_seen_monotonic <= 90
        )

    def accept(self, session: GameSession, packet: ReliablePacket) -> bool:
        """Advance one contiguous data sequence, never a gap or an old packet."""
        if not session.receive_sequence_initialized:
            # SYN normally seeds this prefix. Also support diagnostic callers
            # and legacy restored sessions which have no saved receive marker.
            session.remote_sequence = (packet.local_sequence - 1) % 4096
            session.receive_sequence_initialized = True
        distance = (packet.local_sequence - session.remote_sequence) % 4096
        if distance == 1:
            session.remote_sequence = packet.local_sequence
            buffered = self.out_of_order.get(session.auth_token)
            if buffered is not None and buffered[0] is session:
                sequences = buffered[1]
                while (session.remote_sequence + 1) % 4096 in sequences:
                    session.remote_sequence = (session.remote_sequence + 1) % 4096
                    sequences.remove(session.remote_sequence)
                if not sequences:
                    self.out_of_order.pop(session.auth_token, None)
            if session.receive_high_sequence == session.remote_sequence:
                session.receive_high_sequence = None
            return True
        if 1 < distance <= RECEIVE_WINDOW:
            high = session.receive_high_sequence
            if high is None or distance > (high - session.remote_sequence) % 4096:
                session.receive_high_sequence = packet.local_sequence
            if distance <= FRAGMENT_REORDER_WINDOW and self._bufferable_fragment(session, packet):
                current = self.out_of_order.get(session.auth_token)
                if current is None or current[0] is not session:
                    current = (session, set())
                    self.out_of_order[session.auth_token] = current
                if packet.local_sequence in current[1]:
                    reason = "duplicate_or_old"
                else:
                    current[1].add(packet.local_sequence)
                    self.telemetry.emit(
                        "game_request_fragment_buffered",
                        online_id=session.online_id,
                        received_sequence=packet.local_sequence,
                        acknowledged_sequence=session.remote_sequence,
                        buffered_count=len(current[1]),
                    )
                    return True
            else:
                reason = "gap"
        else:
            reason = "duplicate_or_old"
        self.telemetry.emit(
            "game_request_sequence_deferred",
            online_id=session.online_id,
            received_sequence=packet.local_sequence,
            acknowledged_sequence=session.remote_sequence,
            reason=reason,
        )
        return False

    @staticmethod
    def _bufferable_fragment(session: GameSession, packet: ReliablePacket) -> bool:
        payload = packet.payload
        if len(payload) < 12:
            return False
        group = int.from_bytes(payload[:2], "big")
        total = int.from_bytes(payload[3:8], "big")
        fragment_length = int.from_bytes(payload[10:12], "big")
        return (
            group in session.fragment_parts
            and total > fragment_length
            and fragment_length == len(payload) - 12
        )

    def refresh(self, session: GameSession) -> None:
        """Maintain one timer per incomplete receive stream, not per packet."""
        for state in list(self.pending.values()):
            if not self._live(state.session) or state.session.peer != state.peer:
                self.discard(state.session)
        state = self.pending.get(session.auth_token)
        if state is not None and (
            state.session is not session or state.sequence != session.remote_sequence
        ):
            self.discard(state.session)
            state = None
        if not self._live(session) or not (
            session.receive_high_sequence is not None or session.fragment_parts
        ):
            self.discard(session)
            return
        if state is not None:
            return  # An exhausted prefix stays exhausted until real progress.
        if len(self.pending) >= MAX_RECOVERY_SESSIONS:
            self.telemetry.emit("game_request_recovery_capacity_reached", online_id=session.online_id)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # Read-only/synchronous diagnostic callers own no timers.
        state = _Pending(session, session.peer, session.remote_sequence)
        self.pending[session.auth_token] = state
        state.timer = loop.call_later(0.5, self._recover, state)

    def _recover(self, state: _Pending) -> None:
        session = state.session
        if self.pending.get(session.auth_token) is not state:
            return
        state.timer = None
        if (
            not self._live(session)
            or session.peer != state.peer
            or session.remote_sequence != state.sequence
            or not (session.receive_high_sequence is not None or session.fragment_parts)
        ):
            self.discard(session)
            return
        self.request_next(session)
        state.attempts += 1
        self.telemetry.emit(
            "game_request_missing_packet_requested",
            online_id=session.online_id,
            requested_sequence=(state.sequence + 1) % 4096,
            attempt=state.attempts,
        )
        if state.attempts < MAX_ATTEMPTS:
            state.timer = asyncio.get_running_loop().call_later(0.25, self._recover, state)
        else:
            self.telemetry.emit(
                "game_request_recovery_exhausted",
                online_id=session.online_id,
                requested_sequence=(state.sequence + 1) % 4096,
                attempts=state.attempts,
            )
