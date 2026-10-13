"""Story base/subquest pay and the distinct 0x03C2 reward-list response.

Request expenses are not earnings. World settlement, item rewards,
and multi-owner payout allocation remain separate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .combat_reports import MAX_REPORT_SUBTARGETS, CombatReport
from .protocol import decode_proto, proto_bytes, proto_varint
from .story_reward_data import STORY_SUBQUEST_PAY

STORY_REWARD_POLICY = "story_base_pay_v1"
STORY_SUBQUEST_REWARD_POLICY = "story_subquest_pay_v2"

# ServerSystemParam entries 2-8 are named mercenary-rank expense percentages
# and stored S through F as 30,26,24,20,16,14,10. Retail rank enum zero is F
# and six is S, so expose the table in enum order. This is a rate source, not a
# proved gross-pay split or rounding rule.
MERCENARY_EXPENSE_RATE_PERCENT = (10, 14, 16, 20, 24, 26, 30)
MERCENARY_TEAM_POINT_REDUCTION_PERCENT = (0, 10, 50, 85)

# Active MissionMenuDataParam rows 3190 through 3790 are MissionType 3
# (Online boss), one per World region. Every row's signed
# MissionClearRewardMoney is exactly 1,000,000. Source SHA-256:
# 18b4aab2b332a780488eb5d86584806dd64cd7d5dff0f754b7d7d95888ea7aea.
# Keep this distinct from unresolved Normal, PvP Special and subtarget pay.
WORLD_ONLINE_BOSS_BASE_PAY = {
    3190: 1_000_000,
    3290: 1_000_000,
    3390: 1_000_000,
    3490: 1_000_000,
    3590: 1_000_000,
    3690: 1_000_000,
    3790: 1_000_000,
}
WORLD_ONLINE_BOSS_MISSION_BY_AREA = {
    1: 3790,
    2: 3690,
    3: 3290,
    4: 3590,
    5: 3490,
    6: 3390,
    7: 3190,
}

# Story/AC-duel MissionClearRewardMoney, MissionMenuDataParam SHA-256:
# 18b4aab2b332a780488eb5d86584806dd64cd7d5dff0f754b7d7d95888ea7aea.
# No client file is read at runtime. These are base payments, not bonus grants.
STORY_BASE_PAY = {
    90: 0,
    101: 150000,
    110: 150000,
    201: 120000,
    202: 170000,
    203: 120000,
    204: 0,
    210: 150000,
    301: 150000,
    302: 150000,
    304: 150000,
    310: 180000,
    401: 150000,
    402: 170000,
    404: 150000,
    410: 180000,
    501: 150000,
    502: 150000,
    503: 120000,
    510: 200000,
    601: 150000,
    602: 170000,
    603: 120000,
    610: 200000,
    703: 170000,
    710: 200000,
    801: 170000,
    810: 200000,
    902: 170000,
    910: 200000,
    1010: 250000,
    2000: 100000,
    2001: 100000,
    2002: 100000,
    2003: 130000,
    2004: 130000,
    2005: 130000,
    2006: 130000,
    2007: 130000,
    2008: 130000,
    2009: 130000,
    2010: 130000,
    2011: 130000,
    2012: 130000,
    2013: 150000,
    2014: 150000,
    2015: 150000,
    2016: 150000,
    2017: 150000,
    2018: 150000,
    2019: 150000,
    2020: 150000,
    2021: 150000,
    2022: 150000,
    2023: 150000,
    2024: 150000,
    2025: 150000,
    2026: 150000,
    2027: 150000,
    2028: 150000,
    2029: 150000,
}


@dataclass(frozen=True)
class StoryRewardPlan:
    mission_id: int
    player_id: int
    base_pay: int
    expenses: int
    unit_count: int
    bonus_pay: int = 0
    subquest_ids: tuple[int, ...] = ()

    @property
    def gross_pay(self) -> int:
        return self.base_pay + self.bonus_pay

    @property
    def cash_subquests(self) -> tuple[tuple[int, int], ...]:
        table = STORY_SUBQUEST_PAY.get(self.mission_id, {})
        return tuple((key, table[key]) for key in self.subquest_ids if table[key] > 0)

    @property
    def policy_version(self) -> str:
        # Base-only requests keep their existing replay policy. Old underpaid
        # bonus receipts must not silently replay a larger response or pay twice.
        return STORY_SUBQUEST_REWARD_POLICY if self.bonus_pay else STORY_REWARD_POLICY


@dataclass(frozen=True)
class WorldBattleRewardPlan:
    """Explicit 0x03BF response values; this type performs no reward calculation.

    The World response requires one BattleRewardInfo and one TeamRankInfo.
    Callers must recover and validate every amount and TeamRankInfo scalar
    before using this encoder. Keeping them explicit prevents request
    statistics from being mistaken for payments merely because both happen to
    be integers.
    """

    player_id: int
    basic_reward: int
    special_reward: int
    group_cost_reward_1: int
    group_cost_reward_2: int
    personal_reward: int
    team_rank_tier: int
    team_rank_subdivision: int
    team_rank_progress: int
    team_rank_threshold: int
    # (authenticated report target ID, this recipient's existing cash share).
    subtarget_rewards: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class WorldMissionBaseReward:
    """One exact client-table base reward bound to its World mission context."""

    mission_id: int
    area_id: int
    basic_reward: int


def world_interwar_boss_base_reward(match: dict) -> WorldMissionBaseReward:
    """Resolve only the qualified grace-period Online-boss base reward.

    The single-squad grace Special path freezes kind ``interwar_boss``, World
    phase zero, battle type six and the selected area.  The active client table
    maps areas one through seven to mission IDs 3190 through 3790.  This does
    not supply subtarget/special pay, expenses, direct rewards, or any other
    World mode's gross schedule.
    """
    if not isinstance(match, dict):
        raise ValueError("Online-boss reward requires one retained match")
    criteria = match.get("criteria")
    area_id = match.get("stored_area_id")
    if (
        match.get("kind") != "interwar_boss"
        or match.get("world_phase") != 0
        or match.get("battle_type") != 6
        or not isinstance(criteria, dict)
        or criteria.get(2) != 0
        or criteria.get(3) != 2
        or type(area_id) is not int
        or not 1 <= area_id <= 7
    ):
        raise ValueError("Match is not a qualified grace-period Online boss")
    mission_id = WORLD_ONLINE_BOSS_MISSION_BY_AREA[area_id]
    return WorldMissionBaseReward(mission_id, area_id, WORLD_ONLINE_BOSS_BASE_PAY[mission_id])


def world_cash_reward_plan(
    *,
    player_id: int,
    basic_reward: int,
    special_reward: int,
    combined_group_cost_reward: int,
    personal_reward: int,
    team_rank: tuple[int, int, int, int],
    subtarget_rewards: tuple[tuple[int, int], ...] = (),
) -> WorldBattleRewardPlan:
    """Construct the canonical cash-only World response plan.

    The retail converter adds BattleRewardInfo fields 5 and 6 immediately and
    exposes only their combined group-cost reward.  No recovered consumer
    observes the split, so place the complete validated value in field 5 and
    write required field 6 as zero.  The World result's required field-2
    companion is TeamRankInfo, not QuestRewardInfo; callers must supply its
    exact post-settlement tier, subdivision, progress and threshold.

    This helper chooses none of the five cash values.  Its only policy is a
    wire-equivalent representation after callers have recovered them.
    """
    if (
        not isinstance(team_rank, tuple)
        or len(team_rank) != 4
        or any(type(value) is not int for value in team_rank)
    ):
        raise ValueError("World cash response requires one TeamRankInfo tuple")
    plan = WorldBattleRewardPlan(
        player_id=player_id,
        basic_reward=basic_reward,
        special_reward=special_reward,
        group_cost_reward_1=combined_group_cost_reward,
        group_cost_reward_2=0,
        personal_reward=personal_reward,
        team_rank_tier=team_rank[0],
        team_rank_subdivision=team_rank[1],
        team_rank_progress=team_rank[2],
        team_rank_threshold=team_rank[3],
        subtarget_rewards=subtarget_rewards,
    )
    # Validate immediately so a malformed plan never reaches a transaction.
    _world_battle_result_response(plan)
    return plan


class WorldPayPolicy(str, Enum):
    """The two retail Team Pay Apportionment choices, independent of wire IDs."""

    EQUAL_DISTRIBUTION = "equal_distribution"
    PAY_COSTS_EQUALLY = "pay_costs_equally"


class WorldSubtargetSize(str, Enum):
    """Original ServerSystemParam small/large damage-credit classes."""

    SMALL = "small"
    LARGE = "large"


WORLD_SUBTARGET_DAMAGE_REDUCTION = {
    80_000: WorldSubtargetSize.SMALL,
    120_000: WorldSubtargetSize.LARGE,
}


def mercenary_expense_rate_percent(rank: int) -> int:
    """Return the original rank-specific expense percentage, without applying it."""
    if type(rank) is not int or not 0 <= rank < len(MERCENARY_EXPENSE_RATE_PERCENT):
        raise ValueError("Mercenary rank must use the native F-through-S enum")
    return MERCENARY_EXPENSE_RATE_PERCENT[rank]


def world_subtarget_size(reported_value: int) -> WorldSubtargetSize:
    """Classify BattleResultSubTarget field 3 without treating it as cash.

    Original ServerSystemParam names offsets 0x20 and 0x24 as the small and
    large subtarget damage reductions and stores 80,000 and 120,000. Current
    reports use those exact values. Reward cash or item identity is a separate
    server decision and cannot be inferred from the magnitude alone.
    """
    if type(reported_value) is not int:
        raise ValueError("World subtarget damage credit must be an integer")
    try:
        return WORLD_SUBTARGET_DAMAGE_REDUCTION[reported_value]
    except KeyError as error:
        raise ValueError("World subtarget damage credit is not an original size") from error


def mercenary_team_point_reduction_percent(count: int) -> int:
    """Return the original reduction for zero through three hired mercenaries."""
    if type(count) is not int or not 0 <= count < len(MERCENARY_TEAM_POINT_REDUCTION_PERCENT):
        raise ValueError("Hired-mercenary count must be between zero and three")
    return MERCENARY_TEAM_POINT_REDUCTION_PERCENT[count]


def world_pay_policy_for_team(team: dict) -> WorldPayPolicy:
    """Resolve the leader's persisted Team Pay Apportionment selection.

    RequestUpdateLeaderTeamInfo field 3 is stored zero-based. TeamInfo field
    12 adds one for client readback. A native restart screenshot proves stored
    value 0 renders ``Pay Costs Equally``; the only other retail choice is
    ``Equal Distribution``. Hired-mercenary division is a separate rule and is
    deliberately not inferred from this team setting.
    """
    if not isinstance(team, dict):
        raise ValueError("World pay policy requires a stored team record")
    value = team.get("pay_apportionment", 0)
    if type(value) is not int:
        raise ValueError("Stored Team Pay Apportionment must be an integer")
    try:
        return {
            0: WorldPayPolicy.PAY_COSTS_EQUALLY,
            1: WorldPayPolicy.EQUAL_DISTRIBUTION,
        }[value]
    except KeyError as error:
        raise ValueError("Stored Team Pay Apportionment is unsupported") from error


@dataclass(frozen=True)
class WorldPlayerRewardShare:
    """One player's allocation before an exact World response is constructed."""

    player_id: int
    personal_reward: int
    human_expenses: int
    ai_expenses: int
    group_cost_reimbursement: int

    @property
    def net_income(self) -> int:
        return (
            self.personal_reward
            - self.human_expenses
            - self.ai_expenses
            + self.group_cost_reimbursement
        )


