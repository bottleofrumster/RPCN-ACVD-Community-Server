"""Passive, bounded reliable-delivery measurements; never drive retry policy.

ACK delay includes client scheduling/cumulative ACK delay, not just network RTT.
Retransmitted packets cannot yield an unambiguous round-trip measurement.
Observations are deliberately absent from persisted sessions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .udp import ReliablePacket


@dataclass
class DeliveryObservation:
    pending: dict[int, tuple[ReliablePacket, float, float, int]] = field(default_factory=dict)

    def sent(self, packet: ReliablePacket, retained: dict, now: float) -> None:
        if retained.get(packet.local_sequence) is not packet:
            return
        previous = self.pending.get(packet.local_sequence)
        if previous is not None and previous[0] is packet:
            self.pending[packet.local_sequence] = (packet, previous[1], now, previous[3] + 1)
        else:
            self.pending.pop(packet.local_sequence, None)
            self.pending[packet.local_sequence] = (packet, now, now, 0)
        # Mirror the transport's maximum retained window without owning it.
        while len(self.pending) > 512:
            self.pending.pop(next(iter(self.pending)))

    def acknowledge(self, endpoint: int, retained: dict, now: float) -> dict:
        if endpoint not in retained:
            return {}
        delays, retransmitted, unknown = [], 0, 0
        endpoint_delay = None
        for sequence, packet in retained.items():
            observation = self.pending.pop(sequence, None)
            if observation is None or observation[0] is not packet or now < observation[1]:
                unknown += 1
            else:
                delay = round((now - observation[1]) * 1000, 3)
                delays.append(delay)
                retransmitted += int(observation[3] > 0)
                if sequence == endpoint:
                    endpoint_delay = {
                        "first_send_to_ack_ms": delay,
                        "last_send_to_ack_ms": round(max(0, now - observation[2]) * 1000, 3),
                        "resends": observation[3],
                    }
            if sequence == endpoint:
                break
        if not delays:
            return {}
        return {
            "timed_packets": len(delays),
            "untimed_packets": unknown,
            "resent_packets": retransmitted,
            "oldest_first_send_to_ack_ms": max(delays),
            "newest_first_send_to_ack_ms": min(delays),
            "endpoint": endpoint_delay,
        }
