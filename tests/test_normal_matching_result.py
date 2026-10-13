"""Protect the shared battle type and separate roles observed in Normal room 13."""

import pytest

from acvd_server.match_messages import _matching_result_for_members
from acvd_server.protocol import decode_proto


@pytest.mark.parametrize("is_host,role", [(True, 1), (False, 0)])
def test_normal_result_uses_shared_extermination_type_with_separate_roles(is_host, role):
    payload = _matching_result_for_members(
        {"prepare_id": 3},
        {"prepare_id": 4},
        criteria={8: 2},
        is_host=is_host,
    )
    fields = {field.number: field.value for field in decode_proto(payload)}
    assert fields == {1: 3, 2: 4, 3: role, 4: 1, 5: role, 6: 0, 7: 0, 8: 0, 9: 1}


@pytest.mark.parametrize("is_host", [True, False])
@pytest.mark.parametrize("area_id", range(1, 8))
def test_normal_result_changes_only_the_shared_region(is_host, area_id):
    args = ({"prepare_id": 3}, {"prepare_id": 4})
    baseline = decode_proto(
        _matching_result_for_members(
            *args,
            criteria={8: 2},
            is_host=is_host,
        )
    )
    candidate = decode_proto(
        _matching_result_for_members(
            *args,
            criteria={8: 2},
            is_host=is_host,
            combat_area_id=area_id,
        )
    )
    changes = [
        (old.number, old.value, new.value)
        for old, new in zip(baseline, candidate, strict=True)
        if old != new
    ]
    assert changes == [(6, 0, area_id)]


@pytest.mark.parametrize("area_id", [-1, 8, True, "3", None])
def test_normal_result_rejects_invalid_explicit_regions(area_id):
    with pytest.raises(ValueError, match="combat area"):
        _matching_result_for_members(
            {"prepare_id": 3},
            {"prepare_id": 4},
            criteria={},
            is_host=True,
            combat_area_id=area_id,
        )


@pytest.mark.parametrize("owner", [0, 50627, 2**32 - 1])
def test_layout_owner_changes_only_field_seven(owner):
    args = ({"prepare_id": 3}, {"prepare_id": 4})
    fields = {
        f.number: f.value
        for f in decode_proto(
            _matching_result_for_members(
                *args, criteria={}, is_host=False, combat_area_id=3, territory_layout_owner_id=owner
            )
        )
    }
    assert fields == {1: 3, 2: 4, 3: 0, 4: 1, 5: 0, 6: 3, 7: owner, 8: 0, 9: 1}


@pytest.mark.parametrize("owner", [-1, 2**32, True, "50627", None])
def test_layout_owner_rejects_invalid_values(owner):
    with pytest.raises(ValueError, match="territory layout owner"):
        _matching_result_for_members(
            {"prepare_id": 3},
            {"prepare_id": 4},
            criteria={},
            is_host=True,
            territory_layout_owner_id=owner,
        )


@pytest.mark.parametrize("is_host", [True, False])
def test_cross_faction_classification_changes_only_field_nine(is_host):
    arguments = ({"prepare_id": 3}, {"prepare_id": 4})
    old = decode_proto(_matching_result_for_members(*arguments, criteria={}, is_host=is_host))
    new = decode_proto(
        _matching_result_for_members(
            *arguments, criteria={}, is_host=is_host, is_intra_faction=False
        )
    )
    assert [(a.number, a.value, b.value) for a, b in zip(old, new, strict=True) if a != b] == [
        (9, 1, 0)
    ]


@pytest.mark.parametrize("value", [0, 1, None, "false"])
def test_classification_requires_a_real_boolean(value):
    with pytest.raises(ValueError, match="classification"):
        _matching_result_for_members(
            {"prepare_id": 3}, {"prepare_id": 4}, criteria={}, is_host=True, is_intra_faction=value
        )
