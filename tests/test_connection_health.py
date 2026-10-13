"""Read-only transport triage keeps private peer/account fields out of output."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from tools.analysis.connection_health import MAX_LINE, summarize

NOW = datetime(2026, 9, 21, 13, 15, tzinfo=timezone.utc)


def event(seconds, name, **fields):
    return json.dumps(
        {"timestamp_utc": (NOW - timedelta(seconds=seconds)).isoformat(), "event": name, **fields}
    )


def test_connection_health_separates_game_udp_and_peer_reports_without_identity_leaks():
    lines = [
        event(90, "game_packet_decoded", opcode=2, online_id="private-A", peer=["192.0.2.1", 1234]),
        event(80, "game_packet_decoded", opcode=4, online_id="private-A", peer=["192.0.2.1", 1234]),
        event(70, "game_packet_decoded", opcode=52, online_id="private-A", peer=["192.0.2.1", 5678]),
        event(65, "game_session_established", online_id="private-A"),
        event(60, "game_packet_decoded", opcode=5, online_id="private-B", peer=["198.51.100.2", 4567]),
        event(50, "game_request_sequence_deferred", reason="gap", online_id="private-A"),
        event(49, "game_request_sequence_deferred", reason="duplicate_or_old", online_id="private-A"),
        event(40, "game_request_missing_packet_requested", attempt=1, online_id="private-A"),
        event(30, "game_team_session_member_disconnect_reported", online_id="private-A", real_team_peer_recovered=True),
        event(29, "game_team_session_member_disconnect_reported", online_id="private-B", real_team_peer_recovered=False),
        event(28, "game_team_peer_recovery_sent", online_id="private-A", attempt=2),
        event(10, "game_team_session_member_disconnect_reported", online_id="private-A", foreign_overlay_retired=True),
    ]
    result = summarize(lines, now=NOW, minutes=5)
    assert result["game_udp"] == {
        "distinct_authenticated_accounts": 2,
        "last_packet_age_seconds": [60.0, 70.0],
        "distinct_endpoint_counts": [1, 2],
        "syn": 1,
        "data": 2,
        "heartbeats": 1,
        "fin_or_reset": 0,
        "rejected_datagrams": 0,
        "outbound_retransmitted": 0,
        "outbound_retransmit_unavailable": 0,
        "inbound_gap_reports": 1,
        "inbound_duplicate_or_old": 1,
        "inbound_resend_requests": 1,
        "inbound_recovery_exhausted": 0,
    }
    assert result["team_peer"]["client_disconnect_reports"] == 3
    assert result["team_peer"]["report_episodes_5s"] == 2
    assert result["team_peer"]["server_recovery_pushes"] == 1
    assert result["team_peer"]["client_reports_recovered"] == 1
    assert result["team_peer"]["client_reports_not_recovered"] == 1
    assert result["team_peer"]["foreign_overlays_retired"] == 1
    assert result["team_peer"]["second_attempt_pushes"] == 1
    rendered = json.dumps(result)
    for private in ("private-A", "private-B", "192.0.2.1", "198.51.100.2", "1234"):
        assert private not in rendered


def test_connection_health_rejects_unbounded_input_and_counts_bad_lines():
    result = summarize(
        [event(1800, "game_datagram_rejected"), "bad json", "x" * (MAX_LINE + 1)],
        now=NOW,
        minutes=5,
    )
    assert result["accepted_events"] == 0
    assert result["malformed_lines"] == result["oversized_lines"] == 1
    with pytest.raises(ValueError, match="exceeds"):
        summarize((event(0, "game_packet_decoded") for _ in range(50_001)), now=NOW, minutes=5)


@pytest.mark.parametrize("now,minutes", [(NOW.replace(tzinfo=None), 5), (NOW, 0), (NOW, 1441)])
def test_connection_health_requires_bounded_aware_window(now, minutes):
    with pytest.raises(ValueError):
        summarize([], now=now, minutes=minutes)
