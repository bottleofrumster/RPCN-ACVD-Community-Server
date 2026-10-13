"""Community regressions: earnings, medal points and stable highest-first boards."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest
from test_peer_pilot_name import fixture as profile_fixture, read_peer

from acvd_server.mercenary_earnings import reconcile
from acvd_server.mercenary_messages import _mercenary_ranking_list_response
from acvd_server.player_medals import UNOWNED_DATE, MedalRecord, medal_points
from acvd_server.protocol import decode_proto, first_field
from acvd_server.rankings import (
    RankingPage,
    defeat_rows,
    mercenary_rows,
    page,
    player_rows,
    team_rows,
)
from acvd_server.teams import MAX_BALANCE, TeamStore


def team(store, name, player_id):
    return store.create(online_id=name, player_id=player_id, name=name, operation_base=0,
                        language=2, play_type=0, enlistment_method=2)


def pay(store, name, player_id, gross, sequence, *, won=0, role=1):
    store.remember_player(name, player_id)
    if "balance" not in store.player_profile(name):
        store.synchronize_balance(name, 100000)
    payment = {"online_id": name, "player_id": player_id, "gross_earnings": gross,
               "expenses": 1000, "response_hex": "0800"}
    progress = {"player_id": player_id, "ac_destructions_delta": 0,
                "mercenary_sorties_delta": role, "mercenary_wins_delta": won}
    return store.settle_world_battle_money(
        f"{sequence:064x}", (payment,), policy_version="test-gross-earnings",
        player_ranking_progress=(progress,),
    )


def test_mercenary_board_uses_total_pay_not_wins_or_wallet_and_highest_first(tmp_path):
    store = TeamStore(tmp_path / "teams")
    pay(store, "Winner", 11, 10000, 1, won=1)
    pay(store, "Earner", 22, 100000, 2)
    store.synchronize_balance("Earner", MAX_BALANCE)
    pay(store, "Earner", 22, 50000, 3)
    rows = mercenary_rows(store)
    assert [(r["player_id"], r["score"], r["rank"]) for r in rows] == [
        (22, 150000, 1), (11, 10000, 2),
    ]
    assert store.player_profile("Earner")["balance"] == MAX_BALANCE
    before = deepcopy(store.state)
    assert not pay(store, "Earner", 22, 50000, 3)["applied"]
    assert store.state == before
    assert mercenary_rows(TeamStore(store.root)) == rows


def test_retained_mercenary_receipts_recover_existing_totals_once(tmp_path):
    store = TeamStore(tmp_path / "teams")
    pay(store, "Merc", 11, 80000, 1)
    pay(store, "Merc", 11, 120000, 2, won=1)
    state = deepcopy(store.state)
    state["player_profiles"]["Merc"].pop("observed_mercenary_earnings", None)
    state["player_profiles"]["Merc"].pop("mercenary_earnings_missing_sorties", None)
    store.commit_state(state)
    recovered = TeamStore(store.root)
    assert mercenary_rows(recovered)[0]["score"] == 200000
    assert recovered.player_profile("Merc")["mercenary_earnings_missing_sorties"] == 0
    before = recovered.state_path.read_bytes()
    assert TeamStore(store.root).state_path.read_bytes() == before


def test_earnings_survive_receipt_retention_expiry(tmp_path):
    store = TeamStore(tmp_path / "teams")
    pay(store, "Merc", 11, 80000, 1)
    state = deepcopy(store.state)
    state["world_battle_settlements"] = []
    store.commit_state(state)
    loaded = TeamStore(store.root)
    assert mercenary_rows(loaded)[0]["score"] == 80000
    pay(loaded, "Merc", 11, 70000, 2)
    assert mercenary_rows(loaded)[0]["score"] == 150000


def test_player_medal_points_credit_team_even_when_count_was_already_credited(tmp_path):
    store = TeamStore(tmp_path / "teams")
    team(store, "Pilot", 11)
    state = deepcopy(store.state)
    date = datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat()
    state["player_profiles"]["Pilot"].update(
        player_id=11,
        team_medal_credited_awards=1,
        player_medals={"player_id": 11, "records": [{
            "medal_id": 33, "award_count": 1, "progress_value": 0,
            "first_date": date, "second_date": date,
        }]},
    )
    state["teams"]["1"]["team_player_medals"] = 1
    store.commit_state(state)
    refreshed = store.refresh_team_medal_state("Pilot")
    # Creation50 + MedalParam medal33's300, not its PlayerRatingBonus150.
    assert refreshed["team_fame_points"] == 350
    assert refreshed["team_rank_score"] == 350
    assert refreshed["team_player_medals"] == 1
    before = deepcopy(store.state)
    store.refresh_team_medal_state("Pilot")
    assert store.state == before


def test_peer_language_is_the_players_choice_not_the_team_country_or_viewer(tmp_path):
    store = profile_fixture(tmp_path)
    store.update_team_properties("Viewer", scalar_fields={23: 3, 7: 1})
    store.update_user_properties("Viewer", scalar_fields={5: 8, 6: 1, 7: 1}, byte_fields={})
    store.update_user_properties("Target", scalar_fields={5: 47, 6: 2, 7: 5}, byte_fields={})
    before = store.state_path.read_bytes()
    fields = read_peer(TeamStore(store.root), tmp_path)
    # Native field10 selects Language, not Operation Base. Country readback
    # remains unqualified; preserve the preexisting field13 independently.
    assert [fields[number] for number in (10, 11, 13)] == [2, 5, 2]
    assert fields[15] == 1 and store.state_path.read_bytes() == before


def test_missing_personal_language_does_not_inherit_the_team_country(tmp_path):
    store = profile_fixture(tmp_path)
    store.update_team_properties("Viewer", scalar_fields={23: 47})
    assert read_peer(store, tmp_path)[10] == 0


@pytest.mark.parametrize("country", [0, 1, 5, 104, 105])
def test_personal_country_is_not_the_team_id_and_survives_reload(tmp_path, country):
    store = profile_fixture(tmp_path)
    store.update_team_properties("Viewer", scalar_fields={23: 47})
    store.update_user_properties("Viewer", scalar_fields={5: 8, 6: 1}, byte_fields={})
    store.update_user_properties("Target", scalar_fields={5: country, 6: 3}, byte_fields={})
    before = store.state_path.read_bytes()
    fields = read_peer(TeamStore(store.root), tmp_path)
    assert fields[6] == store.team_for("Target")["team_id"]
    assert fields[9] == country and fields[10] == 3
    assert fields[1] == 22 and fields[2] == b"Target"
    assert store.state_path.read_bytes() == before


def test_unknown_country_stays_zero_instead_of_inheriting_team_or_viewer(tmp_path):
    store = profile_fixture(tmp_path)
    store.update_team_properties("Viewer", scalar_fields={23: 47})
    store.update_user_properties("Viewer", scalar_fields={5: 104}, byte_fields={})
    fields = read_peer(store, tmp_path)
    assert fields[6] == store.team_for("Target")["team_id"]
    assert fields[9] == 0


def test_medal_points_count_awards_not_rating_bonus_or_unearned_progress():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert medal_points((MedalRecord(33, 3, 999, now, now),)) == 900
    assert medal_points((MedalRecord(33, 3, 999, UNOWNED_DATE, now),)) == 0


def test_losing_or_zero_pay_mercenary_sortie_is_recorded_but_ordinary_pay_is_not(tmp_path):
    store = TeamStore(tmp_path / "teams")
    pay(store, "Pilot", 11, 80000, 1, role=0)
    assert "observed_mercenary_earnings" not in store.player_profile("Pilot")
    pay(store, "Pilot", 11, 0, 2)
    pay(store, "Pilot", 11, 60000, 3)
    assert mercenary_rows(store)[0]["score"] == 60000
    assert store.player_profile("Pilot")["observed_mercenary_wins"] == 0


def test_missing_old_receipts_are_explicit_not_estimated_from_wallet(tmp_path):
    store = TeamStore(tmp_path / "teams")
    pay(store, "Merc", 11, 50000, 1)
    state = deepcopy(store.state)
    profile = state["player_profiles"]["Merc"]
    profile.pop("observed_mercenary_earnings")
    profile["observed_mercenary_sorties"] = 4
    assert reconcile(state)
    assert profile["observed_mercenary_earnings"] == 50000
    assert profile["mercenary_earnings_missing_sorties"] == 3
    before = deepcopy(state)
    assert not reconcile(state) and state == before


def test_duplicate_receipt_cannot_double_credit_backfill(tmp_path):
    store = TeamStore(tmp_path / "teams")
    pay(store, "Merc", 11, 50000, 1)
    state = deepcopy(store.state)
    state["player_profiles"]["Merc"].pop("observed_mercenary_earnings")
    state["world_battle_settlements"] *= 2
    assert reconcile(state)
    assert state["player_profiles"]["Merc"]["observed_mercenary_earnings"] == 50000


@pytest.mark.parametrize("fault", ["identity", "negative", "bool", "digest", "conflict"])
def test_untrustworthy_recovery_receipts_do_not_authorize_credit(tmp_path, fault):
    store = TeamStore(tmp_path / "teams")
    pay(store, "Merc", 11, 50000, 1)
    state = deepcopy(store.state)
    state["player_profiles"]["Merc"].pop("observed_mercenary_earnings")
    receipt = state["world_battle_settlements"][0]
    payment = receipt["participants"][0]
    if fault == "identity":
        payment["online_id"] = "Another"
    elif fault == "negative":
        payment["gross_earnings"] = -1
    elif fault == "bool":
        payment["gross_earnings"] = True
    elif fault == "digest":
        receipt["result_digest"] = "invalid"
    else:
        other = deepcopy(receipt)
        other["participants"][0]["gross_earnings"] += 1
        state["world_battle_settlements"].append(other)
    with pytest.raises(ValueError):
        reconcile(state)


def test_failed_atomic_settlement_does_not_increment_earnings(tmp_path, monkeypatch):
    store = TeamStore(tmp_path / "teams")
    pay(store, "Merc", 11, 50000, 1)
    before, disk = deepcopy(store.state), store.state_path.read_bytes()
    def fail():
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(store, "_save", fail)
    with pytest.raises(OSError):
        pay(store, "Merc", 11, 50000, 2)
    assert store.state == before and store.state_path.read_bytes() == disk


def test_team_transfer_does_not_reaward_medal_points(tmp_path):
    store = TeamStore(tmp_path / "teams")
    first = team(store, "Pilot", 11)
    second = team(store, "Other", 22)
    store.remember_player("Pilot", 11)
    state = deepcopy(store.state)
    state["player_profiles"]["Pilot"]["observed_mercenary_wins"] = 5
    store.commit_state(state)
    store.refresh_player_medal_state("Pilot", 11)
    assert store.team_by_id(first["team_id"])["team_fame_points"] == 350
    store.join_team(online_id="Pilot", player_id=11, team_id=second["team_id"], replace_existing=True)
    assert store.refresh_team_medal_state("Pilot")["team_fame_points"] == 50


def test_all_leaderboards_are_highest_first_with_global_ranks_after_paging(tmp_path):
    store = TeamStore(tmp_path / "teams")
    now = datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat()
    for name, player_id, score, medal_id in (("Low", 11, 100, 1), ("High", 22, 300, 3), ("Mid", 33, 200, 2)):
        created = team(store, name, player_id)
        state = deepcopy(store.state)
        state["teams"][str(created["team_id"])].update(team_fame_points=score, team_world_sorties=1)
        state["player_profiles"][name].update(
            player_id=player_id, observed_ac_destructions=score,
            observed_mercenary_sorties=1, observed_mercenary_wins=0,
            observed_mercenary_earnings=score,
            player_medals={"player_id": player_id, "records": [{
                "medal_id": medal_id, "award_count": 1, "progress_value": 0,
                "first_date": now, "second_date": now,
            }]},
        )
        store.commit_state(state)
    for reader in (team_rows, player_rows, defeat_rows, mercenary_rows):
        rows = reader(store)
        assert [row["score"] for row in rows] == [300, 200, 100]
        assert [row["rank"] for row in rows] == [1, 2, 3]
        assert page(rows, RankingPage(3, 1, 1)) == (rows[1],)


def test_unowned_legacy_count_watermark_does_not_block_reload_or_grant_points(tmp_path):
    store = TeamStore(tmp_path / "teams")
    team(store, "Pilot", 11)
    state = deepcopy(store.state)
    sentinel = UNOWNED_DATE.isoformat()
    state["player_profiles"]["Pilot"].update(
        player_id=11, team_medal_credited_awards=3,
        player_medals={"player_id": 11, "records": [{
            "medal_id": 33, "award_count": 3, "progress_value": 2,
            "first_date": sentinel, "second_date": sentinel,
        }]},
    )
    state["teams"]["1"]["team_player_medals"] = 3
    store.commit_state(state)
    loaded = TeamStore(store.root)
    assert loaded.team_for("Pilot")["team_fame_points"] == 50
    assert loaded.team_for("Pilot")["team_player_medals"] == 3
    assert loaded.player_profile("Pilot")["team_medal_credited_points"] == 0
    assert loaded.player_profile("Pilot")["team_medal_credited_awards"] == 3
    before = deepcopy(loaded.state)
    assert loaded.refresh_team_medal_state("Pilot") and loaded.state == before


def test_large_cumulative_pay_remains_exact_even_when_native_display_saturates(tmp_path):
    store = TeamStore(tmp_path / "teams")
    pay(store, "Merc", 11, 1 << 31, 1)
    pay(store, "Merc", 11, 1 << 31, 2)
    rows = mercenary_rows(store)
    assert rows[0]["score"] == 1 << 32
    fields = decode_proto(first_field(decode_proto(_mercenary_ranking_list_response(rows, 1)), 1).value)
    assert first_field(fields, 5).value == (1 << 31) - 1
    assert store.player_profile("Merc")["observed_mercenary_earnings"] == 1 << 32
