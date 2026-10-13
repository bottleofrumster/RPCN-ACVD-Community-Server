"""Bounded Team News events from committed team changes, never login guesses."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone

from .protocol import _proto_datetime, decode_proto, proto_bytes, proto_varint
from .team_progression import view as team_progress_view

MEMBER_JOINED = 1001
TEAM_MEDAL_EARNED = 1100
TEAM_RANK_INCREASED = 1101
EVENT_TYPES = frozenset((MEMBER_JOINED, TEAM_MEDAL_EARNED, TEAM_RANK_INCREASED))
MAX_TEAM_NEWS = 50
# Through9e2bbb4 the encoder incorrectly sent event types as notice IDs.
# Keep this offset fixed even when adding event types: correct IDs must clear
# the largest poisoned cursor1101 without rewriting history or client saves.
NOTICE_ID_OFFSET = 1101
MAX_EVENT_ID = (1 << 32) - 1 - NOTICE_ID_OFFSET


@dataclass(frozen=True)
class NewsQuery:
    limit: int
    after_id: int


def wire_notice_id(event_id: int) -> int:
    """Project one durable ID into the corrected, stable uint32 wire range."""
    if type(event_id) is not int or not 0 < event_id <= MAX_EVENT_ID:
        raise ValueError("Team News event ID exceeds its wire range")
    return NOTICE_ID_OFFSET + event_id


def _medals(team):
    values = team.get("team_medal_ids", [])
    if not isinstance(values, list) or any(type(v) is not int or not 1 <= v <= 32 for v in values):
        raise ValueError("Team News medal source needs review")
    return set(values)


def stored_events(team: dict) -> list[dict]:
    rows = team.get("team_news", [])
    if not isinstance(rows, list) or len(rows) > MAX_TEAM_NEWS:
        raise ValueError("Team News history exceeds its bound")
    last_id = 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "type", "created_at"}:
            raise ValueError("Invalid Team News record")
        if type(row["id"]) is not int or not last_id < row["id"] <= MAX_EVENT_ID:
            raise ValueError("Invalid Team News event ID")
        if type(row["type"]) is not int or row["type"] not in EVENT_TYPES:
            raise ValueError("Unknown Team News event type")
        try:
            date = datetime.fromisoformat(row["created_at"])
        except (ValueError, TypeError) as error:
            raise ValueError("Invalid Team News date") from error
        if date.tzinfo is None or date.utcoffset() is None:
            raise ValueError("Team News dates must have a time zone")
        last_id = row["id"]
    return rows


def record_team_changes(previous: dict, updated: dict, now: datetime | None = None) -> None:
    """Append notifications to the same candidate state as the triggering action.

    No startup backfill: old ownership does not establish historical event dates.
    Retries with identical team state create no events. Failed writes leave IDs
    unconsumed; rebuilding from previous state also handles retrying a candidate.
    """
    changes = []
    old_teams = previous.get("teams", {})
    for key, team in sorted(updated.get("teams", {}).items()):
        old = old_teams.get(key, {})
        if old:
            old_names = {
                name for name, tid in previous.get("members", {}).items() if tid == team["team_id"]
            }
            new_names = {
                name for name, tid in updated.get("members", {}).items() if tid == team["team_id"]
            }
            changes.extend((key, MEMBER_JOINED) for _ in sorted(new_names - old_names))
        if _medals(team) - _medals(old):
            changes.append((key, TEAM_MEDAL_EARNED))
        if old and team_progress_view(team).wire_values[1] > team_progress_view(old).wire_values[1]:
            changes.append((key, TEAM_RANK_INCREASED))
    if not changes:
        return
    next_id = previous.get("next_team_news_id", 1)
    if type(next_id) is not int or not 0 < next_id <= MAX_EVENT_ID - len(changes) + 1:
        raise ValueError("Team News event ID capacity needs review")
    recognized = now if now is not None else datetime.now(timezone.utc)
    if recognized.tzinfo is None or recognized.utcoffset() is None:
        raise ValueError("Team News event needs an aware date")
    prepared = {}
    for key, event_type in changes:
        if key not in prepared:
            prepared[key] = deepcopy(stored_events(old_teams.get(key, {})))
            if prepared[key] and next_id <= prepared[key][-1]["id"]:
                raise ValueError("Team News sequence regressed")
        prepared[key].append(
            {"id": next_id, "type": event_type, "created_at": recognized.isoformat()}
        )
        next_id += 1
    for key, rows in prepared.items():
        updated["teams"][key]["team_news"] = rows[-MAX_TEAM_NEWS:]
    updated["next_team_news_id"] = next_id


def decode_query(payload: bytes) -> NewsQuery:
    """Field1 is the recent-feed limit; field2 is the last received notice ID."""
    if not payload:
        return NewsQuery(MAX_TEAM_NEWS, 0)
    if len(payload) > 24:
        raise ValueError("Team News query exceeds its bound")
    fields = decode_proto(payload)
    if (
        len(fields) != 2
        or {f.number for f in fields} != {1, 2}
        or any(f.wire_type != 0 for f in fields)
    ):
        raise ValueError("Team News query requires its two scalar fields")
    values = {f.number: f.value for f in fields}
    if any(not 0 <= value < 1 << 32 for value in values.values()):
        raise ValueError("Unsupported Team News query range")
    return NewsQuery(min(values[1], MAX_TEAM_NEWS), values[2])


def encode_events(rows: list[dict]) -> bytes:
    """Localized passive notices; optional argument payloads stay absent.

    Native adapter ec23cc passes SDK word0 as notice ID and word1 as event type
    to1c9048/e03408. The latter retains the type and advances the maximum ID.
    Field5 supplies the original date; fixed captions need no argument blob.
    """
    stored_events({"team_news": rows})
    return b"".join(
        proto_bytes(
            1,
            proto_varint(1, wire_notice_id(row["id"]))
            + proto_varint(2, row["type"])
            + proto_bytes(5, _proto_datetime(datetime.fromisoformat(row["created_at"]))),
        )
        for row in reversed(rows)
    )
