"""Combat report structure and unit/player counts, without reward assumptions."""

from pathlib import Path

import pytest

from acvd_server.combat_reports import (
    decode_battle_disconnect,
    decode_briefing_end,
    decode_combat_report,
    decode_report_disconnected,
)
from acvd_server.protocol import ProtocolError, proto_bytes, proto_varint


def payload(players=(11, 22), *, key=1, field2=1, extra_units=()):
    head = {1: key, 2: field2, 3: 2, 4: 0, 5: 0, 6: 0, 7: 0, 8: 0}
    result = b"".join(proto_varint(k, value) for k, value in head.items())
    for player_id, unit_id in [*((p, 0) for p in players), *extra_units]:
        values = {i: 0 for i in range(1, 13)}
        values[1], values[2] = player_id, unit_id
        values[4] = 1  # Native active/surviving unit status.
        result += proto_bytes(9, b"".join(proto_varint(i, v) for i, v in values.items()))
    return result


def test_report_counts_unique_humans_separately_from_their_units():
    # Same structure as the reported two-human Normal battle with one UNAC.
    record = decode_combat_report(payload(extra_units=((11, 6),)))
    assert record.player_ids == {11, 22}
    assert len(record.units) == 3 and record.prepare_id == 1


def test_repeated_nonhuman_reference_preserves_each_native_unit_record():
    # Captured 2026-09-12T23:17:05Z: three units share reference1002.
    # Their costs/statistics differ. Only the owner ID is anonymized here.
    header = (4, 2, 1, 0, 331600, 0, 0, 0)
    units = (
        (11, 0, 1, 2, 146200, 146200, 12900, 0, 1, 0, 0, 6),
        (11, 1002, 1, 2, 61800, 61800, 10680, 0, 0, 0, 0, 6),
        (11, 1002, 1, 2, 61800, 61800, 10560, 0, 0, 0, 0, 6),
        (11, 1002, 1, 2, 61800, 61800, 10320, 0, 0, 449624, 1002, 6),
    )
    raw = b"".join(proto_varint(n, v) for n, v in enumerate(header, 1))
    raw += b"".join(
        proto_bytes(9, b"".join(proto_varint(n, v) for n, v in enumerate(unit, 1)))
        for unit in units
    )
    report = decode_combat_report(raw)
    assert report.player_ids == {11} and report.units == units
    assert report.header[4] == sum(unit[4] for unit in report.units)
    assert report.expenses_by_player_control == {11: (159100, 216960)}


def test_repeated_nonhuman_reference_does_not_merge_identical_records():
    report = decode_combat_report(payload(extra_units=((11, 1002), (11, 1002))))
    assert len(report.units) == 4 and report.units[2] == report.units[3]


@pytest.mark.parametrize(
    "invalid",
    [
        b"",
        b"a" * 4097,
        payload(players=()),
        payload(extra_units=((11, 0),)),
        payload(field2=1 << 32),
        payload(key=0),
        payload(key=1 << 64),
        payload(players=(0,)),
        payload(players=tuple(range(1, 66))),
    ],
)
def test_invalid_reports_cannot_become_completion_receipts(invalid):
    with pytest.raises(ProtocolError):
        decode_combat_report(invalid)


def test_native_normal_result_does_not_assign_a_mode_to_fields_2_and_3():
    # Exact 82-byte report from private room 87, 2026-09-13T02:11:48Z.
    raw = bytes.fromhex(
        "080110021801200028a0c803300038004000"
        "4a1e08c38b0310001800200128c0b60b30f5fb03380040004800500058006000"
        "4a1e0891c71b10001801200228a0c80330a0c803380040004800500058006001"
    )
    report = decode_combat_report(raw)
    assert len(raw) == 82 and report.prepare_id == 1
    assert report.header[1:3] == (2, 1)
    assert report.player_ids == {50627, 451473}
    assert report.side_results == (2, 1) and report.winning_side == 0
    assert report.destroyed_repair_costs == (0, 58400)
    assert report.side_by_player == {50627: 0, 451473: 1}
    assert report.expenses_by_player == {50627: 65013, 451473: 58400}
    assert report.expenses_by_player_control == {50627: (65013, 0), 451473: (58400, 0)}
    assert report.subtargets == () and report.subtarget_values_by_player == {}