@dataclass(frozen=True)
class WorldTeamRewardAllocation:
    """A proved apportionment result for one explicit gross team reward pool."""

    gross_reward: int
    policy: WorldPayPolicy
    shares: tuple[WorldPlayerRewardShare, ...]
    undistributed_reward: int
    operator_subsidy: int


@dataclass(frozen=True)
class WorldRewardParticipant:
    """One authenticated result owner bound to its battle settlement cohort."""

    player_id: int
    combat_side: int
    role: int
    social_team_id: int
    settlement_team_id: int
    pay_policy: WorldPayPolicy | None
    won: bool


@dataclass(frozen=True)
class WorldMercenaryRewardShare:
    """One hired mercenary's proved rank fee and eligibility result."""

    player_id: int
    rank: int
    rate_percent: int
    eligible: bool
    personal_reward: int


@dataclass(frozen=True)
class WorldMercenaryRewardAllocation:
    """Mission payout after eligible hired-mercenary fees are removed."""

    gross_reward: int
    settlement_team_id: int
    ordinary_team_reward: int
    shares: tuple[WorldMercenaryRewardShare, ...]


def world_reward_participants(
    report: CombatReport, preparations: list[dict]
) -> tuple[WorldRewardParticipant, ...]:
    """Bind report roles to frozen authenticated preparation membership.

    Fighters and Operators settle with their registered team and its frozen
    Team Pay policy. A hired mercenary settles with the employer team but keeps
    its distinct role and no ordinary team-share policy. Its social team may
    even be the opposing team, as proved by the current public three-player
    capture. This function chooses no reward amounts.
    """
    if not isinstance(report, CombatReport) or not isinstance(preparations, list):
        raise ValueError("World reward participants require a report and match members")
    report_roles = {
        unit[0]: unit[7]
        for unit in report.units
        if unit[1] == 0
    }
    if set(report_roles) != set(report.player_ids):
        raise ValueError("World result has no unique human role for every owner")
    result: dict[int, WorldRewardParticipant] = {}
    for preparation in preparations:
        if not isinstance(preparation, dict):
            raise ValueError("World match member is malformed")
        session = preparation.get("session")
        team_id = preparation.get("team_id")
        if (
            session is None
            or type(getattr(session, "player_id", None)) is not int
            or type(team_id) is not int
            or team_id <= 0
        ):
            raise ValueError("World match member lacks authenticated team identity")
        try:
            policy = WorldPayPolicy(
                preparation.get("pay_policy", WorldPayPolicy.PAY_COSTS_EQUALLY.value)
            )
        except (TypeError, ValueError) as error:
            raise ValueError("World match member has unsupported pay policy") from error
        player_ids = preparation.get("claimed_player_ids") or [session.player_id]
        claimed_roles = preparation.get("claimed_player_roles") or [0] * len(player_ids)
        squad = preparation.get("squad") or {session.player_id: (session, team_id)}
        if (
            not isinstance(player_ids, list)
            or not isinstance(claimed_roles, list)
            or len(player_ids) != len(claimed_roles)
            or not isinstance(squad, dict)
            or set(player_ids) != set(squad)
        ):
            raise ValueError("World match member has inconsistent frozen squad")
        member_sides = set()
        for player_id, claimed_role in zip(player_ids, claimed_roles):
            if (
                type(player_id) is not int
                or player_id in result
                or player_id not in report_roles
                or type(claimed_role) is not int
                or claimed_role not in (0, 2)
            ):
                raise ValueError("World participant identity or claimed role is invalid")
            squad_member = squad.get(player_id)
            if (
                not isinstance(squad_member, tuple)
                or len(squad_member) != 2
                or type(squad_member[1]) is not int
                or squad_member[1] <= 0
                or getattr(squad_member[0], "player_id", None) != player_id
            ):
                raise ValueError("World participant lacks frozen social-team identity")
            role = report_roles[player_id]
            social_team_id = squad_member[1]
            if claimed_role == 2:
                if role != 2 or social_team_id == team_id:
                    raise ValueError("World hired-mercenary role contradicts its hire binding")
                participant_policy = None
            else:
                if role not in (0, 1) or social_team_id != team_id:
                    raise ValueError("World team-member role contradicts its frozen team")
                participant_policy = policy
            combat_side = report.side_by_player[player_id]
            member_sides.add(combat_side)
            result[player_id] = WorldRewardParticipant(
                player_id=player_id,
                combat_side=combat_side,
                role=role,
                social_team_id=social_team_id,
                settlement_team_id=team_id,
                pay_policy=participant_policy,
                won=combat_side == report.winning_side,
            )
        if len(member_sides) != 1:
            raise ValueError("One preparation spans opposing combat sides")
    if set(result) != set(report.player_ids):
        raise ValueError("World settlement context does not cover the exact report roster")
    return tuple(result[player_id] for player_id in sorted(result))


