"""Durable replacement policy for team rank and Special Sortie charge.

TeamRankInfo uses the observed discriminator 1, a 1..21 rank glyph, points
within that rank and its threshold in fields 1 through 4.
The independent Special Sortie counter is TeamInfo field 13.
Original client data supplies mission-clear rows of 200/300, 30 points per
destroyed AC and the 10/50/85-percent hired-mercenary team-point reductions.
The retired service's row binding and rank thresholds are unavailable, so the
replacement policy explicitly binds 200 to Normal, 300 to Special and keeps
one conservative 1,000-point threshold per subdivision.
"""

from __future__ import annotations

from dataclasses import dataclass

SPECIAL_SORTIE_SEGMENTS = 6
# Old releases stored up to eight charges. Accept those records without a
# startup rewrite, but project and consume them as a full six-point gauge.
LEGACY_SPECIAL_SORTIE_SEGMENTS = 8
TEAM_RANK_SUBDIVISION_POINTS = 1_000
TEAM_RANK_TIERS = 9  # F, E, D, C, B, A, S, SS, SSS
TEAM_RANK_SUBDIVISIONS = 3  # minus, neutral, plus
# Preserve the replacement server's earned-point thresholds. S/SS/SSS each
# have one display glyph, even though stored progression retains three stages.
TEAM_RANK_MAX_POSITION = TEAM_RANK_TIERS * TEAM_RANK_SUBDIVISIONS - 1
TEAM_RANK_MAX_SCORE = (TEAM_RANK_MAX_POSITION + 1) * TEAM_RANK_SUBDIVISION_POINTS - 1
TEAM_RANK_LABELS = ("F", "E", "D", "C", "B", "A", "S", "SS", "SSS")
TEAM_RANK_SUFFIXES = ("-", "", "+")
NORMAL_MISSION_CLEAR_POINTS = 200
SPECIAL_MISSION_CLEAR_POINTS = 300
AC_BREAK_POINTS = 30


@dataclass(frozen=True)
class TeamProgressView:
    score: int
    floor_position: int
    rank_tier: int
    rank_subdivision: int
    rank_progress: int
    rank_threshold: int
    special_sortie_progress: int

    @property
    def wire_values(self) -> tuple[int, int, int, int]:
        # Native field 2 is the complete 1..21 glyph position, not a
        # subdivision that resets when the letter changes. Field 1 remains
        # the observed rank-domain discriminator. Resetting field 2 from 3 to
        # 1 at a letter boundary visibly looped F+ back to F-.
        return (
            1,
            (self.rank_tier - 1) * TEAM_RANK_SUBDIVISIONS + self.rank_subdivision
            if self.rank_tier <= 6 else self.rank_tier + 12,
            self.rank_progress,
            self.rank_threshold,
        )

    @property
    def rank_label(self) -> str:
        if self.rank_tier >= 7:
            return TEAM_RANK_LABELS[self.rank_tier - 1]
        return (
            TEAM_RANK_LABELS[self.rank_tier - 1]
            + TEAM_RANK_SUFFIXES[self.rank_subdivision - 1]
        )


def view(team: dict) -> TeamProgressView:
    """Validate one stored team progression record and derive its rank."""
    if not isinstance(team, dict):
        raise ValueError("Team progression requires one team record")
    score = team.get("team_rank_score", 0)
    floor_position = team.get("team_rank_floor_position", 0)
    special = team.get("special_sortie_progress", 0)
    if (
        type(score) is not int
        or not 0 <= score <= TEAM_RANK_MAX_SCORE
        or type(floor_position) is not int
        or not 0 <= floor_position <= TEAM_RANK_MAX_POSITION
        or floor_position % TEAM_RANK_SUBDIVISIONS != 0
        or type(special) is not int
        or not 0 <= special <= LEGACY_SPECIAL_SORTIE_SEGMENTS
    ):
        raise ValueError("Stored team progression needs review")
    position = min(TEAM_RANK_MAX_POSITION, score // TEAM_RANK_SUBDIVISION_POINTS)
    if position < floor_position:
        raise ValueError("Stored team rank is below its earned floor")
    return TeamProgressView(
        score=score,
        floor_position=floor_position,
        rank_tier=position // TEAM_RANK_SUBDIVISIONS + 1,
        rank_subdivision=position % TEAM_RANK_SUBDIVISIONS + 1,
        rank_progress=score - position * TEAM_RANK_SUBDIVISION_POINTS,
        rank_threshold=TEAM_RANK_SUBDIVISION_POINTS,
        special_sortie_progress=min(special, SPECIAL_SORTIE_SEGMENTS),
    )


def initialize(team: dict) -> bool:
    """Add conservative defaults to one legacy/new team without overwriting."""
    changed = False
    for key, value in (
        ("team_rank_score", 0),
        ("team_rank_floor_position", 0),
        ("special_sortie_progress", 0),
    ):
        if key not in team:
            team[key] = value
            changed = True
    view(team)
    return changed


def apply(team: dict, *, rank_delta: int, special_delta: int) -> dict:
    """Apply one authenticated settlement delta to a copied team record."""
    before = view(team)
    if (
        type(rank_delta) is not int
        or not -10_000 <= rank_delta <= 10_000
        or type(special_delta) is not int
        or not 0 <= special_delta <= SPECIAL_SORTIE_SEGMENTS
    ):
        raise ValueError("Team progression delta is outside its bounds")
    score = min(
        TEAM_RANK_MAX_SCORE,
        max(before.floor_position * TEAM_RANK_SUBDIVISION_POINTS, before.score + rank_delta),
    )
    position = min(TEAM_RANK_MAX_POSITION, score // TEAM_RANK_SUBDIVISION_POINTS)
    floor_position = max(
        before.floor_position,
        position - position % TEAM_RANK_SUBDIVISIONS,
    )
    special = min(SPECIAL_SORTIE_SEGMENTS, before.special_sortie_progress + special_delta)
    team.update(
        team_rank_score=score,
        team_rank_floor_position=floor_position,
        special_sortie_progress=special,
    )
    after = view(team)
    return {
        "previous_score": before.score,
        "score": after.score,
        "rank_delta": after.score - before.score,
        "previous_rank_tier": before.rank_tier,
        "rank_tier": after.rank_tier,
        "previous_rank_subdivision": before.rank_subdivision,
        "rank_subdivision": after.rank_subdivision,
        "previous_special_sortie_progress": before.special_sortie_progress,
        "special_sortie_progress": after.special_sortie_progress,
        "special_delta": after.special_sortie_progress - before.special_sortie_progress,
    }