def test_native_four_player_special_preserves_subtarget_records():
    raw = bytes.fromhex(
        "08021001180220002800300038004000"
        "4a1e08d17710001801200128a8ea0d30fe8f0438804b40004800500058006006"
        "4a1c08c38b0310001801200128c0b60b3000380040004801500058006000"
        "4a1e08c38b0310021801200128c8ab0b300038b4bf0140004800500058006006"
        "4a1d08c6f10610001801200128e8ec08300038800f40004800500058006006"
        "520a080110c38b0318c0a907"
        "520a080710c6f10618c0a907"
    )
    report = decode_combat_report(raw)
    assert len(raw) == 165
    assert report.subtargets == ((1, 50627, 120000), (7, 112838, 120000))
    assert report.subtarget_values_by_player == {50627: 120000, 112838: 120000}


@pytest.mark.parametrize(
    "subtarget",
    [
        proto_varint(1, 1) + proto_varint(2, 99) + proto_varint(3, 120000),
        proto_varint(1, 1) + proto_varint(2, 11),
        proto_varint(1, 1)
        + proto_varint(2, 11)
        + proto_varint(3, 120000)
        + proto_varint(3, 120000),
        proto_varint(1, 1) + proto_varint(2, 11) + proto_bytes(3, b"wrong"),
    ],
)
def test_invalid_subtarget_records_cannot_enter_reward_policy(subtarget):
    with pytest.raises(ProtocolError):
        decode_combat_report(payload(players=(11,)) + proto_bytes(10, subtarget))


def encoded_report(header, units):
    raw = b"".join(proto_varint(number, value) for number, value in enumerate(header, 1))
    return raw + b"".join(
        proto_bytes(
            9, b"".join(proto_varint(number, value) for number, value in enumerate(unit, 1))
        )
        for unit in units
    )


@pytest.mark.parametrize(
    "header,units",
    [
        # Destroyed side-1 repair does not match top-level field 5.
        ((1, 2, 1, 0, 0, 0, 0, 0), ((11, 0, 1, 2, 100, 100, 0, 0, 0, 0, 0, 0),)),
        # Incurred repair cost exceeds the unit's maximum repair cost.
        ((1, 2, 1, 0, 0, 0, 0, 0), ((11, 0, 0, 1, 99, 100, 0, 0, 0, 0, 0, 0),)),
        # One owner cannot belong to both opposing sides in one aggregate.
        (
            (1, 2, 1, 0, 0, 0, 0, 0),
            (
                (11, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0),
                (11, 6, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0),
            ),
        ),
        # Native outcome values must contain one winner and one loser.
        ((1, 2, 2, 0, 0, 0, 0, 0), ((11, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0),)),
        # Participant field 8 is fighter0, operator1, or hired mercenary2.
        ((1, 2, 1, 0, 0, 0, 0, 0), ((11, 0, 0, 1, 0, 0, 0, 3, 0, 0, 0, 0),)),
    ],
)
def test_contradictory_cost_side_or_outcome_claims_are_rejected(header, units):
    with pytest.raises(ProtocolError):
        decode_combat_report(encoded_report(header, units))


def test_native_operator_remains_a_participant_with_zero_costs():
    # Both clients sent this exact report after member02 used the Operator UI.
    # Field8 differs from the earlier fighter record; do not infer a complete
    # field schema or a registration role from one observed discriminator.
    raw = bytes.fromhex((Path(__file__).parent / "fixtures/operator-combat-result.hex").read_text())
    record = decode_combat_report(raw)
    assert len(raw) == 78 and record.prepare_id == 2
    assert record.player_ids == {50627, 451473}
    assert record.units[1] == (451473, 0, 1, 1, 0, 0, 0, 1, 0, 0, 0, 0)
    assert record.operator_player_ids == {451473}
    # The captured boss result is a loss for side 1, so it is not an
    # Operator-win medal event merely because the Operator survived.
    assert record.winning_operator_player_ids == set()


def test_native_fighter_records_do_not_claim_operator_duty():
    raw = bytes.fromhex(
        "080210021801200028a0c803300038004000"
        "4a1e08c38b0310001800200128c0b60b30bebe03380040004800500058006000"
        "4a1e0891c71b10001801200228a0c80330a0c803380040004800500058006001"
    )
    record = decode_combat_report(raw)
    assert record.operator_player_ids == set() and record.mercenary_player_ids == set()
    assert record.winning_operator_player_ids == set()


