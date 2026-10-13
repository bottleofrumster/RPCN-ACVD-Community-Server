"""Own durable teams, profiles, wallets, territory, media, and hire records."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import verdict_war
from .mercenary_earnings import (
    credit as credit_mercenary_earnings,
    reconcile as reconcile_mercenary_earnings,
)
from .player_medals import award_verified_medals, medal_points, records_from_profile
from .team_medals import (
    apply_progress as apply_team_medal_progress,
    award_ranking_medals,
    initialize as initialize_team_medals,
    title_is_owned,
)
from .team_news import record_team_changes
from .team_progression import (
    SPECIAL_SORTIE_SEGMENTS,
    apply as apply_team_progress,
    initialize as initialize_team_progress,
    view as team_progress_view,
)
from .team_records import advance_team_records

MAX_BALANCE = 999_999_999


class TeamStore:
    """Keep one durable state owner for teams and account-domain records.

    Construction can normalize legacy records and persist the result. It is
    not a read-only inspection API. Handlers choose authorization and timing;
    this store owns durable mutations and their existing write order.
    """

    def __init__(self, root: Path):
        self.root = root
        self.state_path = root / "teams.json"
        self.state: dict[str, Any] = {
            "schema_version": 1,
            "next_team_id": 1,
            "teams": {},
            "members": {},
            "mercenaries": {},
            "pending_emblems": {},
            "player_emblems": {},
            "next_ugc_id": 200000,
            "mercenary_messages": {},
            "next_mercenary_message_id": 1,
            "player_profiles": {},
            "money_transfers": [],
            "battle_settlements": [],
            "team_medal_backfill_version": 1,
        }
        if self.state_path.is_file():
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.state.update(loaded)
                if "team_medal_backfill_version" not in loaded:
                    self.state["team_medal_backfill_version"] = 0
        changed = self._normalize_legacy_teams()
        if reconcile_mercenary_earnings(self.state):
            changed = True
        if self._backfill_team_medal_history():
            changed = True
        if self._reconcile_player_medal_progress(self.state):
            changed = True
        teams_before_ranking = json.dumps(self.state.get("teams", {}), sort_keys=True)
        award_ranking_medals(self.state.get("teams", {}))
        if json.dumps(self.state.get("teams", {}), sort_keys=True) != teams_before_ranking:
            changed = True
        if changed:
            self._save()

    # SCHEMA LOAD/MIGRATION: construction may persist normalized legacy records.
    def _normalize_legacy_teams(self) -> bool:
        """Bring pre-membership team records onto the current durable schema."""
        changed = False
        for team in self.state.get("teams", {}).values():
            if not isinstance(team, dict):
                continue
            if initialize_team_progress(team):
                changed = True
            if initialize_team_medals(team):
                changed = True
            expected_open = int(team.get("enlistment_method", 1)) == 2
            if team.get("recruitment_open") is not expected_open:
                team["recruitment_open"] = expected_open
                changed = True
            properties = team.get("changeable_properties", {})
            if "desired_faction_id" not in team and isinstance(properties, dict):
                # Current retail writes next-season faction in request field
                # 26.  Keep field 25 only as a legacy fallback for records
                # created before the sparse editor mapping was recovered.
                faction_value = properties.get("26", properties.get("25"))
                if faction_value is not None:
                    team["desired_faction_id"] = max(0, int(faction_value))
                    changed = True
            if "soldier_id" not in team and isinstance(properties, dict) and "14" in properties:
                team["soldier_id"] = max(0, int(properties["14"]))
                changed = True
            # Recover legacy titles from retained field13 in the read-only
            # TeamInfo projection, not via a startup data rewrite. Field20
            # was a mistaken title alias and must not supply a new title.
            if (
                "headquarters_id" not in team
                and isinstance(properties, dict)
                and "25" in properties
                and "26" in properties
            ):
                # A paired field 26 proves this record came from the current
                # distinct HQ/faction schema. Field-25-only legacy records may
                # predate that recovery and remain faction fallbacks above.
                team["headquarters_id"] = max(0, int(properties["25"]))
                changed = True
            if (
                "desired_area_id" not in team
                and isinstance(properties, dict)
                and "27" in properties
            ):
                team["desired_area_id"] = max(0, int(properties["27"]))
                changed = True
            sparse_comment = str(team.get("team_profile_text", ""))
            stored_comment = str(team.get("comment", ""))
            leader_online_id = str(team.get("leader_online_id", ""))
            if sparse_comment and stored_comment in ("", leader_online_id):
                # The old handler put request bytes field 24 in an unused
                # key, then overwrote TeamInfo's comment with leader request
                # field 2 (the editor identity). Repair only that proven
                # signature while retaining the old key as audit history.
                team["comment"] = sparse_comment
                changed = True
            elif stored_comment and stored_comment == leader_online_id:
                # No legitimate comment can be recovered for this older
                # record, but the exact identity echo is still known-bad.
                # Clear it rather than fabricating replacement text.
                team["comment"] = ""
                changed = True
            faction_id = int(team.get("faction_id", 0))
            if faction_id in (1, 2, 3) and int(team.get("current_faction_id", 0)) not in (1, 2, 3):
                # Older writes persisted the selected faction but left the
                # dedicated TeamInfo readback field at zero, which the client
                # renders as EGF regardless of the selected affiliation.
                team["current_faction_id"] = faction_id
                changed = True
            if "member_records" not in team:
                team["member_records"] = [
                    {
                        "online_id": str(team.get("leader_online_id", "")),
                        "player_id": int(team.get("leader_player_id", 0)),
                        "leader": True,
                        "member_status": 1,
                        "member_id": 1,
                        "member_team_secondary": 0,
                    }
                ]
                changed = True
            used_member_ids = {
                int(record.get("member_id", 0))
                for record in team.get("member_records", [])
                if isinstance(record, dict) and int(record.get("member_id", 0)) > 0
            }
            next_member_id = 1
            for record in team.get("member_records", []):
                if not isinstance(record, dict):
                    continue
                if int(record.get("member_id", 0)) <= 0:
                    while next_member_id in used_member_ids:
                        next_member_id += 1
                    record["member_id"] = next_member_id
                    used_member_ids.add(next_member_id)
                    changed = True
                if "member_team_secondary" not in record:
                    record["member_team_secondary"] = 0
                    changed = True
                if int(record.get("member_status", 0)) != 1:
                    record["member_status"] = 1
                    changed = True
                online_id = str(record.get("online_id", ""))
                if online_id:
                    profile = self.state.setdefault("player_profiles", {}).setdefault(online_id, {})
                    if int(profile.get("member_id", 0)) != int(record["member_id"]):
                        profile["member_id"] = int(record["member_id"])
                        changed = True
                    if int(profile.get("member_team_secondary", 0)) != int(
                        record["member_team_secondary"]
                    ):
                        profile["member_team_secondary"] = int(record["member_team_secondary"])
                        changed = True
            if "profile_text" not in team:
                raw_profile = b""
                try:
                    raw_profile = base64.b64decode(
                        str(team.get("access_data_b64", "")),
                        validate=True,
                    )
                except (ValueError, base64.binascii.Error):
                    pass
                team["profile_text"] = raw_profile.decode("utf-8", "replace")
                changed = True
        return changed

    def _backfill_team_medal_history(self) -> bool:
        """Credit retained World sorties/PvP wins once during schema adoption.

        Historical receipts do not preserve enough information to reconstruct
        every destroyed AC, so that counter starts from the first fully bound
        post-migration report. Sortie participation and two-team victories are
        explicit in the retained team-progress rows and are safe to recover.
        """
        version = self.state.get("team_medal_backfill_version", 0)
        if version == 1:
            return False
        if version not in (0, None):
            raise ValueError("Team Medal history backfill version needs review")
        tallies: dict[int, dict[str, int]] = {}
        history = self.state.get("world_battle_settlements", [])
        if not isinstance(history, list):
            raise ValueError("World battle settlement history is invalid")
        for receipt in history:
            if not isinstance(receipt, dict):
                continue
            rows = receipt.get("team_progress")
            if not isinstance(rows, list) or not rows:
                continue
            valid = [
                row
                for row in rows
                if isinstance(row, dict)
                and type(row.get("team_id")) is int
                and type(row.get("rank_delta")) is int
            ]
            if len(valid) != len(rows) or len({row["team_id"] for row in valid}) != len(valid):
                raise ValueError("Retained Team Medal history needs review")
            is_pvp = len(valid) == 2
            for row in valid:
                if self.team_by_id(row["team_id"]) is None:
                    continue
                values = tallies.setdefault(row["team_id"], {"sorties": 0, "pvp_wins": 0})
                values["sorties"] += 1
                values["pvp_wins"] += int(is_pvp and row["rank_delta"] > 0)
        for team_id, values in tallies.items():
            apply_team_medal_progress(
                self.state["teams"][str(team_id)],
                world_sorties_delta=values["sorties"],
                pvp_wins_delta=values["pvp_wins"],
            )
        self.state["team_medal_backfill_version"] = 1
        return True

    @staticmethod
    def _reconcile_player_medal_progress(state: dict[str, Any]) -> bool:
        """Credit owned player medal counts and MedalPoint totals once.

        Legacy counts did not carry their points. Reconcile those points to the
        current team once, without fabricating historical team membership. The
        per-player credit watermark prevents re-awarding after a team transfer.
        """
        changed = False
        profiles = state.get("player_profiles", {})
        teams = state.get("teams", {})
        members = state.get("members", {})
        if not isinstance(profiles, dict) or not isinstance(teams, dict):
            raise ValueError("Stored player/team medal state needs review")
        for online_id, profile in profiles.items():
            if not isinstance(profile, dict):
                continue
            ledger = profile.get("player_medals")
            if ledger is None:
                continue
            if (
                not isinstance(ledger, dict)
                or ledger.get("player_id") != profile.get("player_id")
                or not isinstance(ledger.get("records"), list)
                or len(ledger["records"]) > 48
            ):
                raise ValueError("Stored player medals are not account-bound")
            records = records_from_profile(profile, profile["player_id"])
            total = sum(record.award_count for record in records if record.owned)
            legacy_total = sum(record.award_count for record in records)
            points = medal_points(records)
            if legacy_total > (1 << 32) - 1 or points > (1 << 32) - 1:
                raise ValueError("Stored player medal total exceeds its native bound")
            credited = profile.get("team_medal_credited_awards", 0)
            if type(credited) is not int or not 0 <= credited <= legacy_total:
                raise ValueError("Stored credited player medals need review")
            # Older code counted unfinished records too. Preserve that old
            # watermark without granting new counts or points for unowned medals.
            delta = max(0, total - credited)
            credited_points = profile.get("team_medal_credited_points", 0)
            if type(credited_points) is not int or not 0 <= credited_points <= points:
                raise ValueError("Stored credited player medal points need review")
            point_delta = points - credited_points
            if delta or point_delta:
                team_id = members.get(online_id)
                team = teams.get(str(team_id)) if type(team_id) is int else None
                if isinstance(team, dict):
                    apply_team_progress(team, rank_delta=point_delta, special_delta=0)
                    apply_team_medal_progress(
                        team, player_medals_delta=delta, fame_delta=point_delta
                    )
                profile["team_medal_credited_awards"] = max(total, credited)
                profile["team_medal_credited_points"] = points
                changed = True
            elif ("team_medal_credited_awards" not in profile
                  or "team_medal_credited_points" not in profile):
                profile["team_medal_credited_awards"] = max(total, credited)
                profile["team_medal_credited_points"] = points
                changed = True
        return changed

    def refresh_player_medal_state(self, online_id: str, player_id: int) -> tuple[int, ...]:
        """Recognize proven prior counters without replaying cash or old results."""
        owner = self.member_by_player_id(player_id)
        if owner is None and not self.player_profile(online_id):
            return ()  # Pre-login/unknown ownership stays a read-only empty reply.
        if owner is None or owner[0] != online_id:
            raise ValueError("Medal recognition account identity changed")
        updated = deepcopy(self.state)
        profile = updated.get("player_profiles", {}).get(online_id, {})
        profile.setdefault("player_id", player_id)  # Proven by the canonical member lookup.
        awarded = award_verified_medals(profile, player_id, datetime.now(timezone.utc))
        if awarded:
            self._reconcile_player_medal_progress(updated)
            self.commit_state(updated)
        return awarded

    def refresh_team_medal_state(self, online_id: str) -> dict[str, Any] | None:
        """Reconcile known player medals/rankings and return the caller's team."""
        updated = deepcopy(self.state)
        changed = self._reconcile_player_medal_progress(updated)
        before = json.dumps(updated.get("teams", {}), sort_keys=True)
        award_ranking_medals(updated.get("teams", {}))
        if json.dumps(updated.get("teams", {}), sort_keys=True) != before:
            changed = True
        if changed:
            self.commit_state(updated)
        return self.team_for(online_id)

    # IDENTITY AND MEMBERSHIP: durable records, not live connections or room slots.
    def team_for(self, online_id: str) -> dict[str, Any] | None:
        team_id = self.state["members"].get(online_id)
        if team_id is None:
            return None
        return self.state["teams"].get(str(team_id))

    def team_by_id(self, team_id: int) -> dict[str, Any] | None:
        return self.state["teams"].get(str(int(team_id)))

    def special_sortie_ready(self, team_id: int) -> bool:
        """Return whether one team has a complete active-war Special gauge."""
        team = self.team_by_id(team_id)
        return bool(
            team is not None
            and team_progress_view(team).special_sortie_progress
            == SPECIAL_SORTIE_SEGMENTS
        )

    def consume_special_sortie(self, team_id: int) -> dict[str, int] | None:
        """Atomically consume one full gauge immediately before match release."""
        team_id = int(team_id)
        current = self.team_by_id(team_id)
        if current is None:
            return None
        before = team_progress_view(current)
        if before.special_sortie_progress != SPECIAL_SORTIE_SEGMENTS:
            return None
        updated = deepcopy(self.state)
        team = updated["teams"][str(team_id)]
        team["special_sortie_progress"] = 0
        self.commit_state(updated)
        return {
            "team_id": team_id,
            "previous_special_sortie_progress": before.special_sortie_progress,
            "special_sortie_progress": 0,
        }

    def member_by_player_id(self, player_id: int) -> tuple[str, dict[str, Any]] | None:
        wanted = int(player_id)
        for online_id, profile in self.state.setdefault("player_profiles", {}).items():
            if isinstance(profile, dict) and int(profile.get("player_id", 0)) == wanted:
                return str(online_id), self.team_for(str(online_id)) or {}
        for team in self.state["teams"].values():
            if not isinstance(team, dict):
                continue
            if int(team.get("leader_player_id", 0)) == wanted:
                return str(team.get("leader_online_id", "")), team
            for record in team.get("member_records", []):
                if isinstance(record, dict) and int(record.get("player_id", 0)) == wanted:
                    return str(record.get("online_id", "")), team
        return None

    def select_opposing_native_territory(
        self, *, attacker_team_id: int, attacker_faction_id: int, season_id: int
    ) -> tuple[str, int, int, int] | None:
        """Select one intact opposing saved territory without requiring its owner online.

        The tuple is ``(owner_online_id, owner_team_id, owner_player_id, area_id)``.
        Stable numeric ordering makes retries deterministic. Corrupt, stale, same-team,
        same-faction and unassigned records are never exposed to matchmaking.
        """
        if (
            type(attacker_team_id) is not int
            or attacker_team_id <= 0
            or type(attacker_faction_id) is not int
            or attacker_faction_id not in (1, 2, 3)
            or type(season_id) is not int
            or season_id <= 0
        ):
            return None
        teams = sorted(
            (team for team in self.state.get("teams", {}).values() if isinstance(team, dict)),
            key=lambda team: int(team.get("team_id", 0)),
        )
        for team in teams:
            team_id = team.get("team_id")
            faction_id = team.get("current_faction_id")
            area_id = team.get("territory_area_id")
            if (
                type(team_id) is not int
                or team_id <= 0
                or team_id == attacker_team_id
                or type(faction_id) is not int
                or faction_id not in (1, 2, 3)
                or faction_id == attacker_faction_id
                or type(area_id) is not int
                or not 1 <= area_id <= 7
            ):
                continue
            try:
                active = self.load_active_native_territory_layout(
                    str(team.get("leader_online_id", "")),
                    season_id=season_id,
                    area_id=area_id,
                )
            except (OSError, ValueError, TypeError, KeyError):
                continue
            if active is None:
                continue
            payload, version, player_id = active
            owner = self.member_by_player_id(player_id)
            if (
                owner is not None
                and int(owner[1].get("team_id", 0)) == team_id
                and version == 7
                and 0 < len(payload) <= 8192
            ):
                return owner[0], team_id, player_id, area_id
        return None

    def remember_player(self, online_id: str, player_id: int) -> dict[str, Any]:
        """Persist the authenticated account-to-player-ID mapping."""
        profile = self.state.setdefault("player_profiles", {}).setdefault(online_id, {})
        player_id = max(0, int(player_id))
        if int(profile.get("player_id", 0)) != player_id:
            profile["player_id"] = player_id
            self._save()
        return dict(profile)

    def search_team_ids(self, *, name: str = "", open_only: bool = True) -> list[int]:
        wanted = name.casefold().strip()
        matches: list[int] = []
        for team in self.state["teams"].values():
            if not isinstance(team, dict):
                continue
            if open_only and not bool(
                team.get("recruitment_open", int(team.get("enlistment_method", 1)) == 2)
            ):
                continue
            if wanted and wanted not in str(team.get("name", "")).casefold():
                continue
            matches.append(int(team.get("team_id", 0)))
        return sorted(team_id for team_id in matches if team_id > 0)

    def create(
        self,
        *,
        online_id: str,
        player_id: int,
        name: str,
        operation_base: int,
        language: int,
        play_type: int,
        enlistment_method: int,
        nationality_id: int = 0,
        creation_wire_fields: dict[int, int] | None = None,
    ) -> dict[str, Any]:
        existing = self.team_for(online_id)
        if existing is not None:
            return existing
        news_before = deepcopy(self.state)
        team_id = int(self.state["next_team_id"])
        self.state["next_team_id"] = team_id + 1
        team = {
            "team_id": team_id,
            "name": name,
            "leader_online_id": online_id,
            "leader_player_id": player_id,
            # Legacy creation alias retained for stored-file/API compatibility.
            # It is not used for admission or TeamInfo enrollment readback.
            "nationality_id": max(0, int(nationality_id)),
            "operation_base": operation_base,
            "language": language,
            "play_type": play_type,
            "enlistment_method": enlistment_method,
            "recruitment_open": enlistment_method == 2,
            "comment": "",
            "profile_text": "",
            "soldier_id": 0,
            "title_id": 0,
            "headquarters_id": 0,
            "desired_faction_id": 1,
            "desired_area_id": 0,
            "current_faction_id": 0,
            "territory_area_id": 0,
            "team_rank_score": 0,
            "team_rank_floor_position": 0,
            "special_sortie_progress": 0,
            "member_records": [
                {
                    "online_id": online_id,
                    "player_id": int(player_id),
                    "leader": True,
                    "member_status": 1,
                    "member_id": 1,
                    "member_team_secondary": 0,
                }
            ],
        }
        if creation_wire_fields:
            team["creation_wire_fields"] = {
                str(int(number)): max(0, int(value))
                for number, value in sorted(creation_wire_fields.items())
            }
        initialize_team_medals(team)
        self.state["teams"][str(team_id)] = team
        self.state["members"][online_id] = team_id
        self.state.setdefault("player_profiles", {}).setdefault(online_id, {}).update(
            {"member_id": 1, "member_team_secondary": 0}
        )
        pending = self.state.setdefault("pending_emblems", {}).pop(online_id, None)
        if isinstance(pending, dict):
            filename = pending.get("emblem_file")
            if (
                isinstance(filename, str)
                and Path(filename).name == filename
                and (self.root / filename).is_file()
            ):
                team["emblem_ugc_id"] = int(pending.get("ugc_id", 0))
                team["emblem_file"] = filename
                team["emblem_metadata_b64"] = str(pending.get("metadata_b64", ""))
        self._commit_membership_news(news_before)
        return team

    def join_team(
        self,
        *,
        online_id: str,
        player_id: int,
        team_id: int,
        replace_existing: bool = False,
    ) -> dict[str, Any] | None:
        target = self.team_by_id(team_id)
        if target is None or not bool(
            target.get("recruitment_open", int(target.get("enlistment_method", 1)) == 2)
        ):
            return None
        existing = self.team_for(online_id)
        news_before = deepcopy(self.state)
        if existing is not None and int(existing.get("team_id", 0)) != int(team_id):
            if not replace_existing:
                return None
            # The retail free-enlistment UI displays its own leave warning but
            # sends only RequestJoinTeamAuto.  Treat that one request as the
            # complete transfer transaction.  The target was validated before
            # touching the old membership, so a closed/missing destination can
            # never eject the player from their current team.
            self._remove_team_membership(online_id)
        self.state["members"][online_id] = int(team_id)
        records = target.setdefault("member_records", [])
        if not records:
            records.append(
                {
                    "online_id": str(target.get("leader_online_id", "")),
                    "player_id": int(target.get("leader_player_id", 0)),
                    "leader": True,
                    "member_status": 1,
                    "member_id": 1,
                    "member_team_secondary": 0,
                }
            )
        if not any(
            isinstance(record, dict) and record.get("online_id") == online_id for record in records
        ):
            used_member_ids = {
                int(record.get("member_id", 0))
                for record in records
                if isinstance(record, dict) and int(record.get("member_id", 0)) > 0
            }
            member_id = 1
            while member_id in used_member_ids:
                member_id += 1
            records.append(
                {
                    "online_id": online_id,
                    "player_id": int(player_id),
                    "leader": False,
                    "member_status": 1,
                    "member_id": member_id,
                    "member_team_secondary": 0,
                }
            )
        joined_record = next(
            record
            for record in records
            if isinstance(record, dict) and record.get("online_id") == online_id
        )
        self.state.setdefault("player_profiles", {}).setdefault(online_id, {}).update(
            {
                "member_id": int(joined_record.get("member_id", 0)),
                "member_team_secondary": int(joined_record.get("member_team_secondary", 0)),
            }
        )
        self._commit_membership_news(news_before)
        return target

    def _commit_membership_news(self, previous: dict[str, Any]) -> None:
        """Legacy membership writers share one atomic save with their news."""
        try:
            record_team_changes(previous, self.state)
            self._save()
        except BaseException:
            self.state = previous
            raise

    def _remove_team_membership(self, online_id: str) -> dict[str, Any] | None:
        """Mutate one membership without saving the surrounding transaction."""
        team = self.team_for(online_id)
        if team is None:
            return None
        team_id = int(team.get("team_id", 0))
        records = [
            record
            for record in team.setdefault("member_records", [])
            if not (isinstance(record, dict) and record.get("online_id") == online_id)
        ]
        team["member_records"] = records
        self.state.setdefault("members", {}).pop(online_id, None)
        self.state.setdefault("player_profiles", {}).setdefault(online_id, {}).update(
            {"member_id": 0, "member_team_secondary": 0}
        )

        result: dict[str, Any] = {
            "team_id": team_id,
            "team_deleted": False,
            "leader_promoted": "",
        }
        if not records:
            self.state.setdefault("teams", {}).pop(str(team_id), None)
            result["team_deleted"] = True
        elif str(team.get("leader_online_id", "")) == online_id:
            promoted = next(
                (record for record in records if isinstance(record, dict)),
                None,
            )
            if promoted is not None:
                for record in records:
                    if isinstance(record, dict):
                        record["leader"] = record is promoted
                team["leader_online_id"] = str(promoted.get("online_id", ""))
                team["leader_player_id"] = int(promoted.get("player_id", 0))
                result["leader_promoted"] = team["leader_online_id"]
        return result

    def leave_team(self, online_id: str) -> dict[str, Any] | None:
        """Remove one member and delete or transfer an abandoned team."""
        result = self._remove_team_membership(online_id)
        if result is None:
            return None
        self._save()
        return result

    # EMBLEMS AND TERRITORY: stored bytes plus ownership/region lookup metadata.
    def register_pending_emblem(
        self, online_id: str, metadata: bytes, emblem: bytes
    ) -> dict[str, Any]:
        ugc_id = max(1, int(self.state.setdefault("next_ugc_id", 200000)))
        self.state["next_ugc_id"] = ugc_id + 1
        self.root.mkdir(parents=True, exist_ok=True)
        filename = f"ugc-{ugc_id}-emblem.jpg"
        path = self.root / filename
        temporary = path.with_suffix(".jpg.tmp")
        temporary.write_bytes(emblem)
        temporary.replace(path)
        pending = {
            "ugc_id": ugc_id,
            "emblem_file": filename,
            "metadata_b64": base64.b64encode(metadata).decode("ascii"),
        }
        self.state.setdefault("pending_emblems", {})[online_id] = pending
        self.state.setdefault("player_emblems", {})[online_id] = dict(pending)
        self._save()
        return pending

    def player_emblem(self, online_id: str) -> dict[str, Any] | None:
        stored = self.state.setdefault("player_emblems", {}).get(online_id)
        if not isinstance(stored, dict):
            return None
        result = dict(stored)
        assignment_payload = b""
        encoded_assignment = stored.get("assignment_payload_b64")
        if isinstance(encoded_assignment, str):
            try:
                assignment_payload = base64.b64decode(encoded_assignment, validate=True)
            except (ValueError, base64.binascii.Error):
                assignment_payload = b""
        if assignment_payload:
            result["emblem"] = assignment_payload
            return result
        filename = stored.get("emblem_file")
        if not isinstance(filename, str) or Path(filename).name != filename:
            return None
        path = self.root / filename
        if not path.is_file():
            return None
        result["emblem"] = path.read_bytes()
        return result

    def set_player_emblem_by_ugc_id(
        self, online_id: str, ugc_id: int, payload: bytes
    ) -> dict[str, Any]:
        """Persist the emblem currently applied to a player's AC.

        RequestSetPlayerEmblemByUGCID includes the selected UGC identifier and
        the complete application-encoded 1,808-byte emblem.  Keeping that
        assignment separate from the original uploaded JPEG preserves both
        authoring metadata and the exact representation remote clients need.
        """
        current = self.state.setdefault("player_emblems", {}).get(online_id)
        stored = dict(current) if isinstance(current, dict) else {}
        stored["ugc_id"] = max(0, int(ugc_id))
        stored["assignment_payload_b64"] = base64.b64encode(bytes(payload)).decode("ascii")
        self.state["player_emblems"][online_id] = stored
        self._save()
        return dict(stored)

    def save_emblem(self, online_id: str, ugc_id: int, payload: bytes) -> Path | None:
        team = self.team_for(online_id)
        if team is None:
            return None
        team_id = int(team["team_id"])
        self.root.mkdir(parents=True, exist_ok=True)
        emblem_path = self.root / f"{team_id}-emblem.bin"
        temporary = emblem_path.with_suffix(".bin.tmp")
        temporary.write_bytes(payload)
        temporary.replace(emblem_path)
        team["emblem_ugc_id"] = ugc_id
        team["emblem_file"] = emblem_path.name
        self._save()
        return emblem_path

    def save_territory_layout(
        self,
        online_id: str,
        *,
        season_id: int,
        player_id: int,
        area_id: int,
        payload: bytes,
    ) -> Path | None:
        """Atomically persist the client's opaque cannon-layout protobuf."""
        team = self.team_for(online_id)
        if team is None:
            return None
        team_id = int(team["team_id"])
        key = f"{season_id}:{area_id}"
        filename = f"{team_id}-territory-{season_id}-{area_id}.bin"
        self.root.mkdir(parents=True, exist_ok=True)
        layout_path = self.root / filename
        temporary = layout_path.with_suffix(".bin.tmp")
        temporary.write_bytes(payload)
        temporary.replace(layout_path)
        layouts = team.setdefault("territory_layouts", {})
        layouts[key] = {
            "file": filename,
            "player_id": player_id,
            "size": len(payload),
        }
        self._save()
        return layout_path

    def load_territory_layout(
        self, online_id: str, *, season_id: int, area_id: int
    ) -> bytes | None:
        team = self.team_for(online_id)
        if team is None:
            return None
        metadata = team.get("territory_layouts", {}).get(f"{season_id}:{area_id}")
        if not isinstance(metadata, dict):
            return None
        filename = metadata.get("file")
        if not isinstance(filename, str) or Path(filename).name != filename:
            return None
        layout_path = self.root / filename
        return layout_path.read_bytes() if layout_path.is_file() else None

    def save_native_territory_layout(
        self,
        online_id: str,
        *,
        player_id: int,
        season_id: int,
        area_id: int,
        layout_version: int,
        payload: bytes,
    ) -> Path | None:
        """Store a native upload without replacing another uploader's record."""
        team = self.team_for(online_id)
        if team is None:
            return None
        digest = hashlib.sha256(payload).hexdigest()
        filename = (
            f"{int(team['team_id'])}-territory-{player_id}-{season_id}-{area_id}-{digest}.bin"
        )
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / filename
        if path.exists():
            if path.read_bytes() != payload:
                raise OSError("Territory payload does not match its stored hash")
        else:
            temporary = path.with_suffix(".bin.tmp")
            temporary.write_bytes(payload)
            temporary.replace(path)
        previous = team.get("native_territory_layouts")
        previous_active = team.get("active_native_territory_layouts")
        records = dict(previous or {})
        key = f"{player_id}:{season_id}:{area_id}"
        record = {
            "file": filename,
            "player_id": player_id,
            "season_id": season_id,
            "area_id": area_id,
            "layout_version": layout_version,
            "size": len(payload),
            "sha256": digest,
        }
        active = dict(previous_active or {})
        active_key = f"{season_id}:{area_id}"
        if records.get(key) == record and active.get(active_key) == player_id:
            return path
        records[key] = record
        active[active_key] = player_id
        team["native_territory_layouts"] = records
        team["active_native_territory_layouts"] = active
        try:
            self._save()
        except Exception:
            if previous is None:
                team.pop("native_territory_layouts", None)
            else:
                team["native_territory_layouts"] = previous
            if previous_active is None:
                team.pop("active_native_territory_layouts", None)
            else:
                team["active_native_territory_layouts"] = previous_active
            raise
        return path

    def load_active_native_territory_layout(
        self, online_id: str, *, season_id: int, area_id: int
    ) -> tuple[bytes, int, int] | None:
        """Return the latest team-selected native layout and its uploader."""
        team = self.team_for(online_id)
        if team is None:
            return None
        team_id = int(team.get("team_id", 0))
        active = team.get("active_native_territory_layouts", {})
        player_id = active.get(f"{season_id}:{area_id}") if isinstance(active, dict) else None

        def load(candidate_player_id):
            if type(candidate_player_id) is not int or not 0 < candidate_player_id < 1 << 32:
                return None
            owner = self.member_by_player_id(candidate_player_id)
            if owner is None or int(owner[1].get("team_id", 0)) != team_id:
                return None
            record = self.load_native_territory_layout(
                owner[0],
                player_id=candidate_player_id,
                season_id=season_id,
                area_id=area_id,
            )
            return None if record is None else (record[0], record[1], candidate_player_id)

        selected = load(player_id)
        if selected is not None:
            return selected
        records = team.get("native_territory_layouts", {})
        candidates = []
        if isinstance(records, dict):
            for record in records.values():
                if (
                    not isinstance(record, dict)
                    or record.get("season_id") != season_id
                    or record.get("area_id") != area_id
                ):
                    continue
                filename = record.get("file")
                if not isinstance(filename, str) or Path(filename).name != filename:
                    continue
                path = self.root / filename
                try:
                    candidates.append((path.stat().st_mtime_ns, filename, record.get("player_id")))
                except OSError:
                    continue
        for _stamp, _filename, candidate_player_id in sorted(candidates, reverse=True):
            selected = load(candidate_player_id)
            if selected is not None:
                return selected
        return None

    def load_native_territory_layout(
        self,
        online_id: str,
        *,
        player_id: int,
        season_id: int,
        area_id: int,
    ) -> tuple[bytes, int] | None:
        """Read an uploader-scoped native record with its declared version."""
        team = self.team_for(online_id)
        if team is None:
            return None
        record = team.get("native_territory_layouts", {}).get(f"{player_id}:{season_id}:{area_id}")
        if not isinstance(record, dict):
            return None
        filename = record.get("file")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError("Invalid territory payload path")
        if (record.get("player_id"), record.get("season_id"), record.get("area_id")) != (
            player_id,
            season_id,
            area_id,
        ):
            raise ValueError("Territory record identity does not match")
        payload = (self.root / filename).read_bytes()
        if len(payload) != record.get("size") or hashlib.sha256(payload).hexdigest() != record.get(
            "sha256"
        ):
            raise ValueError("Territory payload integrity check failed")
        return payload, int(record["layout_version"])

    # TEAM/PLAYER SETTINGS: request handlers decide who may call these mutations.
    def _initial_faction_area(self, faction_id: int) -> int:
        war = self.test_war_snapshot()
        if war is None:
            return faction_id
        return next(
            (a["area_id"] for a in war["areas"] if a["faction_id"] == faction_id), 0
        )

    def set_faction(self, online_id: str, world_id: int, faction_id: int) -> dict[str, Any] | None:
        team = self.team_for(online_id)
        if team is None:
            return None
        team["world_id"] = world_id
        team["faction_id"] = faction_id
        team["current_faction_id"] = faction_id
        # Native clients can register a sortie immediately after selecting a
        # faction, before reading the seasonal result that historically
        # promoted an unassigned team.  Persist the initial faction-to-area
        # assignment in the same write while preserving any existing holding.
        area_id = team.get("territory_area_id", 0)
        if type(area_id) is int and area_id == 0 and faction_id in (1, 2, 3):
            team["territory_area_id"] = self._initial_faction_area(faction_id)
        self._save()
        return team

    def update_leader_settings(
        self,
        online_id: str,
        *,
        pay_apportionment: int,
        leader_identity: bytes,
        enlistment_method: int,
        profile_text: bytes,
    ) -> dict[str, Any] | None:
        """Atomically persist every field from the retail leader-settings update."""
        team = self.team_for(online_id)
        if team is None or team.get("leader_online_id") != online_id:
            return None
        team.update(
            {
                "pay_apportionment": max(0, int(pay_apportionment)),
                "enlistment_method": max(0, int(enlistment_method)),
                "recruitment_open": int(enlistment_method) == 2,
                "profile_text": profile_text.decode("utf-8", "replace"),
                # Request field 2 is the editor/leader identity, not the
                # changeable team comment. Retain it separately for protocol
                # audit and never project it into TeamInfo field 14.
                "leader_update_identity": leader_identity.decode("utf-8", "replace"),
            }
        )
        self._save()
        return team

    def update_team_properties(
        self,
        online_id: str,
        *,
        scalar_fields: dict[int, int],
        color: bytes | None = None,
        comment: bytes | None = None,
    ) -> dict[str, Any] | None:
        """Persist each sparse member-changeable team property independently."""
        team = self.team_for(online_id)
        if team is None:
            return None
        if 13 in scalar_fields and not title_is_owned(team, scalar_fields[13]):
            raise ValueError("The selected title's Team Medal has not been earned")
        stored = team.setdefault("changeable_properties", {})
        for number, value in scalar_fields.items():
            stored[str(int(number))] = max(0, int(value))
        if 8 in scalar_fields:
            team["language"] = max(0, int(scalar_fields[8]))
        if 10 in scalar_fields:
            team["play_type"] = max(0, int(scalar_fields[10]))
        if 13 in scalar_fields:
            team["title_id"] = max(0, int(scalar_fields[13]))
        if 14 in scalar_fields:
            team["soldier_id"] = max(0, int(scalar_fields[14]))
        if 20 in scalar_fields:
            team["sortie_text_id"] = max(0, int(scalar_fields[20]))
        if 23 in scalar_fields:
            team["operation_base"] = max(0, int(scalar_fields[23]))
        if 25 in scalar_fields:
            team["headquarters_id"] = max(0, int(scalar_fields[25]))
        if 26 in scalar_fields:
            team["desired_faction_id"] = max(0, int(scalar_fields[26]))
        if 27 in scalar_fields:
            team["desired_area_id"] = max(0, int(scalar_fields[27]))
        if color is not None:
            team["team_color_b64"] = base64.b64encode(color).decode("ascii")
        if comment is not None:
            team["comment"] = comment.decode("utf-8", "replace")
        self._save()
        return team

    def update_user_property(self, online_id: str, value: bytes) -> dict[str, Any]:
        return self.update_member_comment(online_id, value)

    def update_user_properties(
        self,
        online_id: str,
        *,
        scalar_fields: dict[int, int],
        byte_fields: dict[int, bytes],
    ) -> dict[str, Any]:
        """Merge sparse RequestUpdateUserProperty fields without resetting peers."""
        profile = self.state.setdefault("player_profiles", {}).setdefault(online_id, {})
        stored_scalars = profile.setdefault("user_property_scalars", {})
        stored_bytes = profile.setdefault("user_property_bytes_b64", {})
        for number, value in scalar_fields.items():
            stored_scalars[str(int(number))] = max(0, int(value))
        for number, value in byte_fields.items():
            stored_bytes[str(int(number))] = base64.b64encode(value).decode("ascii")
        if 6 in scalar_fields:
            profile["language"] = max(0, int(scalar_fields[6]))
        self._save()
        return profile

    def update_member_comment(self, online_id: str, value: bytes) -> dict[str, Any]:
        profile = self.state.setdefault("player_profiles", {}).setdefault(online_id, {})
        profile["status"] = value.decode("utf-8", "replace")
        self._save()
        return profile

    def update_member_team_info(
        self,
        online_id: str,
        *,
        member_id: int,
        secondary: int,
    ) -> dict[str, Any]:
        """Persist one editable member number without duplicating a teammate."""
        normalized_member_id = max(0, min(99, int(member_id)))
        normalized_secondary = max(0, int(secondary))
        team = self.team_for(online_id)
        own_record = None
        if team is not None:
            records = team.setdefault("member_records", [])
            own_records = [
                record
                for record in records
                if isinstance(record, dict) and record.get("online_id") == online_id
            ]
            if len(own_records) != 1:
                raise ValueError("Team member number update lacks one bound roster record")
            own_record = own_records[0]
            if normalized_member_id > 0 and any(
                isinstance(record, dict)
                and record is not own_record
                and int(record.get("member_id", 0)) == normalized_member_id
                for record in records
            ):
                raise ValueError("Team member number is already in use")
        profile = self.state.setdefault("player_profiles", {}).setdefault(online_id, {})
        profile["member_id"] = normalized_member_id
        profile["member_team_secondary"] = normalized_secondary
        if own_record is not None:
            own_record["member_id"] = normalized_member_id
            own_record["member_team_secondary"] = normalized_secondary
        self._save()
        return profile

    def player_profile(self, online_id: str) -> dict[str, Any]:
        profile = self.state.setdefault("player_profiles", {}).get(online_id, {})
        return dict(profile) if isinstance(profile, dict) else {}

    # WALLETS AND SETTLEMENTS: balance() returns a default when absent; it does
    # not store a wallet. A caller can still expose or later persist that value.
    # Handlers must establish a legitimate baseline before applying money changes.
    def balance(self, online_id: str, *, initial: int = 1_000_000) -> int:
        profile = self.state.setdefault("player_profiles", {}).get(online_id, {})
        if not isinstance(profile, dict) or "balance" not in profile:
            return max(0, int(initial))
        return int(profile["balance"])

    def synchronize_balance(
        self,
        online_id: str,
        reported: int,
        *,
        initial: int = 0,
    ) -> int:
        """Adopt the local first-sync balance, then accept later client syncs."""
        if (
            type(reported) is not int
            or type(initial) is not int
            or not -MAX_BALANCE <= reported <= MAX_BALANCE
            or not -MAX_BALANCE <= initial <= MAX_BALANCE
        ):
            raise ValueError("Cash value is outside the supported signed range")
        profile = self.state.setdefault("player_profiles", {}).setdefault(online_id, {})
        profile["balance"] = (
            reported if "balance" in profile or reported < 0 else max(reported, initial)
        )
        self._save()
        return int(profile["balance"])

    def provision_new_balance(self, online_id: str, amount: int = 100_000) -> tuple[int, bool]:
        """Persist the retail new-save wallet without classifying it as a client sync.

        Callers must independently establish that the account has no team and
        no wallet.  A later SyncMoney may replace this value only through the
        same authenticated login that created it.
        """
        if type(amount) is not int or not 0 <= amount <= MAX_BALANCE:
            raise ValueError("New-save cash is outside the supported signed range")
        current = self.player_profile(online_id)
        if "balance" in current:
            balance = current["balance"]
            if type(balance) is not int or not -MAX_BALANCE <= balance <= MAX_BALANCE:
                raise ValueError("Stored wallet requires review")
            return balance, False
        updated = deepcopy(self.state)
        profile = updated.setdefault("player_profiles", {}).setdefault(online_id, {})
        profile["balance"] = amount
        profile["cash_enrollment"] = {
            "policy": "native-new-save-v1",
            "starting_balance": amount,
        }
        self.commit_state(updated)
        return amount, True

    def enroll_balance(
        self,
        online_id: str,
        *,
        offline_balance: int,
        online_balance: int,
        client_time: int,
        replace_new_save_provisional: bool = False,
    ) -> tuple[int, bool]:
        """Import offline cash once; later syncs read the current server wallet.

        A saved online value is not a new charge or earning. Never let a
        reconnect, stale report, or retry replace purchases and credits that
        the server has already committed. Existing wallets, including zero,
        are not new enrollments and are not repaired from an arbitrary save.
        """
        if any(
            type(value) is not int or not -MAX_BALANCE <= value <= MAX_BALANCE
            for value in (offline_balance, online_balance)
        ):
            raise ValueError("Cash value is outside the supported signed range")
        if type(client_time) is not int or not 0 <= client_time <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("Cash source time is outside uint64")
        current = self.player_profile(online_id)
        if "balance" in current:
            balance = current["balance"]
            if type(balance) is not int or not -MAX_BALANCE <= balance <= MAX_BALANCE:
                raise ValueError("Stored wallet requires review")
            enrollment = current.get("cash_enrollment", {})
            if replace_new_save_provisional and enrollment == {
                "policy": "native-new-save-v1",
                "starting_balance": balance,
            }:
                updated = deepcopy(self.state)
                profile = updated.setdefault("player_profiles", {}).setdefault(online_id, {})
                profile["balance"] = offline_balance
                profile["cash_enrollment"] = {
                    "policy": "offline-first-v1",
                    "offline_balance": offline_balance,
                    "previous_online_balance": online_balance,
                    "client_time": client_time,
                    "replaced_new_save_provisional": balance,
                }
                self.commit_state(updated)
                return offline_balance, True
            return balance, False
        updated = deepcopy(self.state)
        profile = updated.setdefault("player_profiles", {}).setdefault(online_id, {})
        profile["balance"] = offline_balance
        profile["cash_enrollment"] = {
            "policy": "offline-first-v1",
            "offline_balance": offline_balance,
            "previous_online_balance": online_balance,
            "client_time": client_time,
        }
        # Keep the balance and its provenance in the same atomic state commit.
        # A failed write must leave both in-memory and durable state unchanged.
        self.commit_state(updated)
        return offline_balance, True

    def settle_battle_money(
        self,
        online_id: str,
        result_digest: str,
        *,
        mission_id: int,
        gross_earnings: int,
        expenses: int,
        initial: int = 1_000_000,
        policy_version: str | None = None,
        item_receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Apply one authenticated battle's net money exactly once.

        The caller supplies validated pay and expenses, not raw field guesses.
        Persist the net delta before replying. The request digest makes exact
        retries idempotent while their receipts are retained. A changed policy
        must not replay an old receipt as a new reward or silently repair an old
        balance. The legacy initial argument cannot create a missing wallet.
        """
        result_digest = str(result_digest)
        reward_key = online_id + ":" + result_digest
        reward_history = self.state.get("story_rewards", {})
        if not isinstance(reward_history, dict):
            raise ValueError("Story reward history is invalid")
        if reward_key in reward_history:
            previous = reward_history[reward_key]
            if previous.get("policy_version") != policy_version:
                raise ValueError("Existing battle receipt uses a different reward policy")
            return dict(previous["settlement"], applied=False)
        if item_receipt is not None:
            pending_specs = item_receipt.get("pending_specs", [])
            emblems = item_receipt.get("emblems", [])
            item_count = (
                len(item_receipt["items"]) + len(pending_specs) + len(emblems)
                if isinstance(item_receipt.get("items"), list)
                and isinstance(pending_specs, list)
                and isinstance(emblems, list)
                else -1
            )
            if (
                item_receipt.get("online_id") != online_id
                or item_receipt.get("result_digest") != result_digest
                or not 0 <= item_count <= 96
                or (item_count == 0 and policy_version != "story_items_emblems_request_v5")
                or not isinstance(item_receipt.get("reply_hex"), str)
            ):
                raise ValueError("Invalid Story item receipt")
            response = bytes.fromhex(item_receipt["reply_hex"])
            if not response or len(response) > 4096 or item_receipt["reply_hex"] != response.hex():
                raise ValueError("Invalid Story item reply")
        gross_earnings = max(0, int(gross_earnings))
        expenses = max(0, int(expenses))
        history = self.state.get("battle_settlements", [])
        if not isinstance(history, list):
            raise ValueError("Battle settlement history is invalid")
        for settlement in reversed(history):
            if (
                isinstance(settlement, dict)
                and settlement.get("online_id") == online_id
                and settlement.get("result_digest") == result_digest
            ):
                if (
                    policy_version is not None
                    and settlement.get("policy_version") != policy_version
                ):
                    raise ValueError("Existing battle receipt uses a different reward policy")
                replay = dict(settlement)
                replay["applied"] = False
                return replay

        previous_balance = self.player_profile(online_id).get("balance")
        if type(previous_balance) is not int or not -MAX_BALANCE <= previous_balance <= MAX_BALANCE:
            raise ValueError("Battle settlement requires a known wallet balance")
        nominal_delta = gross_earnings - expenses
        uncapped_balance = previous_balance + nominal_delta
        balance = min(MAX_BALANCE, max(-MAX_BALANCE, uncapped_balance))
        updated = deepcopy(self.state)
        profile = updated.setdefault("player_profiles", {}).setdefault(online_id, {})
        profile["balance"] = balance
        settlement = {
            "online_id": online_id,
            "result_digest": result_digest,
            "mission_id": max(0, int(mission_id)),
            "gross_earnings": gross_earnings,
            "expenses": expenses,
            "delta": balance - previous_balance,
            "nominal_delta": nominal_delta,
            "discarded_over_cap": max(0, uncapped_balance - MAX_BALANCE),
            "forgiven_below_debt_floor": max(0, -MAX_BALANCE - uncapped_balance),
            "previous_balance": previous_balance,
            "balance": balance,
        }
        if policy_version is not None:
            settlement["policy_version"] = policy_version
        history = updated.setdefault("battle_settlements", [])
        history.append(settlement)
        del history[:-1_000]
        if item_receipt is not None:
            retained = deepcopy(item_receipt)
            retained["settlement"] = dict(settlement)
            retained["policy_version"] = policy_version
            # Ownership and the exact item reply outlive the bounded cash history.
            updated.setdefault("story_rewards", {})[reward_key] = retained
        # Balance and receipt become visible together. A failed file write or
        # replacement must not make the next request look already paid.
        self.commit_state(updated)
        result = dict(settlement)
        result["applied"] = True
        return result

    def settle_story_group_money(
        self,
        request_digest: str,
        participants: tuple[dict[str, Any], ...],
        *,
        response_hex: str,
        policy_version: str,
    ) -> dict[str, Any]:
        """Atomically settle one two-human Story result and every item receipt."""
        try:
            digest_bytes = bytes.fromhex(request_digest)
            response = bytes.fromhex(response_hex)
        except (TypeError, ValueError) as error:
            raise ValueError("Story group identity or reply is not hexadecimal") from error
        if (
            len(digest_bytes) != 32
            or request_digest != digest_bytes.hex()
            or not response
            or len(response) > 8192
            or response_hex != response.hex()
            or not isinstance(policy_version, str)
            or not policy_version
            or len(policy_version) > 128
            or not isinstance(participants, tuple)
            or len(participants) != 2
        ):
            raise ValueError("Story group settlement is outside its bounds")
        ledger = self.state.get("story_group_rewards", {})
        if not isinstance(ledger, dict):
            raise ValueError("Story group reward history needs review")
        prior = ledger.get(request_digest)
        if prior is not None:
            if (
                not isinstance(prior, dict)
                or prior.get("policy_version") != policy_version
                or prior.get("response_hex") != response_hex
            ):
                raise ValueError("Existing Story group receipt conflicts with this request")
            replay = deepcopy(prior)
            replay["applied"] = False
            return replay

        required = {
            "online_id",
            "player_id",
            "mission_id",
            "gross_earnings",
            "expenses",
            "reward_key",
            "item_receipt",
        }
        normalized = []
        online_ids: set[str] = set()
        player_ids: set[int] = set()
        reward_keys: set[str] = set()
        for row in participants:
            if not isinstance(row, dict) or set(row) != required:
                raise ValueError("Story group participant fields are invalid")
            online_id, player_id = row["online_id"], row["player_id"]
            mission_id = row["mission_id"]
            gross, expenses = row["gross_earnings"], row["expenses"]
            reward_key, receipt = row["reward_key"], row["item_receipt"]
            if (
                not isinstance(online_id, str)
                or not online_id
                or len(online_id) > 64
                or type(player_id) is not int
                or not 0 < player_id < 1 << 32
                or type(mission_id) is not int
                or mission_id <= 0
                or type(gross) is not int
                or not 0 <= gross < 1 << 32
                or type(expenses) is not int
                or not 0 <= expenses < 1 << 32
                or not isinstance(reward_key, str)
                or not isinstance(receipt, dict)
                or reward_key != online_id + ":" + receipt.get("result_digest", "")
                or receipt.get("online_id") != online_id
                or receipt.get("player_id") != player_id
                or receipt.get("reply_hex") != response_hex
                or receipt.get("group_request_digest") != request_digest
            ):
                raise ValueError("Story group participant values are invalid")
            if online_id in online_ids or player_id in player_ids or reward_key in reward_keys:
                raise ValueError("Story group repeats a participant identity")
            online_ids.add(online_id)
            player_ids.add(player_id)
            reward_keys.add(reward_key)
            owner = self.member_by_player_id(player_id)
            balance = self.player_profile(online_id).get("balance")
            if owner is None or owner[0] != online_id:
                raise ValueError("Story group player is not bound to its account")
            if type(balance) is not int or not -MAX_BALANCE <= balance <= MAX_BALANCE:
                raise ValueError("Story group requires every known wallet balance")
            if reward_key in self.state.get("story_rewards", {}):
                raise ValueError("Story group participant receipt already exists")
            normalized.append(deepcopy(row))

        payload_digests = {
            row["item_receipt"].get("payload_sha256") for row in normalized
        }
        if len(payload_digests) != 1:
            raise ValueError("Story group participants disagree on the result payload")
        payload_digest = payload_digests.pop()
        try:
            payload_digest_bytes = bytes.fromhex(payload_digest)
        except (TypeError, ValueError) as error:
            raise ValueError("Story group payload identity is not hexadecimal") from error
        if len(payload_digest_bytes) != 32 or payload_digest != payload_digest_bytes.hex():
            raise ValueError("Story group payload identity must be SHA-256")

        updated = deepcopy(self.state)
        settlements = []
        for row in normalized:
            profile = updated["player_profiles"][row["online_id"]]
            previous = profile["balance"]
            nominal = row["gross_earnings"] - row["expenses"]
            uncapped = previous + nominal
            balance = min(MAX_BALANCE, max(-MAX_BALANCE, uncapped))
            profile["balance"] = balance
            settlement = {
                "online_id": row["online_id"],
                "result_digest": row["item_receipt"]["result_digest"],
                "mission_id": row["mission_id"],
                "gross_earnings": row["gross_earnings"],
                "expenses": row["expenses"],
                "delta": balance - previous,
                "nominal_delta": nominal,
                "discarded_over_cap": max(0, uncapped - MAX_BALANCE),
                "forgiven_below_debt_floor": max(0, -MAX_BALANCE - uncapped),
                "previous_balance": previous,
                "balance": balance,
                "policy_version": policy_version,
            }
            receipt = deepcopy(row["item_receipt"])
            receipt["settlement"] = dict(settlement)
            receipt["policy_version"] = policy_version
            updated.setdefault("story_rewards", {})[row["reward_key"]] = receipt
            updated.setdefault("battle_settlements", []).append(settlement)
            settlements.append(settlement)
        del updated["battle_settlements"][:-1_000]
        retained = {
            "request_digest": request_digest,
            "request_aliases": [request_digest],
            "payload_sha256": payload_digest,
            "participant_player_ids": [row["player_id"] for row in normalized],
            "settled_at_unix_ns": time.time_ns(),
            "policy_version": policy_version,
            "response_hex": response_hex,
            "participants": settlements,
            "reward_keys": sorted(reward_keys),
        }
        groups = updated.setdefault("story_group_rewards", {})
        groups[request_digest] = retained
        while len(groups) > 1_000:
            groups.pop(next(iter(groups)))
        self.commit_state(updated)
        result = deepcopy(retained)
        result["applied"] = True
        return result

    def world_battle_settlement(self, result_digest: str) -> dict[str, Any] | None:
        """Return an immutable-copy view of one retained exact World receipt."""
        try:
            digest_bytes = bytes.fromhex(result_digest)
        except (TypeError, ValueError) as error:
            raise ValueError("World settlement lookup requires a SHA-256 digest") from error
        if len(digest_bytes) != 32 or result_digest != digest_bytes.hex():
            raise ValueError("World settlement lookup requires a canonical SHA-256 digest")
        history = self.state.get("world_battle_settlements", [])
        if not isinstance(history, list):
            raise ValueError("World battle settlement history is invalid")
        for receipt in reversed(history):
            if isinstance(receipt, dict) and receipt.get("result_digest") == result_digest:
                return deepcopy(receipt)
        return None

    def test_war_snapshot(self) -> dict | None:
        """Read the opt-in canonical war; ordinary repositories remain untouched."""
        state = self.state.get("test_war")
        if state is None:
            return None
        verdict_war.validate(state)
        return deepcopy(state)

    def start_test_war(self, *, settings: dict, owners: list[int], now: int) -> dict:
        """Operator-only primitive; the caller must first enforce an empty server."""
        if self.state.get("test_war") is not None:
            raise ValueError("A test war already exists; do not reset ongoing progress")
        updated = deepcopy(self.state)
        updated["test_war"] = verdict_war.new(
            season=3,
            generation=secrets.token_hex(16),
            now=now,
            settings=settings,
            owners=owners,
        )
        self._assign_test_war_teams(updated, next_season=False)
        self.commit_state(updated)
        return self.test_war_snapshot()

    @staticmethod
    def _assign_test_war_teams(updated: dict, *, next_season: bool = True) -> None:
        """Apply next-season preferences without altering saves, wallets or ranks."""
        war = updated["test_war"]
        for team in updated.get("teams", {}).values():
            faction = team.get("current_faction_id", 0)
            explicit = team.get("changeable_properties", {})
            if next_season and isinstance(explicit, dict) and "26" in explicit:
                faction = team.get("desired_faction_id", faction)
            if type(faction) is not int or faction not in (1, 2, 3):
                continue
            areas = [a["area_id"] for a in war["areas"] if a["faction_id"] == faction]
            desired = team.get("territory_area_id", 0)
            if next_season and isinstance(explicit, dict) and "27" in explicit:
                desired = team.get("desired_area_id", desired)
            team.update(
                faction_id=faction,
                current_faction_id=faction,
                territory_area_id=desired
                if type(desired) is int and desired in areas
                else areas[0],
            )

    def advance_test_war(self, now: int) -> dict | None:
        """Persist due transitions once; never synthesize missed offline battles."""
        before = self.test_war_snapshot()
        if before is None:
            return None
        after = verdict_war.advance(before, now)
        if after != before:
            updated = deepcopy(self.state)
            updated["test_war"] = after
            if after["season"] != before["season"]:
                self._assign_test_war_teams(updated)
            self.commit_state(updated)
        return after

    def world_season_id(self) -> int:
        war = self.test_war_snapshot()
        return war["season"] if war else 3

    def settle_world_battle_money(
        self,
        result_digest: str,
        payments: tuple[dict[str, Any], ...],
        *,
        policy_version: str,
        operator_win_player_ids: tuple[int, ...] = (),
        boss_win_player_ids: tuple[int, ...] = (),
        team_progress: tuple[dict[str, Any], ...] = (),
        team_medal_progress: tuple[dict[str, Any], ...] = (),
        player_ranking_progress: tuple[dict[str, Any], ...] = (),
        war_impact: dict | None = None,
    ) -> dict[str, Any]:
        """Commit every participant wallet and exact World reply together.

        A World report is one aggregate submitted by every participant.  Paying
        one request at a time would permit a crash, missing retry, or storage
        failure to leave only part of a battle settled.  This method therefore
        validates every bound account and known wallet before one state-file
        replacement makes all balances and the shared receipt visible.

        ``payments`` contains calculated policy outputs, never raw combat-report
        guesses.  Each entry must have exactly ``online_id``, ``player_id``,
        ``gross_earnings``, ``expenses`` and ``response_hex``.  The retained
        response is the authoritative retry result; later policy or balance
        changes cannot rewrite it.
        """
        try:
            digest_bytes = bytes.fromhex(result_digest)
        except (TypeError, ValueError) as error:
            raise ValueError("World settlement requires a SHA-256 result digest") from error
        if len(digest_bytes) != 32 or result_digest != digest_bytes.hex():
            raise ValueError("World settlement requires a canonical SHA-256 result digest")
        if not isinstance(policy_version, str) or not policy_version or len(policy_version) > 128:
            raise ValueError("World settlement requires a bounded reward policy version")
        if not isinstance(payments, tuple) or not 1 <= len(payments) <= 10:
            raise ValueError("World settlement requires one bounded participant set")
        if war_impact is not None and (
            not isinstance(war_impact, dict)
            or set(war_impact) != {"context", "winning_faction", "boss"}
        ):
            raise ValueError("Invalid authenticated test-war impact")
        if (
            not isinstance(operator_win_player_ids, tuple)
            or len(operator_win_player_ids) > len(payments)
            or any(type(player_id) is not int for player_id in operator_win_player_ids)
            or len(set(operator_win_player_ids)) != len(operator_win_player_ids)
        ):
            raise ValueError("World Operator progress requires unique participant IDs")
        if not isinstance(team_progress, tuple) or len(team_progress) > 2:
            raise ValueError("World team progression requires a bounded team set")
        if (
            not isinstance(boss_win_player_ids, tuple)
            or len(boss_win_player_ids) > len(payments)
            or any(type(player_id) is not int for player_id in boss_win_player_ids)
            or len(set(boss_win_player_ids)) != len(boss_win_player_ids)
        ):
            raise ValueError("World boss progress requires unique participant IDs")
        if not isinstance(team_medal_progress, tuple) or len(team_medal_progress) > 2:
            raise ValueError("World Team Medal progress requires a bounded team set")
        if not isinstance(player_ranking_progress, tuple) or len(player_ranking_progress) > 10:
            raise ValueError("World player ranking progress requires a bounded participant set")

        required = {
            "online_id",
            "player_id",
            "gross_earnings",
            "expenses",
            "response_hex",
        }
        normalized = []
        online_ids: set[str] = set()
        player_ids: set[int] = set()
        for payment in payments:
            if not isinstance(payment, dict) or set(payment) != required:
                raise ValueError("World settlement payment fields are invalid")
            online_id = payment["online_id"]
            player_id = payment["player_id"]
            gross_earnings = payment["gross_earnings"]
            expenses = payment["expenses"]
            response_hex = payment["response_hex"]
            if (
                not isinstance(online_id, str)
                or not online_id
                or len(online_id) > 64
                or type(player_id) is not int
                or not 0 < player_id < 1 << 32
                or type(gross_earnings) is not int
                or not 0 <= gross_earnings < 1 << 32
                or type(expenses) is not int
                or not 0 <= expenses < 1 << 32
                or not isinstance(response_hex, str)
            ):
                raise ValueError("World settlement payment values are invalid")
            try:
                response = bytes.fromhex(response_hex)
            except ValueError as error:
                raise ValueError("World settlement reply is not hexadecimal") from error
            if not response or len(response) > 512 or response_hex != response.hex():
                raise ValueError("World settlement reply is not canonical or bounded")
            if online_id in online_ids or player_id in player_ids:
                raise ValueError("World settlement repeats a participant identity")
            online_ids.add(online_id)
            player_ids.add(player_id)
            normalized.append(
                {
                    "online_id": online_id,
                    "player_id": player_id,
                    "gross_earnings": gross_earnings,
                    "expenses": expenses,
                    "response_hex": response_hex,
                }
            )
        normalized.sort(key=lambda value: value["player_id"])
        if not set(operator_win_player_ids).issubset(player_ids):
            raise ValueError("World Operator progress includes a foreign player")
        operator_win_player_ids = tuple(sorted(operator_win_player_ids))
        if not set(boss_win_player_ids).issubset(player_ids):
            raise ValueError("World boss progress includes a foreign player")
        boss_win_player_ids = tuple(sorted(boss_win_player_ids))
        progress_required = {"team_id", "rank_delta", "special_delta"}
        normalized_progress = []
        progress_team_ids: set[int] = set()
        for progress in team_progress:
            if not isinstance(progress, dict) or set(progress) != progress_required:
                raise ValueError("World team progression fields are invalid")
            team_id = progress["team_id"]
            rank_delta = progress["rank_delta"]
            special_delta = progress["special_delta"]
            if (
                type(team_id) is not int
                or team_id <= 0
                or team_id in progress_team_ids
                or type(rank_delta) is not int
                or not -10_000 <= rank_delta <= 10_000
                or type(special_delta) is not int
                or not 0 <= special_delta <= SPECIAL_SORTIE_SEGMENTS
            ):
                raise ValueError("World team progression values are invalid")
            progress_team_ids.add(team_id)
            normalized_progress.append(
                {
                    "team_id": team_id,
                    "rank_delta": rank_delta,
                    "special_delta": special_delta,
                }
            )
        normalized_progress.sort(key=lambda value: value["team_id"])
        medal_progress_required = {
            "team_id",
            "ac_kills_delta",
            "fame_delta",
            "world_sorties_delta",
            "pvp_wins_delta",
        }
        normalized_medal_progress = []
        medal_progress_team_ids: set[int] = set()
        for progress in team_medal_progress:
            if not isinstance(progress, dict) or set(progress) != medal_progress_required:
                raise ValueError("World Team Medal progress fields are invalid")
            team_id = progress["team_id"]
            ac_kills_delta = progress["ac_kills_delta"]
            fame_delta = progress["fame_delta"]
            world_sorties_delta = progress["world_sorties_delta"]
            pvp_wins_delta = progress["pvp_wins_delta"]
            if (
                type(team_id) is not int
                or team_id <= 0
                or team_id in medal_progress_team_ids
                or type(ac_kills_delta) is not int
                or not 0 <= ac_kills_delta <= 64
                or type(fame_delta) is not int
                or not -10_000 <= fame_delta <= 10_000
                or type(world_sorties_delta) is not int
                or world_sorties_delta not in (0, 1)
                or type(pvp_wins_delta) is not int
                or pvp_wins_delta not in (0, 1)
            ):
                raise ValueError("World Team Medal progress values are invalid")
            medal_progress_team_ids.add(team_id)
            normalized_medal_progress.append(
                {
                    "team_id": team_id,
                    "ac_kills_delta": ac_kills_delta,
                    "fame_delta": fame_delta,
                    "world_sorties_delta": world_sorties_delta,
                    "pvp_wins_delta": pvp_wins_delta,
                }
            )
        normalized_medal_progress.sort(key=lambda value: value["team_id"])
        if normalized_medal_progress and medal_progress_team_ids != progress_team_ids:
            raise ValueError("World rank and Team Medal progress teams differ")
        player_progress_required = {
            "player_id",
            "ac_destructions_delta",
            "mercenary_sorties_delta",
            "mercenary_wins_delta",
        }
        normalized_player_progress = []
        progress_player_ids: set[int] = set()
        for progress in player_ranking_progress:
            if not isinstance(progress, dict) or set(progress) != player_progress_required:
                raise ValueError("World player ranking progress fields are invalid")
            player_id = progress["player_id"]
            destructions = progress["ac_destructions_delta"]
            sorties = progress["mercenary_sorties_delta"]
            wins = progress["mercenary_wins_delta"]
            if (
                type(player_id) is not int
                or player_id not in player_ids
                or player_id in progress_player_ids
                or type(destructions) is not int
                or not 0 <= destructions <= 64
                or type(sorties) is not int
                or sorties not in (0, 1)
                or type(wins) is not int
                or wins not in (0, 1)
                or wins > sorties
            ):
                raise ValueError("World player ranking progress values are invalid")
            progress_player_ids.add(player_id)
            normalized_player_progress.append(dict(progress))
        normalized_player_progress.sort(key=lambda value: value["player_id"])

        history = self.state.get("world_battle_settlements", [])
        if not isinstance(history, list):
            raise ValueError("World battle settlement history is invalid")
        for previous in reversed(history):
            if not isinstance(previous, dict) or previous.get("result_digest") != result_digest:
                continue
            if previous.get("policy_version") != policy_version:
                raise ValueError("Existing World receipt uses a different reward policy")
            if previous.get("war_request") != war_impact:
                raise ValueError("Existing World receipt conflicts with test-war context")
            if (
                "operator_win_player_ids" in previous
                and tuple(previous["operator_win_player_ids"]) != operator_win_player_ids
            ):
                raise ValueError("Existing World receipt conflicts with Operator progress")
            if "team_progress" in previous and previous["team_progress"] != normalized_progress:
                raise ValueError("Existing World receipt conflicts with team progression")
            if (
                "team_medal_progress" in previous
                and previous["team_medal_progress"] != normalized_medal_progress
            ):
                raise ValueError("Existing World receipt conflicts with Team Medal progress")
            if (
                "player_ranking_progress" in previous
                and previous["player_ranking_progress"] != normalized_player_progress
            ):
                raise ValueError("Existing World receipt conflicts with player ranking progress")
            if (
                "boss_win_player_ids" in previous
                and tuple(previous["boss_win_player_ids"]) != boss_win_player_ids
            ):
                raise ValueError("Existing World receipt conflicts with boss progress")
            retained_inputs = [
                {key: participant.get(key) for key in required}
                for participant in previous.get("participants", [])
                if isinstance(participant, dict)
            ]
            if retained_inputs != normalized:
                raise ValueError("Existing World receipt conflicts with proposed settlement")
            replay = deepcopy(previous)
            replay["applied"] = False
            return replay

        # A committed receipt remains authoritative even if a later snapshot
        # loses an account mapping or wallet. Validate those mutable sources
        # only when creating a new transaction, after the replay check.
        for payment in normalized:
            owner = self.member_by_player_id(payment["player_id"])
            profile = self.player_profile(payment["online_id"])
            balance = profile.get("balance")
            if owner is None or owner[0] != payment["online_id"]:
                raise ValueError("World settlement player is not bound to its account")
            if type(balance) is not int or not -MAX_BALANCE <= balance <= MAX_BALANCE:
                raise ValueError("World settlement requires every known wallet balance")
        for progress in normalized_progress:
            team = self.team_by_id(progress["team_id"])
            if team is None:
                raise ValueError("World progression team no longer exists")
            team_progress_view(team)
        for progress in normalized_medal_progress:
            if self.team_by_id(progress["team_id"]) is None:
                raise ValueError("World Team Medal progress team no longer exists")

        updated = deepcopy(self.state)
        participants = []
        reconcile_mercenary_earnings(updated)
        operator_progress = []
        player_medal_awards = []
        recognized_at = datetime.now(timezone.utc)
        player_ranking_audit = []
        progress_audit = []
        medal_progress_audit = []
        for progress in normalized_progress:
            team = updated["teams"][str(progress["team_id"])]
            progress_audit.append(
                {
                    "team_id": progress["team_id"],
                    **apply_team_progress(
                        team,
                        rank_delta=progress["rank_delta"],
                        special_delta=progress["special_delta"],
                    ),
                }
            )
        for progress in normalized_medal_progress:
            team = updated["teams"][str(progress["team_id"])]
            medal_progress_audit.append(
                {
                    "team_id": progress["team_id"],
                    **apply_team_medal_progress(
                        team,
                        ac_kills_delta=progress["ac_kills_delta"],
                        fame_delta=progress["fame_delta"],
                        world_sorties_delta=progress["world_sorties_delta"],
                        pvp_wins_delta=progress["pvp_wins_delta"],
                    ),
                }
            )
        ranking_awards = award_ranking_medals(updated["teams"])
        player_progress_by_id = {
            progress["player_id"]: progress for progress in normalized_player_progress
        }
        for payment in normalized:
            profile = updated.setdefault("player_profiles", {}).setdefault(
                payment["online_id"], {}
            )
            previous_balance = profile["balance"]
            nominal_delta = payment["gross_earnings"] - payment["expenses"]
            uncapped_balance = previous_balance + nominal_delta
            balance = min(MAX_BALANCE, max(-MAX_BALANCE, uncapped_balance))
            profile["balance"] = balance
            if payment["player_id"] in boss_win_player_ids:
                previous_boss_wins = profile.get("observed_world_boss_wins", 0)
                if (
                    type(previous_boss_wins) is not int
                    or not 0 <= previous_boss_wins < (1 << 32) - 1
                ):
                    raise ValueError("Stored World boss progress needs review")
                profile["observed_world_boss_wins"] = previous_boss_wins + 1
            if payment["player_id"] in operator_win_player_ids:
                previous_wins = profile.get("observed_operator_wins", 0)
                if type(previous_wins) is not int or not 0 <= previous_wins < (1 << 32) - 1:
                    raise ValueError("Stored World Operator progress needs review")
                profile["observed_operator_wins"] = previous_wins + 1
                operator_progress.append(
                    {
                        "player_id": payment["player_id"],
                        "previous_wins": previous_wins,
                        "current_wins": previous_wins + 1,
                    }
                )
            ranking_progress = player_progress_by_id.get(payment["player_id"])
            if ranking_progress is not None:
                previous_destructions = profile.get("observed_ac_destructions", 0)
                previous_sorties = profile.get("observed_mercenary_sorties", 0)
                previous_wins = profile.get("observed_mercenary_wins", 0)
                mercenary_rank = profile.get("mercenary_rank", 0)
                if (
                    any(
                        type(value) is not int or not 0 <= value < (1 << 32)
                        for value in (previous_destructions, previous_sorties, previous_wins)
                    )
                    or previous_wins > previous_sorties
                    or (type(mercenary_rank) is not int or not 0 <= mercenary_rank <= 6)
                ):
                    raise ValueError("Stored player ranking progress needs review")
                current_destructions = (
                    previous_destructions + ranking_progress["ac_destructions_delta"]
                )
                current_sorties = previous_sorties + ranking_progress["mercenary_sorties_delta"]
                current_wins = previous_wins + ranking_progress["mercenary_wins_delta"]
                if any(
                    value >= 1 << 32
                    for value in (current_destructions, current_sorties, current_wins)
                ):
                    raise ValueError("Player ranking progress exceeds its native bound")
                profile["observed_ac_destructions"] = current_destructions
                if ranking_progress["mercenary_sorties_delta"]:
                    credit_mercenary_earnings(profile, payment["gross_earnings"])
                    profile["observed_mercenary_sorties"] = current_sorties
                    profile["observed_mercenary_wins"] = current_wins
                    profile.setdefault("mercenary_rank", 0)
                player_ranking_audit.append(
                    {
                        **ranking_progress,
                        "previous_ac_destructions": previous_destructions,
                        "observed_ac_destructions": current_destructions,
                        "previous_mercenary_sorties": previous_sorties,
                        "observed_mercenary_sorties": current_sorties,
                        "previous_mercenary_wins": previous_wins,
                        "observed_mercenary_wins": current_wins,
                    }
                )
            profile.setdefault("player_id", payment["player_id"])
            awarded = award_verified_medals(profile, payment["player_id"], recognized_at)
            if awarded:
                player_medal_awards.append(
                    {"player_id": payment["player_id"], "medal_ids": list(awarded)}
                )
            participants.append(
                {
                    **payment,
                    "delta": balance - previous_balance,
                    "nominal_delta": nominal_delta,
                    "discarded_over_cap": max(0, uncapped_balance - MAX_BALANCE),
                    "forgiven_below_debt_floor": max(0, -MAX_BALANCE - uncapped_balance),
                    "previous_balance": previous_balance,
                    "balance": balance,
                }
            )
        if player_medal_awards:
            self._reconcile_player_medal_progress(updated)
        advance_team_records(self.state, updated, normalized_progress)
        receipt = {
            "result_digest": result_digest,
            "policy_version": policy_version,
            "participants": participants,
            "operator_win_player_ids": list(operator_win_player_ids),
            "operator_win_progress": operator_progress,
            "boss_win_player_ids": list(boss_win_player_ids),
            "player_medal_awards": player_medal_awards,
            "team_progress": normalized_progress,
            "team_progress_audit": progress_audit,
            "team_medal_progress": normalized_medal_progress,
            "team_medal_progress_audit": medal_progress_audit,
            "team_ranking_medal_awards": {
                str(team_id): list(medal_ids)
                for team_id, medal_ids in sorted(ranking_awards.items())
            },
            "player_ranking_progress": normalized_player_progress,
            "player_ranking_progress_audit": player_ranking_audit,
        }
        if war_impact is not None:
            receipt["war_request"] = deepcopy(war_impact)
            if updated.get("test_war") is not None:
                previous_season = updated["test_war"]["season"]
                updated["test_war"], receipt["war_impact"] = verdict_war.impact(
                    updated["test_war"],
                    war_impact["context"],
                    winning_faction=war_impact["winning_faction"],
                    boss=war_impact["boss"],
                    now=int(time.time()),
                )
                if updated["test_war"]["season"] != previous_season:
                    self._assign_test_war_teams(updated)
        retained = updated.setdefault("world_battle_settlements", [])
        retained.append(receipt)
        del retained[:-1_000]
        self.commit_state(updated)
        result = deepcopy(receipt)
        result["applied"] = True
        return result

    def transfer_money(
        self,
        sender_online_id: str,
        recipient_player_id: int,
        amount: int,
        *,
        initial: int = 1_000_000,
        request_digest: str | None = None,
    ) -> dict[str, Any] | None:
        """Commit once per retained authenticated request, never per amount alone.

        The optional identity shares the existing bounded transfer history and
        atomic balance write. Both wallets must already have stored balances;
        the legacy initial argument cannot bootstrap a transfer. Acquisition of
        missing baselines and incoming-credit reconciliation remain separate.
        """
        recipient = self.member_by_player_id(int(recipient_player_id))
        amount = int(amount)
        if recipient is None or amount <= 0:
            return None
        recipient_online_id = recipient[0]
        if not recipient_online_id or recipient_online_id == sender_online_id:
            return None
        if request_digest is not None:
            for previous in reversed(self.state.get("money_transfers", [])):
                if (
                    isinstance(previous, dict)
                    and previous.get("sender_online_id") == sender_online_id
                    and previous.get("request_digest") == request_digest
                ):
                    if (
                        previous.get("recipient_player_id") != int(recipient_player_id)
                        or previous.get("amount") != amount
                    ):
                        return None
                    # Check before overdraft: the first successful payment may
                    # have consumed all funds. A retry still needs its success ACK.
                    return {**previous, "applied": False}
        # Identity/membership alone is not evidence of either wallet's value.
        # Native replay showed the old fallback minting 900,000 extra Au for
        # an existing save. Do not replace an unknown value with zero or a grant.
        if any(
            "balance" not in self.player_profile(online_id)
            for online_id in (sender_online_id, recipient_online_id)
        ):
            return None
        sender_balance = self.player_profile(sender_online_id).get("balance")
        recipient_balance = self.player_profile(recipient_online_id).get("balance")
        if any(
            type(balance) is not int or not -MAX_BALANCE <= balance <= MAX_BALANCE
            for balance in (sender_balance, recipient_balance)
        ):
            return None
        if sender_balance < amount or recipient_balance + amount > MAX_BALANCE:
            return None
        updated = deepcopy(self.state)
        sender_profile = updated.setdefault("player_profiles", {}).setdefault(
            sender_online_id, {}
        )
        recipient_profile = updated.setdefault("player_profiles", {}).setdefault(
            recipient_online_id, {}
        )
        sender_profile["balance"] = sender_balance - amount
        recipient_profile["balance"] = recipient_balance + amount
        transfer = {
            "sender_online_id": sender_online_id,
            "recipient_online_id": recipient_online_id,
            "recipient_player_id": int(recipient_player_id),
            "amount": amount,
            "sender_balance": int(sender_profile["balance"]),
            "recipient_balance": int(recipient_profile["balance"]),
        }
        if request_digest is not None:
            transfer["request_digest"] = request_digest
        history = updated.setdefault("money_transfers", [])
        history.append(transfer)
        del history[:-1_000]
        self.commit_state(updated)
        return {**transfer, "applied": True} if request_digest is not None else dict(transfer)

    # GARAGE/FACTION RECORDS: these do not grant DLC or restore missing ownership.
    def set_garage_custom_info(
        self, online_id: str, primary: int, secondary: int
    ) -> dict[str, int]:
        profile = self.state.setdefault("player_profiles", {}).setdefault(online_id, {})
        profile["garage_custom_primary"] = max(0, int(primary))
        profile["garage_custom_secondary"] = max(0, int(secondary))
        self._save()
        return self.garage_custom_info(online_id)

    def garage_custom_info(self, online_id: str) -> dict[str, int]:
        profile = self.state.setdefault("player_profiles", {}).get(online_id, {})
        if not isinstance(profile, dict):
            profile = {}
        return {
            "primary": max(0, int(profile.get("garage_custom_primary", 0))),
            "secondary": max(0, int(profile.get("garage_custom_secondary", 0))),
        }

    def promote_faction(self, online_id: str) -> dict[str, Any] | None:
        """Apply the granted faction without replacing an assigned territory."""
        team = self.team_for(online_id)
        if team is None:
            return None
        faction_id = int(team.get("faction_id", 0))
        if faction_id not in (1, 2, 3):
            return team
        changed = team.get("current_faction_id") != faction_id
        if changed:
            team["current_faction_id"] = faction_id
        # A results read can recur during grace-period polling. Preserve every
        # existing assignment and its stored-layout binding. The legacy initial
        # faction-to-area mapping applies only to an unassigned integer zero.
        area_id = team.get("territory_area_id", 0)
        if type(area_id) is int and area_id == 0:
            area = self._initial_faction_area(faction_id)
            if area != area_id:
                team["territory_area_id"] = area
                changed = True
        if changed:
            self._save()
        return team

    # MERCENARY DIRECTORY/OFFERS: accepted live hires are owned by HireBindings.
    def register_mercenary(
        self,
        *,
        online_id: str,
        player_id: int,
        ac_data: bytes,
        result_values: list[int],
    ) -> dict[str, Any]:
        """Persist one currently available mercenary and their advertised AC."""
        values = [max(0, int(value)) for value in result_values[:4]]
        values.extend([0] * (4 - len(values)))
        record = {
            "online_id": online_id,
            "player_id": int(player_id),
            "ac_data_b64": base64.b64encode(ac_data).decode("ascii"),
            "result_values": values,
        }
        self.state.setdefault("mercenaries", {})[online_id] = record
        self._save()
        return record

    def unregister_mercenary(self, online_id: str) -> bool:
        """Remove transient availability and undelivered hire requests."""
        removed = self.state.setdefault("mercenaries", {}).pop(online_id, None) is not None
        inbox_removed = (
            self.state.setdefault("mercenary_messages", {}).pop(online_id, None) is not None
        )
        if removed or inbox_removed:
            self._save()
        return removed

    def available_mercenaries(self, *, exclude_online_id: str = "") -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for online_id, stored in self.state.setdefault("mercenaries", {}).items():
            if online_id == exclude_online_id or not isinstance(stored, dict):
                continue
            record = dict(stored)
            try:
                record["ac_data"] = base64.b64decode(
                    str(record.get("ac_data_b64", "")), validate=True
                )
            except (ValueError, base64.binascii.Error):
                record["ac_data"] = b""
            records.append(record)
        return sorted(
            records,
            key=lambda item: (str(item.get("online_id", "")), int(item.get("player_id", 0))),
        )

    def mercenary_for_player_id(self, player_id: int) -> dict[str, Any] | None:
        return next(
            (
                record
                for record in self.available_mercenaries()
                if int(record.get("player_id", 0)) == int(player_id)
            ),
            None,
        )

    def queue_mercenary_message(
        self,
        *,
        target_player_id: int,
        employer_online_id: str,
        employer_player_id: int,
        employer_team_name: str,
    ) -> dict[str, Any] | None:
        target = self.mercenary_for_player_id(target_player_id)
        if target is None:
            return None
        target_online_id = str(target.get("online_id", ""))
        employer_team = self.team_for(employer_online_id)
        target_team = self.team_for(target_online_id)
        if (
            employer_team is not None
            and target_team is not None
            and int(employer_team.get("team_id", 0)) > 0
            and int(employer_team.get("team_id", 0)) == int(target_team.get("team_id", 0))
        ):
            return None
        message_id = max(1, int(self.state.setdefault("next_mercenary_message_id", 1)))
        self.state["next_mercenary_message_id"] = message_id + 1
        message = {
            "message_id": message_id,
            "target_online_id": target_online_id,
            "target_player_id": int(target_player_id),
            "employer_online_id": employer_online_id,
            "employer_player_id": int(employer_player_id),
            "employer_team_name": employer_team_name,
        }
        inbox = self.state.setdefault("mercenary_messages", {}).setdefault(
            message["target_online_id"], []
        )
        inbox.append(message)
        self._save()
        return message

    def mercenary_messages_for(self, online_id: str) -> list[dict[str, Any]]:
        messages = self.state.setdefault("mercenary_messages", {}).get(online_id, [])
        return [dict(message) for message in messages if isinstance(message, dict)]

    def cancel_mercenary_messages(self, employer_online_id: str) -> int:
        removed = 0
        inboxes = self.state.setdefault("mercenary_messages", {})
        for target_online_id, messages in list(inboxes.items()):
            kept = [
                message
                for message in messages
                if not isinstance(message, dict)
                or message.get("employer_online_id") != employer_online_id
            ]
            removed += len(messages) - len(kept)
            if kept:
                inboxes[target_online_id] = kept
            else:
                inboxes.pop(target_online_id, None)
        if removed:
            self._save()
        return removed

    # DURABLE COMMIT: all mutations above retain this shared storage boundary.
    def commit_state(self, updated: dict[str, Any]) -> None:
        """Commit a prepared state in the server's serialized mutation path.

        Callers prepare a separate copy. A failed write leaves the current
        in-memory state unchanged. _save replaces the file atomically. This
        is not a lock for independent processes that edit the same store.
        """
        previous = self.state
        try:
            record_team_changes(previous, updated)
            self.state = updated
            self._save()
        except BaseException:
            self.state = previous
            raise

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(self.state_path)