def allocate_world_mercenary_reward(
    gross_reward: int,
    report: CombatReport,
    participants: tuple[WorldRewardParticipant, ...],
    *,
    settlement_team_id: int,
    ranks_by_player: dict[int, int],
) -> WorldMercenaryRewardAllocation:
    """Remove eligible rank-based mercenary fees from one team's gross pay.

    Original ServerSystemParam calls the F-through-S percentages mercenary-rank
    expense rates.  Original-era rules and current-player descriptions agree
    that the hired player receives that fraction of the mission's total payout
    only when the employer side wins and the hired AC survives.  The ordinary
    employer team retains the remainder for its separate Team Pay allocation.

    The function requires the already authenticated result topology and an
    explicit rank for every hired mercenary. It supports the native maximum of
    three hires and floors each percentage fee independently. It does not
    recover the mission's gross amount, infer rank from current server defaults,
    reimburse costs, or mutate wallets.
    """
    if type(gross_reward) is not int or not 0 <= gross_reward < 1 << 31:
        raise ValueError("Mercenary allocation requires a signed-range gross reward")
    if not isinstance(report, CombatReport) or not isinstance(participants, tuple):
        raise ValueError("Mercenary allocation requires a report-bound participant set")
    if type(settlement_team_id) is not int or settlement_team_id <= 0:
        raise ValueError("Mercenary allocation requires one settlement team")
    if not isinstance(ranks_by_player, dict):
        raise ValueError("Mercenary allocation requires explicit rank authority")
    if (
        any(not isinstance(participant, WorldRewardParticipant) for participant in participants)
        or len(participants) != len(report.player_ids)
        or {participant.player_id for participant in participants} != set(report.player_ids)
    ):
        raise ValueError("Mercenary participants do not cover the exact combat report")
    team = tuple(
        participant
        for participant in participants
        if participant.settlement_team_id == settlement_team_id
    )
    if not team or not any(participant.role in (0, 1) for participant in team):
        raise ValueError("Mercenary allocation requires an ordinary employer team member")
    sides = {participant.combat_side for participant in team}
    outcomes = {participant.won for participant in team}
    if len(sides) != 1 or len(outcomes) != 1:
        raise ValueError("Mercenary settlement team spans contradictory combat outcomes")
    won = outcomes.pop()
    if won != (sides.pop() == report.winning_side):
        raise ValueError("Mercenary participant outcome contradicts the combat report")
    if not won and gross_reward:
        raise ValueError("A losing employer team has no mission payout for mercenary fees")

    mercenaries = tuple(sorted((p for p in team if p.role == 2), key=lambda p: p.player_id))
    mercenary_ids = {participant.player_id for participant in mercenaries}
    if len(mercenaries) > 3:
        raise ValueError("A World employer team cannot settle more than three mercenaries")
    if set(ranks_by_player) != mercenary_ids:
        raise ValueError("Mercenary allocation ranks do not match the hired roster")
    human_status = {
        unit[0]: unit[3]
        for unit in report.units
        if unit[1] == 0
    }
    shares = []
    for participant in mercenaries:
        rank = ranks_by_player[participant.player_id]
        rate = mercenary_expense_rate_percent(rank)
        eligible = participant.won and human_status.get(participant.player_id) == 1
        reward = gross_reward * rate // 100 if eligible else 0
        shares.append(
            WorldMercenaryRewardShare(
                participant.player_id,
                rank,
                rate,
                eligible,
                reward,
            )
        )
    total = sum(share.personal_reward for share in shares)
    if total > gross_reward:
        raise ValueError("Mercenary fees exceed the mission payout")
    return WorldMercenaryRewardAllocation(
        gross_reward,
        settlement_team_id,
        gross_reward - total,
        tuple(shares),
    )