def test_public_three_player_normal_result_identifies_hired_mercenary_role():
    raw = bytes.fromhex(
        "080a1001180220d8e00b2800300038004000"
        "4a1b08d17710001801200128e8ed103000380a40024800500058006002"
        "4a1f08fdf60310001801200128c0ee0930a1f20638b82640004800500058006001"
        "4a2008b99b0f10001800200228d8e00b30d8e00b38004000480050fdf60358006000"
        "520a080110fdf60318c0a907"
        "5209080210d17718c0a907"
        "5209080310d1771880f104"
    )
    report = decode_combat_report(raw)
    assert len(raw) == 148 and report.prepare_id == 10
    assert report.side_results == (1, 2) and report.winning_side == 1
    assert report.player_ids == {15313, 64381, 249273}
    assert report.mercenary_player_ids == {15313}
    assert report.operator_player_ids == set()
    assert report.side_by_player == {15313: 1, 64381: 1, 249273: 0}
    assert report.expenses_by_player_control == {
        15313: (10, 0),
        64381: (117849, 0),
        249273: (192600, 0),
    }
    assert report.subtargets == (
        (1, 64381, 120000),
        (2, 15313, 120000),
        (3, 15313, 80000),
    )
    assert report.opposing_destructions_by_player == {64381: 1}


def test_unattributed_and_same_side_destructions_do_not_credit_a_player():
    header = (4, 2, 1, 0, 123, 0, 0, 0)
    units = (
        (11, 0, 0, 1, 100, 0, 0, 0, 0, 0, 0, 0),
        (22, 0, 1, 2, 123, 123, 0, 0, 0, 0, 0, 0),
    )
    report = decode_combat_report(encoded_report(header, units))
    assert report.opposing_destructions_by_player == {}


def test_native_disconnect_preserves_host_key_and_opaque_details():
    report = decode_battle_disconnect(bytes.fromhex("0803100018808080809080c080800120c38b03"))
    assert report.host_prepare_id == 3 and report.player_id == 50627
    assert report.detail32 == 0 and report.detail64 == 9223653516126453760


@pytest.mark.parametrize(
    "raw,reported_id",
    [
        (bytes.fromhex("08001000"), 0),
        (bytes.fromhex("08351000"), 53),
        (bytes.fromhex("08481000"), 72),
    ],
)
def test_native_report_disconnected_preserves_reported_id_and_detail(raw, reported_id):
    report = decode_report_disconnected(raw)
    assert report.reported_id == reported_id and report.detail32 == 0


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"x" * 25,
        b"\xff",
        proto_varint(1, 1),
        proto_varint(1, 1) + proto_varint(1, 2) + proto_varint(2, 0),
        proto_bytes(1, b"wrong") + proto_varint(2, 0),
        proto_varint(1, 1 << 64) + proto_varint(2, 0),
        proto_varint(1, 1) + proto_varint(2, 1 << 32),
    ],
)
def test_report_disconnected_rejects_invalid_native_fields(raw):
    with pytest.raises(ProtocolError):
        decode_report_disconnected(raw)


@pytest.mark.parametrize("value", [0, 2, 3, 56, 75, (1 << 64) - 1])
def test_briefing_end_preserves_opaque_native_uint64(value):
    assert decode_briefing_end(proto_varint(1, value)) == value


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"x" * 17,
        b"\xff",
        proto_varint(2, 1),
        proto_varint(1, 1) + proto_varint(1, 2),
        proto_bytes(1, b"wrong"),
        proto_varint(1, 1 << 64),
    ],
)
def test_briefing_end_rejects_missing_duplicate_wrong_type_and_out_of_range_fields(raw):
    with pytest.raises(ProtocolError):
        decode_briefing_end(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"x" * 65,
        b"\xff",
        proto_varint(4, 11),
        bytes.fromhex("080110001800200b") + proto_varint(4, 22),
        proto_bytes(1, b"wrong") + bytes.fromhex("10001800200b"),
        proto_varint(1, 1) + proto_varint(2, 1 << 32) + bytes.fromhex("1800200b"),
        bytes.fromhex("080110001800") + proto_varint(4, 1 << 32),
    ],
)
def test_disconnect_rejects_incomplete_duplicate_and_out_of_range_fields(raw):
    with pytest.raises(ProtocolError):
        decode_battle_disconnect(raw)


def test_native_zero_target_is_not_an_invalid_wire_record():
    report = decode_battle_disconnect(bytes.fromhex("0811100018808080809080c08080012000"))
    assert report.host_prepare_id == 17 and report.player_id == 0
