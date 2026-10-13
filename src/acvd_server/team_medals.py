"""Durable Team Medal ownership, counters, and ranking-point awards.

The installed ``TeamMedalParam`` contains 32 medals in six progress families
plus the team-creation medal. This module keeps the counters server-owned and
makes each medal's point award exactly once, even when a battle result is
retried. The 0x03FD projection uses required summary fields and a separate
field-7 ownership list; award calculation remains independent of that encoding.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .team_progression import apply as apply_team_progression, view as team_progress_view

UINT32_MAX = (1 << 32) - 1
TEAM_MEDAL_STATE_VERSION = 1

# Exact MedalPoint values from the 32 TeamMedalParam rows.
MEDAL_POINTS = (
    50,
    100,
    200,
    250,
    300,
    350,
    400,
    450,
    500,
    50,
    100,
    200,
    300,
    400,
    500,
    50,
    100,
    200,
    300,
    400,
    500,
    50,
    100,
    200,
    300,
    50,
    100,
    50,
    100,
    300,
    300,
    300,
)

AC_KILL_THRESHOLDS = (100, 500, 1_000, 5_000, 10_000, 50_000)
FAME_THRESHOLDS = (5_000, 10_000, 50_000, 100_000, 500_000, 1_000_000)
PLAYER_MEDAL_THRESHOLDS = (100, 500, 1_000, 5_000)
WORLD_SORTIE_THRESHOLDS = (10, 50)
PVP_WIN_THRESHOLDS = (5, 20)
RANKING_THRESHOLDS = (1_000, 500, 100)

_STATE_DEFAULTS = {
    "team_medal_state_version": TEAM_MEDAL_STATE_VERSION,
    "team_medal_ids": [],
    "team_ac_kills": 0,
    "team_fame_points": 0,
    "team_player_medals": 0,
    "team_world_sorties": 0,
    "team_pvp_wins": 0,
    "team_best_ranking": 0,
}


def _valid_team(team: dict | None) -> bool:
    return isinstance(team, dict) and type(team.get("team_id")) is int and team["team_id"] > 0


def _validate(team: dict) -> None:
    if not _valid_team(team):
        raise ValueError("Team Medal state requires one valid team")
    if team.get("team_medal_state_version") != TEAM_MEDAL_STATE_VERSION:
        raise ValueError("Stored Team Medal version needs review")
    medal_ids = team.get("team_medal_ids")
    if (
        not isinstance(medal_ids, list)
        or any(type(value) is not int or not 1 <= value <= 32 for value in medal_ids)
        or medal_ids != sorted(set(medal_ids))
    ):
        raise ValueError("Stored Team Medal ownership needs review")
    for key in (
        "team_ac_kills",
        "team_fame_points",
        "team_player_medals",
        "team_world_sorties",
        "team_pvp_wins",
    ):
        value = team.get(key)
        if type(value) is not int or not 0 <= value <= UINT32_MAX:
            raise ValueError(f"Stored {key} needs review")
    ranking = team.get("team_best_ranking")
    if type(ranking) is not int or not 0 <= ranking <= UINT32_MAX:
        raise ValueError("Stored Team Medal ranking needs review")
    team_progress_view(team)


def _threshold_ids(start: int, value: int, thresholds: tuple[int, ...]) -> set[int]:
    return {
        start + index
        for index, threshold in enumerate(thresholds)
        if value >= threshold
    }


def _eligible_ids(team: dict) -> set[int]:
    progress = team_progress_view(team)
    eligible = set(range(1, progress.rank_tier + 1))
    eligible.update(_threshold_ids(10, team["team_ac_kills"], AC_KILL_THRESHOLDS))
    eligible.update(_threshold_ids(16, team["team_fame_points"], FAME_THRESHOLDS))
    eligible.update(
        _threshold_ids(22, team["team_player_medals"], PLAYER_MEDAL_THRESHOLDS)
    )
    eligible.update(
        _threshold_ids(26, team["team_world_sorties"], WORLD_SORTIE_THRESHOLDS)
    )
    eligible.update(_threshold_ids(28, team["team_pvp_wins"], PVP_WIN_THRESHOLDS))
    ranking = team["team_best_ranking"]
    if ranking:
        eligible.update(
            30 + index
            for index, threshold in enumerate(RANKING_THRESHOLDS)
            if ranking <= threshold
        )
    return eligible


def _award_eligible(team: dict) -> tuple[int, ...]:
    """Award all currently eligible medals, including point-driven cascades."""
    _validate(team)
    awarded: list[int] = []
    while True:
        owned = set(team["team_medal_ids"])
        new_ids = sorted(_eligible_ids(team) - owned)
        if not new_ids:
            break
        points = sum(MEDAL_POINTS[medal_id - 1] for medal_id in new_ids)
        if team["team_fame_points"] + points > UINT32_MAX:
            raise ValueError("Team Medal points exceed their native bound")
        team["team_medal_ids"] = sorted((*owned, *new_ids))
        team["team_fame_points"] += points
        apply_team_progression(team, rank_delta=points, special_delta=0)
        awarded.extend(new_ids)
    return tuple(awarded)


def initialize(team: dict) -> bool:
    """Initialize one legacy/new team and award every already-proven medal."""
    if not _valid_team(team):
        raise ValueError("Team Medal initialization requires one valid team")
    present = {key for key in _STATE_DEFAULTS if key in team}
    if present and present != set(_STATE_DEFAULTS):
        raise ValueError("Partial Team Medal state needs review")
    changed = False
    if not present:
        legacy_fame = team_progress_view(team).score
        team.update(_STATE_DEFAULTS)
        team["team_medal_ids"] = []
        team["team_fame_points"] = legacy_fame
        changed = True
    _validate(team)
    if _award_eligible(team):
        changed = True
    return changed


def owned_rank_medals(team: dict | None) -> tuple[int, ...]:
    """Return creation/rank medal IDs without requiring a durable migration."""
    if not _valid_team(team):
        return ()
    return tuple(range(1, team_progress_view(team).rank_tier + 1))


def owned_medals(team: dict | None) -> tuple[int, ...]:
    if not _valid_team(team):
        return ()
    stored = team.get("team_medal_ids")
    if stored is None:
        return owned_rank_medals(team)
    _validate(team)
    return tuple(stored)


def fame_points(team: dict | None) -> int:
    if not _valid_team(team):
        return 0
    stored = team.get("team_fame_points")
    if stored is None:
        return sum(MEDAL_POINTS[medal_id - 1] for medal_id in owned_rank_medals(team))
    _validate(team)
    return stored


def title_is_owned(team: dict | None, title_id: int) -> bool:
    """TeamMedalParam HonorID matches medal IDs 1..32; zero clears the title."""
    return type(title_id) is int and (title_id == 0 or title_id in owned_medals(team))


def _add_counter(team: dict, key: str, delta: int) -> None:
    if type(delta) is not int or delta < 0 or team[key] + delta > UINT32_MAX:
        raise ValueError(f"Team Medal {key} delta needs review")
    team[key] += delta


def apply_progress(
    team: dict,
    *,
    ac_kills_delta: int = 0,
    fame_delta: int = 0,
    player_medals_delta: int = 0,
    world_sorties_delta: int = 0,
    pvp_wins_delta: int = 0,
) -> dict:
    """Apply one authenticated, non-replayed progress event to a team."""
    initialize(team)
    if type(fame_delta) is not int or not -UINT32_MAX <= fame_delta <= UINT32_MAX:
        raise ValueError("Team fame delta needs review")
    previous = {
        "team_ac_kills": team["team_ac_kills"],
        "team_fame_points": team["team_fame_points"],
        "team_player_medals": team["team_player_medals"],
        "team_world_sorties": team["team_world_sorties"],
        "team_pvp_wins": team["team_pvp_wins"],
        "team_best_ranking": team["team_best_ranking"],
        "team_medal_ids": tuple(team["team_medal_ids"]),
    }
    _add_counter(team, "team_ac_kills", ac_kills_delta)
    _add_counter(team, "team_player_medals", player_medals_delta)
    _add_counter(team, "team_world_sorties", world_sorties_delta)
    _add_counter(team, "team_pvp_wins", pvp_wins_delta)
    updated_fame = max(0, team["team_fame_points"] + fame_delta)
    if updated_fame > UINT32_MAX:
        raise ValueError("Team fame exceeds its native bound")
    team["team_fame_points"] = updated_fame
    awarded = _award_eligible(team)
    return {
        "previous": previous,
        "current": {
            "team_ac_kills": team["team_ac_kills"],
            "team_fame_points": team["team_fame_points"],
            "team_player_medals": team["team_player_medals"],
            "team_world_sorties": team["team_world_sorties"],
            "team_pvp_wins": team["team_pvp_wins"],
            "team_best_ranking": team["team_best_ranking"],
            "team_medal_ids": tuple(team["team_medal_ids"]),
        },
        "awarded_medal_ids": awarded,
    }


def ranking_positions(teams: Iterable[dict]) -> dict[int, int]:
    """Return stable one-based community rankings for participating teams."""
    candidates = []
    for team in teams:
        if not _valid_team(team):
            continue
        initialize(team)
        if team["team_world_sorties"] <= 0:
            continue
        candidates.append(team)
    candidates.sort(key=lambda team: (-team["team_fame_points"], team["team_id"]))
    return {team["team_id"]: index for index, team in enumerate(candidates, 1)}


def award_ranking_medals(teams: Mapping[str, dict]) -> dict[int, tuple[int, ...]]:
    """Snapshot rankings and durably retain every newly achieved tier."""
    positions = ranking_positions(teams.values())
    awarded: dict[int, tuple[int, ...]] = {}
    for team in teams.values():
        if not _valid_team(team):
            continue
        initialize(team)
        position = positions.get(team["team_id"])
        if position is not None and (
            team["team_best_ranking"] == 0 or position < team["team_best_ranking"]
        ):
            team["team_best_ranking"] = position
        new_ids = _award_eligible(team)
        if new_ids:
            awarded[team["team_id"]] = new_ids
    return awarded
