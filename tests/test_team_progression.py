"""Team rank, Normal-earned Special gauge and active-war entry boundaries."""

import json
import time
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from acvd_server.matchmaking import MatchmakingCoordinator
from acvd_server.protocol import decode_proto, first_field
from acvd_server.team_medals import (
    MEDAL_POINTS,
    apply_progress as apply_team_medal_progress,
    award_ranking_medals,
    fame_points,
    initialize as initialize_team_medals,
    owned_medals,
    owned_rank_medals,
)
from acvd_server.team_messages import _team_info, _team_medal_info
from acvd_server.team_progression import (
    SPECIAL_SORTIE_SEGMENTS,
    apply,
    initialize,
    view,
)
from acvd_server.teams import TeamStore
from acvd_server.udp import GameSession


class Events:
    def emit(self, *_args, **_fields):
        pass


def team_record(**updates):
    team = {
        "team_id": 1,
        "name": "test",
        "leader_online_id": "Leader",
        "leader_player_id": 11,
        "team_rank_score": 0,
        "team_rank_floor_position": 0,
        "special_sortie_progress": 0,
    }
    team.update(updates)
    return team


def test_team_rank_wire_separates_rank_points_from_special_gauge():
    fields = decode_proto(_team_info(team_record(), datetime.now(timezone.utc)))
    rank = decode_proto(first_field(fields, 30).value)
    assert [first_field(rank, number).value for number in range(1, 5)] == [1, 1, 0, 1_000]
    assert first_field(fields, 13).value == 0

    progressed = team_record(team_rank_score=200, special_sortie_progress=1)
    fields = decode_proto(_team_info(progressed, datetime.now(timezone.utc)))
    rank = decode_proto(first_field(fields, 30).value)
    assert [first_field(rank, number).value for number in range(1, 5)] == [1, 1, 200, 1_000]
    assert first_field(fields, 13).value == 1


def test_rank_progress_advances_subdivisions_and_never_falls_below_earned_letter():
    team = team_record()
    assert not initialize(team)
    first = apply(team, rank_delta=1_000, special_delta=1)
    assert (first["rank_tier"], first["rank_subdivision"]) == (1, 2)
    assert view(team).rank_progress == 0
    apply(team, rank_delta=2_000, special_delta=6)
    promoted = view(team)
    assert (promoted.rank_tier, promoted.rank_subdivision) == (2, 1)
    assert promoted.special_sortie_progress == SPECIAL_SORTIE_SEGMENTS
    demoted = apply(team, rank_delta=-10_000, special_delta=0)
    assert (demoted["rank_tier"], demoted["rank_subdivision"]) == (2, 1)
    assert demoted["score"] == 3_000


def test_rank_wire_advances_from_f_plus_to_e_minus_without_looping():
    team = team_record(team_rank_score=2_999)
    before = view(team)
    assert before.rank_label == "F+"
    assert before.wire_values == (1, 3, 999, 1_000)

    apply(team, rank_delta=1, special_delta=0)
    after = view(team)
    assert after.rank_label == "E-"
    assert after.wire_values == (1, 4, 0, 1_000)


def test_creation_and_rank_medals_supply_distinct_team_fame_points():
    team = team_record()
    assert owned_rank_medals(team) == (1,)
    assert fame_points(team) == 50
    assert [f.value for f in decode_proto(_team_medal_info(team)) if f.number == 7] == [1]
    apply(team, rank_delta=3_000, special_delta=0)
    assert owned_rank_medals(team) == (1, 2)
    assert fame_points(team) == 150
    assert [f.value for f in decode_proto(_team_medal_info(team)) if f.number == 7] == [1, 2]


def test_rank_ladder_includes_ss_and_sss_medal_tiers():
    team = team_record(team_rank_score=24_000, team_rank_floor_position=24)
    progress = view(team)
    assert progress.rank_label == "SSS"
    assert progress.wire_values == (1, 21, 0, 1_000)
    assert team["team_rank_score"] == 24_000 and team["team_rank_floor_position"] == 24
    assert owned_rank_medals(team) == tuple(range(1, 10))
    assert fame_points(team) == 2_600


@pytest.mark.parametrize("position", range(27))
def test_every_stored_rank_has_a_native_glyph_without_changing_points(position):
    team = team_record(team_rank_score=position * 1_000 + 123)
    before = deepcopy(team)
    progress = view(team)
    glyph = position + 1 if position < 18 else 19 + (position - 18) // 3
    assert progress.wire_values == (1, glyph, 123, 1_000)
    assert 1 <= glyph <= 21
    assert team == before


