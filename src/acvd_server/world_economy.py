"""Server-owned replacement economy for authenticated World battle results.

The original Normal/Special base-pay table has not been recovered.  This
module therefore keeps those amounts explicit, operator-controlled policy.
It consumes only a match and combat report already authenticated by the
matchmaking coordinator; raw result requests must never call it directly.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .combat_reports import CombatReport
from .player_medals import award_verified_medals
from .rewards import (
    WorldRewardParticipant,
    WorldSubtargetSize,
    allocate_world_mercenary_reward,
    allocate_world_team_reward,
    faction_compensation_by_player,
    mercenary_team_point_reduction_percent,
    world_cash_reward_plan,
    world_interwar_boss_base_reward,
    world_subtarget_size,
    world_wallet_payment,
)
from .team_medals import (
    apply_progress as apply_team_medal_progress,
    award_ranking_medals,
    initialize as initialize_team_medals,
)
from .team_progression import (
    AC_BREAK_POINTS,
    NORMAL_MISSION_CLEAR_POINTS,
    SPECIAL_MISSION_CLEAR_POINTS,
    apply as apply_team_progression,
    view as team_progress_view,
)
from .teams import TeamStore
from .world_messages import WORLD_PHASE_ACTIVE

WORLD_REWARD_POLICY = "replacement-world-cash-v1"
WORLD_REWARD_LIMIT = 10_000_000
WORLD_REWARD_DEFAULTS = {
    # An owner-provided historical JP-server Normal Mission Report shows an
    # exact 150,000 basic/total reward. Its 50,000 personal share does not by
    # itself identify the divisor, so keep the pool and division evidence
    # separate rather than forcing one roster interpretation.
    "world_normal_base_reward": 150_000,
    "world_special_base_reward": 1_000_000,
    # Every active Online-boss mission row stores this original base. Keep its
    # replacement override separate from active-war Special and default exact.
    "world_boss_base_reward": 1_000_000,
    # The 80k/120k native values classify damage credits, not cash. The owner
    # confirmed a 10k cash reward per subtarget, regardless of size.
    "world_small_subtarget_reward": 10_000,
    "world_large_subtarget_reward": 10_000,
    # Rank progression is not authoritative yet.  F is the retail new-entry
    # rank and matches the rank currently returned by this server.
    "world_mercenary_rank": 0,
}


def validate_world_reward_settings(values: dict[str, Any]) -> dict[str, int]:
    """Validate and return the reward-policy subset from operator settings."""
    if not isinstance(values, dict):
        raise ValueError("World reward settings must be an object")
    result: dict[str, int] = {}
    for key, default in WORLD_REWARD_DEFAULTS.items():
        value = values.get(key, default)
        if type(value) is not int:
            raise ValueError("World reward settings must be integers")
        if key == "world_mercenary_rank":
            if not 0 <= value <= 6:
                raise ValueError("World fallback mercenary rank must be F through S")
        elif not 0 <= value <= WORLD_REWARD_LIMIT:
            raise ValueError(
                f"World reward amounts must be between 0 and {WORLD_REWARD_LIMIT:,}"
            )
        result[key] = value
    return result


@dataclass(frozen=True)
class WorldSettlementPlan:
    """One complete atomic settlement and its bounded audit summary."""

    result_digest: str
    policy_version: str
    payments: tuple[dict[str, int | str], ...]
    operator_win_player_ids: tuple[int, ...]
    team_progress: tuple[dict[str, int], ...]
    team_medal_progress: tuple[dict[str, int], ...]
    player_ranking_progress: tuple[dict[str, int], ...]
    audit: dict[str, Any]
    boss_win_player_ids: tuple[int, ...] = ()


def world_result_identity(match: dict, report: CombatReport) -> str:
    """Identify one authenticated match, not merely its repeatable report bytes.

    The coordinator freezes a random receipt nonce on the matched context after
    checking the native report and full participant roster. Both sides of that
    match reuse the same nonce. A new match gets another nonce even if its
    client report and preparation number happen to repeat after a restart.
    """
    if not isinstance(match, dict) or not isinstance(report, CombatReport):
        raise ValueError("World receipt requires one retained match and report")
    if match.get("result_sha256") != report.digest:
        raise ValueError("World receipt report does not match the retained battle")
    nonce = match.get("reward_nonce")
    if type(nonce) is not str:
        raise ValueError("World receipt requires a server-owned match nonce")
    try:
        nonce_bytes = bytes.fromhex(nonce)
        report_bytes = bytes.fromhex(report.digest)
    except ValueError as error:
        raise ValueError("World receipt nonce or report digest is invalid") from error
    if (
        len(nonce_bytes) != 16
        or nonce != nonce_bytes.hex()
        or len(report_bytes) != 32
        or report.digest != report_bytes.hex()
    ):
        raise ValueError("World receipt nonce or report digest is not canonical")
    return hashlib.sha256(b"acvd-world-match-result-v2\0" + nonce_bytes + report_bytes).hexdigest()


def _policy_version(settings: dict[str, int]) -> str:
    return (
        f"{WORLD_REWARD_POLICY}:"
        f"n{settings['world_normal_base_reward']}:"
        f"s{settings['world_special_base_reward']}:"
        f"b{settings['world_boss_base_reward']}:"
        f"sm{settings['world_small_subtarget_reward']}:"
        f"lg{settings['world_large_subtarget_reward']}:"
        f"r{settings['world_mercenary_rank']}:tp2:tm1:sd1"
    )


def _base_reward(match: dict, settings: dict[str, int]) -> tuple[str, int]:
    if match.get("kind") == "interwar_boss":
        world_interwar_boss_base_reward(match)  # Validate the exact boss/area binding.
        return "interwar_boss", settings["world_boss_base_reward"]
    criteria = match.get("criteria")
    if (
        match.get("world_phase") != WORLD_PHASE_ACTIVE
        or not isinstance(criteria, dict)
        or criteria.get(2) != WORLD_PHASE_ACTIVE
        or type(criteria.get(3)) is not int
    ):
        raise ValueError("World match lacks an active-war Normal/Special class")
    mission_class = criteria[3]
    if mission_class == 1:
        return "normal", settings["world_normal_base_reward"]
    if mission_class == 2:
        return "special", settings["world_special_base_reward"]
    raise ValueError("World match has an unsupported mission class")


def build_world_settlement(
    match: dict,
    report: CombatReport,
    team_store,
    operator_settings: dict[str, Any],
) -> WorldSettlementPlan:
    """Calculate every participant payment from one retained match/report.

    Policy summary:

    * only the winning employer team receives the configured base pool;
    * eligible mercenary percentages are removed before ordinary Team Pay;
    * PvP faction compensation offsets actual costs before Team Pay, preventing
      the same repair/ammunition cost from being reimbursed twice;
    * each subtarget cash pool is split across the collector's settlement team;
    * a winning Pay-Costs-Equally pool that cannot cover all remaining costs is
      floored at zero and the shortfall is an explicit server subsidy.
    """
    if not isinstance(match, dict) or not isinstance(report, CombatReport):
        raise ValueError("World settlement requires one retained match and report")
    if match.get("result_sha256") != report.digest:
        raise ValueError("World settlement report is not the retained match result")
    participants = match.get("reward_participants")
    if (
        not isinstance(participants, tuple)
        or not participants
        or any(not isinstance(value, WorldRewardParticipant) for value in participants)
        or {value.player_id for value in participants} != set(report.player_ids)
    ):
        raise ValueError("World settlement lacks an authenticated participant context")
    squad = match.get("squad")
    if not isinstance(squad, dict) or set(squad) != set(report.player_ids):
        raise ValueError("World settlement lacks the exact frozen squad")

    settings = validate_world_reward_settings(operator_settings)
    mission_class, base_pool = _base_reward(match, settings)
    identity_by_player: dict[int, str] = {}
    for participant in participants:
        member = squad.get(participant.player_id)
        if (
            not isinstance(member, tuple)
            or len(member) != 2
            or member[1] != participant.social_team_id
            or getattr(member[0], "player_id", None) != participant.player_id
            or not isinstance(getattr(member[0], "online_id", None), str)
            or not member[0].online_id
        ):
            raise ValueError("World settlement squad identity changed after matching")
        identity_by_player[participant.player_id] = member[0].online_id

    affiliated = frozenset(
        participant.player_id
        for participant in participants
        if int(
            (team_store.team_by_id(participant.social_team_id) or {}).get("faction_id", 0)
        )
        in (1, 2, 3)
    )
    compensation = faction_compensation_by_player(
        report, affiliated_player_ids=affiliated
    )
    actual_expenses = report.expenses_by_player
    controlled_expenses = report.expenses_by_player_control
    basic_by_player = {participant.player_id: 0 for participant in participants}
    group_by_player = {
        participant.player_id: compensation.get(participant.player_id, 0)
        for participant in participants
    }
    subtargets_by_player: dict[int, list[tuple[int, int]]] = {
        participant.player_id: [] for participant in participants
    }
    team_audit = []

    for settlement_team_id in sorted({value.settlement_team_id for value in participants}):
        team = tuple(
            value for value in participants if value.settlement_team_id == settlement_team_id
        )
        ordinary = tuple(value for value in team if value.role in (0, 1))
        mercenaries = tuple(value for value in team if value.role == 2)
        if not ordinary:
            raise ValueError("World settlement team has no ordinary employer member")
        sides = {value.combat_side for value in team}
        outcomes = {value.won for value in team}
        policies = {value.pay_policy for value in ordinary}
        if len(sides) != 1 or len(outcomes) != 1 or len(policies) != 1 or None in policies:
            raise ValueError("World settlement team has contradictory frozen policy")
        won = outcomes.pop()
        gross = base_pool if won else 0
        mercenary_allocation = allocate_world_mercenary_reward(
            gross,
            report,
            participants,
            settlement_team_id=settlement_team_id,
            ranks_by_player={
                value.player_id: settings["world_mercenary_rank"] for value in mercenaries
            },
        )
        for share in mercenary_allocation.shares:
            basic_by_player[share.player_id] += share.personal_reward

        subsidy = 0
        undistributed = 0
        if won:
            uncovered: dict[int, tuple[int, int]] = {}
            for participant in ordinary:
                human, ai = controlled_expenses[participant.player_id]
                covered = compensation.get(participant.player_id, 0)
                if covered > human + ai:
                    raise ValueError("Faction compensation exceeds submitted expenses")
                # Compensation covers the aggregate cost.  Consume human cost
                # first only to preserve a deterministic two-control tuple;
                # Team Pay uses their sum and does not privilege either unit.
                human_uncovered = max(0, human - covered)
                ai_uncovered = ai - max(0, covered - human)
                uncovered[participant.player_id] = (human_uncovered, ai_uncovered)
            allocation = allocate_world_team_reward(
                mercenary_allocation.ordinary_team_reward,
                uncovered,
                next(iter(policies)),
            )
            subsidy = allocation.operator_subsidy
            undistributed = allocation.undistributed_reward
            for share in allocation.shares:
                basic_by_player[share.player_id] += share.personal_reward
                group_by_player[share.player_id] += share.group_cost_reimbursement

        team_audit.append(
            {
                "settlement_team_id": settlement_team_id,
                "won": won,
                "base_pool": gross,
                "mercenary_fees": sum(
                    share.personal_reward for share in mercenary_allocation.shares
                ),
                "operator_subsidy": subsidy,
                "undistributed_reward": undistributed,
            }
        )

    participant_by_player = {value.player_id: value for value in participants}
    target_ids: set[int] = set()
    subtarget_reward_undistributed = 0
    for target_id, collector_player_id, reported_value in report.subtargets:
        if target_id in target_ids:
            raise ValueError("World result repeats a subtarget identity")
        target_ids.add(target_id)
        size = world_subtarget_size(reported_value)
        reward = settings[
            "world_small_subtarget_reward"
            if size is WorldSubtargetSize.SMALL
            else "world_large_subtarget_reward"
        ]
        collector = participant_by_player[collector_player_id]
        recipients = sorted(
            value.player_id
            for value in participants
            if value.settlement_team_id == collector.settlement_team_id
        )
        if not recipients:
            raise ValueError("World subtarget collector has no settlement team")
        share, remainder = divmod(reward, len(recipients))
        subtarget_reward_undistributed += remainder
        for player_id in recipients:
            subtargets_by_player[player_id].append((target_id, share))

    # Retain only proven, non-practice active-war progress.  Compute it before
    # response construction so TeamRankInfo and the atomic durable mutation
    # use the same projected post-settlement state.
    active_war = (
        match.get("world_phase") == WORLD_PHASE_ACTIVE
        and match["criteria"].get(2) == WORLD_PHASE_ACTIVE
        and match.get("kind") in (None, "active_single_team")
    )
    faction_classification = match.get("is_intra_faction")
    classified_non_practice = faction_classification is False or (
        match.get("kind") == "active_single_team" and faction_classification is None
    )
    operator_winners = (
        tuple(sorted(report.winning_operator_player_ids))
        if active_war and classified_non_practice
        else ()
    )
    team_progress = []
    boss_winners = (
        tuple(sorted(value.player_id for value in participants if value.won))
        if active_war
        and classified_non_practice
        and mission_class == "special"
        and match.get("kind") == "active_single_team"
        else ()
    )
    team_medal_progress = []
    player_ranking_progress = []
    if active_war and classified_non_practice:
        clear_points = (
            NORMAL_MISSION_CLEAR_POINTS
            if mission_class == "normal"
            else SPECIAL_MISSION_CLEAR_POINTS
        )
        settlement_team_ids = sorted(
            {value.settlement_team_id for value in participants}
        )
        is_pvp = len(settlement_team_ids) == 2
        for settlement_team_id in settlement_team_ids:
            team = tuple(
                value
                for value in participants
                if value.settlement_team_id == settlement_team_id
            )
            sides = {value.combat_side for value in team}
            outcomes = {value.won for value in team}
            if len(sides) != 1 or len(outcomes) != 1:
                raise ValueError("World progression team has contradictory results")
            won = next(iter(outcomes))
            own_side = next(iter(sides))
            destroyed_opponents = sum(
                unit[2] != own_side and unit[3] == 2 for unit in report.units
            )
            rank_delta = -(clear_points // 2)
            if won:
                rank_delta = clear_points + AC_BREAK_POINTS * destroyed_opponents
                mercenary_count = sum(value.role == 2 for value in team)
                reduction = mercenary_team_point_reduction_percent(mercenary_count)
                rank_delta = rank_delta * (100 - reduction) // 100
            team_progress.append(
                {
                    "team_id": settlement_team_id,
                    "rank_delta": rank_delta,
                    "special_delta": int(won and mission_class == "normal"),
                }
            )
            team_medal_progress.append(
                {
                    "team_id": settlement_team_id,
                    "ac_kills_delta": destroyed_opponents,
                    "fame_delta": rank_delta,
                    "world_sorties_delta": 1,
                    "pvp_wins_delta": int(is_pvp and won),
                }
            )
        destruction_counts = report.opposing_destructions_by_player
        for participant in sorted(participants, key=lambda value: value.player_id):
            ac_destructions_delta = destruction_counts.get(participant.player_id, 0)
            mercenary_sorties_delta = int(participant.role == 2)
            mercenary_wins_delta = int(participant.role == 2 and participant.won)
            player_ranking_progress.append(
                {
                    "player_id": participant.player_id,
                    "ac_destructions_delta": ac_destructions_delta,
                    "mercenary_sorties_delta": mercenary_sorties_delta,
                    "mercenary_wins_delta": mercenary_wins_delta,
                }
            )

    progress_by_team = {row["team_id"]: row for row in team_progress}
    medal_progress_by_team = {
        row["team_id"]: row for row in team_medal_progress
    }
    stored_teams = getattr(team_store, "state", {}).get("teams", {})
    if isinstance(stored_teams, dict):
        projected_teams = deepcopy(stored_teams)
    else:
        projected_teams = {}
    for settlement_team_id in sorted(
        {value.settlement_team_id for value in participants}
    ):
        if str(settlement_team_id) not in projected_teams:
            stored_team = team_store.team_by_id(settlement_team_id)
            if not isinstance(stored_team, dict):
                raise ValueError("World result team has no durable rank state")
            projected_teams[str(settlement_team_id)] = deepcopy(stored_team)
    for projected_team in projected_teams.values():
        if isinstance(projected_team, dict):
            initialize_team_medals(projected_team)
    for settlement_team_id, progress in progress_by_team.items():
        apply_team_progression(
            projected_teams[str(settlement_team_id)],
            rank_delta=progress["rank_delta"],
            special_delta=progress["special_delta"],
        )
    for settlement_team_id, progress in medal_progress_by_team.items():
        apply_team_medal_progress(
            projected_teams[str(settlement_team_id)],
            ac_kills_delta=progress["ac_kills_delta"],
            fame_delta=progress["fame_delta"],
            world_sorties_delta=progress["world_sorties_delta"],
            pvp_wins_delta=progress["pvp_wins_delta"],
        )
    award_ranking_medals(projected_teams)
    # Medal-derived Team points must also appear in the exact persisted reward
    # response. Use the same award/credit functions on a private projection.
    projected_state = {
        "teams": projected_teams,
        "members": deepcopy(getattr(team_store, "state", {}).get("members", {})),
        "player_profiles": deepcopy(getattr(team_store, "state", {}).get("player_profiles", {})),
    }
    ranking_by_player = {row["player_id"]: row for row in player_ranking_progress}
    has_player_awards = False
    for player_id, online_id in identity_by_player.items():
        profile = projected_state["player_profiles"].setdefault(online_id, {"player_id": player_id})
        profile.setdefault("player_id", player_id)
        deltas = {
            "observed_operator_wins": int(player_id in operator_winners),
            "observed_world_boss_wins": int(player_id in boss_winners),
            "observed_mercenary_wins": ranking_by_player.get(player_id, {}).get("mercenary_wins_delta", 0),
        }
        for counter, delta in deltas.items():
            previous = profile.get(counter, 0)
            if type(previous) is not int or not 0 <= previous < (1 << 32) - delta:
                raise ValueError("Stored medal progress needs review")
            if delta:
                profile[counter] = previous + delta
        has_player_awards |= bool(award_verified_medals(profile, player_id, datetime.now(timezone.utc)))
    if has_player_awards:
        TeamStore._reconcile_player_medal_progress(projected_state)
    rank_wire_by_team: dict[int, tuple[int, int, int, int]] = {}
    for settlement_team_id in sorted(
        {value.settlement_team_id for value in participants}
    ):
        rank_wire_by_team[settlement_team_id] = team_progress_view(
            projected_teams[str(settlement_team_id)]
        ).wire_values

    payments = []
    for participant in sorted(participants, key=lambda value: value.player_id):
        player_id = participant.player_id
        basic = basic_by_player[player_id]
        subtargets = tuple(subtargets_by_player[player_id])
        bonus = sum(amount for _target, amount in subtargets)
        group = group_by_player[player_id]
        plan = world_cash_reward_plan(
            player_id=player_id,
            basic_reward=basic,
            special_reward=0,
            combined_group_cost_reward=group,
            personal_reward=basic + bonus,
            team_rank=rank_wire_by_team[participant.settlement_team_id],
            subtarget_rewards=subtargets,
        )
        payments.append(
            world_wallet_payment(
                identity_by_player[player_id],
                plan,
                expenses=actual_expenses[player_id],
            )
        )

    return WorldSettlementPlan(
        result_digest=world_result_identity(match, report),
        policy_version=_policy_version(settings),
        payments=tuple(payments),
        operator_win_player_ids=operator_winners,
        boss_win_player_ids=boss_winners,
        team_progress=tuple(team_progress),
        team_medal_progress=tuple(team_medal_progress),
        player_ranking_progress=tuple(player_ranking_progress),
        audit={
            "mission_class": mission_class,
            "configured_base_pool": base_pool,
            "subtarget_count": len(report.subtargets),
            "subtarget_reward_undistributed": subtarget_reward_undistributed,
            "faction_compensated_player_ids": sorted(affiliated),
            "operator_win_progress_player_ids": list(operator_winners),
            "team_progress": team_progress,
            "team_medal_progress": team_medal_progress,
            "player_ranking_progress": player_ranking_progress,
            "teams": team_audit,
        },
    )
