"""Stored medal readback and rating semantics; no invented native awards."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone

import pytest

from acvd_server.player_medals import (
    RATING_BONUSES,
    UNOWNED_DATE,
    VERIFIED_THRESHOLDS,
    MedalRecord,
    award_verified_medals,
    encode_records,
    load_records,
    rating_bonus,
    stored_rating_bonus,
)
from acvd_server.protocol import ProtocolError, decode_proto
from acvd_server.teams import TeamStore
from acvd_server.udp import GameSession

DATE = datetime(2026, 9, 14, 12, 30, tzinfo=timezone.utc)


def row(record):
    data = asdict(record)
    data["first_date"] = record.first_date.isoformat()
    data["second_date"] = record.second_date.isoformat()
    return data


def test_wire_has_all_five_required_fields_and_two_complete_dates():
    record = MedalRecord(1, 2, 4, DATE, DATE)
    (outer,) = decode_proto(encode_records((record,)))
    fields = {f.number: f.value for f in decode_proto(outer.value)}
    assert {n: fields[n] for n in (1, 2, 3)} == {1: 1, 2: 2, 3: 4}
    assert {f.number: f.value for f in decode_proto(fields[4])} == {
        1: 2026,
        2: 9,
        3: 14,
        4: 12,
        5: 30,
        6: 0,
        7: 0,
    }
    assert fields[4] == fields[5]


def test_rank_bonus_is_once_per_valid_type_not_award_count_or_progress():
    first = MedalRecord(1, 9, 100, DATE, DATE)
    assert rating_bonus((first,)) == 100
    assert rating_bonus((replace(first, first_date=UNOWNED_DATE),)) == 0
    # The client validity check uses date4, not the second date or count.
    assert rating_bonus((replace(first, award_count=0, second_date=UNOWNED_DATE),)) == 100
    assert rating_bonus((first, MedalRecord(4, 1, 0, DATE, DATE))) == 600


def test_unknown_empty_and_owned_ledgers_stay_distinct_and_read_only(tmp_path):
    teams = TeamStore(tmp_path / "teams")
    teams.remember_player("One", 11)
    actor = GameSession(b"owner001", bytes(16), "One", 11)
    before = teams.state_path.read_bytes()
    assert load_records(teams, actor) is None and stored_rating_bonus(teams, actor) == 0
    assert teams.state_path.read_bytes() == before
    updated = deepcopy(teams.state)
    updated["player_profiles"]["One"]["player_medals"] = {"player_id": 11, "records": []}
    teams.commit_state(updated)
    assert load_records(teams, actor) == ()
    medal = MedalRecord(1, 1, 0, DATE, DATE)
    updated = deepcopy(teams.state)
    updated["player_profiles"]["One"]["player_medals"]["records"] = [row(medal)]
    teams.commit_state(updated)
    loaded = TeamStore(teams.root)
    before = loaded.state_path.read_bytes()
    assert load_records(loaded, actor) == (medal,) and stored_rating_bonus(loaded, actor) == 100
    assert loaded.state_path.read_bytes() == before
    with pytest.raises(ProtocolError):
        load_records(loaded, replace(actor, player_id=12))


@pytest.mark.parametrize("records", [None, [], (MedalRecord(1, 1, 0, DATE, DATE),) * 2])
def test_unknown_or_ambiguous_list_cannot_be_encoded(records):
    with pytest.raises(ProtocolError):
        encode_records(records)


@pytest.mark.parametrize(
    "changes",
    [
        {"medal_id": 0},
        {"medal_id": 49},
        {"medal_id": True},
        {"award_count": -1},
        {"progress_value": 1 << 32},
        {"first_date": DATE.replace(tzinfo=None)},
    ],
)
def test_invalid_records_are_not_silently_normalized(changes):
    with pytest.raises(ProtocolError):
        replace(MedalRecord(1, 1, 0, DATE, DATE), **changes)


@pytest.mark.parametrize("counter,thresholds", VERIFIED_THRESHOLDS.items())
def test_awards_only_at_verified_thresholds_and_never_duplicates(counter, thresholds):
    profile = {"player_id": 11}
    for wins in range(thresholds[-1][1] + 2):
        profile[counter] = wins
        awarded = award_verified_medals(profile, 11, DATE)
        assert awarded == tuple(mid for mid, target in thresholds if wins == target)
        if wins < thresholds[0][1]:
            assert "player_medals" not in profile
        else:
            records = profile["player_medals"]["records"]
            assert [r["medal_id"] for r in records] == [
                mid for mid, target in thresholds if wins >= target
            ]
            assert all(r["award_count"] == 1 and r["progress_value"] == 0 for r in records)
            assert all(r["first_date"] == DATE.isoformat() for r in records)
        before = deepcopy(profile)
        assert award_verified_medals(profile, 11, DATE) == ()
        assert profile == before


def test_existing_medals_and_opaque_values_survive_recognition():
    existing = row(MedalRecord(1, 9, 12, DATE, DATE))
    unfinished = row(MedalRecord(26, 0, 2, UNOWNED_DATE, DATE))
    profile = {
        "player_id": 11,
        "observed_operator_wins": 3,
        "player_medals": {"player_id": 11, "records": [existing, unfinished]},
    }
    assert award_verified_medals(profile, 11, DATE) == (26,)
    assert profile["player_medals"]["records"][0] == existing
    assert profile["player_medals"]["records"][1]["progress_value"] == 2
    assert profile["player_medals"]["records"][1]["second_date"] == DATE.isoformat()


@pytest.mark.parametrize("bad", [-1, True, "5", 1 << 32])
def test_bad_counter_cannot_partially_change_ledger(bad):
    profile = {"player_id": 11, "observed_operator_wins": 10, "observed_mercenary_wins": bad}
    before = deepcopy(profile)
    with pytest.raises(ProtocolError):
        award_verified_medals(profile, 11, DATE)
    assert profile == before


def test_wrong_account_cannot_acquire_another_accounts_medals():
    profile = {
        "player_id": 11,
        "observed_operator_wins": 10,
        "player_medals": {"player_id": 12, "records": []},
    }
    with pytest.raises(ProtocolError):
        award_verified_medals(profile, 11, DATE)


def test_proven_history_recognition_preserves_money_and_drives_rating(tmp_path):
    teams = TeamStore(tmp_path / "teams")
    teams.remember_player("Pilot", 11)
    teams.synchronize_balance("Pilot", 123456)
    updated = deepcopy(teams.state)
    updated["player_profiles"]["Pilot"].update(observed_operator_wins=5, observed_mercenary_wins=10)
    teams.commit_state(updated)
    assert teams.refresh_player_medal_state("Pilot", 11) == (26, 27, 33, 34)
    actor = GameSession(b"owner001", bytes(16), "Pilot", 11)
    assert stored_rating_bonus(teams, actor) == sum(
        RATING_BONUSES[mid - 1] for mid in (26, 27, 33, 34)
    )
    assert teams.player_profile("Pilot")["balance"] == 123456
    before = teams.state_path.read_bytes()
    assert teams.refresh_player_medal_state("Pilot", 11) == ()
    assert teams.state_path.read_bytes() == before
    with pytest.raises(ValueError):
        teams.refresh_player_medal_state("Pilot", 12)