def faction_compensation_by_player(
    report: CombatReport, *, affiliated_player_ids: frozenset[int]
) -> dict[int, int]:
    """Return full expenses when an affiliated player faced human/UNAC ACs.

    Historical World-mode descriptions agree that faction compensation covers
    repair and ammunition against a human or UNAC opponent, including a loss,
    but not a turret/NPC burn run. A retained report proves that distinction
    when another player owner occurs on the opposite side. This pure rule does
    not choose either reimbursement wire field or mutate a wallet.
    """
    if not isinstance(report, CombatReport) or not isinstance(affiliated_player_ids, frozenset):
        raise ValueError("Faction compensation requires a report and explicit affiliation set")
    if any(
        type(player_id) is not int
        or player_id not in report.player_ids
        or not 0 < player_id < 1 << 32
        for player_id in affiliated_player_ids
    ):
        raise ValueError("Faction compensation affiliation is not report-bound")
    sides = report.side_by_player
    expenses = report.expenses_by_player
    result = {}
    for player_id in sorted(affiliated_player_ids):
        has_ac_opponent = any(
            other_id != player_id and other_side != sides[player_id]
            for other_id, other_side in sides.items()
        )
        result[player_id] = expenses[player_id] if has_ac_opponent else 0
    return result


def allocate_world_team_reward(
    gross_reward: int,
    expenses_by_player: dict[int, tuple[int, int]],
    policy: WorldPayPolicy,
) -> WorldTeamRewardAllocation:
    """Apply the two retail sharing rules without inventing a gross reward.

    ``expenses_by_player`` contains the still-uncovered ``(human, owned-AI)``
    repair-plus-ammunition totals. Equal Distribution divides gross pay first
    and leaves each owner responsible for their own costs. Pay Costs Equally
    deducts the complete team cost first, divides the nonnegative remainder,
    and supplies a combined reimbursement for each owner's submitted costs so
    the client-side net is the equal share. If a winning reward pool is smaller
    than those costs, the distributable pool floors at zero and
    ``operator_subsidy`` records the shortfall rather than rejecting the entire
    aggregate result. The two wire fields that form the combined reimbursement
    remain deliberately unmapped.

    Integer division follows the client's signed integer division behavior.
    Any remainder stays undistributed rather than being assigned to a guessed
    leader or report order.  This function does not choose the gross pool,
    response quest values, winner eligibility, faction compensation, or wallet
    receipt behavior.
    """
    if type(gross_reward) is not int or not 0 <= gross_reward < 1 << 31:
        raise ValueError("World gross reward must be a nonnegative signed-range integer")
    if not isinstance(policy, WorldPayPolicy) or not expenses_by_player:
        raise ValueError("World allocation requires an explicit policy and recipients")
    normalized = []
    for player_id, costs in expenses_by_player.items():
        if (
            type(player_id) is not int
            or not 0 < player_id < 1 << 32
            or not isinstance(costs, tuple)
            or len(costs) != 2
            or any(type(value) is not int or not 0 <= value < 1 << 31 for value in costs)
        ):
            raise ValueError("World allocation has an invalid recipient or expense pair")
        normalized.append((player_id, costs[0], costs[1]))
    normalized.sort()
    total_expenses = sum(human + ai for _, human, ai in normalized)
    if total_expenses >= 1 << 31:
        raise ValueError("World team expenses exceed the supported signed range")
    if policy is WorldPayPolicy.EQUAL_DISTRIBUTION:
        pool = gross_reward
        reimburse_costs = False
        operator_subsidy = 0
    else:
        operator_subsidy = max(0, total_expenses - gross_reward)
        pool = max(0, gross_reward - total_expenses)
        reimburse_costs = True
    personal_reward, remainder = divmod(pool, len(normalized))
    shares = tuple(
        WorldPlayerRewardShare(
            player_id,
            personal_reward,
            human,
            ai,
            human + ai if reimburse_costs else 0,
        )
        for player_id, human, ai in normalized
    )
    return WorldTeamRewardAllocation(
        gross_reward, policy, shares, remainder, operator_subsidy
    )


