"""World, faction, ranking and territory response encoders.

These preserve the existing community-world defaults, not a complete retail
world simulation. Empty rankings and synthetic territory values are explicit
compatibility behavior; do not mistake initialized messages for qualification."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import verdict_war
from .cq_map_policy import locked_world_progress
from .protocol import _proto_datetime, proto_bytes, proto_varint
from .team_progression import SPECIAL_SORTIE_SEGMENTS

WORLD_PHASE_ACTIVE = 2
CURRENT_SEASON_ID = 3


def _world_basic_info(
    now: datetime,
    *,
    phase: int = WORLD_PHASE_ACTIVE,
    cq_map_settings: dict | None = None,
    war: dict | None = None,
) -> bytes:
    if war is not None:
        verdict_war.validate(war)
        phase = war["phase"]
    if type(phase) is not int or phase not in (0, 2):
        raise ValueError("Unsupported world phase")
    city = proto_varint(1, 0) + proto_varint(2, 0)
    locked_progress = locked_world_progress(cq_map_settings or {})
    area_ids = (1,) if locked_progress is None else tuple(locked_progress)
    areas = b"".join(
        proto_bytes(
            4,
            proto_varint(1, area_id)
            + proto_varint(2, 0)
            + proto_varint(3, 0)
            + proto_bytes(4, city),
        )
        for area_id in area_ids
    )
    if war is not None:
        base_ap = war["rules"]["base_ap"]
        areas = b"".join(
            proto_bytes(
                4,
                proto_varint(1, area["area_id"])
                + proto_varint(2, area["faction_id"])
                + proto_varint(3, base_ap * 8)
                + b"".join(
                    proto_bytes(4, proto_varint(1, city) + proto_varint(2, base_ap * (8 - city)))
                    for city in range(1, 9)
                ),
            )
            for area in war["areas"]
        )
    season = (
        proto_varint(1, war["season"] if war else CURRENT_SEASON_ID)
        + proto_varint(2, 0)
        # Both this field and WorldCurrentInfo field 3 write the phase cache.
        + proto_varint(3, phase)
        + proto_bytes(
            4,
            _proto_datetime(
                datetime.fromtimestamp(war["started_at"], timezone.utc)
                if war
                else now - timedelta(days=1)
            ),
        )
        + proto_bytes(
            5,
            _proto_datetime(
                datetime.fromtimestamp(war["ends_at"], timezone.utc)
                if war
                else now + timedelta(days=365)
            ),
        )
    )
    return (
        proto_varint(1, 1)
        + proto_bytes(2, season)
        # WorldBasicInfo field3 is the client's Special requirement, not a
        # reserved scalar. Advertising zero marks even an empty gauge ready.
        # Keep the existing free-entry grace-period value independent of war.
        + proto_varint(3, SPECIAL_SORTIE_SEGMENTS if phase == WORLD_PHASE_ACTIVE else 0)
        + areas
        + proto_bytes(7, _proto_datetime(now))
        + proto_varint(8, 0)
    )


def _faction_selection(selected_faction_id: int = 0) -> bytes:
    offers = b"".join(proto_bytes(1, proto_varint(1, faction_id)) for faction_id in (1, 2, 3))
    return offers + proto_varint(2, selected_faction_id)


def _canonical_faction_selection_id(requested_faction_id: int) -> int:
    """Return the retail selection's canonical Sirius/Venide/EGF ID."""
    requested_faction_id = int(requested_faction_id)
    return requested_faction_id if requested_faction_id in (1, 2, 3) else 0


def _faction_struggle_results(standings: list[dict] | None = None) -> bytes:
    """Initialized result table for the three factions in a completed season.

    The response is a repeated field-1 FactionStruggleResult list.  Each
    nested result carries the faction id, its 1-based finish, and a bounded
    score.  Live transition qualification determines whether the retail
    client requires additional optional fields.
    """
    results = []
    rows = (
        ((1, 1, 100), (2, 2, 50), (3, 3, 25))
        if standings is None
        else tuple((row["faction_id"], row["rank"], row["areas"]) for row in standings)
    )
    for faction_id, rank, score in rows:
        values = {1: faction_id, 2: rank, 3: score}
        result = b"".join(
            proto_varint(number, values.get(number, 1)) + proto_bytes(number, b"")
            for number in range(1, 9)
        )
        results.append(proto_bytes(1, result))
    envelope_fallbacks = b"".join(
        proto_varint(number, 1) + proto_bytes(number, b"") for number in range(2, 9)
    )
    return b"".join(results) + envelope_fallbacks


def _world_current_info(
    now: datetime,
    *,
    phase: int = WORLD_PHASE_ACTIVE,
    cq_map_settings: dict | None = None,
    war: dict | None = None,
) -> bytes:
    if war is not None:
        verdict_war.validate(war)
        phase = war["phase"]
    if type(phase) is not int or phase not in (0, 2):
        raise ValueError("Unsupported world phase")
    locked_progress = locked_world_progress(cq_map_settings or {})
    progress_rows = (
        ((1, 0),)
        if locked_progress is None
        else tuple(locked_progress.items())
    )
    if war is not None:
        progress_rows = tuple((area["area_id"], area["ap"]) for area in war["areas"])
    areas = b"".join(
        proto_bytes(
            4,
            proto_varint(1, area_id)
            + proto_varint(2, progress)
            + proto_bytes(3, _proto_datetime(now))
            + proto_varint(4, 0),
        )
        for area_id, progress in progress_rows
    )
    return (
        proto_varint(1, 1)
        + proto_varint(2, war["season"] if war else CURRENT_SEASON_ID)
        + proto_varint(3, phase)
        + areas
        + proto_varint(5, 0)
        + proto_bytes(6, _proto_datetime(now))
    )


