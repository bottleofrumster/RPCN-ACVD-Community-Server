"""Retail post-battle completion messages independent of reward calculation."""

from __future__ import annotations

from datetime import datetime, timezone

from .protocol import _proto_datetime, proto_bytes, proto_varint

BATTLE_RESULT_FIXED_PUSH = 6


def battle_result_fixed_push(now: datetime | None = None) -> bytes:
    """Encode the initialized PushRequestBattleResultFixed signal.

    The owned client proves wire subtype five (one-based server subtype six),
    required fields 1-4, and a required DateTime field 5.  Its concrete
    application callback ignores every payload value and clears only the
    post-battle wait flag.  Keep the unknown scalars at initialized zero rather
    than inventing battle IDs or rank values.
    """
    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("Battle-result-fixed time must be timezone-aware")
    return (
        proto_varint(1, 0)
        + proto_varint(2, 0)
        + proto_varint(3, 0)
        + proto_varint(4, 0)
        + proto_bytes(5, _proto_datetime(observed))
    )