def _world_battle_result_response(plan: WorldBattleRewardPlan) -> bytes:
    """Encode the two required initialized envelopes for World opcode 0x03BF.

    This is deliberately separate from Story's repeated field-1-only response.
    It neither updates a wallet nor derives values from a submitted combat report.
    """
    if not isinstance(plan, WorldBattleRewardPlan):
        raise ValueError("World result response requires an explicit reward plan")
    values = (
        plan.player_id,
        plan.basic_reward,
        plan.special_reward,
        plan.group_cost_reward_1,
        plan.group_cost_reward_2,
        plan.personal_reward,
        plan.team_rank_tier,
        plan.team_rank_subdivision,
        plan.team_rank_progress,
        plan.team_rank_threshold,
    )
    if any(type(value) is not int or not 0 <= value < 1 << 32 for value in values):
        raise ValueError("World result response values must be unsigned 32-bit integers")
    if plan.player_id == 0:
        raise ValueError("World result response requires one authenticated player ID")
    if (
        plan.team_rank_tier != 1
        or not 1 <= plan.team_rank_subdivision <= 27
        or not 0 <= plan.team_rank_progress < plan.team_rank_threshold
        or not 1 <= plan.team_rank_threshold < 1 << 32
    ):
        raise ValueError("World result response requires a valid TeamRankInfo")
    battle_reward = b"".join(
        proto_varint(number, value)
        for number, value in (
            (3, plan.basic_reward),
            (4, plan.special_reward),
            (5, plan.group_cost_reward_1),
            (6, plan.group_cost_reward_2),
            (7, plan.personal_reward),
            (9, plan.player_id),
        )
    )
    rows = plan.subtarget_rewards
    if (
        not isinstance(rows, tuple)
        or len(rows) > MAX_REPORT_SUBTARGETS
        or any(
            not isinstance(row, tuple)
            or len(row) != 2
            or any(type(value) is not int or not 0 <= value < 1 << 32 for value in row)
            for row in rows
        )
    ):
        raise ValueError("World subtarget reward details are invalid")
    if len({target for target, _amount in rows}) != len(rows):
        raise ValueError("World subtarget reward details repeat a target")
    if sum(amount for _target, amount in rows) > plan.personal_reward:
        raise ValueError("World subtarget details exceed funded personal pay")
    for _target, amount in rows:
        # The retail converter at 0x002bcfb8 leaves generic (non-1000)
        # rewards unassociated with Story subquest names. Required QuestReward
        # scalars are neutral: do not invent a Story achievement or map target
        # IDs into its namespace. Each cash child becomes one acquired-reward
        # row and contributes to Bonus Pay. Its amount is already in field 7.
        quest = b"".join(proto_varint(number, 0) for number in (1, 2, 3, 5))
        quest += proto_bytes(6, proto_varint(1, 1) + proto_varint(2, amount))
        battle_reward += proto_bytes(8, quest)
    team_rank = b"".join(
        proto_varint(number, value)
        for number, value in (
            (1, plan.team_rank_tier),
            (2, plan.team_rank_subdivision),
            (3, plan.team_rank_progress),
            (4, plan.team_rank_threshold),
        )
    )
    return proto_bytes(1, battle_reward) + proto_bytes(2, team_rank)