def test_six_normal_charges_fill_the_gauge_without_an_eighth_point():
    team = team_record()
    for count in range(1, 9):
        apply(team, rank_delta=0, special_delta=1)
        assert view(team).special_sortie_progress == min(count, 6)


def test_every_team_medal_family_awards_once_and_cascades_points():
    team = team_record(team_rank_score=24_000, team_rank_floor_position=24)
    assert initialize_team_medals(team)
    assert owned_medals(team) == (*range(1, 10), 16, 17)
    audit = apply_team_medal_progress(
        team,
        ac_kills_delta=50_000,
        fame_delta=1_000_000,
        player_medals_delta=5_000,
        world_sorties_delta=50,
        pvp_wins_delta=20,
    )
    assert audit["awarded_medal_ids"] == (*range(10, 16), *range(18, 30))
    assert award_ranking_medals({"1": team}) == {1: (30, 31, 32)}
    assert owned_medals(team) == tuple(range(1, 33))
    assert fame_points(team) == 24_000 + 1_000_000 + sum(MEDAL_POINTS)
    assert view(team).wire_values == (1, 21, 999, 1_000)

    before = json.dumps(team, sort_keys=True)
    assert not apply_team_medal_progress(team)["awarded_medal_ids"]
    assert award_ranking_medals({"1": team}) == {}
    assert json.dumps(team, sort_keys=True) == before


def test_legacy_receipts_backfill_sorties_and_ranking_once(tmp_path):
    root = tmp_path / "teams"
    root.mkdir()
    state = {
        "schema_version": 1,
        "next_team_id": 2,
        "teams": {
            "1": team_record(),
        },
        "members": {"Leader": 1},
        "player_profiles": {"Leader": {"player_id": 11}},
        "world_battle_settlements": [
            {
                "result_digest": f"{index:064x}",
                "team_progress": [
                    {"team_id": 1, "rank_delta": 200, "special_delta": 1}
                ],
            }
            for index in range(10)
        ],
    }
    (root / "teams.json").write_text(json.dumps(state))

    store = TeamStore(root)
    team = store.team_by_id(1)
    assert team["team_world_sorties"] == 10
    assert team["team_pvp_wins"] == 0
    assert owned_medals(team) == (1, 26, 30, 31, 32)
    assert fame_points(team) == view(team).score == 1_000

    before = store.state_path.read_bytes()
    reloaded = TeamStore(root)
    assert reloaded.state_path.read_bytes() == before
    assert owned_medals(reloaded.team_by_id(1)) == (1, 26, 30, 31, 32)


def test_known_player_medal_counts_credit_the_current_team_only_once(tmp_path):
    store = TeamStore(tmp_path / "teams")
    team = store.create(
        online_id="Leader",
        player_id=11,
        name="test",
        operation_base=0,
        language=0,
        play_type=0,
        enlistment_method=2,
    )
    updated = deepcopy(store.state)
    updated["player_profiles"]["Leader"].update(
        player_id=11,
        player_medals={
            "player_id": 11,
            "records": [{"medal_id": 5, "award_count": 100, "progress_value": 0,
                         "first_date": "2026-10-01T00:00:00+00:00",
                         "second_date": "2026-10-01T00:00:00+00:00"}],
        },
    )
    store.commit_state(updated)

    refreshed = store.refresh_team_medal_state("Leader")
    assert refreshed["team_player_medals"] == 100
    assert owned_medals(refreshed) == (1, 22)
    assert fame_points(refreshed) == 1100  # creation50 + 100*MedalPoint10 + count medal50
    before = store.state_path.read_bytes()
    assert store.refresh_team_medal_state("Leader")["team_id"] == team["team_id"]
    assert store.state_path.read_bytes() == before


def test_team_store_consumes_one_full_special_gauge_atomically(tmp_path):
    store = TeamStore(tmp_path / "teams")
    team = store.create(
        online_id="Leader",
        player_id=11,
        name="test",
        operation_base=0,
        language=0,
        play_type=0,
        enlistment_method=2,
    )
    state = deepcopy(store.state)
    state["teams"][str(team["team_id"])]["special_sortie_progress"] = 8
    store.commit_state(state)
    assert store.special_sortie_ready(team["team_id"])
    assert store.consume_special_sortie(team["team_id"]) == {
        "team_id": team["team_id"],
        "previous_special_sortie_progress": 6,
        "special_sortie_progress": 0,
    }
    assert not store.special_sortie_ready(team["team_id"])
    assert store.consume_special_sortie(team["team_id"]) is None


