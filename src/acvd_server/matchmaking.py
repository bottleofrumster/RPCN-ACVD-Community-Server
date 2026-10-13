"""Single owner of transient preparation, match and room-watch state.

Named Normal and Special controls have limited native qualification. Signaling
or room archives alone do not prove gameplay. The optional RPCS3-log bridge is
legacy compatibility; normal room transfer uses the authenticated native archive.
Transport and durable teams remain outside this coordinator.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from pathlib import Path
from typing import Callable

from . import verdict_war
from .combat_reports import BattleDisconnectReport, decode_combat_report
from .cq_map_policy import (
    CQ_AREA_MAPS,
    CQ_MAP_DEFAULTS,
    CQ_MAP_PROFILES,
    select_cq_map,
    validate_cq_map_settings,
)
from .hire_bindings import HireBindings
from .match_messages import (
    ACVD_MATCHING2_SESSION_PASSWORD,
    NORMAL_BATTLE_TYPE_EXTERMINATION,
    NORMAL_BATTLE_TYPE_NORMAL_WEAPONS,
    NORMAL_BATTLE_TYPE_OFFLINE_TERRITORY,
    NORMAL_BATTLE_TYPE_SPECIAL_WEAPONS,
    NORMAL_BATTLE_TYPE_STORED_TERRITORY,
    _match_room_info_push,
    _matching_result_for_members,
    active_single_team_result,
    interwar_boss_result,
)
from .match_policy import (
    MAX_MATCH_CLIENTS,
    MAX_TEAM_CLIENTS,
    intra_faction_pair,
    normal_pair_roles,
    opponent_faction_allowed,
    preparation_squad,
    same_squad,
)
from .protocol import ProtocolError
from .rewards import world_reward_participants
from .room_messages import native_room_info_push, validate_room_archive
from .team_messages import (
    SESSION_MEMBER_JOIN_PUSH,
    SESSION_MEMBER_LEAVE_PUSH,
    _team_session_leave_push,
    _team_session_member_push,
)
from .telemetry import Telemetry
from .udp import GameSession, PushSender
from .world_messages import CURRENT_SEASON_ID, WORLD_PHASE_ACTIVE

INTERWAR_BOSS = "interwar_boss"
ACTIVE_SINGLE_TEAM = "active_single_team"
INTERWAR_CONTEXT_SECONDS = 30 * 60
INTERWAR_RECEIPTS_PER_PLAYER = 8
MATCH_HANDOFF_SECONDS = 180
MATCH_ACTIVE_SECONDS = 60 * 60  # Bounded room/briefing/combat context, not a game timer.
# Human clients, including hired mercenaries. UNAC records do not fill this list.
# Story has a separate two-client contract; do not apply it to single-team bosses.
MAX_SINGLE_TEAM_CLIENTS = MAX_TEAM_CLIENTS
SINGLE_TEAM_FALLBACK_SECONDS = 30.0


def _rpcn_create_room_tuple(log_text: str) -> dict[int, int] | None:
    """Read the latest response. Return None if required fields are absent."""
    marker = "SceNpMatching2CreateJoinRoomResponse:"
    marker_at = log_text.rfind(marker)
    if marker_at < 0:
        return None
    section = log_text[marker_at:]
    values: dict[int, int] = {}
    for line in section.splitlines():
        for label, field_number in (("serverId", 1), ("worldId", 2), ("roomId", 3)):
            token = f"{label}:"
            if token not in line:
                continue
            raw_value = line.split(token, 1)[1].strip().split(maxsplit=1)[0]
            try:
                values[field_number] = int(raw_value, 0)
            except ValueError:
                pass
    return values if set(values) == {1, 2, 3} else None


class SpecialSortieChargeRequired(ValueError):
    """A stale/undercharged wartime Special must receive a terminal refusal."""


class MatchmakingCoordinator:
    def __init__(
        self,
        *,
        get_team_sessions: Callable[[], dict[int, dict]],
        send_push: PushSender,
        telemetry: Telemetry,
        room_log_paths: list[Path],
        get_world_phase: Callable[[], int] | None = None,
        get_sessions: Callable | None = None,
        get_team_id: Callable[[str], int] | None = None,
        select_opposing_territory: Callable[[int, int], tuple[str, int, int, int] | None]
        | None = None,
        special_sortie_ready: Callable[[int], bool] | None = None,
        consume_special_sortie: Callable[[int], dict[str, int] | None] | None = None,
        get_cq_map_settings: Callable[[], dict] | None = None,
        single_team_fallback_seconds: float = SINGLE_TEAM_FALLBACK_SECONDS,
        get_test_war: Callable[[], dict | None] | None = None,
    ) -> None:
        self._get_team_sessions = get_team_sessions
        self._send_game_push = send_push
        self.telemetry = telemetry
        self.get_world_phase = get_world_phase or (lambda: WORLD_PHASE_ACTIVE)
        self.hires = HireBindings(get_sessions or (lambda: ()), get_team_id or (lambda _: 0))
        self.select_opposing_territory = select_opposing_territory or (lambda _team, _faction: None)
        self.special_sortie_ready = special_sortie_ready or (lambda _team_id: True)
        self.consume_special_sortie = consume_special_sortie or (
            lambda team_id: {"team_id": team_id}
        )
        self.get_cq_map_settings = get_cq_map_settings or (lambda: dict(CQ_MAP_DEFAULTS))
        self.single_team_fallback_seconds = max(0.0, float(single_team_fallback_seconds))
        self.get_test_war = get_test_war or (lambda: None)
        self.preparations: dict[str, dict] = {}
        self.matches: dict[int, dict] = {}
        # Preserve peer readiness across short-lived matches until session cleanup.
        self.signaling_ready_pairs: set[frozenset[str]] = set()
        self.next_prepare_id = 1
        self.next_match_id = 1
        self.match_room_log_paths = room_log_paths
        self._closed = False

    def _select_cq_combat_area(
        self,
        original_area_id: int,
        *,
        match_id: int,
        battle_type: int,
        kind: str | None,
    ) -> tuple[int, bool, str, dict | None]:
        """Apply exact-map locking only to the level-driven Extermination mode.

        Burn runs and Special sorties select distinct region-specific mission
        columns, so the approved Extermination names must not be presented as
        exact locks for those modes.
        """
        compatible = kind != INTERWAR_BOSS and battle_type == NORMAL_BATTLE_TYPE_EXTERMINATION
        war = self.get_test_war()
        if war is not None:
            if not compatible:
                return original_area_id, False, "test_war", None
            area = next((a for a in war["areas"] if a["area_id"] == original_area_id), None)
            if area is None:
                return original_area_id, False, "test_war", None
            mission = CQ_AREA_MAPS[original_area_id][verdict_war.level(area, war["rules"])][0]
            return original_area_id, False, "test_war", dict(CQ_MAP_PROFILES[mission])
        settings = validate_cq_map_settings(self.get_cq_map_settings())
        mode = settings["cq_map_mode"]
        if not compatible:
            return original_area_id, False, mode, None
        profile = select_cq_map(settings, match_id)
        selected = original_area_id if profile is None else int(profile["area_id"])
        return selected, profile is not None, mode, profile

    def close(self) -> None:
        """Cancel every owned async watcher before the transport loop stops."""
        if self._closed:
            return
        self._closed = True
        preparation_count = len(self.preparations)
        match_count = len(self.matches)
        for preparation in tuple(self.preparations.values()):
            self._cancel_single_team_fallback(preparation)
        for match in tuple(self.matches.values()):
            self._cancel_match_room_log_watch(match)
        self.preparations.clear()
        self.matches.clear()
        self.signaling_ready_pairs.clear()
        self.telemetry.emit(
            "game_matchmaking_closed",
            preparation_count=preparation_count,
            match_count=match_count,
        )

    @property
    def team_sessions(self) -> dict[int, dict]:
        """Borrow the current live directory, including after registry replacement."""
        return self._get_team_sessions()

    def season_id(self) -> int:
        war = self.get_test_war()
        return war["season"] if war else CURRENT_SEASON_ID

    def _war_context(self, area_id: int, attacker: int) -> dict | None:
        war = self.get_test_war()
        if war is None:
            return None
        area = war["areas"][area_id - 1]
        return {
            "generation": war["generation"],
            "season": war["season"],
            "area_id": area_id,
            "area_epoch": area["epoch"],
            "attacker_faction": attacker,
            "defender_faction": area["faction_id"],
        }

    def _war_area_current_for_squad(self, area_id: int, squad: dict) -> bool:
        war = self.get_test_war()
        if war is None:
            return True
        expected = verdict_war.level(war["areas"][area_id - 1], war["rules"])
        return all(
            isinstance(peer.test_war_view, dict)
            and peer.test_war_view.get("generation") == war["generation"]
            and peer.test_war_view.get("season") == war["season"]
            and peer.test_war_view.get("levels", {}).get(area_id) == expected
            for peer, _team in squad.values()
        )

    @staticmethod
    def war_impact(match: dict, report) -> dict | None:
        if match.get("war_context") is None:
            return None
        factions = {m["team_id"]: m.get("faction_id", 0) for m in match["members"]}
        winners = {
            factions.get(p.settlement_team_id, 0)
            for p in world_reward_participants(report, match["members"])
            if report.side_by_player.get(p.player_id) == report.winning_side
        }
        winner = next(iter(winners)) if len(winners) == 1 else 0
        return {
            "context": dict(match["war_context"]),
            "winning_faction": winner,
            "boss": match.get("criteria", {}).get(3) == 2,
        }

    # PREPARATION: validate current squad identity before allocating match state.
    def validate_registration(
        self,
        session: GameSession,
        criteria: dict[int, int],
        player_ids: list[int],
        team_id: int,
        phase: int,
        player_roles: list[int | None] | None = None,
    ) -> bool:
        """Gate the qualified grace path before any preparation or reply changes."""
        current = self.preparations.get(session.online_id)
        match = self.matches.get(int((current or {}).get("match_id", 0)))
        # Retain completed contexts for result replay, not as an active-mission
        # barrier when the same unit enters a different mode after settlement.
        if match and match.get("result_sha256"):
            match = None
        if self.get_test_war() is not None and criteria.get(1) != self.season_id():
            raise ValueError("Preparation is from an old test-war season; return to World")
        if phase == 0 and criteria.get(3) == 1:
            raise ValueError("Normal Sortie is unavailable during the grace period")
        retrying_active_special = bool(
            match is not None
            and match.get("kind") == ACTIVE_SINGLE_TEAM
            and not match.get("result_sha256")
            and current is not None
            and current.get("session") is session
            and current.get("criteria", {}).get(3) == 2
            and current.get("criteria") == criteria
            and current.get("team_id") == team_id
            and current.get("claimed_player_ids") == player_ids
            and current.get("claimed_player_roles") == player_roles
        )
        if (
            phase == WORLD_PHASE_ACTIVE
            and criteria.get(3) == 2
            and not retrying_active_special
            and not self.special_sortie_ready(team_id)
        ):
            raise SpecialSortieChargeRequired("Special Sortie requires a full Normal-mission gauge")
        interwar = criteria.get(3) == 2 and (criteria.get(2) == 0 or phase == 0)
        if interwar and match and match.get("kind") != INTERWAR_BOSS:
            raise ValueError("A different matched mission is still active")
        if (
            match
            and match.get("kind") == INTERWAR_BOSS
            and current["session"] is session
            and not match.get("result_sha256")
            and (
                current["criteria"] != criteria
                or current["team_id"] != team_id
                or current.get("claimed_player_ids") != player_ids
                or current.get("claimed_player_roles") != player_roles
            )
        ):
            raise ValueError("The current grace-period mission has not ended")
        if not interwar:
            return False
        if phase != 0 or criteria.get(2) != 0 or criteria.get(1) != self.season_id():
            raise ValueError("The requested grace-period context is not current")
        if not session.established or session.peer is None or team_id <= 0:
            raise ValueError("Grace-period entry requires a live authenticated team member")
        if type(session.player_id) is not int or not 0 < session.player_id < 1 << 32:
            raise ValueError("Grace-period entry requires a valid authenticated player ID")
        self.resolve_interwar_squad(session, player_ids, player_roles, team_id)
        return True

    def resolve_activity_squad(self, session, player_ids, player_roles, team_id):
        """Freeze up to five current teammates or explicitly accepted mercenaries."""
        if player_ids == [session.player_id] and player_roles is None:
            player_roles = [0]  # Existing solo callers can omit the role list.
        if (
            not 1 <= len(player_ids) <= MAX_SINGLE_TEAM_CLIENTS
            or any(
                type(player_id) is not int or not 0 < player_id < 1 << 32
                for player_id in player_ids
            )
            or len(set(player_ids)) != len(player_ids)
            or player_ids[0] != session.player_id
            or player_roles is None
            or len(player_roles) != len(player_ids)
            or player_roles[0] != 0
            or any(type(role) is not int or role not in (0, 2) for role in player_roles)
        ):
            raise ValueError("Squad requires one to five distinct clients with known roles")
        if not self.hires.is_current_member(session, session.player_id, team_id):
            raise ValueError("Squad leader is not a current authenticated team member")
        squad = {session.player_id: (session, team_id)}
        for player_id, role in zip(player_ids[1:], player_roles[1:]):
            if role == 2:
                binding = self.hires.resolve(session, player_id)
                if binding is None or binding.employer_team != team_id:
                    raise ValueError("Mercenary has no current authenticated hire binding")
                member, member_team = binding.mercenary, binding.mercenary_team
            else:
                candidates = [
                    peer for peer in self.hires.get_sessions() if peer.player_id == player_id
                ]
                if len(candidates) != 1 or not self.hires.is_current_member(
                    candidates[0], player_id, team_id
                ):
                    raise ValueError("Teammate is not one current authenticated team member")
                member, member_team = candidates[0], team_id
            squad[player_id] = member, member_team
        if len({peer.online_id for peer, _ in squad.values()}) != len(squad):
            raise ValueError("Squad contains an ambiguous account identity")
        return squad

    def resolve_interwar_squad(self, session, player_ids, player_roles, team_id):
        """Keep the authenticated squad exclusive to its current boss context."""
        squad = self.resolve_activity_squad(session, player_ids, player_roles, team_id)
        for match in self.matches.values():
            if match.get("kind") != INTERWAR_BOSS or match.get("result_sha256"):
                continue
            if not set(squad).intersection(match.get("squad", {})):
                continue
            owner = match["members"][0]["session"]
            if owner.online_id != session.online_id:
                raise ValueError("Boss squad member is assigned to another active mission")
            if owner is not session:
                # A genuine new login replaces the old owner's context in
                # prepare_registration; it is not a retry of that context.
                continue
            # A retry must not replace a frozen participant's authenticated session.
            if any(
                match["squad"].get(pid, (None, 0))[0] is not peer
                for pid, (peer, _) in squad.items()
            ):
                raise ValueError("Boss squad session changed after mission release")
        return squad

    def prepare_registration(self, session: GameSession) -> None:
        """Give a completed boss mission a new key without erasing its receipt."""
        current = self.preparations.get(session.online_id)
        match = self.matches.get(int((current or {}).get("match_id", 0)))
        if current and current["session"] is not session:
            self._remove_matchmaking_member(session.online_id, reason="session_replaced")
        elif match and match.get("result_sha256"):
            self.preparations.pop(session.online_id, None)

    def _active_squad_match(self, session: GameSession) -> dict | None:
        """Resolve one live frozen participant, not only a registered captain.

        Crew members do not send their own preparation. Their authority comes
        from the captain's validated roster and exact authenticated transport.
        Completed, replaced and ambiguous contexts grant no new read access.
        """
        now = time.monotonic()
        candidates = []
        for match in self.matches.values():
            if (
                match.get("result_sha256")
                or match.get("preflight_retry_pending")
                or now - match["created_monotonic"] > self.match_lifetime(match)
            ):
                continue
            own = match.get("squad", {}).get(session.player_id)
            if own is None or own[0] is not session:
                continue
            if not self.hires.is_current_member(session, session.player_id, own[1]):
                continue
            captains = match.get("members", [])
            if not captains or any(
                self.preparations.get(member["session"].online_id) is not member
                or not self.hires.is_current_member(
                    member["session"], member["session"].player_id, member["team_id"]
                )
                for member in captains
            ):
                continue
            candidates.append(match)
        return candidates[0] if len(candidates) == 1 else None

    def accepted_world_phase(self, session: GameSession) -> int | None:
        match = self._active_squad_match(session)
        return match.get("world_phase", WORLD_PHASE_ACTIVE) if match else None

    def team_peer_recovery_allowed(self, reporter: GameSession, peer: GameSession) -> bool:
        """Do not apply a generation-free old report to a newer live battle.

        Either participant's current search, preflight, room or combat blocks
        recovery. A completed result permits it. Mock has no central prepare;
        the authenticated disconnect report is its available lifecycle signal.
        """
        now = time.monotonic()

        def contains(record):
            return any(
                member is reporter or member is peer
                for member, _ in record.get("squad", {}).values()
            ) or any(record.get("session") is member for member in (reporter, peer))

        for match in self.matches.values():
            if (contains(match) or any(contains(p) for p in match.get("members", []))) and (
                not match.get("result_sha256")
                and now - match["created_monotonic"] <= self.match_lifetime(match)
            ):
                return False
        for preparation in self.preparations.values():
            if not contains(preparation):
                continue
            match = self.matches.get(int(preparation.get("match_id", 0)))
            if (
                match is None
                or not match.get("result_sha256")
                or not any(p is preparation for p in match.get("members", []))
            ):
                return False
        return True

    # GRACE-PERIOD MISSIONS: authenticated single-team boss context.
    def _release_interwar_boss(self, preparation: dict) -> None:
        session = preparation["session"]
        current = self.matches.get(int(preparation.get("match_id", 0)))
        if current and current.get("kind") == INTERWAR_BOSS:
            current["members"] = [preparation]
            self.telemetry.emit(
                "game_interwar_preparation_reused",
                online_id=session.online_id,
                match_id=current["match_id"],
                prepare_id=preparation["prepare_id"],
            )
            return
        payload = interwar_boss_result(preparation["prepare_id"], preparation["territory_area_id"])
        match_id = self.next_match_id
        self.next_match_id += 1
        match = {
            "match_id": match_id,
            "kind": INTERWAR_BOSS,
            "members": [preparation],
            "criteria": dict(preparation["criteria"]),
            "world_phase": 0,
            "battle_type": 6,
            "stored_area_id": preparation["territory_area_id"],
            "squad": dict(
                preparation.get("squad", {session.player_id: (session, preparation["team_id"])})
            ),
            "created_monotonic": time.monotonic(),
            "state": "released",
        }
        self.matches[match_id] = match
        preparation["match_id"] = match_id
        self._send_game_push(session, payload, subtype=2)
        self.telemetry.emit(
            "game_interwar_boss_released",
            online_id=session.online_id,
            match_id=match_id,
            prepare_id=preparation["prepare_id"],
            stored_area_id=preparation["territory_area_id"],
            payload=payload,
        )

    @staticmethod
    def _cancel_single_team_fallback(preparation: dict) -> None:
        task = preparation.pop("single_team_fallback_task", None)
        if task is None or task.done():
            return
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task is not current:
            task.cancel()

    def _schedule_single_team_fallback(self, preparation: dict) -> None:
        task = preparation.get("single_team_fallback_task")
        if task is not None and not task.done():
            return
        started = float(
            preparation.setdefault(
                "search_started_monotonic",
                preparation.get("registered_monotonic", time.monotonic()),
            )
        )
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                self._wait_for_single_team_fallback(
                    preparation["session"].online_id,
                    int(preparation["prepare_id"]),
                    started,
                )
            )
        except RuntimeError:
            # Pure policy tests may call the coordinator without an event loop.
            return
        preparation["single_team_fallback_task"] = task

    async def _wait_for_single_team_fallback(
        self, online_id: str, prepare_id: int, search_started_monotonic: float
    ) -> None:
        try:
            delay = max(
                0.0,
                search_started_monotonic
                + self.single_team_fallback_seconds
                - time.monotonic(),
            )
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        preparation = self.preparations.get(online_id)
        if (
            preparation is None
            or preparation.get("prepare_id") != prepare_id
            or preparation.get("search_started_monotonic") != search_started_monotonic
            or int(preparation.get("match_id", 0)) != 0
        ):
            return
        self._purge_stale_matchmaking()
        preparation = self.preparations.get(online_id)
        if preparation is None or int(preparation.get("match_id", 0)) != 0:
            return
        if self.get_world_phase() != WORLD_PHASE_ACTIVE:
            self.preparations.pop(online_id, None)
            self.telemetry.emit(
                "game_active_preparation_invalidated",
                online_id=online_id,
                prepare_id=prepare_id,
                reason="World left the active-war phase before fallback release",
            )
            return
        self._match_preparation(preparation, allow_single_team_fallback=False)

    def _release_active_single_team(self, preparation: dict) -> bool:
        """Release one current active-war squad through a qualified one-team branch."""
        session = preparation["session"]
        criteria = preparation["criteria"]
        squad = preparation_squad(preparation)
        team_id = preparation.get("team_id")
        faction_id = preparation.get("faction_id")
        area_id = preparation.get("territory_area_id")
        special = criteria.get(3) == 2
        war = self.get_test_war()
        if war is not None:
            enemies = [
                a["area_id"]
                for a in war["areas"]
                if a["faction_id"] != faction_id
                and (special or opponent_faction_allowed(criteria, a["faction_id"]))
                and squad is not None
                and self._war_area_current_for_squad(a["area_id"], squad)
            ]
            if not enemies or (not special and criteria.get(8, 3) == 2):
                self.telemetry.emit(
                    "game_test_war_search_waiting",
                    online_id=session.online_id,
                    requested_opponent_faction=criteria.get(9, 0) if not special else None,
                    reason="No enemy area satisfies sortie conditions and current squad World data",
                )
                return False
            area_id = secrets.choice(enemies)
        elif not special and (
            type(criteria.get(9, 0)) is not int or criteria.get(9, 0) != 0
        ):
            # Without an authoritative ownership map, a home region cannot
            # prove the requested enemy faction. Leave this search queued.
            return False
        effective_area_id = (
            area_id
            if type(area_id) is int and 1 <= area_id <= 7
            else faction_id
            if special
            else 0
        )
        battle_type = (
            NORMAL_BATTLE_TYPE_SPECIAL_WEAPONS
            if criteria.get(3) == 2
            else NORMAL_BATTLE_TYPE_NORMAL_WEAPONS
        )
        match_id = self.next_match_id
        area_id, cq_map_locked, cq_map_mode, cq_map_profile = self._select_cq_combat_area(
            effective_area_id,
            match_id=match_id,
            battle_type=battle_type,
            kind=ACTIVE_SINGLE_TEAM,
        )
        if (
            preparation.get("world_phase") != WORLD_PHASE_ACTIVE
            or criteria.get(1) != self.season_id()
            or criteria.get(2) != WORLD_PHASE_ACTIVE
            or criteria.get(3) not in (1, 2)
            or squad is None
            or type(team_id) is not int
            or team_id <= 0
            or type(faction_id) is not int
            or faction_id not in (1, 2, 3)
            or type(area_id) is not int
            or not 1 <= area_id <= 7
            or not session.established
            or session.peer is None
        ):
            self.telemetry.emit(
                "game_single_team_fallback_unavailable",
                online_id=session.online_id,
                prepare_id=int(preparation.get("prepare_id", 0)),
                reason="Preparation lacks a current authenticated active-war area",
            )
            return False
        owner_online_id = ""
        owner_team_id = 0
        owner_player_id = 0
        payload = active_single_team_result(
            int(preparation["prepare_id"]),
            battle_type=battle_type,
            area_id=area_id,
            territory_layout_owner_id=owner_player_id,
        )
        special_consumption = None
        if special:
            special_consumption = self.consume_special_sortie(team_id)
            if special_consumption is None:
                self.telemetry.emit(
                    "game_special_sortie_release_rejected",
                    online_id=session.online_id,
                    prepare_id=int(preparation.get("prepare_id", 0)),
                    team_id=team_id,
                    reason="The Special Sortie gauge was no longer full",
                )
                return False
        self.next_match_id += 1
        match = {
            "match_id": match_id,
            "kind": ACTIVE_SINGLE_TEAM,
            "members": [preparation],
            "squad": dict(squad),
            "criteria": dict(criteria),
            "world_phase": WORLD_PHASE_ACTIVE,
            "battle_type": battle_type,
            "combat_area_id": area_id,
            "source_combat_area_id": effective_area_id,
            "cq_map_mode": cq_map_mode,
            "cq_map_locked": cq_map_locked,
            "cq_map_mission_id": int((cq_map_profile or {}).get("mission_id", 0)),
            "cq_map_name": str((cq_map_profile or {}).get("map_name", "")),
            "territory_layout_owner_id": owner_player_id,
            "offline_owner_online_id": owner_online_id,
            "offline_owner_team_id": owner_team_id,
            "special_progress_consumed": special_consumption,
            "created_monotonic": time.monotonic(),
            "state": "released",
        }
        match["war_context"] = self._war_context(area_id, faction_id)
        self.matches[match_id] = match
        preparation["match_id"] = match_id
        self._cancel_single_team_fallback(preparation)
        self._send_game_push(session, payload, subtype=2)
        self.telemetry.emit(
            (
                "game_special_world_boss_released"
                if criteria[3] == 2
                else "game_single_team_fallback_released"
            ),
            online_id=session.online_id,
            match_id=match_id,
            prepare_id=preparation["prepare_id"],
            battle_type=battle_type,
            source_combat_area_id=effective_area_id,
            combat_area_id=area_id,
            requested_opponent_faction=criteria.get(9, 0) if not special else None,
            defending_faction=(match.get("war_context") or {}).get("defender_faction"),
            cq_map_mode=cq_map_mode,
            cq_map_locked=cq_map_locked,
            cq_map_mission_id=int((cq_map_profile or {}).get("mission_id", 0)),
            cq_map_name=str((cq_map_profile or {}).get("map_name", "")),
            territory_layout_owner_id=owner_player_id,
            offline_owner_online_id=owner_online_id,
            squad_player_ids=sorted(squad),
            wait_seconds=self.single_team_fallback_seconds,
            payload=payload,
        )
        return True

    def observe_interwar_result(
        self, session: GameSession, payload: bytes, *, team_id: int
    ) -> dict | None:
        """Compatibility entry point restricted to an existing boss context."""
        return self.observe_combat_result(session, payload, team_id=team_id, kind=INTERWAR_BOSS)

    @staticmethod
    def match_lifetime(match: dict) -> float:
        if match.get("kind") in (INTERWAR_BOSS, ACTIVE_SINGLE_TEAM):
            return INTERWAR_CONTEXT_SECONDS
        return MATCH_ACTIVE_SECONDS if match.get("room_published") else MATCH_HANDOFF_SECONDS

    def observe_combat_result(
        self, session: GameSession, payload: bytes, *, team_id: int, kind: str | None = None
    ) -> dict | None:
        """Validate and retain one exact result context for atomic settlement."""
        if not session.established or session.peer is None:
            return False
        try:
            report = decode_combat_report(payload)
        except (ProtocolError, ValueError):
            return False
        if session.player_id not in report.player_ids:
            return False
        now = time.monotonic()

        def reporter_allowed(match: dict) -> bool:
            squad = match.get("squad")
            if not isinstance(squad, dict):
                return False
            frozen = squad.get(session.player_id)
            if not isinstance(frozen, tuple) or len(frozen) != 2 or frozen[1] != team_id:
                return False
            original = frozen[0]
            if original is session:
                return True
            # A new authenticated transport may read only a receipt already
            # committed for this frozen account/player/team. It must never
            # authorize a first settlement or rebind the match roster.
            return bool(
                match.get("reward_receipt_digest")
                and getattr(original, "online_id", None) == session.online_id
                and getattr(original, "player_id", None) == session.player_id
                and self.hires.is_current_member(session, session.player_id, team_id)
            )

        candidates = [
            m
            for m in self.matches.values()
            if (kind is None or m.get("kind") == kind)
            and (m.get("kind") in (INTERWAR_BOSS, ACTIVE_SINGLE_TEAM) or m.get("room_published"))
            and set(m.get("squad", {})) == report.player_ids
            and reporter_allowed(m)
            and set().union(
                *(
                    set(member.get("claimed_player_ids") or [member["session"].player_id])
                    for member in m["members"]
                )
            )
            == report.player_ids
            and m["members"][0]["prepare_id"] == report.prepare_id
            and now - m["created_monotonic"] <= self.match_lifetime(m)
        ]
        if len(candidates) != 1:
            return False
        match = candidates[0]
        prefix = "game_interwar" if match.get("kind") == INTERWAR_BOSS else "game_matched"
        try:
            reward_participants = world_reward_participants(report, match["members"])
        except ValueError as error:
            self.telemetry.emit(
                prefix + "_reward_context_rejected",
                online_id=session.online_id,
                match_id=match["match_id"],
                prepare_id=report.prepare_id,
                reason=str(error),
            )
            return False
        digest = report.digest
        previous = match.get("result_sha256")
        if previous is not None and previous != digest:
            self.telemetry.emit(
                prefix + "_result_conflict",
                online_id=session.online_id,
                match_id=match["match_id"],
                prepare_id=report.prepare_id,
            )
            return False
        # A report's native preparation ID can repeat after a server restart.
        # Freeze a new server-owned receipt domain only after this exact match
        # and its authenticated participants have passed validation.
        match.setdefault("reward_nonce", secrets.token_hex(16))
        match.update(
            state="result_received",
            result_sha256=digest,
            reward_participants=reward_participants,
            combat_report=report,
        )
        # Result receipt precedes the World-return signaling teardown.
        # The explicit disconnect report below invalidates readiness at that
        # boundary. A result alone must not force a live peer to register again.
        self.telemetry.emit(
            prefix + "_result_observed",
            online_id=session.online_id,
            match_id=match["match_id"],
            prepare_id=report.prepare_id,
            duplicate=previous == digest,
            settlement="validated",
            reward_roles=[
                {
                    "player_id": participant.player_id,
                    "role": participant.role,
                    "combat_side": participant.combat_side,
                    "social_team_id": participant.social_team_id,
                    "settlement_team_id": participant.settlement_team_id,
                    "pay_policy": (
                        participant.pay_policy.value
                        if participant.pay_policy is not None
                        else "mercenary_rank_rate"
                    ),
                    "won": participant.won,
                }
                for participant in reward_participants
            ],
        )
        owner = match["members"][0]["session"].online_id
        receipts = sorted(
            (
                m
                for m in self.matches.values()
                if m.get("result_sha256") and m["members"][0]["session"].online_id == owner
            ),
            key=lambda m: m["match_id"],
        )
        for old in receipts[:-INTERWAR_RECEIPTS_PER_PLAYER]:
            self.matches.pop(old["match_id"], None)
        return match

    # LIFECYCLE: expire or remove transient matches without changing durable teams.
    def remove_reported_battle(
        self, session: GameSession, report: BattleDisconnectReport
    ) -> tuple[int, list[str]]:
        """Remove one owned battle by its host key, including reports from crew.

        A lost peer need not remain live. The reporter must be the current
        authenticated transport frozen in the roster. A stale report cannot
        fall back to removing that account's newer preparation or search.
        """
        team_id = self.hires.get_team_id(session.online_id)
        if report.host_prepare_id == 0 or not self.hires.is_current_member(
            session, session.player_id, team_id
        ):
            return 0, []
        now = time.monotonic()
        candidates = []
        for match in self.matches.values():
            members = match.get("members", [])
            if (
                not members
                or members[0].get("prepare_id") != report.host_prepare_id
                or match.get("result_sha256")
                or match.get("preflight_retry_pending")
                or now - match["created_monotonic"] > self.match_lifetime(match)
                or ("combat_area_id" not in match and match.get("kind") != INTERWAR_BOSS)
            ):
                continue
            squad = match.get("squad", {})
            own = squad.get(session.player_id)
            if own is None or own[0] is not session or own[1] != team_id:
                continue
            # Native failures can report zero. It identifies no additional
            # player; the exact host key and reporting participant still bind
            # the whole removal. A nonzero target must belong to the roster.
            if report.player_id and report.player_id not in squad:
                continue
            if any(self.preparations.get(m["session"].online_id) is not m for m in members):
                continue
            candidates.append(match)
        if len(candidates) != 1:
            return 0, []
        match_id = int(candidates[0]["match_id"])
        return match_id, self._remove_matchmaking_state(match_id)

    def _remove_matchmaking_member(self, online_id: str, *, reason: str) -> list[int]:
        """Remove open work, but retain a finished result for surviving peers.

        The first accepted World report settles every frozen participant. A
        disconnect immediately after that report must not erase the remaining
        peers' authenticated reply context. The normal match lifetime and
        bounded receipt pruning still retire it later.
        """
        self.hires.discard(online_id)
        preparation = self.preparations.pop(online_id, None)
        if preparation is not None:
            self._cancel_single_team_fallback(preparation)
        removed_match_ids: list[int] = []
        for match_id, match in list(self.matches.items()):
            if not any(
                member["session"].online_id == online_id for member in match.get("members", [])
            ) and not any(s.online_id == online_id for s, _ in match.get("squad", {}).values()):
                continue
            self._cancel_match_room_log_watch(match)
            self._retire_terminal_overlays(match)
            if match.get("result_sha256") and match.get("reward_receipt_digest"):
                self.telemetry.emit(
                    "game_match_result_context_retained",
                    online_id=online_id,
                    match_id=int(match_id),
                    reason=reason,
                )
            else:
                self.matches.pop(match_id, None)
                removed_match_ids.append(int(match_id))
            for participant, _ in match.get("squad", {}).values():
                self._clear_signaling_ready_for(participant.online_id)
            for member in match.get("members", []):
                member_online_id = member["session"].online_id
                current = self.preparations.get(member_online_id)
                if (
                    member_online_id != online_id
                    and current is not None
                    and int(current.get("match_id", 0)) == int(match_id)
                ):
                    self.preparations.pop(member_online_id, None)
                member["match_id"] = 0
        if preparation is not None or removed_match_ids:
            self.telemetry.emit(
                "game_matchmaking_state_removed",
                online_id=online_id,
                reason=reason,
                prepare_id=int((preparation or {}).get("prepare_id", 0)),
                removed_match_ids=removed_match_ids,
            )
        return removed_match_ids

    def _purge_stale_matchmaking(self) -> None:
        """Expire disconnected searches and bounded central-match handoffs."""
        self.hires.purge()
        now = time.monotonic()

        def expired(preparation):
            match = self.matches.get(int(preparation.get("match_id", 0)))
            squad = preparation.get("squad", {})
            if any(
                not self.hires.is_current_member(peer, player_id, team_id)
                for player_id, (peer, team_id) in squad.items()
            ):
                return True
            if match and match.get("kind") == INTERWAR_BOSS:
                return now - match["created_monotonic"] > INTERWAR_CONTEXT_SECONDS or any(
                    not self.hires.is_current_member(peer, player_id, team_id)
                    for player_id, (peer, team_id) in match.get("squad", {}).items()
                )
            if match:
                return now - match["created_monotonic"] > self.match_lifetime(match)
            # Search has no room to hand off. Keep it while its authenticated
            # clients are live; cancel/disconnect/freshness checks remove it.
            # Dropping a live search at 180 s leaves retail waiting forever.
            return False

        stale_online_ids = [
            online_id
            for online_id, preparation in self.preparations.items()
            if expired(preparation)
            or not preparation["session"].established
            or preparation["session"].peer is None
            or now - float(preparation["session"].last_seen_monotonic) > 90.0
        ]
        for online_id in stale_online_ids:
            self._remove_matchmaking_member(online_id, reason="stale")
        for match_id, match in list(self.matches.items()):
            lifetime = self.match_lifetime(match)
            if now - float(match.get("created_monotonic", now)) <= lifetime:
                continue
            self._cancel_match_room_log_watch(match)
            self._retire_terminal_overlays(match)
            self.matches.pop(match_id, None)
            for member in match.get("members", []):
                member_online_id = member["session"].online_id
                current = self.preparations.get(member_online_id)
                if current is not None and int(current.get("match_id", 0)) == int(match_id):
                    self.preparations.pop(member_online_id, None)
                member["match_id"] = 0
            self.telemetry.emit(
                "game_matchmaking_state_removed",
                online_id="",
                reason="match_expired",
                prepare_id=0,
                removed_match_ids=[int(match_id)],
            )

    def _match_team_session_membership(self, preparation: dict) -> tuple[int, dict, dict] | None:
        """Resolve one preparation's live local session and NP identity."""
        team_id = int(preparation.get("team_id", 0))
        team_session = self.team_sessions.get(team_id)
        if team_session is None:
            return None
        members = team_session.get("members", {})
        if not isinstance(members, dict):
            return None
        online_id = preparation["session"].online_id
        member = members.get(online_id)
        if not isinstance(member, dict) or len(bytes(member.get("np_identity", b""))) != 37:
            return None
        return int(team_session["session_id"]), team_session, member

    def _clear_signaling_ready_for(self, online_id: str) -> None:
        self.signaling_ready_pairs = {
            pair for pair in self.signaling_ready_pairs if online_id not in pair
        }

    def _remove_matchmaking_state(self, match_id: int) -> list[str]:
        """Discard one finished battle handoff without touching durable data."""
        match = self.matches.pop(int(match_id), None) if match_id > 0 else None
        if match is None:
            return []
        self._cancel_match_room_log_watch(match)
        self._retire_terminal_overlays(match)
        removed: list[str] = []
        for member in match.get("members", []):
            online_id = member["session"].online_id
            current = self.preparations.get(online_id)
            if current is not None and int(current.get("match_id", 0)) == int(match_id):
                self.preparations.pop(online_id, None)
            self._clear_signaling_ready_for(online_id)
            if online_id not in removed:
                removed.append(online_id)
        for participant, _ in match.get("squad", {}).values():
            self._clear_signaling_ready_for(participant.online_id)
            if participant.online_id not in removed:
                removed.append(participant.online_id)
        return removed

    def _requeue_unpublished_pair(self, match: dict, *, trigger_online_id: str) -> None:
        """Release a failed pre-room handoff while preserving both live searches."""
        match_id = int(match["match_id"])
        if (
            self.matches.get(match_id) is not match
            or match.get("room_published")
            or match.get("counterparts_released")
            or match.get("result_sha256")
        ):
            raise ValueError("Only an unpublished active pair can be requeued")
        self.matches.pop(match_id)
        self._cancel_match_room_log_watch(match)
        online_ids = []
        for member in match.get("members", []):
            online_id = member["session"].online_id
            current = self.preparations.get(online_id)
            if (
                current is not None
                and current.get("session") is member.get("session")
                and int(current.get("match_id", 0)) == match_id
            ):
                current["match_id"] = 0
            member["match_id"] = 0
            online_ids.append(online_id)
        self.telemetry.emit(
            "game_unpublished_match_requeued",
            match_id=match_id,
            trigger_online_id=trigger_online_id,
            online_ids=online_ids,
            handoff_seconds=round(
                max(0.0, time.monotonic() - float(match["created_monotonic"])), 3
            ),
            reason="Matched host registered again after cancelling before room publication",
        )

    def _retire_terminal_overlays(self, match: dict) -> None:
        """Send departures for recorded foreign peers before discarding them.

        Use only at terminal removal, not during the retained preflight retry
        or on the first combat result. A leave push names a global player ID.
        Protect replaced transports, real members, reused slots and newer
        matches. This alone does not prove that the displayed row is removed.
        """
        overlays = match.get("signaling_session_overlays", [])
        if not overlays:
            return
        retired = 0
        for overlay in overlays:
            if overlay.get("retired"):
                continue
            candidates = [
                (peer, team_id)
                for peer, team_id in match.get("squad", {}).values()
                if peer.online_id == overlay.get("recipient_online_id")
            ]
            if len(candidates) != 1:
                continue
            recipient, team_id = candidates[0]
            if not self.hires.is_current_member(recipient, recipient.player_id, team_id):
                continue
            membership = self._match_team_session_membership(
                {"session": recipient, "team_id": team_id}
            )
            ordinal = overlay.get("announced_member_id")
            player_id = overlay.get("announced_player_id")
            if (
                membership is None
                or membership[0] != overlay.get("shared_session_id")
                or type(ordinal) is not int
                or not 0 < ordinal < 1 << 32
                or type(player_id) is not int
                or not 0 < player_id < 1 << 32
                or player_id == recipient.player_id
                or any(r.get("player_id") == player_id for r in membership[1]["members"].values())
                or any(r.get("member_id") == ordinal for r in membership[1]["members"].values())
            ):
                continue
            protected = False
            for other in self.matches.values():
                if other is match:
                    continue
                if not other.get("result_sha256") and (
                    any(peer is recipient for peer, _ in other.get("squad", {}).values())
                    or any(m["session"] is recipient for m in other.get("members", []))
                ):
                    protected = True
                    break
                if any(
                    not o.get("retired")
                    and o.get("recipient_online_id") == recipient.online_id
                    and o.get("shared_session_id") == membership[0]
                    and (
                        o.get("announced_member_id") == ordinal
                        or o.get("announced_player_id") == player_id
                    )
                    for o in other.get("signaling_session_overlays", [])
                ):
                    protected = True
                    break
            if protected:
                continue
            self._send_game_push(
                recipient,
                _team_session_leave_push(membership[0], player_id),
                subtype=SESSION_MEMBER_LEAVE_PUSH,
            )
            overlay["retired"] = True
            retired += 1
            self._clear_signaling_ready_for(recipient.online_id)
        self.telemetry.emit(
            "game_match_terminal_overlays_retired",
            match_id=match["match_id"],
            retired_count=retired,
            skipped_count=sum(not o.get("retired", False) for o in overlays),
        )

    def _squad_identities_available(self, match: dict) -> bool:
        """Check the frozen human roster without changing team membership."""
        pair = match.get("members", [])
        if len(pair) != 2:
            return False
        squad = {}
        for member in pair:
            resolved = preparation_squad(member)
            if not resolved or squad.keys() & resolved.keys():
                return False
            squad.update(resolved)
        if not 2 <= len(squad) <= MAX_MATCH_CLIENTS:
            return False
        if len({peer.online_id for peer, _ in squad.values()}) != len(squad):
            return False
        for player_id, (peer, team_id) in squad.items():
            membership = self._match_team_session_membership(
                {"session": peer, "team_id": team_id}
            )
            if (
                not self.hires.is_current_member(peer, player_id, team_id)
                or membership is None
                or membership[2].get("player_id") != player_id
            ):
                return False
        return True

    # PEER READINESS: signaling identity is separate from RPCN room membership.
    def _announce_cross_session_match_peers(self, match: dict) -> list[str]:
        """Replay historical overlay state for diagnostic and cleanup controls.

        Normal matching must not call this method. Synthetic team joins reset
        Compose and leave ghost display rows. The native room relay supplies
        battle peers; see 2026-09-20-normal-without-team-overlays.json.

        Same-session peers already get identities from the team directory.
        Each recipient's team session owns its own foreign-member ordinals.
        Never use another local member's slot. This does not
        merge durable teams or give room-creation authority to squad members.
        More than two native humans still requires live qualification.
        """
        if match.get("signaling_peers_announced") or len(match.get("members", [])) < 2:
            return []
        pair = list(match["members"][:2])
        squad = {}
        for member in pair:
            resolved = preparation_squad(member)
            if not resolved or squad.keys() & resolved.keys():
                return []
            squad.update(resolved)
        if not 2 <= len(squad) <= MAX_MATCH_CLIENTS:
            return []
        host_session = pair[0]["session"]
        participants = [{"session": host_session, "team_id": pair[0]["team_id"]}]
        participants.extend(
            {"session": actor, "team_id": team_id}
            for actor, team_id in squad.values()
            if actor is not host_session
        )
        pair_key = frozenset(member["session"].online_id for member in participants)
        if len(pair_key) != len(participants):
            return []
        memberships = [self._match_team_session_membership(member) for member in participants]
        if any(
            membership is None
            or not member["session"].established
            or member["session"].peer is None
            or membership[2].get("player_id") != member["session"].player_id
            for member, membership in zip(participants, memberships)
        ):
            self.telemetry.emit(
                "game_match_signaling_peers_skipped",
                match_id=int(match["match_id"]),
                online_ids=sorted(pair_key),
                reason="missing live 37-byte team-session NP identity",
            )
            return []
        if pair_key in self.signaling_ready_pairs:
            match["signaling_peers_announced"] = True
            self.telemetry.emit(
                "game_match_signaling_preflight_reused",
                match_id=int(match["match_id"]),
                online_ids=sorted(pair_key),
            )
            return []

        recipients: list[str] = []
        overlays: list[dict[str, int | str]] = []
        pending = []
        for index, recipient in enumerate(participants):
            recipient_session = recipient["session"]
            pending_before = len(pending)
            recipient_session_id = int(memberships[index][0])
            occupied = [
                record.get("member_id") for record in memberships[index][1]["members"].values()
            ]
            if any(type(value) is not int or value <= 0 for value in occupied):
                return []
            next_foreign_id = max(occupied, default=0) + 1
            for other, counterpart in enumerate(participants):
                if memberships[index][0] == memberships[other][0]:
                    continue
                counterpart_session = counterpart["session"]
                announced_member_id = next_foreign_id
                next_foreign_id += 1
                payload = _team_session_member_push(
                    recipient_session_id,
                    counterpart_session.player_id,
                    bytes(memberships[other][2]["np_identity"]),
                    counterpart_session.online_id,
                )
                pending.append((recipient_session, payload))
                overlays.append(
                    {
                        "recipient_online_id": recipient_session.online_id,
                        "shared_session_id": recipient_session_id,
                        "announced_online_id": counterpart_session.online_id,
                        "announced_player_id": counterpart_session.player_id,
                        "announced_member_id": announced_member_id,
                    }
                )
            if len(pending) > pending_before:
                recipients.append(recipient_session.online_id)

        # Prepare the whole exchange before sending any part of it.
        if len(recipients) == len(participants):
            for recipient_session, payload in pending:
                self._send_game_push(recipient_session, payload, subtype=SESSION_MEMBER_JOIN_PUSH)
            match["signaling_peers_announced"] = True
            match["signaling_session_overlays"] = overlays
            self.signaling_ready_pairs.add(pair_key)
            self.telemetry.emit(
                "game_match_signaling_peers_announced",
                match_id=int(match["match_id"]),
                recipient_online_ids=recipients,
                overlays=overlays,
                subtype=9,
                announced_identity_length=37,
            )
        return recipients

    def retire_reported_overlay(
        self, session: GameSession, session_id: int, player_id: int
    ) -> bool:
        """Remove only the caller's recorded foreign shadow, never a real member.

        A disconnect report has no match ID. Refuse ambiguous old/new overlays,
        replaced sessions and reused local slots rather than remove the wrong AC.
        """
        if (
            not session.established
            or session.peer is None
            or type(session_id) is not int
            or not 0 < session_id < 1 << 64
            or type(player_id) is not int
            or not 0 < player_id < 1 << 32
            or player_id == session.player_id
        ):
            return False
        current_team_id = self.hires.get_team_id(session.online_id)
        if not self.hires.is_current_member(session, session.player_id, current_team_id):
            return False
        membership = self._match_team_session_membership(
            {"session": session, "team_id": current_team_id}
        )
        if membership is None or membership[0] != session_id:
            return False
        candidates = []
        now = time.monotonic()
        for match in self.matches.values():
            if now - match["created_monotonic"] > self.match_lifetime(match):
                continue
            own = match.get("squad", {}).get(session.player_id)
            if own is None or own[0] is not session or own[1] != current_team_id:
                continue
            # The report contains no match generation. A new match can reuse
            # either the peer or its temporary slot, including a cached
            # preflight with no new overlay records. Do not remove any old
            # shadow or clear new readiness while this caller has live combat.
            if not match.get("result_sha256"):
                return False
            for overlay in match.get("signaling_session_overlays", []):
                if (
                    overlay.get("recipient_online_id") == session.online_id
                    and overlay.get("announced_player_id") == player_id
                    and overlay.get("shared_session_id") == session_id
                    and not overlay.get("retired")
                ):
                    candidates.append((match, overlay))
        if len(candidates) != 1:
            return False
        match, overlay = candidates[0]
        ordinal = overlay.get("announced_member_id")
        if type(ordinal) is not int or ordinal <= 0:
            return False
        if any(record.get("member_id") == ordinal for record in membership[1]["members"].values()):
            return False
        if any(record.get("player_id") == player_id for record in membership[1]["members"].values()):
            return False
        self._send_game_push(
            session,
            _team_session_leave_push(session_id, player_id),
            subtype=SESSION_MEMBER_LEAVE_PUSH,
        )
        overlay["retired"] = True
        self._clear_signaling_ready_for(session.online_id)
        self.telemetry.emit(
            "game_match_foreign_overlay_retired",
            match_id=match["match_id"],
            online_id=session.online_id,
            session_id=session_id,
            reported_player_id=player_id,
            member_id=ordinal,
        )
        return True

    # PAIR SELECTION: freeze compatible sides/type before releasing participants.
    def _match_preparation(
        self, preparation: dict, *, allow_single_team_fallback: bool = True
    ) -> None:
        if preparation.get("interwar_boss"):
            self._release_interwar_boss(preparation)
            return
        if (
            int(preparation.get("match_id", 0)) == 0
            and self.get_world_phase() != WORLD_PHASE_ACTIVE
        ):
            self._cancel_single_team_fallback(preparation)
            online_id = preparation["session"].online_id
            if self.preparations.get(online_id) is preparation:
                self.preparations.pop(online_id, None)
            self.telemetry.emit(
                "game_active_preparation_invalidated",
                online_id=online_id,
                prepare_id=int(preparation.get("prepare_id", 0)),
                reason="World left the active-war phase before match release",
            )
            return
        match = None
        if int(preparation.get("match_id", 0)) > 0:
            match = self.matches.get(int(preparation["match_id"]))
        if match is not None:
            if match.get("kind") == ACTIVE_SINGLE_TEAM:
                original = match.get("members", [None])[0]
                if (
                    original is None
                    or match.get("result_sha256")
                    or original["session"] is not preparation["session"]
                    or original.get("team_id") != preparation.get("team_id")
                    or original.get("faction_id") != preparation.get("faction_id")
                    or original.get("criteria") != preparation.get("criteria")
                    or not same_squad(
                        preparation_squad(original), preparation_squad(preparation)
                    )
                ):
                    self.telemetry.emit(
                        "game_match_retry_rejected",
                        online_id=preparation["session"].online_id,
                        match_id=match["match_id"],
                        prepare_id=preparation["prepare_id"],
                        reason="Preparation no longer matches the frozen single-team contract",
                    )
                    return
                match["members"] = [preparation]
                match["squad"] = dict(preparation_squad(preparation))
                self.telemetry.emit(
                    "game_single_team_preparation_reused",
                    online_id=preparation["session"].online_id,
                    match_id=match["match_id"],
                    prepare_id=preparation["prepare_id"],
                )
                return
            # A formed pair is closed to other squads. Only its original live
            # sessions can repeat an unchanged preparation request.
            member_index = next(
                (
                    index
                    for index, member in enumerate(match["members"])
                    if member["session"] is preparation["session"]
                    and member.get("team_id") == preparation.get("team_id")
                    and member.get("faction_id") == preparation.get("faction_id")
                ),
                None,
            )
            if (
                match.get("kind") == INTERWAR_BOSS
                or len(match["members"]) != 2
                or member_index is None
            ):
                self.telemetry.emit(
                    "game_match_retry_rejected",
                    online_id=preparation["session"].online_id,
                    match_id=match["match_id"],
                    prepare_id=preparation["prepare_id"],
                    reason="Preparation does not belong to the original paired sessions",
                )
                return
            candidate_pair = list(match["members"])
            candidate_pair[member_index] = preparation
            roles = normal_pair_roles(*candidate_pair, season_id=self.season_id())
            if (
                (
                    match.get("host_result_sent")
                    and preparation["prepare_id"] != match["members"][member_index]["prepare_id"]
                )
                or preparation["criteria"].get(3) != match["members"][member_index]["criteria"].get(3)
                or roles is None
                or any(a is not b for a, b in zip(roles, candidate_pair))
                or any(
                    self.preparations.get(m["session"].online_id) is not m for m in candidate_pair
                )
                or not same_squad(
                    preparation_squad(preparation),
                    preparation_squad(match["members"][member_index]),
                )
            ):
                self.telemetry.emit(
                    "game_match_retry_rejected",
                    online_id=preparation["session"].online_id,
                    match_id=match["match_id"],
                    prepare_id=preparation["prepare_id"],
                    reason="Preparation no longer matches the frozen pair contract",
                )
                return
            if (
                member_index == 0
                and match.get("host_transition_cancelled")
                and match.get("host_result_sent")
                and not match.get("room_published")
                and not match.get("counterparts_released")
                and not match.get("result_sha256")
            ):
                self._requeue_unpublished_pair(
                    match,
                    trigger_online_id=preparation["session"].online_id,
                )
                preparation["match_id"] = 0
                match = None
        if match is None:
            preparation["match_id"] = 0
            # Active-war Special Sorties are one-team World boss missions.
            # They never enter the Normal attacker/defender pairing queue and
            # never use an offline-territory burn-run fallback.
            if preparation["criteria"].get(3) == 2:
                self._release_active_single_team(preparation)
                return
            pair = None
            for candidate in self.preparations.values():
                if candidate is preparation or int(candidate.get("match_id", 0)) != 0:
                    continue
                pair = normal_pair_roles(candidate, preparation, season_id=self.season_id())
                if pair is not None:
                    participants = set(preparation_squad(pair[0])) | set(preparation_squad(pair[1]))
                    if any(
                        not current.get("result_sha256")
                        and participants.intersection(current.get("squad", {}))
                        for current in self.matches.values()
                    ):
                        pair = None
                        continue
                    break
            if pair is None:
                if not allow_single_team_fallback:
                    self._release_active_single_team(preparation)
                    return
                self._schedule_single_team_fallback(preparation)
                self.telemetry.emit(
                    "game_match_waiting",
                    online_id=preparation["session"].online_id,
                    prepare_id=preparation["prepare_id"],
                    team_id=int(preparation.get("team_id", 0)),
                    claimed_player_ids=list(preparation.get("claimed_player_ids", [])),
                    criteria=dict(preparation["criteria"]),
                )
                return
            match_id = self.next_match_id
            match = {
                "match_id": match_id,
                "members": list(pair),
                "squad": {
                    player_id: value
                    for member in pair
                    for player_id, value in preparation_squad(member).items()
                },
                "criteria": dict(pair[0]["criteria"]),
                "world_phase": pair[0].get("world_phase", WORLD_PHASE_ACTIVE),
                "is_intra_faction": intra_faction_pair(*pair),
                "battle_type": (
                    NORMAL_BATTLE_TYPE_STORED_TERRITORY
                    if pair[0]["criteria"][3] == 2
                    else NORMAL_BATTLE_TYPE_EXTERMINATION
                ),
                "created_monotonic": time.monotonic(),
            }
            source_area_id = int(pair[1].get("territory_area_id", 0))
            war = self.get_test_war()
            if war is not None:
                defending_areas = [
                    a["area_id"]
                    for a in war["areas"]
                    if a["faction_id"] == pair[1]["faction_id"]
                    and self._war_area_current_for_squad(a["area_id"], match["squad"])
                ]
                if not defending_areas:
                    self.telemetry.emit(
                        "game_test_war_search_waiting",
                        online_id=preparation["session"].online_id,
                        reason="Squad needs current World area data before a safe match assignment",
                    )
                    return
                if source_area_id not in defending_areas:
                    source_area_id = secrets.choice(defending_areas)
            # Only retire the searches once a safe assignment exists. A
            # rejected stale World view must not cancel the other captain's
            # pending fallback or consume an unallocated match identity.
            for member in pair:
                self._cancel_single_team_fallback(member)
            self.next_match_id += 1
            (
                selected_area_id,
                cq_map_locked,
                cq_map_mode,
                cq_map_profile,
            ) = self._select_cq_combat_area(
                source_area_id,
                match_id=match_id,
                battle_type=int(match["battle_type"]),
                kind=None,
            )
            match.update(
                source_combat_area_id=source_area_id,
                combat_area_id=selected_area_id,
                cq_map_mode=cq_map_mode,
                cq_map_locked=cq_map_locked,
                cq_map_mission_id=int((cq_map_profile or {}).get("mission_id", 0)),
                cq_map_name=str((cq_map_profile or {}).get("map_name", "")),
            )
            match["war_context"] = self._war_context(selected_area_id, pair[0]["faction_id"])
            self.matches[match_id] = match
            for member in pair:
                member["match_id"] = match_id
        else:
            match["members"][member_index] = preparation
            preparation["match_id"] = match["match_id"]

        # RPCN room membership supplies battle peers. Synthetic team joins
        # reset Compose and leave foreign rows in the World display. Two
        # native consecutive matches passed without that exchange or retries.
        # Validate larger squads without sending team-membership messages.
        host = match["members"][0]
        counterpart = match["members"][1]
        host_session = host["session"]
        if len(match.get("squad", {})) > 2 and not self._squad_identities_available(match):
            self.telemetry.emit(
                "game_match_squad_identity_waiting",
                match_id=int(match["match_id"]),
                reason="The complete authenticated squad identity set is not available",
            )
            return
        if match.get("host_result_sent"):
            return
        if "combat_area_id" not in match:
            # The current role builder assigns Stronghold to the counterpart.
            # Freeze its registered team region before either peer is released.
            match["combat_area_id"] = int(counterpart.get("territory_area_id", 0))
        if "source_combat_area_id" not in match:
            source_area_id = int(match["combat_area_id"])
            (
                selected_area_id,
                cq_map_locked,
                cq_map_mode,
                cq_map_profile,
            ) = self._select_cq_combat_area(
                source_area_id,
                match_id=int(match["match_id"]),
                battle_type=int(
                    match.get("battle_type", NORMAL_BATTLE_TYPE_EXTERMINATION)
                ),
                kind=match.get("kind"),
            )
            match["source_combat_area_id"] = source_area_id
            match["combat_area_id"] = selected_area_id
            match["cq_map_mode"] = cq_map_mode
            match["cq_map_locked"] = cq_map_locked
            match["cq_map_mission_id"] = int(
                (cq_map_profile or {}).get("mission_id", 0)
            )
            match["cq_map_name"] = str((cq_map_profile or {}).get("map_name", ""))
        if "territory_layout_owner_id" not in match:
            # Keep the same authenticated Stronghold layout owner for all peers.
            # Missing uploads remain unassigned, not another player's territory.
            match["territory_layout_owner_id"] = int(
                counterpart.get("territory_layout_owner_id", 0)
            )
        guest_prereleased = False
        if host_session.established and host_session.peer is not None:
            payload = _matching_result_for_members(
                host,
                counterpart,
                criteria=dict(match["criteria"]),
                is_host=True,
                combat_area_id=int(match["combat_area_id"]),
                territory_layout_owner_id=int(match["territory_layout_owner_id"]),
                is_intra_faction=match.get("is_intra_faction", True),
                battle_type=match.get("battle_type", NORMAL_BATTLE_TYPE_EXTERMINATION),
            )
            self._send_game_push(host_session, payload, subtype=2)
            match["host_result_sent"] = True
            if self.match_room_log_paths:
                self._cancel_match_room_log_watch(match)
                offsets = {
                    str(path): path.stat().st_size if path.exists() else 0
                    for path in self.match_room_log_paths
                }
                match["room_log_bridge_task"] = asyncio.create_task(
                    self._watch_match_room_logs(
                        int(match["match_id"]), host_session.online_id, offsets
                    )
                )
        self.telemetry.emit(
            "game_match_result_published",
            match_id=match["match_id"],
            online_ids=[member["session"].online_id for member in match["members"]],
            prepare_ids=[member["prepare_id"] for member in match["members"]],
            team_ids=[int(member.get("team_id", 0)) for member in match["members"]],
            same_nonzero_team=(
                len({int(member.get("team_id", 0)) for member in match["members"]}) == 1
                and int(match["members"][0].get("team_id", 0)) > 0
            ),
            criteria=dict(match["criteria"]),
            host_online_id=host_session.online_id,
            guest_prereleased=guest_prereleased,
            source_combat_area_id=int(match["source_combat_area_id"]),
            combat_area_id=int(match["combat_area_id"]),
            cq_map_mode=match.get("cq_map_mode", "territory"),
            cq_map_locked=bool(match.get("cq_map_locked", False)),
            cq_map_mission_id=int(match.get("cq_map_mission_id", 0)),
            cq_map_name=str(match.get("cq_map_name", "")),
            territory_layout_owner_id=int(match["territory_layout_owner_id"]),
            is_intra_faction=match.get("is_intra_faction", True),
            battle_type=match.get("battle_type", NORMAL_BATTLE_TYPE_EXTERMINATION),
            faction_ids=[int(member.get("faction_id", 0)) for member in match["members"]],
            requested_opponent_factions=[member["criteria"].get(9, 0) for member in match["members"]],
            defending_faction=(match.get("war_context") or {}).get("defender_faction"),
        )

    def _cancel_match_room_log_watch(self, match: dict) -> None:
        task = match.pop("room_log_bridge_task", None)
        if task is None or task.done():
            return
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task is not current:
            task.cancel()

    # NATIVE HANDOFF: validate the host's archive and relay its exact bytes.
    def publish_native_room(
        self,
        session: GameSession,
        *,
        prepare_id: int,
        encoded: bytes,
        reply: Callable[[bytes], None],
    ) -> None:
        """Accept only the live matched host; relay its archive once per match."""
        preparation = self.preparations.get(session.online_id)
        match = self.matches.get(int(preparation.get("match_id", 0))) if preparation else None
        if (
            not session.established
            or session.peer is None
            or preparation is None
            or preparation.get("session") is not session
            or match is None
            or not match.get("members")
            or match["members"][0] is not preparation
            or type(prepare_id) is not int
            or prepare_id <= 0
            or preparation.get("prepare_id") != prepare_id
            or "combat_area_id" not in match
            or match.get("preflight_retry_pending")
            or time.monotonic() - match["created_monotonic"] > self.match_lifetime(match)
        ):
            raise ValueError("Room notification does not belong to the active matched host")
        if any(
            not self.hires.is_current_member(peer, player_id, team_id)
            for player_id, (peer, team_id) in match.get("squad", {}).items()
        ) or (len(match.get("squad", {})) > 2 and not self._squad_identities_available(match)):
            raise ValueError("The frozen room squad is no longer current")
        validate_room_archive(encoded)
        previous = match.get("native_room_archive")
        if previous is not None:
            if previous != encoded:
                raise ValueError("Room notification conflicts with the accepted archive")
            reply(b"")
            return
        if match.get("counterparts_released"):
            raise ValueError("Room state was already published by another source")
        pending = []
        for member in match["members"][1:]:
            recipient = member["session"]
            if (
                not recipient.established
                or recipient.peer is None
                or self.preparations.get(recipient.online_id) is not member
            ):
                raise ValueError("The matched room recipient is no longer active")
            pending.append(
                (
                    recipient,
                    _matching_result_for_members(
                        member,
                        preparation,
                        criteria=dict(match["criteria"]),
                        is_host=False,
                        combat_area_id=int(match["combat_area_id"]),
                        territory_layout_owner_id=int(match.get("territory_layout_owner_id", 0)),
                        is_intra_faction=match.get("is_intra_faction", True),
                        battle_type=match.get("battle_type", NORMAL_BATTLE_TYPE_EXTERMINATION),
                    ),
                    native_room_info_push(int(member["prepare_id"]), prepare_id, encoded),
                )
            )
        if not pending:
            raise ValueError("Room notification has no matched recipient")
        # Validate all payloads before ACK or publication. Reliable transport
        # owns delivery retries; an exact request retry must not enqueue twice.
        reply(b"")
        match["native_room_archive"] = encoded
        match["counterparts_released"] = True
        match["room_published"] = True
        self._cancel_match_room_log_watch(match)
        for recipient, result, room in pending:
            self._send_game_push(recipient, result, subtype=2)
            self._send_game_push(recipient, room, subtype=5)
        self.telemetry.emit(
            "game_native_match_room_published",
            match_id=match["match_id"],
            host_online_id=session.online_id,
            host_prepare_id=prepare_id,
            recipient_online_ids=[recipient.online_id for recipient, _, _ in pending],
            encoded_archive_length=len(encoded),
            handoff_seconds=round(
                max(0.0, time.monotonic() - float(match["created_monotonic"])), 3
            ),
        )

    # TERRITORY ACCESS: bind opponent reads to the actual matched owner and region.
    def matched_territory_owner(
        self,
        session: GameSession,
        *,
        player_id: int,
        area_id: int,
        team_id: int,
    ) -> tuple[str, int] | None:
        """Return the exact live Stronghold owner allowed by this released match."""
        match = self._active_squad_match(session)
        if (
            match is None
            or match["squad"][session.player_id][1] != team_id
            or match.get("territory_layout_owner_id") != player_id
            or player_id <= 0
            or match.get("combat_area_id") != area_id
        ):
            return None
        if (
            match.get("kind") == ACTIVE_SINGLE_TEAM
            and match.get("battle_type") == NORMAL_BATTLE_TYPE_OFFLINE_TERRITORY
        ):
            owner_online_id = match.get("offline_owner_online_id")
            owner_team_id = match.get("offline_owner_team_id")
            if (
                isinstance(owner_online_id, str)
                and owner_online_id
                and type(owner_team_id) is int
                and owner_team_id > 0
                and owner_team_id != team_id
            ):
                return owner_online_id, owner_team_id
            return None
        members = match.get("members", [])
        if len(members) != 2:
            return None
        # The current role builder assigns Stronghold to the second member.
        owner = members[1]
        owner_session = owner["session"]
        if (
            owner_session.player_id != player_id
            or not owner_session.established
            or owner_session.peer is None
            or self.preparations.get(owner_session.online_id) is not owner
        ):
            return None
        return owner_session.online_id, int(owner["team_id"])

    async def _watch_match_room_logs(
        self, match_id: int, host_online_id: str, offsets: dict[str, int]
    ) -> None:
        buffers = {raw_path: "" for raw_path in offsets}
        deadline = time.monotonic() + 50.0
        while time.monotonic() < deadline:
            match = self.matches.get(int(match_id))
            if match is None or match.get("counterparts_released"):
                return
            for raw_path in offsets:
                path = Path(raw_path)
                try:
                    size = path.stat().st_size
                    if size < offsets[raw_path]:
                        offsets[raw_path] = 0
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        handle.seek(offsets[raw_path])
                        chunk = handle.read()
                        offsets[raw_path] = handle.tell()
                except OSError:
                    continue
                if not chunk:
                    continue
                buffers[raw_path] = (buffers[raw_path] + chunk)[-65536:]
                values = _rpcn_create_room_tuple(buffers[raw_path])
                if values is None:
                    continue
                host = next(
                    (
                        member["session"]
                        for member in match.get("members", [])
                        if member["session"].online_id == host_online_id
                    ),
                    None,
                )
                if host is None or not host.established or host.peer is None:
                    return
                self.telemetry.emit(
                    "game_match_room_log_bridge_observed",
                    match_id=int(match_id),
                    host_online_id=host_online_id,
                    source_path=raw_path,
                    notify_values=values,
                )
                self._publish_match_room(
                    host,
                    room_id=int(values[3]),
                    room_password=ACVD_MATCHING2_SESSION_PASSWORD,
                    source_values=values,
                )
                return
            await asyncio.sleep(0.05)
        if int(match_id) in self.matches:
            self.telemetry.emit(
                "game_match_room_log_bridge_timeout",
                match_id=int(match_id),
                host_online_id=host_online_id,
            )

    # HISTORICAL LOG-BRIDGE PATH: retained compatibility, not the native relay.
    def _publish_match_room(
        self,
        session: GameSession,
        *,
        room_id: int,
        room_password: bytes,
        source_values: dict[int, int],
        include_room_info: bool = True,
    ) -> None:
        preparation = self.preparations.get(session.online_id)
        match = self.matches.get(int(preparation.get("match_id", 0))) if preparation else None
        if match is None:
            self.telemetry.emit(
                "game_match_room_orphaned",
                online_id=session.online_id,
                notify_values=source_values,
            )
            return
        if match.get("counterparts_released"):
            return
        match["counterparts_released"] = True
        match["room_published"] = bool(include_room_info)
        self._cancel_match_room_log_watch(match)
        recipients = []
        recipient_payloads: dict[str, bytes] = {}
        recipient_match_payloads: dict[str, bytes] = {}
        host_member = next(member for member in match["members"] if member["session"] is session)
        for member in list(match["members"]):
            member_session = member["session"]
            if (
                member_session is session
                or not member_session.established
                or member_session.peer is None
            ):
                continue
            match_payload = _matching_result_for_members(
                member,
                host_member,
                criteria=dict(match["criteria"]),
                is_host=False,
                combat_area_id=int(match.get("combat_area_id", 0)),
                territory_layout_owner_id=int(match.get("territory_layout_owner_id", 0)),
                is_intra_faction=match.get("is_intra_faction", True),
                battle_type=match.get("battle_type", NORMAL_BATTLE_TYPE_EXTERMINATION),
            )
            # Retain MatchingResult before RoomInfo. This is the existing
            # compatibility sequence, not evidence of a proven timeout fix.
            self._send_game_push(member_session, match_payload, subtype=2)
            recipient_match_payloads[member_session.online_id] = match_payload
            if include_room_info:
                # Current explicit-notify and log-bridge paths include central
                # RoomInfo by default. P2P-only Normal discovery was insufficient
                # in controlled trials; do not revive the rejected explanation.
                payload = _match_room_info_push(
                    recipient_prepare_id=int(member["prepare_id"]),
                    room_id=int(room_id),
                    room_password=bytes(room_password),
                    room_owner_online_id=session.online_id,
                )
                self._send_game_push(member_session, payload, subtype=5)
                recipient_payloads[member_session.online_id] = payload
            recipients.append(member_session.online_id)
        self.telemetry.emit(
            (
                "game_match_room_published"
                if include_room_info
                else "game_match_guest_released_for_p2p_room"
            ),
            match_id=match["match_id"],
            host_online_id=session.online_id,
            recipient_online_ids=recipients,
            notify_values=source_values,
            room_id=int(room_id),
            room_password=bytes(room_password),
            room_info_published=bool(include_room_info),
            room_payloads=recipient_payloads,
            recipient_match_payloads=recipient_match_payloads,
        )