def world_wallet_payment(
    online_id: str,
    plan: WorldBattleRewardPlan,
    *,
    expenses: int,
) -> dict[str, int | str]:
    """Build one explicit wallet input and exact retry reply for World play.

    The client applies BattleRewardInfo personal pay (field 7), adds the two
    group-cost reimbursements (fields 5 and 6), then accounts for the submitted
    owner expenses.  Basic and special reward fields are the displayed
    breakdown of that pay, as are field-8 bonus details, not additional wallet
    credits. Keeping the encoded
    response beside the wallet inputs lets a durable multi-player transaction
    replay the identical reply without recalculating policy.

    This helper still chooses no reward amount.  Every response scalar must be
    supplied through an already validated ``WorldBattleRewardPlan``.
    """
    response = _world_battle_result_response(plan)
    if not isinstance(online_id, str) or not online_id or len(online_id) > 64:
        raise ValueError("World wallet payment requires one bounded account identity")
    if type(expenses) is not int or not 0 <= expenses < 1 << 32:
        raise ValueError("World wallet payment expenses must be unsigned 32-bit")
    gross_earnings = (
        plan.personal_reward + plan.group_cost_reward_1 + plan.group_cost_reward_2
    )
    if gross_earnings >= 1 << 32:
        raise ValueError("World wallet payment earnings exceed unsigned 32-bit")
    return {
        "online_id": online_id,
        "player_id": plan.player_id,
        "gross_earnings": gross_earnings,
        "expenses": expenses,
        "response_hex": response.hex(),
    }