def test_world_wallet_and_team_progress_share_one_idempotent_commit(tmp_path):
    store = TeamStore(tmp_path / "teams")
    team = store.create(
        online_id="Leader",
        player_id=11,
        name="test",
        operation_base=0,
        language=0,
        play_type=0,
        enlistment_method=2,
    )
    store.synchronize_balance("Leader", 100_000)
    kwargs = dict(
        policy_version="test-team-progress-v1",
        team_progress=(
            {"team_id": team["team_id"], "rank_delta": 200, "special_delta": 1},
        ),
        team_medal_progress=(
            {
                "team_id": team["team_id"],
                "ac_kills_delta": 1,
                "fame_delta": 200,
                "world_sorties_delta": 1,
                "pvp_wins_delta": 0,
            },
        ),
    )
    payment = (
        {
            "online_id": "Leader",
            "player_id": 11,
            "gross_earnings": 150_000,
            "expenses": 25_000,
            "response_hex": "0800",
        },
    )
    first = store.settle_world_battle_money("11" * 32, payment, **kwargs)
    assert first["applied"]
    assert store.balance("Leader") == 225_000
    progress = view(store.team_by_id(team["team_id"]))
    assert progress.score == 1_150 and progress.special_sortie_progress == 1
    awarded = store.team_by_id(team["team_id"])
    assert awarded["team_ac_kills"] == 1 and awarded["team_world_sorties"] == 1
    assert owned_medals(awarded) == (1, 30, 31, 32)
    replay = store.settle_world_battle_money("11" * 32, payment, **kwargs)
    assert not replay["applied"]
    assert store.balance("Leader") == 225_000
    progress = view(store.team_by_id(team["team_id"]))
    assert progress.score == 1_150 and progress.special_sortie_progress == 1
    replayed = store.team_by_id(team["team_id"])
    assert replayed["team_ac_kills"] == 1 and replayed["team_world_sorties"] == 1
    assert owned_medals(replayed) == (1, 30, 31, 32)


def test_active_war_special_registration_requires_full_gauge():
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        send_push=lambda *_args, **_kwargs: None,
        telemetry=Events(),
        room_log_paths=[],
        special_sortie_ready=lambda _team_id: False,
    )
    session = GameSession(
        b"owner001",
        bytes(16),
        "Leader",
        11,
        established=True,
        peer=("127.0.0.1", 1111),
    )
    with pytest.raises(ValueError, match="full Normal-mission gauge"):
        coordinator.validate_registration(
            session,
            {1: 3, 2: 2, 3: 2},
            [11],
            1,
            2,
            [0],
        )


def test_active_single_team_special_world_boss_consumes_full_gauge(tmp_path):
    store = TeamStore(tmp_path / "teams")
    team = store.create(
        online_id="Leader",
        player_id=11,
        name="test",
        operation_base=0,
        language=0,
        play_type=0,
        enlistment_method=2,
    )
    state = deepcopy(store.state)
    current = state["teams"][str(team["team_id"])]
    current.update(current_faction_id=1, territory_area_id=3, special_sortie_progress=8)
    store.commit_state(state)
    pushes = []
    coordinator = MatchmakingCoordinator(
        get_team_sessions=lambda: {},
        send_push=lambda *args, **kwargs: pushes.append((args, kwargs)),
        telemetry=Events(),
        room_log_paths=[],
        special_sortie_ready=store.special_sortie_ready,
        consume_special_sortie=store.consume_special_sortie,
    )
    session = GameSession(
        b"owner001",
        bytes(16),
        "Leader",
        11,
        established=True,
        peer=("127.0.0.1", 1111),
    )
    preparation = {
        "session": session,
        "prepare_id": 1,
        "team_id": team["team_id"],
        "faction_id": 1,
        "territory_area_id": 3,
        "territory_layout_owner_id": 0,
        "criteria": {1: 3, 2: 2, 3: 2, 8: 3},
        "world_phase": 2,
        "claimed_player_ids": [11],
        "claimed_player_roles": [0],
        "squad": {11: (session, team["team_id"])},
        "match_id": 0,
        "registered_monotonic": time.monotonic(),
        "search_started_monotonic": time.monotonic(),
    }
    coordinator.preparations[session.online_id] = preparation
    assert coordinator._release_active_single_team(preparation)
    assert len(pushes) == 1
    assert view(store.team_by_id(team["team_id"])).special_sortie_progress == 0
    assert coordinator.matches[1]["battle_type"] == 6
    assert coordinator.matches[1]["special_progress_consumed"] == {
        "team_id": team["team_id"],
        "previous_special_sortie_progress": 6,
        "special_sortie_progress": 0,
    }
