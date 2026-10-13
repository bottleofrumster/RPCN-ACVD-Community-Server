"""Application-fragment reassembly after reliable-UDP decryption.

Accumulate only in the supplied session, then return a decoded complete request.
Preserve the retail40-bit total length; low16-bit interpretation truncated real
large screenshot uploads. Incomplete/rejected packets never reach handlers.
Incomplete groups are bounded by declared bytes, count, idle time and total
lifetime. Compression remains unsupported.
"""

from __future__ import annotations

import time

from .telemetry import Telemetry
from .udp import GameMessage, GameSession, ReliablePacket, decode_game_message

MAX_REASSEMBLED_BYTES = 1024 * 1024
MAX_CACHED_FRAGMENT_BYTES = 2 * 1024 * 1024
MAX_FRAGMENT_GROUPS = 16
FRAGMENT_GROUP_IDLE_SECONDS = 30.0
FRAGMENT_GROUP_MAX_AGE_SECONDS = 180.0


def _drop_group(session: GameSession, fragment_counter: int) -> None:
    session.fragment_parts.pop(fragment_counter, None)
    session.fragment_totals.pop(fragment_counter, None)
    session.fragment_started_monotonic.pop(fragment_counter, None)
    session.fragment_updated_monotonic.pop(fragment_counter, None)


def _purge_expired(session: GameSession, telemetry: Telemetry, now: float) -> None:
    expired = []
    for counter, started in session.fragment_started_monotonic.items():
        updated = session.fragment_updated_monotonic.get(counter, started)
        if (
            now - updated > FRAGMENT_GROUP_IDLE_SECONDS
            or now - started > FRAGMENT_GROUP_MAX_AGE_SECONDS
        ):
            expired.append(counter)
    for counter in expired:
        started = session.fragment_started_monotonic[counter]
        updated = session.fragment_updated_monotonic.get(counter, started)
        received_length = sum(
            len(part) for part in session.fragment_parts.get(counter, {}).values()
        )
        total_length = session.fragment_totals.get(counter, 0)
        _drop_group(session, counter)
        telemetry.emit(
            "game_fragment_group_expired",
            peer=session.peer,
            online_id=session.online_id,
            packet_counter=counter,
            age_seconds=now - started,
            idle_seconds=now - updated,
            received_length=received_length,
            total_length=total_length,
        )


def assemble_request(
    session: GameSession,
    packet: ReliablePacket,
    *,
    telemetry: Telemetry,
) -> GameMessage | None:
    now = time.monotonic()
    _purge_expired(session, telemetry, now)
    payload = packet.payload
    if len(payload) < 12:
        telemetry.emit(
            "game_message_rejected", peer=session.peer, reason="fragment header is too short"
        )
        return
    fragment_counter = int.from_bytes(payload[:2], "big")
    compressed = payload[2]
    # The retail envelope stores a 40-bit total across bytes 3-7. Reading
    # only the low 16 bits made a valid 203,999-byte ownership upload look
    # like 7,391 bytes and triggered premature reassembly at fragment 7.
    total_length = int.from_bytes(payload[3:8], "big")
    # Retail uses the complete two-byte slot at offsets8-9. Voice of War
    # screenshots can exceed256 fragments; reading only byte9 aliases index256
    # to index0 and makes the valid upload look like a changed duplicate.
    fragment_index = int.from_bytes(payload[8:10], "big")
    fragment_length = int.from_bytes(payload[10:12], "big")
    fragment_payload = payload[12:]
    if not 0 < total_length <= MAX_REASSEMBLED_BYTES:
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            reason="fragment total is outside the supported bound",
            total_length=total_length,
        )
        return
    if compressed:
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            reason="compressed fragments are not supported yet",
        )
        return
    if fragment_length != len(fragment_payload):
        telemetry.emit(
            "game_message_rejected", peer=session.peer, reason="fragment length mismatch"
        )
        return
    if fragment_index != 0 or fragment_length != total_length:
        if fragment_counter not in session.fragment_totals:
            if (
                len(session.fragment_totals) >= MAX_FRAGMENT_GROUPS
                or sum(session.fragment_totals.values()) + total_length
                > MAX_CACHED_FRAGMENT_BYTES
            ):
                telemetry.emit(
                    "game_message_rejected",
                    peer=session.peer,
                    reason="fragment cache capacity reached",
                    total_length=total_length,
                )
                return
            session.fragment_totals[fragment_counter] = total_length
            session.fragment_parts[fragment_counter] = {}
            session.fragment_started_monotonic[fragment_counter] = now
            session.fragment_updated_monotonic[fragment_counter] = now
        expected = session.fragment_totals[fragment_counter]
        if expected != total_length:
            telemetry.emit(
                "game_message_rejected", peer=session.peer, reason="fragment total changed"
            )
            return
        parts = session.fragment_parts[fragment_counter]
        existing = parts.get(fragment_index)
        if existing is not None and existing != fragment_payload:
            telemetry.emit(
                "game_message_rejected",
                peer=session.peer,
                reason="fragment duplicate changed",
                packet_counter=fragment_counter,
                fragment_index=fragment_index,
            )
            return
        parts[fragment_index] = fragment_payload
        if existing is None:
            # Extend the idle window only for genuine forward progress. Exact
            # retransmissions cannot keep an abandoned allocation alive.
            session.fragment_updated_monotonic[fragment_counter] = now
        received = sum(len(part) for part in parts.values())
        telemetry.emit(
            "game_fragment_received",
            peer=session.peer,
            packet_counter=fragment_counter,
            fragment_index=fragment_index,
            received_length=received,
            total_length=total_length,
        )
        if received < total_length:
            return
        if received > total_length:
            telemetry.emit(
                "game_message_rejected",
                peer=session.peer,
                reason="fragment bytes exceed declared total",
                received_length=received,
                total_length=total_length,
            )
            _drop_group(session, fragment_counter)
            return
        indexes = sorted(parts)
        if indexes != list(range(indexes[-1] + 1)):
            return
        message_payload = b"".join(parts[index] for index in indexes)
        if len(message_payload) != total_length:
            telemetry.emit(
                "game_message_rejected",
                peer=session.peer,
                reason="reassembled length mismatch",
                expected_length=total_length,
                actual_length=len(message_payload),
                packet_counter=fragment_counter,
            )
            _drop_group(session, fragment_counter)
            return
        _drop_group(session, fragment_counter)
        payload = (
            fragment_counter.to_bytes(2, "big")
            + b"\x00"
            + total_length.to_bytes(5, "big")
            + bytes(2)
            + (total_length & 0xFFFF).to_bytes(2, "big")
            + message_payload
        )
        telemetry.emit(
            "game_fragments_reassembled",
            peer=session.peer,
            packet_counter=fragment_counter,
            fragment_count=len(indexes),
            total_length=total_length,
        )
    try:
        message = decode_game_message(payload)
    except ValueError as exc:
        telemetry.emit("game_message_rejected", peer=session.peer, reason=str(exc))
        return
    return message
