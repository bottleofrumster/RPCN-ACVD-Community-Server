"""Pin historical bytes except explicit native-qualified protocol corrections."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from acvd_server import server

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/response_cleanup_baseline.json").read_text()
)


def decoded(value):
    if isinstance(value, list):
        return [decoded(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$bytes"}:
            return bytes.fromhex(value["$bytes"])
        if set(value) == {"$datetime"}:
            return datetime.fromisoformat(value["$datetime"])
        if set(value) == {"$tuple"}:
            return tuple(decoded(item) for item in value["$tuple"])
        return {key: decoded(item) for key, item in value.items()}
    return value


# Preserve the historical fixture, but do not require the disproved Story
# expense-reflection response. Its corrected contract is in test_story_rewards.
UNCHANGED_CASES = [
    case for case in FIXTURE["cases"] if case["function"] != "_register_battle_result_response"
]


@pytest.mark.parametrize("case", UNCHANGED_CASES, ids=lambda case: case["function"])
def test_response_matches_pre_refactor_bytes(case):
    kwargs = decoded(case["kwargs"])
    if case["function"] == "_team_session_leave_push":
        # Only the argument name changed here. Call-site tests cover the
        # recovered global-ID meaning; preserve this serializer byte fixture.
        kwargs["player_id"] = kwargs.pop("member_id")
    if case["function"] == "_team_session_member_push":
        # Field 4 was disproved as a durable team number. The fixture's value
        # happens to be the corrected active-peer enum, so only adapt the API.
        kwargs.pop("member_id")
    if case["function"] == "_team_session_member_push_pair":
        kwargs.pop("new_member_id")
    if case["function"] == "_world_basic_info":
        # This historical fixture encodes grace. Keep it byte-exact explicitly;
        # test_world_phase_coherence covers the active default and shared policy.
        kwargs["phase"] = 0
    actual = getattr(server, case["function"])(**kwargs)
    expected = decoded(case["expected"])
    if case["function"] == "_user_info" and kwargs.get("team") is not None:
        # Native field10 is Language, not the team's Operation Base.
        assert expected.count(bytes.fromhex("5002580060026801")) == 1
        expected = expected.replace(
            bytes.fromhex("5002580060026801"), bytes.fromhex("5001580060026801"), 1
        )
        # UserInfo converter0x1860074: field6 -> native team identity+0x24;
        # field9 -> native Operation Base+0x90, not the other way around.
        assert expected.count(bytes.fromhex("300038004801")) == 1
        expected = expected.replace(bytes.fromhex("300038004801"), bytes.fromhex("300138004802"), 1)
    if case["function"] == "_team_rank_info":
        # Team rank and Special progression are now server-owned. Visible
        # controls prove fields 3/4 are current/required rank points, not
        # the separate Special gauge returned by TeamInfo field 13.
        assert expected == bytes.fromhex("0801100018002000")
        expected = bytes.fromhex("08011001180020e807")
    if case["function"] == "_world_log" and case["kwargs"]["requested_phase"] == 0:
        # The old compatibility AreaBasicInfo was not history. Its two zero
        # faction scalars render as "Unaffiliated has withdrawn." Preserve the
        # genuine phase record but remove the fabricated area event.
        fake_area = bytes.fromhex("2206080110001800")
        assert expected.count(fake_area) == 1
        expected = expected.replace(fake_area, b"", 1)
    if case["function"] == "_team_medal_info":
        # Exact type-name/opcode recovery restores required scalars1..6/8.
        # Repeated medal IDs belong in field7, empty for an absent team.
        assert expected == bytes.fromhex(
            "08000a001000120018001a002000220028002a003000320038003a0040004200"
        )
        expected = bytes.fromhex("0800100018002000280030004000")
    if case["function"] in ("_team_info", "_team_info_list_response"):
        # This historical fixture has no enrollment value. Its field 3 used
        # to copy Operation Base=2. Native creation/readback controls prove
        # field 3 is enrollment; the missing policy defaults to Closed=0.
        # Change only that scalar and retain every other baseline byte.
        marker = bytes.fromhex("0801100b1802")
        assert expected.count(marker) == 1
        expected = expected.replace(marker, bytes.fromhex("0801100b1800"), 1)
        old_rank = bytes.fromhex("f201080801100018002000")
        assert expected.count(old_rank) == 1
        expected = expected.replace(
            old_rank,
            bytes.fromhex("f2010908011001180020e807"),
            1,
        )
        # @HopeFaction reads field7, not Team Points. Preserve default choice1.
        old_points = bytes.fromhex("30003800")
        assert expected.count(old_points) == 1
        expected = expected.replace(old_points, bytes.fromhex("30003801"), 1)
        # Native Sortie Text uses field25. This fixture has no selection;
        # preserve the unrelated compatibility field27.
        duplicated_faction = bytes.fromhex("c80101d201055265616479d80101")
        assert expected.count(duplicated_faction) == 1
        expected = expected.replace(
            duplicated_faction,
            bytes.fromhex("c80100d201055265616479d80101"),
            1,
        )
        if case["function"] == "_team_info_list_response":
            assert expected.startswith(bytes.fromhex("0ae801"))
            expected = bytes.fromhex("0ae901") + expected[3:]
    if case["function"] == "_team_session_member_push_pair":
        assert isinstance(expected, tuple) and len(expected) == 2
        old_state = bytes.fromhex("2002")
        assert expected[1].count(old_state) == 1
        expected = (expected[0], expected[1].replace(old_state, bytes.fromhex("2001"), 1))
    # Preserve the original fixture. Normal room 13 qualifies this one change.
    if case["function"] == "_matching_result_for_members" and case["kwargs"]["is_host"]:
        assert expected.hex() == "080b10161801200028013000380040004801"
        expected = bytes.fromhex("080b10161801200128013000380040004801")
    if case["function"] == "_my_player_ranking_response":
        # Exact serializer and native leaderboard controls establish strings
        # in PlayerRankingInfo fields2/3, not nested RankingBasicInfo records.
        expected = bytes.fromhex("0a0a080012001a0020002800")
    if case["function"] == "_team_ranking_info":
        # Native rows carry team identity in field1 and placement in field4.
        assert expected.startswith(bytes.fromhex("0800"))
        expected = bytes.fromhex("0801") + expected[2:]
    assert actual == expected
