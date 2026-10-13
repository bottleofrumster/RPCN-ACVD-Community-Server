"""Read verified player medals without inferring awards from missing data.

Wire fields1/2/3 become record offsets0/4/8; dates4/5 become+0x10/+0x20.
The client uses date4 after2000-01-01 for ownership/rank validity. Field2
multiplies medal score, but not the per-type player rating bonus. Field3 is
retained as an opaque progress value until its award producer is recovered.
Awards use only qualified server counters; unverified save ownership is never imported.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .protocol import ProtocolError, _proto_datetime, proto_bytes, proto_varint
from .udp import GameSession

if TYPE_CHECKING:
    from .teams import TeamStore

UNOWNED_DATE = datetime(2000, 1, 1, tzinfo=timezone.utc)
# MedalParam.MedalPoint from the same verified table as RATING_BONUSES.
# Award count multiplies this score; PlayerRatingBonus is a different value.
MEDAL_POINTS = (
    300, 1000, 3000, 15000, 10, 100, 500, 10, 100, 500, 10, 100, 500,
    10, 100, 500, 10, 100, 500, 10, 100, 500, 10, 100, 500, 10, 100, 500,
    1000, 2000, 3000, 5000, 300, 500, 1000, 2000, 500, 1500, 500, 1500,
    500, 1500, 500, 1500, 1000, 3000, 1000, 3000,
)
# MedalParam.PlayerRatingBonus, not MedalPoint. Table SHA-256:
# 712e4316e5add1e6ed96ec862a8aa92d06e9eda0d3b1f9c98be13382a004a107.
RATING_BONUSES = (
    100,
    200,
    300,
    500,
    100,
    200,
    300,
    100,
    200,
    300,
    100,
    200,
    300,
    100,
    200,
    300,
    100,
    200,
    300,
    100,
    200,
    300,
    100,
    200,
    300,
    100,
    200,
    300,
    150,
    250,
    150,
    250,
    150,
    250,
    150,
    250,
    150,
    250,
    150,
    250,
    150,
    250,
    150,
    250,
    150,
    250,
    150,
    250,
)


@dataclass(frozen=True)
class MedalRecord:
    medal_id: int
    award_count: int
    progress_value: int
    first_date: datetime
    second_date: datetime

    def __post_init__(self):
        if type(self.medal_id) is not int or not 1 <= self.medal_id <= len(RATING_BONUSES):
            raise ProtocolError("Medal ID is outside the verified table")
        if any(
            type(n) is not int or not 0 <= n < 1 << 32
            for n in (self.award_count, self.progress_value)
        ):
            raise ProtocolError("Medal counters must be uint32 values")
        if any(
            not isinstance(d, datetime) or d.tzinfo is None or d.utcoffset() is None
            for d in (self.first_date, self.second_date)
        ):
            raise ProtocolError("Medal dates require an explicit time zone")

    @property
    def owned(self) -> bool:
        # The native check uses a fixed sentinel, not now or a season boundary.
        return self.first_date > UNOWNED_DATE


def _validate_records(records: tuple[MedalRecord, ...]) -> None:
    if (
        not isinstance(records, tuple)
        or len(records) > 48
        or any(not isinstance(r, MedalRecord) for r in records)
    ):
        raise ProtocolError("Supply a known tuple of at most48 medal records")
    if len({r.medal_id for r in records}) != len(records):
        raise ProtocolError("Medal list repeats an ID")


def load_records(teams: TeamStore, session: GameSession) -> tuple[MedalRecord, ...] | None:
    """None means unknown ownership; an explicit empty ledger means known empty."""
    profile = teams.player_profile(session.online_id)
    return records_from_profile(profile, session.player_id)


def records_from_profile(profile: dict, player_id: int) -> tuple[MedalRecord, ...] | None:
    """Validate the canonical ledger without requiring a mutable store."""
    ledger = profile.get("player_medals")
    if ledger is None:
        return None
    if (
        not isinstance(ledger, dict)
        or type(ledger.get("player_id")) is not int
        or profile.get("player_id") != player_id
        or ledger.get("player_id") != player_id
        or not isinstance(ledger.get("records"), list)
    ):
        raise ProtocolError("Medal ownership is not bound to this player")
    if len(ledger["records"]) > 48:
        raise ProtocolError("Medal list exceeds48 records")
    result = []
    for row in ledger["records"]:
        if not isinstance(row, dict) or set(row) != {
            "medal_id",
            "award_count",
            "progress_value",
            "first_date",
            "second_date",
        }:
            raise ProtocolError("Stored medal record needs review")
        try:
            result.append(
                MedalRecord(
                    row["medal_id"],
                    row["award_count"],
                    row["progress_value"],
                    datetime.fromisoformat(row["first_date"]),
                    datetime.fromisoformat(row["second_date"]),
                )
            )
        except (ValueError, TypeError) as error:
            raise ProtocolError("Stored medal values need review") from error
    records = tuple(result)
    _validate_records(records)
    return records


# Original MedalParam/English captions, retained in the September 14 contract.
# Only these counters currently have qualified, server-owned result producers.
VERIFIED_THRESHOLDS = {
    "observed_operator_wins": ((26, 3), (27, 5), (28, 10)),
    "observed_mercenary_wins": ((33, 5), (34, 10)),
    "observed_world_boss_wins": ((35, 3), (36, 5)),
}


def award_verified_medals(profile: dict, player_id: int, awarded_at: datetime) -> tuple[int, ...]:
    """Apply once-per-type awards to a transaction copy, never live state.

    Recognition time is recorded honestly; historical earning dates are not
    reconstructed. Opaque progress/second-date fields are preserved, or neutral
    for a new record. Missing evidence never creates a known-empty ledger.
    """
    if type(player_id) is not int or player_id <= 0 or profile.get("player_id") != player_id:
        raise ProtocolError("Medal award is not bound to this player")
    if (
        not isinstance(awarded_at, datetime) or awarded_at.tzinfo is None
        or awarded_at.utcoffset() is None or awarded_at <= UNOWNED_DATE
    ):
        raise ProtocolError("Medal recognition needs a valid aware timestamp")
    records = records_from_profile(profile, player_id)
    by_id = {r.medal_id: r for r in records or ()}
    eligible = []
    for counter, thresholds in VERIFIED_THRESHOLDS.items():
        value = profile.get(counter, 0)
        if type(value) is not int or not 0 <= value < 1 << 32:
            raise ProtocolError("Stored medal progress needs review")
        eligible.extend(medal_id for medal_id, threshold in thresholds if value >= threshold)
    awarded = []
    for medal_id in eligible:
        old = by_id.get(medal_id)
        if old is not None and old.owned:
            continue
        if old is not None and old.award_count:
            raise ProtocolError("Unowned medal has an ambiguous award count")
        by_id[medal_id] = MedalRecord(
            medal_id,
            1,
            old.progress_value if old else 0,
            awarded_at,
            old.second_date if old else UNOWNED_DATE,
        )
        awarded.append(medal_id)
    if not awarded:
        return ()
    rows = [
        {
            "medal_id": r.medal_id,
            "award_count": r.award_count,
            "progress_value": r.progress_value,
            "first_date": r.first_date.isoformat(),
            "second_date": r.second_date.isoformat(),
        }
        for r in sorted(by_id.values(), key=lambda r: r.medal_id)
    ]
    profile["player_medals"] = {
        **profile.get("player_medals", {}),
        "player_id": player_id,
        "records": rows,
    }
    return tuple(awarded)


def encode_records(records: tuple[MedalRecord, ...]) -> bytes:
    _validate_records(records)
    return b"".join(
        proto_bytes(
            1,
            proto_varint(1, r.medal_id)
            + proto_varint(2, r.award_count)
            + proto_varint(3, r.progress_value)
            + proto_bytes(4, _proto_datetime(r.first_date))
            + proto_bytes(5, _proto_datetime(r.second_date)),
        )
        for r in records
    )


def medal_points(records: tuple[MedalRecord, ...]) -> int:
    """Return earned medal points, never rating bonuses or unfinished progress."""
    _validate_records(records)
    return sum(MEDAL_POINTS[r.medal_id - 1] * r.award_count for r in records if r.owned)


def rating_bonus(records: tuple[MedalRecord, ...]) -> int:
    _validate_records(records)
    return sum(RATING_BONUSES[r.medal_id - 1] for r in records if r.owned)


def stored_rating_bonus(teams: TeamStore, session: GameSession) -> int:
    records = load_records(teams, session)
    # Preserve the existing zero-contribution compatibility path when no medal
    # source exists. This is not proof that the player owns no medals.
    return 0 if records is None else rating_bonus(records)
