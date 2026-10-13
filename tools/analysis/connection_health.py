"""Summarize bounded ACVD protocol-event input without exposing identities.

Example: tail -n 10000 logs/protocol-events.jsonl | python3 -B
tools/analysis/connection_health.py --minutes 30

The game's encrypted application UDP is distinct from its direct RPCN peer
path. A teammate-disconnect report does not by itself identify which link
failed. Ordinary ACK packets currently reuse the historical
game_session_established event name, so only actual SYN opcodes are counted as
new handshakes here.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

MAX_LINE = 1_000_000
MAX_RECORDS = 50_000


def summarize(lines, *, now: datetime, minutes: int) -> dict:
    if now.tzinfo is None or type(minutes) is not int or not 1 <= minutes <= 1440:
        raise ValueError("Use a timezone-aware UTC time and 1-1440 minutes")
    start = now.astimezone(timezone.utc) - timedelta(minutes=minutes)
    events = Counter()
    opcodes = Counter()
    deferred_reasons = Counter()
    peer_outcomes = Counter()
    recovery_attempts = Counter()
    endpoints: dict[str, set[str]] = defaultdict(set)
    last_seen: dict[str, datetime] = {}
    peer_reports: list[datetime] = []
    malformed = oversized = 0
    accepted = 0
    latest = None
    for index, line in enumerate(lines):
        if index >= MAX_RECORDS:
            raise ValueError("Connection-health input exceeds 50000 records")
        if len(line) > MAX_LINE:
            oversized += 1
            continue
        try:
            row = json.loads(line)
            timestamp = datetime.fromisoformat(row["timestamp_utc"])
            if timestamp.tzinfo is None:
                raise ValueError("Protocol timestamp has no timezone")
            timestamp = timestamp.astimezone(timezone.utc)
            event = row["event"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            malformed += 1
            continue
        if not start <= timestamp <= now or not isinstance(event, str):
            continue
        accepted += 1
        latest = max(latest, timestamp) if latest else timestamp
        events[event] += 1
        if event == "game_request_sequence_deferred":
            deferred_reasons[row.get("reason")] += 1
        if event == "game_packet_decoded":
            opcode = row.get("opcode")
            if type(opcode) is int:
                opcodes[opcode] += 1
            owner = row.get("online_id")
            if isinstance(owner, str) and owner:
                last_seen[owner] = max(last_seen.get(owner, timestamp), timestamp)
                if row.get("peer") is not None:
                    endpoints[owner].add(repr(row["peer"]))
        elif event == "game_team_session_member_disconnect_reported":
            peer_reports.append(timestamp)
            if row.get("foreign_overlay_retired") is True:
                peer_outcomes["overlay_retired"] += 1
            elif row.get("real_team_peer_recovered") is True:
                peer_outcomes["recovered"] += 1
            else:
                peer_outcomes["not_recovered"] += 1
        elif event == "game_team_peer_recovery_sent":
            attempt = row.get("attempt")
            recovery_attempts[attempt if type(attempt) is int and attempt in (1, 2) else 0] += 1
    peer_reports.sort()
    episodes = 0
    previous = None
    for timestamp in peer_reports:
        if previous is None or timestamp - previous > timedelta(seconds=5):
            episodes += 1
        previous = timestamp
    return {
        "window_minutes": minutes,
        "accepted_events": accepted,
        "latest_event_utc": latest.isoformat() if latest else None,
        "malformed_lines": malformed,
        "oversized_lines": oversized,
        "game_udp": {
            "distinct_authenticated_accounts": len(last_seen),
            "last_packet_age_seconds": sorted(
                round((now - seen).total_seconds(), 1) for seen in last_seen.values()
            ),
            "distinct_endpoint_counts": sorted(len(endpoints.get(owner, ())) for owner in last_seen),
            "syn": opcodes[2],
            "data": opcodes[4] + opcodes[0x34],
            "heartbeats": opcodes[5],
            "fin_or_reset": opcodes[6] + opcodes[7],
            "rejected_datagrams": events["game_datagram_rejected"],
            "outbound_retransmitted": events["game_reliable_packet_retransmitted"],
            "outbound_retransmit_unavailable": events["game_reliable_retransmit_unavailable"],
            "inbound_gap_reports": deferred_reasons["gap"],
            "inbound_duplicate_or_old": deferred_reasons["duplicate_or_old"],
            "inbound_resend_requests": events["game_request_missing_packet_requested"],
            "inbound_recovery_exhausted": events["game_request_recovery_exhausted"],
        },
        "team_peer": {
            "client_disconnect_reports": len(peer_reports),
            "report_episodes_5s": episodes,
            "server_recovery_pushes": events["game_team_peer_recovery_sent"],
            "client_reports_recovered": peer_outcomes["recovered"],
            "client_reports_not_recovered": peer_outcomes["not_recovered"],
            "foreign_overlays_retired": peer_outcomes["overlay_retired"],
            "first_attempt_pushes": recovery_attempts[1],
            "second_attempt_pushes": recovery_attempts[2],
            "pushes_without_attempt_field": recovery_attempts[0],
            "first_report_utc": peer_reports[0].isoformat() if peer_reports else None,
            "last_report_utc": peer_reports[-1].isoformat() if peer_reports else None,
        },
        "interpretation_limit": "Server UDP health cannot prove direct RPCN peer-link health.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=30)
    args = parser.parse_args()
    report = summarize(sys.stdin, now=datetime.now(timezone.utc), minutes=args.minutes)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["malformed_lines"] == report["oversized_lines"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
