"""Proved Operator-win medal facts, still disconnected from live awards."""

from pathlib import Path

import pytest

from acvd_server.combat_reports import decode_combat_report
from acvd_server.protocol import proto_bytes, proto_varint
from acvd_server.world_medal_rules import advance_operator_wins


def encoded_report(header, units):
    raw = b"".join(proto_varint(number, value) for number, value in enumerate(header, 1))
    return raw + b"".join(
        proto_bytes(
            9, b"".join(proto_varint(number, value) for number, value in enumerate(unit, 1))
        )
        for unit in units
    )


def operator_loss():
    return decode_combat_report(
        bytes.fromhex((Path(__file__).parent / "fixtures/operator-combat-result.hex").read_text())
    )


def operator_win():
    loss = operator_loss()
    return decode_combat_report(encoded_report((2, 1, 2, *loss.header[3:]), loss.units))


@pytest.mark.parametrize(
    "previous,current,awards",
    [(0, 1, ()), (2, 3, (26,)), (4, 5, (27,)), (9, 10, (28,)), (10, 11, ())],
)
def test_operator_win_crosses_only_original_thresholds(previous, current, awards):
    result = advance_operator_wins(operator_win(), player_id=451473, previous_wins=previous)
    assert (result.previous_wins, result.current_wins, result.newly_eligible_medal_ids) == (
        previous,
        current,
        awards,
    )


def test_loss_fighter_and_other_player_do_not_advance():
    loss = operator_loss()
    assert advance_operator_wins(loss, player_id=451473, previous_wins=2).current_wins == 2
    assert advance_operator_wins(operator_win(), player_id=50627, previous_wins=2).current_wins == 2


@pytest.mark.parametrize(
    "player,wins",
    [(0, 0), (True, 0), (1 << 32, 0), (1, -1), (1, True), (1, 1 << 32)],
)
def test_invalid_identity_or_counter_is_rejected(player, wins):
    with pytest.raises(ValueError):
        advance_operator_wins(operator_win(), player_id=player, previous_wins=wins)
