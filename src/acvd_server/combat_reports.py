"""Bounded combat-result identity parsing; this module does not award rewards.

Native reports can contain several units owned by one player (for example a
human and a UNAC). Count unique players separately from unit records.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .protocol import ProtocolError, decode_proto

MAX_REPORT_BYTES = 4096
MAX_REPORT_UNITS = 64  # Input-work bound, not a claim about native battle capacity.
MAX_REPORT_SUBTARGETS = 64


@dataclass(frozen=True)
class BattleDisconnectReport:
    host_prepare_id: int
    detail32: int  # Field2 meaning is not established.
    detail64: int  # Field3 meaning is not established.
    player_id: int


@dataclass(frozen=True)
class DisconnectedReport:
    reported_id: int  # Both retained native values equal the current preparation ID.
    detail32: int  # Field2 meaning is not established.


def decode_report_disconnected(payload: bytes) -> DisconnectedReport:
    """Decode the two required native scalars without inventing side effects."""
    if not 0 < len(payload) <= 24:
        raise ProtocolError("Disconnected report size is outside the supported bound")
    fields = decode_proto(payload)
    if len(fields) != 2:
        raise ProtocolError("Disconnected report requires two scalar fields")
    values = _scalars(fields, 2)
    if not 0 <= values[0] < 1 << 64 or not 0 <= values[1] < 1 << 32:
        raise ProtocolError("Disconnected report fields exceed their native bounds")
    return DisconnectedReport(*values)


def decode_briefing_end(payload: bytes) -> int:
    """Decode the request's one required uint64 without assigning semantics."""
    if not 0 < len(payload) <= 16:
        raise ProtocolError("Briefing-end report size is outside the supported bound")
    fields = decode_proto(payload)
    if len(fields) != 1 or fields[0].number != 1 or fields[0].wire_type != 0:
        raise ProtocolError("Briefing-end report requires one scalar field")
    value = fields[0].value
    if not isinstance(value, int) or not 0 <= value < 1 << 64:
        raise ProtocolError("Briefing-end report field exceeds its native bound")
    return value


def decode_battle_disconnect(payload: bytes) -> BattleDisconnectReport:
    """Decode the four required native scalars; field4 is not a player list."""
    if not 0 < len(payload) <= 64:
        raise ProtocolError("Battle disconnect size is outside the supported bound")
    fields = decode_proto(payload)
    if len(fields) != 4:
        raise ProtocolError("Battle disconnect requires four scalar fields")
    values = _scalars(fields, 4)
    if (
        not 0 <= values[0] < 1 << 64
        or not 0 <= values[1] < 1 << 32
        or not 0 <= values[2] < 1 << 64
        or not 0 <= values[3] < 1 << 32
    ):
        raise ProtocolError("Battle disconnect fields exceed their native bounds")
    return BattleDisconnectReport(*values)


@dataclass(frozen=True)
class CombatReport:
    header: tuple[int, ...]
    units: tuple[tuple[int, ...], ...]
    subtargets: tuple[tuple[int, int, int], ...]
    digest: str

    @property
    def prepare_id(self) -> int:
        return self.header[0]

    @property
    def player_ids(self) -> frozenset[int]:
        return frozenset(unit[0] for unit in self.units)

    @property
    def side_results(self) -> tuple[int, int]:
        """Return side-0 and side-1 outcomes; native value 2 wins and 1 loses."""
        return self.header[1], self.header[2]

    @property
    def winning_side(self) -> int:
        return 0 if self.header[1] == 2 else 1

    @property
    def destroyed_repair_costs(self) -> tuple[int, int]:
        """Return the report's independently cross-checked destroyed totals."""
        return self.header[3], self.header[4]

    @property
    def expenses_by_player(self) -> dict[int, int]:
        """Sum repair plus ammunition across every retained unit for each owner."""
        expenses: dict[int, int] = {}
        for unit in self.units:
            expenses[unit[0]] = expenses.get(unit[0], 0) + unit[5] + unit[6]
        return expenses

    @property
    def expenses_by_player_control(self) -> dict[int, tuple[int, int]]:
        """Return each owner's human and owned-AI costs separately.

        Native human records use unit reference zero.  Every nonzero reference
        remains a distinct owned nonhuman record, including repeated reference
        values, so no UNAC cost is deduplicated here.
        """
        expenses: dict[int, list[int]] = {}
        for unit in self.units:
            values = expenses.setdefault(unit[0], [0, 0])
            values[0 if unit[1] == 0 else 1] += unit[5] + unit[6]
        return {player_id: (values[0], values[1]) for player_id, values in expenses.items()}

    @property
    def side_by_player(self) -> dict[int, int]:
        return {unit[0]: unit[2] for unit in self.units}

    @property
    def operator_player_ids(self) -> frozenset[int]:
        """Return human owners whose native result record marks Operator duty.

        BattleResultForPlayer field 8 is one only for the player placed in the
        Operator position.  The same two players emit zero in ordinary fighter
        controls.  Nonzero unit references are owned AI records, not operators.
        """
        return frozenset(unit[0] for unit in self.units if unit[1] == 0 and unit[7] == 1)

    @property
    def mercenary_player_ids(self) -> frozenset[int]:
        """Return human owners whose native result role is hired mercenary.

        A public three-player Normal capture correlates unit field 8 value 2
        with the same participant's authenticated RegisterPrepare role 2. The
        mercenary's social team matched the opposing player, while its combat
        side matched the employer, so neither team ID nor side can replace this
        explicit role.
        """
        return frozenset(unit[0] for unit in self.units if unit[1] == 0 and unit[7] == 2)

    @property
    def winning_operator_player_ids(self) -> frozenset[int]:
        """Return operators on the side whose top-level outcome is victory."""
        winner = self.winning_side
        return frozenset(
            unit[0]
            for unit in self.units
            if unit[1] == 0 and unit[7] == 1 and unit[2] == winner
        )

    @property
    def opposing_destructions_by_player(self) -> dict[int, int]:
        """Count only native destroyed-unit records with an explicit opponent.

        Unit fields 10/11 repeatedly identify the defeating player/unit on
        destroyed records. Some native results leave both zero, and solo boss
        reports can identify another unit owned by the same player. Neither
        form authorizes individual leaderboard credit. Count only a known
        participant on the opposing combat side; this may conservatively
        undercount but cannot assign a team kill to the wrong individual.
        """
        sides = self.side_by_player
        result: dict[int, int] = {}
        for unit in self.units:
            defeated_by_player = unit[9]
            if (
                unit[3] != 2
                or defeated_by_player not in sides
                or sides[defeated_by_player] == unit[2]
            ):
                continue
            result[defeated_by_player] = result.get(defeated_by_player, 0) + 1
        return result

    @property
    def subtarget_values_by_player(self) -> dict[int, int]:
        """Sum the third native subtarget scalar without naming it as money."""
        values: dict[int, int] = {}
        for _target_id, player_id, reported_value in self.subtargets:
            values[player_id] = values.get(player_id, 0) + reported_value
        return values