def _world_log(
    now: datetime,
    requested_phase: int = 0,
    *,
    phase: int = WORLD_PHASE_ACTIVE,
    cq_map_settings: dict | None = None,
    war: dict | None = None,
) -> bytes:
    """Return World Log entries newer than the client's phase cursor.

    Exact Unbound serializers put repeated AreaBasicInfo in field 4 and
    repeated WorldPhaseInfo in field 5. AreaBasicInfo requires scalar fields
    1-3; WorldPhaseInfo requires scalar field 1 and DateTime field 4. Request
    field 2 is the last phase already known by the client: returning phase 2
    again for a phase-2 cursor makes retail immediately request it forever.
    The first cursor-0 request receives the current record; a caught-up cursor
    receives the initialized empty page that terminates the fetch.
    """
    if war is not None:
        verdict_war.validate(war)
        phase = war["phase"]
    if type(phase) is not int or phase not in (0, 2):
        raise ValueError("Unsupported world phase")
    entries = b""
    if int(requested_phase) < phase:
        world_phase = proto_varint(1, phase) + proto_bytes(4, _proto_datetime(now))
        # AreaBasicInfo is a history event, not the current area cache used by
        # map selection. Zero-filled synthetic rows render as "Unaffiliated
        # has withdrawn" for every region. Until one real ownership transition
        # is retained, emit only the truthful phase event. World Basic/Current
        # continue to carry all current locked-map progress rows.
        entries = proto_bytes(5, world_phase)
    return (
        proto_varint(1, 1)
        + proto_varint(2, war["season"] if war else CURRENT_SEASON_ID)
        + entries
        + proto_bytes(6, _proto_datetime(now - timedelta(days=1)))
        + proto_bytes(7, _proto_datetime(now))
    )


def _territory_cannon_layout(
    player_id: int,
    layout: bytes | None = None,
    *,
    layout_version: int | None = None,
) -> bytes:
    """Exact GetTerritoryCannonLayout response recovered from retail code.

    Field 2 is a required integer, field 3 is the required embedded opaque
    layout, and field 4 is the required absence flag. The client creates a new
    layout when field 4 is one; a persisted layout is returned with zero.
    """
    return (
        proto_varint(2, player_id if layout_version is None else layout_version)
        + proto_bytes(3, layout or b"")
        + proto_varint(4, 0 if layout is not None else 1)
    )


def _territory_layout_update_response() -> bytes:
    """All nine required scalar fields of the retail upload acknowledgement."""
    return b"".join(proto_varint(number, 0) for number in range(1, 10))


def _territory_info(player_id: int, season_id: int, area_id: int) -> bytes:
    """Initialized territory identity and AP for a new team holding."""
    response = b"".join(
        proto_varint(number, 0) + proto_varint(number, 1) + proto_bytes(number, b"")
        for number in range(1, 13)
    )
    return (
        response
        + proto_varint(1, 1_000)
        + proto_varint(2, 100_000)
        + proto_varint(3, area_id)
        + proto_varint(4, player_id)
        + proto_varint(5, season_id)
    )


def _empty_ranking_list_response() -> bytes:
    """Required result-count field for a valid empty-season ranking list."""
    return proto_varint(1, 0)


def _ranking_basic_info(rank: int = 0, score: int = 0, previous_rank: int = 0) -> bytes:
    return (
        proto_varint(1, max(0, int(rank)))
        + proto_varint(2, max(0, int(score)))
        + proto_varint(3, max(0, int(previous_rank)))
    )


def _player_ranking_info(row: dict | None = None) -> bytes:
    row = row or {}
    rank = max(0, int(row.get("rank", 0)))
    score = max(0, int(row.get("score", 0)))
    info = (
        proto_varint(1, max(0, int(row.get("player_id", 0))))
        # Retail serializer 0x190c624 uses WriteString for fields 2/3,
        # not embedded RankingBasicInfo records.
        + proto_bytes(2, str(row.get("online_id", ""))[:64])
        + proto_bytes(3, str(row.get("team_name", ""))[:64])
        + proto_varint(4, score)
        + proto_varint(5, rank)
    )
    return info


def _player_ranking_list_response(rows: tuple[dict, ...], season_id: int) -> bytes:
    return proto_varint(1, season_id) + b"".join(
        proto_bytes(2, _player_ranking_info(row)) for row in rows
    )


def _my_player_ranking_response(row: dict | None = None) -> bytes:
    return proto_bytes(1, _player_ranking_info(row))


def _defeat_ranking_info(row: dict) -> bytes:
    return (
        proto_varint(1, max(0, int(row.get("player_id", 0))))
        # Native personal readback selects the human row with discriminator0.
        # Player IDs, team IDs and season IDs are not this row discriminator.
        + proto_varint(2, 0)
        + proto_bytes(3, str(row.get("online_id", ""))[:64])
        + proto_bytes(4, str(row.get("team_name", ""))[:64])
        + proto_varint(5, max(0, int(row.get("score", 0))))
        + proto_varint(6, max(0, int(row.get("rank", 0))))
    )


def _defeat_ranking_list_response(rows: tuple[dict, ...], season_id: int) -> bytes:
    return proto_varint(1, season_id) + b"".join(
        proto_bytes(2, _defeat_ranking_info(row)) for row in rows
    )


def _my_defeat_ranking_response(row: dict | None) -> bytes:
    return b"" if row is None else proto_bytes(1, _defeat_ranking_info(row))