def story_reward_plans(
    payload: bytes, player_ids: tuple[int, ...]
) -> tuple[StoryRewardPlan, ...]:
    """Validate one solo or two-human Story aggregate.

    The caller supplies the exact authenticated result-owner cohort. Each
    human receives the mission/subquest rewards once and pays only the costs
    of their own human/UNAC records. This parser does not establish the cohort;
    the live handler binds it to the current team session or accepted hire.
    """
    if (
        not isinstance(player_ids, tuple)
        or not 1 <= len(player_ids) <= 2
        or any(type(value) is not int or not 0 < value < 1 << 32 for value in player_ids)
        or len(set(player_ids)) != len(player_ids)
        or not 0 < len(payload) <= 4096
    ):
        raise ValueError("Invalid Story result identities or size")
    fields = decode_proto(payload)
    mission = [f for f in fields if f.number == 2]
    if len(mission) != 1 or mission[0].wire_type != 0 or mission[0].value not in STORY_BASE_PAY:
        raise ValueError("Story result has no supported mission payment")
    records = [f for f in fields if f.number == 3]
    if not 1 <= len(records) <= 4:
        raise ValueError("Story result exceeds two humans with one owned UNAC each")
    units_by_player = {player_id: [] for player_id in player_ids}
    expenses_by_player = {player_id: 0 for player_id in player_ids}
    owner_order = []
    for record in records:
        if record.wire_type != 2:
            raise ValueError("Invalid Story participant record")
        nested = decode_proto(record.value)
        values = {}
        for number in range(1, 8):
            found = [f for f in nested if f.number == number]
            if len(found) != 1 or found[0].wire_type != 0:
                raise ValueError("Missing or duplicate Story participant field")
            values[number] = found[0].value
        owner = values[1]
        if len(nested) != 7 or owner not in units_by_player:
            raise ValueError("Story payout includes an unauthenticated owner")
        # A completed native mission can report status 2 for any destroyed
        # owned unit, including the second human in two-player Story.  The old
        # unit-index gate only permitted that status for a UNAC and rejected
        # the entire successful co-op payout when the guest's AC was lost.
        # Same-team Story owners and owned UNACs use the captured (0, 0)
        # reserved pair.  A hired cross-team human uses (2, 1) on the second
        # owner record; four retained public results repeat that exact form,
        # including two mission-1010 completions rejected before this rule was
        # recovered. Keep the exception tied to the second human's unit-zero
        # record. Its independent completed/destroyed status remains governed
        # by field 3, so an employer's preceding UNAC cannot shift this rule and
        # a destroyed hired human is not accidentally excluded. It must not
        # authorize a solo record, an owned UNAC, or either scalar independently.
        reserved_pair = (values[6], values[7])
        hired_second_human = (
            len(player_ids) == 2
            and owner == player_ids[1]
            and values[2] == 0
            and reserved_pair == (2, 1)
        )
        if values[3] not in (1, 2) or (
            reserved_pair != (0, 0) and not hired_second_human
        ):
            raise ValueError("Unqualified Story participant record form")
        if any(not 0 <= values[n] < 1 << 31 for n in (4, 5)):
            raise ValueError("Invalid Story expense amount")
        if owner not in owner_order:
            owner_order.append(owner)
        units_by_player[owner].append(values[2])
        expenses_by_player[owner] += values[4] + values[5]
    if tuple(owner_order) != player_ids:
        raise ValueError("Story result owner order differs from the authenticated cohort")
    for owner in player_ids:
        units = units_by_player[owner]
        if not 1 <= len(units) <= 2 or sorted(units) != list(range(len(units))):
            raise ValueError("Story result has duplicate or missing owner unit indices")
    mission_id = int(mission[0].value)
    subquests = [field for field in fields if field.number == 4]
    table = STORY_SUBQUEST_PAY.get(mission_id, {})
    if len(subquests) > len(table):
        raise ValueError("Story subquest count exceeds the mission table")
    quest_ids = set()
    bonus = 0
    for quest in subquests:
        if quest.wire_type != 2:
            raise ValueError("Story subquest must be a nested record")
        nested = decode_proto(quest.value)
        if (
            len(nested) != 4
            or {field.number for field in nested} != {1, 2, 3, 4}
            or any(field.wire_type != 0 or not 0 <= field.value < 1 << 32 for field in nested)
        ):
            raise ValueError("Invalid Story subquest record")
        values = {field.number: field.value for field in nested}
        quest_id = values[3]
        # Fields 1/4 are restricted to the captured 1000/0 form. Their broader
        # meaning is not inferred here. Money comes only from the mission table.
        if (
            values[1] != 1000
            or values[2] != mission_id
            or values[4] != 0
            or quest_id not in table
            or quest_id in quest_ids
        ):
            raise ValueError("Unqualified or repeated Story subquest")
        quest_ids.add(quest_id)
        bonus += table[quest_id]
    if (
        any(expenses >= 1 << 31 for expenses in expenses_by_player.values())
        or STORY_BASE_PAY[mission_id] + bonus >= 1 << 31
    ):
        raise ValueError("Story totals exceed the supported signed range")
    return tuple(
        StoryRewardPlan(
            mission_id,
            owner,
            STORY_BASE_PAY[mission_id],
            expenses_by_player[owner],
            len(units_by_player[owner]),
            bonus,
            tuple(sorted(quest_ids)),
        )
        for owner in player_ids
    )