def _scalars(fields, count):
    result = []
    for number in range(1, count + 1):
        values = [field for field in fields if field.number == number]
        if len(values) != 1 or values[0].wire_type != 0:
            raise ProtocolError("Combat report has missing or repeated scalar fields")
        result.append(values[0].value)
    return tuple(result)


def decode_combat_report(payload: bytes) -> CombatReport:
    if not 0 < len(payload) <= MAX_REPORT_BYTES:
        raise ProtocolError("Combat report size is outside the supported bound")
    fields = decode_proto(payload)
    header = _scalars(fields, 8)
    records = [field for field in fields if field.number == 9]
    if not 1 <= len(records) <= MAX_REPORT_UNITS or any(field.wire_type != 2 for field in records):
        raise ProtocolError("Combat report unit list is invalid")
    units = tuple(_scalars(decode_proto(field.value), 12) for field in records)
    subtarget_records = [field for field in fields if field.number == 10]
    if len(subtarget_records) > MAX_REPORT_SUBTARGETS or any(
        field.wire_type != 2 for field in subtarget_records
    ):
        raise ProtocolError("Combat report subtarget list is invalid")
    subtargets = tuple(_scalars(decode_proto(field.value), 3) for field in subtarget_records)
    # Header fields 2/3 are the two side outcomes, not mode IDs. Across the
    # retained native corpus exactly one side wins (2) and one loses (1).
    if (
        not 0 < header[0] < 1 << 64
        or any(not 0 <= header[i] < 1 << 32 for i in (1, 2, 6, 7))
        or set(header[1:3]) != {1, 2}
        or any(not 0 <= header[i] < 1 << 64 for i in (3, 4))
        or header[5] not in (0, 1)
    ):
        raise ProtocolError("Combat report context is invalid")
    if any(
        not 0 < unit[0] < 1 << 32
        or unit[2] not in (0, 1)
        or unit[3] not in (1, 2)
        or unit[8] not in (0, 1)
        or unit[7] not in (0, 1, 2)
        or any(not 0 <= unit[index] < 1 << 31 for index in (1, 4, 5, 6, 10))
        or unit[5] > unit[4]
        or any(not 0 <= unit[index] < 1 << 32 for index in (3, 7, 9, 11))
        for unit in units
    ):
        raise ProtocolError("Combat report unit identity, side, status or costs are invalid")
    sides_by_owner: dict[int, set[int]] = {}
    for unit in units:
        sides_by_owner.setdefault(unit[0], set()).add(unit[2])
    if any(len(sides) != 1 for sides in sides_by_owner.values()):
        raise ProtocolError("Combat report assigns one owner to multiple sides")
    if any(
        not 0 <= target_id < 1 << 32
        or player_id not in sides_by_owner
        or not 0 <= reported_value < 1 << 31
        for target_id, player_id, reported_value in subtargets
    ):
        raise ProtocolError("Combat report subtarget identity or value is invalid")
    destroyed = tuple(
        sum(unit[5] for unit in units if unit[2] == side and unit[3] == 2) for side in (0, 1)
    )
    if header[3:5] != destroyed:
        raise ProtocolError("Combat report destroyed repair totals do not match its units")
    # Field2 is not a unique nonhuman instance key. A captured report has
    # three reference1002 records for one owner, with separate costs/stats.
    # Preserve every record; deduplication would discard real unit results.
    # The human reference0 must still occur at most once for each player.
    humans = [unit[0] for unit in units if unit[1] == 0]
    if len(set(humans)) != len(humans):
        raise ProtocolError("Combat report repeats a human identity")
    return CombatReport(header, units, subtargets, hashlib.sha256(payload).hexdigest())
