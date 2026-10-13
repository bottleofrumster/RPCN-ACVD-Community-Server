"""Proved World-result medal facts without persistence or award side effects.

The client report identifies an Operator and its winning side.  Original medal
captions define tiered Operator-win thresholds at 3, 5, and 10.  Match phase,
practice classification, replay receipts, dates, and durable ownership remain
the settlement layer's responsibility; this module must not award by itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from .combat_reports import CombatReport

OPERATOR_MEDAL_THRESHOLDS = ((26, 3), (27, 5), (28, 10))


@dataclass(frozen=True, slots=True)
class OperatorWinAdvance:
    previous_wins: int
    current_wins: int
    newly_eligible_medal_ids: tuple[int, ...]


def advance_operator_wins(
    report: CombatReport, *, player_id: int, previous_wins: int
) -> OperatorWinAdvance:
    """Advance one verified aggregate once when this player won as Operator.

    The caller must first bind the report to its authenticated retained match,
    exclude grace/practice contexts, and enforce a durable result receipt.
    """
    if not isinstance(report, CombatReport):
        raise ValueError("Operator medal progress requires a decoded combat report")
    if type(player_id) is not int or not 0 < player_id < 1 << 32:
        raise ValueError("Operator medal progress requires a valid player ID")
    if type(previous_wins) is not int or not 0 <= previous_wins < 1 << 32:
        raise ValueError("Operator win progress must be a uint32 value")
    if player_id not in report.winning_operator_player_ids:
        return OperatorWinAdvance(previous_wins, previous_wins, ())
    if previous_wins == (1 << 32) - 1:
        raise ValueError("Operator win progress cannot overflow")
    current = previous_wins + 1
    awards = tuple(
        medal_id
        for medal_id, threshold in OPERATOR_MEDAL_THRESHOLDS
        if previous_wins < threshold <= current
    )
    return OperatorWinAdvance(previous_wins, current, awards)