def story_result_owner_ids(payload: bytes) -> tuple[int, ...]:
    """Read the ordered human-owner cohort before live-session authorization."""
    if not isinstance(payload, bytes) or not 0 < len(payload) <= 4096:
        raise ValueError("Invalid Story result size")
    owners = []
    for record in (field for field in decode_proto(payload) if field.number == 3):
        if record.wire_type != 2 or not isinstance(record.value, bytes):
            raise ValueError("Invalid Story participant record")
        fields = [field for field in decode_proto(record.value) if field.number == 1]
        if (
            len(fields) != 1
            or fields[0].wire_type != 0
            or type(fields[0].value) is not int
            or not 0 < fields[0].value < 1 << 32
        ):
            raise ValueError("Story participant has no valid owner")
        if fields[0].value not in owners:
            owners.append(fields[0].value)
    if not 1 <= len(owners) <= 2:
        raise ValueError("Story result requires one or two human owners")
    return tuple(owners)


def story_reward_plan(payload: bytes, player_id: int) -> StoryRewardPlan:
    """Validate the observed single-owner Story/UNAC result form."""
    return story_reward_plans(payload, (player_id,))[0]


def _register_battle_result_response(
    plan: StoryRewardPlan | None = None,
    *,
    items: tuple[dict, ...] = (),
    emblems: tuple[dict, ...] = (),
) -> bytes:
    """Encode 0x03C2's repeated field-1 BattleRewardInfo list.

    The sender owns all qualified units, so base pay is credited once.
    Field 7 supplies full personal pay. The Bonus Pay display requires field-8
    QuestRewardInfo records with field-6 cash RewardInfo entries (type1).
    Field 4 is separate special consideration; using it hid the bonus row in
    the first native candidate. Fields 5/6 are reimbursement, not expenses.
    No top-level QuestRewardInfo field 2 belongs to this Story response.
    """
    if plan is None:
        return b""
    return proto_bytes(1, _story_battle_reward_info(plan, items=items, emblems=emblems))


def _register_story_group_response(
    rewards: tuple[tuple[StoryRewardPlan, tuple[dict, ...], tuple[dict, ...]], ...]
) -> bytes:
    """Encode one BattleRewardInfo per authenticated human result owner."""
    if (
        not isinstance(rewards, tuple)
        or not 1 <= len(rewards) <= 2
        or len({plan.player_id for plan, _items, _emblems in rewards}) != len(rewards)
    ):
        raise ValueError("Story group response requires one or two unique owners")
    return b"".join(
        proto_bytes(1, _story_battle_reward_info(plan, items=items, emblems=emblems))
        for plan, items, emblems in rewards
    )


def _story_battle_reward_info(
    plan: StoryRewardPlan,
    *,
    items: tuple[dict, ...] = (),
    emblems: tuple[dict, ...] = (),
) -> bytes:
    """Encode one owner-filtered Story BattleRewardInfo message."""
    if not isinstance(plan, StoryRewardPlan):
        raise ValueError("Story response requires a reward plan")
    entries = plan.cash_subquests
    if sum(amount for _, amount in entries) != plan.bonus_pay:
        raise ValueError("Story bonus details do not match the payment")
    values = {3: plan.base_pay, 4: 0, 5: 0, 6: 0, 7: plan.gross_pay}
    reward = b"".join(proto_varint(n, value) for n, value in values.items())
    groups = {}
    for quest_id, amount in entries:
        cash = proto_varint(1, 1) + proto_varint(2, amount)
        groups[(1000, plan.mission_id, quest_id)] = [cash]
    for item in items:
        simple = b"".join(
            proto_varint(n, item[key])
            for n, key in enumerate(
                ("category", "part_id", "part_class", "instance_id", "tuning"), 1
            )
        )
        groups.setdefault(tuple(item["condition"]), []).append(
            proto_varint(1, 2) + proto_bytes(4, simple)
        )
    for emblem in emblems:
        identity = emblem["identity"]
        selector = emblem["selector"]
        row_id = emblem["row_id"]
        if (
            type(identity) is not int
            or not 0 <= identity < 1 << 64
            or type(selector) is not int
            or not 0 <= selector < 1 << 32
            or type(row_id) is not int
            or not 0 < row_id < 1 << 32
        ):
            raise ValueError("Story emblem KeyItem values are invalid")
        key_item = proto_varint(1, identity) + proto_varint(2, selector) + proto_varint(3, row_id)
        groups.setdefault(tuple(emblem["condition"]), []).append(
            proto_varint(1, 3) + proto_bytes(3, key_item)
        )
    for (namespace, mission, quest_id), details in groups.items():
        quest = (
            proto_varint(1, namespace)
            + proto_varint(2, mission)
            + proto_varint(3, quest_id)
            + proto_varint(5, 0)
            + b"".join(proto_bytes(6, detail) for detail in details)
        )
        reward += proto_bytes(8, quest)
    reward += proto_varint(9, plan.player_id)
    return reward
