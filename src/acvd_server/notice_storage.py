"""One atomic personal-notice store per authenticated player.

Acknowledged records remain available for retry checks and review. Never prune
or reuse their IDs implicitly. Capacity failures require reviewed retention
work, not silent data loss. Advisory locks serialize cooperating writers.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .notice_messages import MAX_ACK_IDS, PersonalNotice
from .player_storage import _replace_player_blob

MAX_RECORDS = 1024
MAX_STORE_BYTES = 4 * 1024 * 1024
MAX_PENDING_REPLY = 32


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate notice store key")
        result[key] = value
    return result


class PersonalNoticeStore:
    def __init__(self, player_data: Path):
        self.root = Path(player_data)

    def _path(self, player_id: int) -> Path:
        if type(player_id) is not int or not 0 < player_id < 1 << 32:
            raise ValueError("Notice recipient must be a positive uint32")
        return self.root / f"{player_id}.personal-notices.json"

    @staticmethod
    def _notice(record: dict) -> PersonalNotice:
        return PersonalNotice(
            record["id"],
            record["title"],
            record["body"],
            datetime.fromisoformat(record["created_at"]),
        )

    def _load(self, player_id: int) -> dict:
        path = self._path(player_id)
        if path.is_symlink():
            raise ValueError("Notice store must not be a symbolic link")
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_STORE_BYTES + 1)
        except FileNotFoundError:
            return {"version": 1, "player_id": player_id, "next_id": 1, "notices": []}
        if len(raw) > MAX_STORE_BYTES:
            raise ValueError("Notice store exceeds its size limit")
        try:
            data = json.loads(raw, object_pairs_hook=_unique_object)
            if (
                type(data) is not dict
                or set(data) != {"version", "player_id", "next_id", "notices"}
                or type(data["version"]) is not int
                or data["version"] != 1
                or type(data["player_id"]) is not int
                or data["player_id"] != player_id
                or type(data["next_id"]) is not int
                or not 0 < data["next_id"] <= 1 << 32
                or type(data["notices"]) is not list
                or len(data["notices"]) > MAX_RECORDS
            ):
                raise ValueError("Invalid notice store header")
            identifiers, keys = set(), set()
            for record in data["notices"]:
                if type(record) is not dict or set(record) != {
                    "id",
                    "event_key",
                    "title",
                    "body",
                    "created_at",
                    "acknowledged",
                }:
                    raise ValueError("Invalid notice store record")
                notice = self._notice(record)
                key = record["event_key"]
                self._check_event_key(key)
                if (
                    notice.identifier in identifiers
                    or notice.identifier >= data["next_id"]
                    or key in keys
                    or type(record["acknowledged"]) is not bool
                ):
                    raise ValueError("Ambiguous notice store record")
                identifiers.add(notice.identifier)
                keys.add(key)
            return data
        except (KeyError, TypeError, ValueError, UnicodeError) as error:
            raise ValueError("Notice store requires review") from error

    @staticmethod
    def _check_event_key(key: str) -> None:
        if type(key) is not str or not key or "\0" in key or len(key.encode("utf-8")) > 128:
            raise ValueError("Notice event key is invalid")

    @contextmanager
    def _locked(self, player_id: int):
        path = self._path(player_id)
        lock_dir = self.root / ".locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            lock_dir / f"{path.name}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def _save(self, player_id: int, data: dict) -> None:
        raw = (json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        if len(raw) > MAX_STORE_BYTES:
            raise ValueError("Notice store exceeds its size limit")
        _replace_player_blob(self._path(player_id), raw)

    def pending(
        self, player_id: int, *, exclude_event_prefixes: tuple[str, ...] = ()
    ) -> list[PersonalNotice]:
        data = self._load(player_id)
        return [
            self._notice(record)
            for record in data["notices"]
            if not record["acknowledged"]
            and not record["event_key"].startswith(exclude_event_prefixes)
        ][:MAX_PENDING_REPLY]

    def reserve_identifier(self, player_id: int) -> int:
        """Reserve a unique ID for a session notice without adding a history record.

        Never reuse IDs: a late acknowledgement from an older login must not
        acknowledge the next login's welcome or a future durable notice.
        """
        with self._locked(player_id):
            data = self._load(player_id)
            identifier = data["next_id"]
            if identifier >= 1 << 32:
                raise ValueError("Notice ID capacity requires review")
            data["next_id"] += 1
            self._save(player_id, data)
            return identifier

    def find_event(self, player_id: int, event_key: str) -> PersonalNotice | None:
        """Read a trusted producer's receipt, including acknowledged notices."""
        self._check_event_key(event_key)
        for record in self._load(player_id)["notices"]:
            if record["event_key"] == event_key:
                return self._notice(record)
        return None

    def enqueue(
        self, player_id: int, *, event_key: str, title: str, body: str, created_at: datetime
    ) -> int:
        """Internal producer API; the caller must establish sender authority.

        Retries keep the first timestamp. A reused key cannot change content.
        No network request directly supplies this event key or recipient path.
        """
        self._path(player_id)
        self._check_event_key(event_key)
        PersonalNotice(1, title, body, created_at)
        with self._locked(player_id):
            data = self._load(player_id)
            for record in data["notices"]:
                if record["event_key"] == event_key:
                    if record["title"] != title or record["body"] != body:
                        raise ValueError("Notice retry conflicts with stored content")
                    return record["id"]
            identifier = data["next_id"]
            if len(data["notices"]) >= MAX_RECORDS or identifier >= 1 << 32:
                raise ValueError("Notice store capacity requires review")
            data["notices"].append(
                {
                    "id": identifier,
                    "event_key": event_key,
                    "title": title,
                    "body": body,
                    "created_at": created_at.astimezone(timezone.utc).isoformat(
                        timespec="milliseconds"
                    ),
                    "acknowledged": False,
                }
            )
            data["next_id"] += 1
            self._save(player_id, data)
            return identifier

    def acknowledge(self, player_id: int, identifiers: list[int]) -> list[int]:
        path = self._path(player_id)
        if len(identifiers) > MAX_ACK_IDS or any(
            type(value) is not int or not 0 < value < 1 << 32 for value in identifiers
        ):
            raise ValueError("Invalid notice acknowledgement IDs")
        if not identifiers:
            return []
        if path.is_symlink():
            raise ValueError("Notice store must not be a symbolic link")
        if not path.exists():
            return []
        wanted = set(identifiers)
        with self._locked(player_id):
            data = self._load(player_id)
            changed = []
            for record in data["notices"]:
                if record["id"] in wanted and not record["acknowledged"]:
                    record["acknowledged"] = True
                    changed.append(record["id"])
            if changed:
                self._save(player_id, data)
            return changed
