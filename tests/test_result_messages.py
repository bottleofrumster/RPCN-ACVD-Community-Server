from datetime import datetime, timezone

import pytest

from acvd_server.protocol import decode_proto
from acvd_server.result_messages import BATTLE_RESULT_FIXED_PUSH, battle_result_fixed_push


def test_battle_result_fixed_is_exact_initialized_signal():
    payload = battle_result_fixed_push(datetime(2026, 9, 27, 17, 0, 1, tzinfo=timezone.utc))
    fields = decode_proto(payload)
    assert BATTLE_RESULT_FIXED_PUSH == 6
    assert [(field.number, field.wire_type, field.value) for field in fields[:4]] == [
        (1, 0, 0),
        (2, 0, 0),
        (3, 0, 0),
        (4, 0, 0),
    ]
    date = {field.number: field.value for field in decode_proto(fields[4].value)}
    assert date == {1: 2026, 2: 9, 3: 27, 4: 17, 5: 0, 6: 1, 7: 0}


def test_battle_result_fixed_rejects_naive_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        battle_result_fixed_push(datetime(2026, 9, 27))
