"""Team Record projections and bounded PvP history from committed World facts.

Existing medal counters own sortie/PvP totals. Old missing win outcomes stay
unknown: the win count is a supported lower bound until coverage is complete.
No raw logs, rankings or personal mercenary activity become Team victories.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TeamRecord:
    sorties: int = 0
    wins: int = 0
    pvp_wins: int = 0
    history: tuple[tuple[bool, str], ...] = ()  # Oldest first; native reverses it.
    wins_complete: bool = True
    observed_sorties: int = 0


def _counter(value):
    if type(value) is not int or not 0 <= value < 1 << 32:
        raise ValueError("Stored Team Record counter needs review")
    return value


def _rows(progress):
    if not isinstance(progress, (list, tuple)) or not 1 <= len(progress) <= 2:
        return ()
    if any(
        not isinstance(r, dict)
        or type(r.get("team_id")) is not int
        or r["team_id"] <= 0
        or type(r.get("rank_delta")) is not int
        for r in progress
    ):
        return ()
    if len({r["team_id"] for r in progress}) != len(progress):
        return ()
    return progress


def _versus(state, rows, team_id):
    if len(rows) != 2:
        return None
    own = next((r for r in rows if r["team_id"] == team_id), None)
    if own is None:
        return None
    opponent = next(r for r in rows if r is not own)
    name = state.get("teams", {}).get(str(opponent["team_id"]), {}).get("name")
    if not isinstance(name, str) or not name:
        return None
    return own["rank_delta"] > 0, name[:32]


def team_record(state: dict, team_id: int) -> TeamRecord:
    team = state.get("teams", {}).get(str(team_id))
    if not isinstance(team, dict):
        return TeamRecord()
    if "team_record_wins" in team:
        wins = _counter(team["team_record_wins"])
        observed = _counter(team.get("team_record_observed_sorties"))
        history = team.get("team_record_history", [])
        if (
            not isinstance(history, list)
            or len(history) > 5
            or any(
                not isinstance(r, (list, tuple))
                or len(r) != 2
                or type(r[0]) is not bool
                or not isinstance(r[1], str)
                or len(r[1]) > 32
                for r in history
            )
        ):
            raise ValueError("Stored Team Record history needs review")
        history = [tuple(r) for r in history]
    else:
        wins, observed, history = 0, 0, []
        for receipt in state.get("world_battle_settlements", []):
            if not isinstance(receipt, dict):
                continue
            rows = _rows(receipt.get("team_progress"))
            own = next((r for r in rows if r["team_id"] == team_id), None)
            if own is None:
                continue
            observed += 1
            wins += own["rank_delta"] > 0
            versus = _versus(state, rows, team_id)
            if versus:
                history = [*history, versus][-5:]
    sorties = max(_counter(team.get("team_world_sorties", 0)), observed)
    pvp_wins = _counter(team.get("team_pvp_wins", 0))
    wins = max(wins, pvp_wins)  # Every already-proven PvP victory is also a win.
    if wins > sorties:
        raise ValueError("Team Record wins exceed known sorties")
    return TeamRecord(sorties, wins, pvp_wins, tuple(history), observed >= sorties, observed)


def advance_team_records(previous: dict, updated: dict, progress: list[dict]) -> None:
    """Called only inside a new atomic settlement, after the receipt replay gate."""
    rows = _rows(progress)
    for own in rows:
        tid = own["team_id"]
        team = updated.get("teams", {}).get(str(tid))
        if not isinstance(team, dict):
            raise ValueError("Team Record settlement team no longer exists")
        old = team_record(previous, tid)
        team["team_record_wins"] = _counter(old.wins + int(own["rank_delta"] > 0))
        team["team_record_observed_sorties"] = _counter(old.observed_sorties + 1)
        versus = _versus(updated, rows, tid)
        history = (*old.history, versus) if versus else old.history
        team["team_record_history"] = [list(row) for row in history[-5:]]
