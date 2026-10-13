"""Authoritative community leaderboard projections and request pagination.

Only durable server-owned facts enter these views. Unknown player medal
ownership is not treated as an empty ledger, and AC destructions are credited
only when a native combat record explicitly identifies an opposing defeater.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .player_medals import MedalRecord, rating_bonus
from .protocol import ProtocolError, decode_proto
from .teams import TeamStore

MAX_RANKING_PAGE = 100


@dataclass(frozen=True)
class RankingPage:
    season_id: int
    offset: int
    limit: int


def decode_page(payload: bytes, *, mercenary: bool = False) -> RankingPage:
    """Decode the two retained native list-request forms."""
    fields = decode_proto(payload)
    season_number, page_number = ((2, 1) if mercenary else (1, 2))
    seasons = [field for field in fields if field.number == season_number]
    pages = [field for field in fields if field.number == page_number]
    if (
        len(fields) != 2
        or len(seasons) != 1
        or seasons[0].wire_type != 0
        or type(seasons[0].value) is not int
        or not 0 < seasons[0].value < 1 << 32
        or len(pages) != 1
        or pages[0].wire_type != 2
    ):
        raise ProtocolError("Ranking request requires one season and one page")
    nested = decode_proto(pages[0].value)
    if (
        len(nested) != 2
        or {field.number for field in nested} != {1, 2}
        or any(field.wire_type != 0 for field in nested)
    ):
        raise ProtocolError("Ranking page requires offset and limit")
    values = {field.number: field.value for field in nested}
    if (
        type(values[1]) is not int
        or not 0 <= values[1] < 1 << 32
        or type(values[2]) is not int
        or not 1 <= values[2] <= MAX_RANKING_PAGE
    ):
        raise ProtocolError("Ranking page is outside the supported bounds")
    return RankingPage(int(seasons[0].value), values[1], values[2])


def decode_self_season(payload: bytes) -> int:
    fields = decode_proto(payload)
    if (
        len(fields) != 1
        or fields[0].number != 1
        or fields[0].wire_type != 0
        or type(fields[0].value) is not int
        or not 0 < fields[0].value < 1 << 32
    ):
        raise ProtocolError("Self-ranking request requires one season")
    return int(fields[0].value)


def page(rows: tuple[dict, ...], request: RankingPage) -> tuple[dict, ...]:
    return rows[request.offset : request.offset + request.limit]


def _profile_team(store: TeamStore, online_id: str) -> tuple[int, str]:
    team = store.team_for(online_id)
    if not isinstance(team, dict):
        return 0, ""
    team_id = team.get("team_id", 0)
    return (
        team_id if type(team_id) is int and 0 < team_id < 1 << 32 else 0,
        str(team.get("name", ""))[:64],
    )


def team_rows(store: TeamStore) -> tuple[dict, ...]:
    rows = []
    for team in store.state.get("teams", {}).values():
        if not isinstance(team, dict):
            continue
        team_id = team.get("team_id")
        score = team.get("team_fame_points")
        sorties = team.get("team_world_sorties")
        if (
            type(team_id) is not int
            or not 0 < team_id < 1 << 32
            or type(score) is not int
            or not 0 <= score < 1 << 32
            or type(sorties) is not int
            or not 0 <= sorties < 1 << 32
        ):
            raise ValueError("Stored team ranking values need review")
        if sorties == 0:
            continue
        rows.append({"team_id": team_id, "name": str(team.get("name", ""))[:64], "score": score})
    rows.sort(key=lambda row: (-row["score"], row["team_id"]))
    return tuple({**row, "rank": rank} for rank, row in enumerate(rows, 1))


def _medal_score(profile: dict) -> int | None:
    ledger = profile.get("player_medals")
    if ledger is None:
        return None
    if (
        not isinstance(ledger, dict)
        or ledger.get("player_id") != profile.get("player_id")
        or not isinstance(ledger.get("records"), list)
        or len(ledger["records"]) > 48
    ):
        raise ValueError("Stored player medal ranking needs review")
    records = []
    for row in ledger["records"]:
        if not isinstance(row, dict):
            raise ValueError("Stored player medal ranking needs review")
        try:
            records.append(
                MedalRecord(
                    row["medal_id"],
                    row["award_count"],
                    row["progress_value"],
                    datetime.fromisoformat(row["first_date"]),
                    datetime.fromisoformat(row["second_date"]),
                )
            )
        except (KeyError, TypeError, ValueError, ProtocolError) as error:
            raise ValueError("Stored player medal ranking needs review") from error
    return rating_bonus(tuple(records))


def player_rows(store: TeamStore) -> tuple[dict, ...]:
    rows = []
    for online_id, profile in store.state.get("player_profiles", {}).items():
        if not isinstance(profile, dict):
            continue
        player_id = profile.get("player_id")
        team_id, team_name = _profile_team(store, str(online_id))
        if type(player_id) is not int or not 0 < player_id < 1 << 32:
            continue
        score = _medal_score(profile)
        if score is None:
            continue
        rows.append({"player_id": player_id, "team_id": team_id, "score": score,
                     "online_id": str(online_id)[:64], "team_name": team_name})
    rows.sort(key=lambda row: (-row["score"], row["player_id"]))
    return tuple({**row, "rank": rank} for rank, row in enumerate(rows, 1))


def defeat_rows(store: TeamStore) -> tuple[dict, ...]:
    rows = []
    for online_id, profile in store.state.get("player_profiles", {}).items():
        if not isinstance(profile, dict):
            continue
        player_id = profile.get("player_id")
        score = profile.get("observed_ac_destructions")
        team_id, team_name = _profile_team(store, str(online_id))
        if score is None:
            continue
        if (
            type(player_id) is not int
            or not 0 < player_id < 1 << 32
            or type(score) is not int
            or not 0 <= score < 1 << 32
        ):
            raise ValueError("Stored AC-destruction ranking needs review")
        rows.append(
            {
                "player_id": player_id,
                "online_id": str(online_id)[:64],
                "team_id": team_id,
                "team_name": team_name,
                "score": score,
            }
        )
    rows.sort(key=lambda row: (-row["score"], row["player_id"]))
    return tuple({**row, "rank": rank} for rank, row in enumerate(rows, 1))


def mercenary_rows(store: TeamStore) -> tuple[dict, ...]:
    rows = []
    for online_id, profile in store.state.get("player_profiles", {}).items():
        if not isinstance(profile, dict):
            continue
        player_id = profile.get("player_id")
        sorties = profile.get("observed_mercenary_sorties")
        wins = profile.get("observed_mercenary_wins")
        earnings = profile.get("observed_mercenary_earnings")
        grade = profile.get("mercenary_rank", 0)
        team_id, team_name = _profile_team(store, str(online_id))
        if sorties is None or wins is None or earnings is None:
            continue
        if (
            type(player_id) is not int
            or not 0 < player_id < 1 << 32
            or type(sorties) is not int
            or not 0 <= sorties < 1 << 32
            or type(wins) is not int
            or not 0 <= wins <= sorties
            or type(earnings) is not int
            or not 0 <= earnings < 1 << 64
            or type(grade) is not int
            or not 0 <= grade <= 6
        ):
            raise ValueError("Stored mercenary ranking needs review")
        rows.append(
            {
                "player_id": player_id,
                "online_id": str(online_id)[:64],
                "team_id": team_id,
                "team_name": team_name,
                "grade": grade,
                "score": earnings,
                "sorties": sorties,
            }
        )
    rows.sort(key=lambda row: (-row["score"], -row["sorties"], row["player_id"]))
    return tuple({**row, "rank": rank} for rank, row in enumerate(rows, 1))


def own_row(rows: tuple[dict, ...], player_id: int) -> dict | None:
    return next((row for row in rows if row.get("player_id") == player_id), None)
