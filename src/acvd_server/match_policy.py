"""Pure active-war pair selection from live preparation snapshots.

Normal request fields 8 and 9 select side and opponent faction respectively.
Special Sorties are one-team World boss missions and never enter this pair selector.
"""

from .udp import GameSession
from .world_messages import CURRENT_SEASON_ID, WORLD_PHASE_ACTIVE

MAX_TEAM_CLIENTS = 5
MAX_MATCH_CLIENTS = 10


def preparation_squad(member: dict) -> dict | None:
    """Read a frozen squad; legacy single-captain records remain supported."""
    session, team_id = member["session"], member["team_id"]
    squad = member.get("squad")
    if squad is None:
        claimed = member.get("claimed_player_ids", [])
        if claimed and claimed != [session.player_id]:
            return None  # Never silently reduce a larger unbound roster to its captain.
        squad = {session.player_id: (session, team_id)}
    if not isinstance(squad, dict) or not 1 <= len(squad) <= MAX_TEAM_CLIENTS:
        return None
    for player_id, value in squad.items():
        if (
            type(player_id) is not int
            or not 0 < player_id < 1 << 32
            or not isinstance(value, tuple)
            or len(value) != 2
            or not isinstance(value[0], GameSession)
            or value[0].player_id != player_id
            or type(value[1]) is not int
            or value[1] <= 0
        ):
            return None
    captain = squad.get(session.player_id)
    if captain is None or captain[0] is not session or captain[1] != team_id:
        return None
    if len({peer.online_id for peer, _ in squad.values()}) != len(squad):
        return None
    return dict(squad)


def same_squad(first: dict | None, second: dict | None) -> bool:
    """A copied/replaced transport is not the frozen participant, even if values match."""
    return (
        first is not None
        and second is not None
        and first.keys() == second.keys()
        and all(first[p][0] is second[p][0] and first[p][1] == second[p][1] for p in first)
    )


def intra_faction_pair(first: dict, second: dict) -> bool | None:
    """Use authenticated current factions, or leave the pair unclassified."""
    factions = first.get("faction_id"), second.get("faction_id")
    if any(type(value) is not int or value not in (1, 2, 3) for value in factions):
        return None
    return factions[0] == factions[1]


def opponent_faction_allowed(criteria: dict, faction_id: int) -> bool:
    """Normal request field 9: zero means any; 1/2/3 mean Sirius/Venide/EGF.

    Compare authenticated factions (or current area owners), never region IDs.
    Missing legacy preferences remain unrestricted; unsupported values do not.
    """
    preference = criteria.get(9, 0)
    return (
        type(preference) is int
        and preference in (0, 1, 2, 3)
        and type(faction_id) is int
        and faction_id in (1, 2, 3)
        and preference in (0, faction_id)
    )


def normal_pair_roles(
    first: dict, second: dict, *, season_id: int = CURRENT_SEASON_ID
) -> tuple[dict, dict] | None:
    """Select attacker/defender roles for two active Normal Sorties."""
    practice = intra_faction_pair(first, second)
    if practice is None:
        return None
    sides = []
    for member, opponent in ((first, second), (second, first)):
        session = member["session"]
        criteria = member["criteria"]
        if (
            not session.established
            or session.peer is None
            or type(member.get("team_id")) is not int
            or member["team_id"] <= 0
            or member.get("interwar_boss")
            or member.get("world_phase", WORLD_PHASE_ACTIVE) != WORLD_PHASE_ACTIVE
            or any(
                type(criteria.get(number)) is not int or criteria[number] != value
                for number, value in ((1, season_id), (2, WORLD_PHASE_ACTIVE))
            )
            or type(criteria.get(3)) is not int
            or criteria[3] not in (1, 2)
        ):
            return None
        if criteria[3] != 1:
            return None
        if not opponent_faction_allowed(criteria, opponent["faction_id"]):
            return None
        # Older preparation records can omit the optional preference. Preserve
        # their existing no-preference behavior; reject unsupported values.
        preference = criteria.get(8, 3)
        if type(preference) is not int or preference not in (1, 2, 3):
            return None
        sides.append(preference)
    if (
        first["team_id"] == second["team_id"]
        or first["session"].online_id == second["session"].online_id
    ):
        return None
    squads = [preparation_squad(member) for member in (first, second)]
    if any(squad is None for squad in squads):
        return None
    if set(squads[0]).intersection(squads[1]) or sum(map(len, squads)) > MAX_MATCH_CLIENTS:
        return None
    # When both sides have no preference, retain queue order as the tie-break.
    if sides[0] in (1, 3) and sides[1] in (2, 3):
        pair = first, second
    elif sides[1] in (1, 3) and sides[0] in (2, 3):
        pair = second, first
    else:
        return None
    return pair
