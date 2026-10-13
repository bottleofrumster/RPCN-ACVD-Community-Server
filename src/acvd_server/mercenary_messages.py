"""Mercenary search, ranking and contract-message encoders.

Only encode supplied registrations/messages. Eligibility, expiry, negotiation
and actual RPCN readiness are separate lifecycle concerns. Empty ranking
responses do not establish a full ranking implementation."""

from __future__ import annotations

from .protocol import decode_proto, first_field, proto_bytes, proto_varint


def _mercenary_rank_response(rank: int = 0) -> bytes:
    """Required current mercenary rank; zero is the new-registration F rank."""
    return proto_varint(1, max(0, rank))


def _mercenary_ranking_info(
    online_id: str,
    team_name: str,
    *,
    player_id: int = 0,
    rank: int = 0,
    score: int = 0,
) -> bytes:
    """Player identity (1), signed score (5), and leaderboard position (6).

    Mercenary grade is a separate response, not the leaderboard score. See
    evidence/2026-10-04-mercenary-ranking-wire.md for the reported UI/log control.
    """
    return (
        proto_varint(1, max(0, int(player_id)))
        + proto_bytes(2, online_id)
        + proto_bytes(4, team_name)
        + proto_varint(5, min((1 << 31) - 1, max(0, int(score))))
        + proto_varint(6, max(0, int(rank)))
    )


def _mercenary_continuation_pairs(payload: bytes) -> list[tuple[int, int]]:
    """Decode RequestSendMercenaryMessage's repeated two-scalar records."""
    pairs: list[tuple[int, int]] = []
    for field in decode_proto(payload):
        if field.number != 1 or not isinstance(field.value, bytes):
            continue
        nested = decode_proto(field.value)
        first = first_field(nested, 1)
        second = first_field(nested, 2)
        if (
            first is not None
            and isinstance(first.value, int)
            and second is not None
            and isinstance(second.value, int)
        ):
            pairs.append((int(first.value), int(second.value)))
    return pairs


def _mercenary_self_ranking_response(
    online_id: str, team_name: str, row: dict | None = None, *, player_id: int = 0
) -> bytes:
    row = row or {}
    return proto_bytes(
        1,
        _mercenary_ranking_info(
            online_id,
            team_name,
            player_id=row.get("player_id", player_id),
            rank=row.get("rank", 0),
            score=row.get("score", 0),
        ),
    )


def _mercenary_ranking_list_response(
    rows: tuple[dict, ...] = (), total: int = 0
) -> bytes:
    return b"".join(
        proto_bytes(
            1,
            _mercenary_ranking_info(
                row["online_id"],
                row["team_name"],
                player_id=row["player_id"],
                rank=row["rank"],
                score=row["score"],
            ),
        )
        for row in rows
    ) + proto_varint(2, total)


def _mercenary_search_result(registration: dict, team: dict | None) -> bytes:
    """Initialized retail MercenarySearchResultInfo for an available pilot.

    Retail requires fields 1 through 10. Fields 2-4 are length-delimited,
    field 9 is an initialized MercenaryRankingInfo, and the rest are scalars.
    """
    online_id = str(registration.get("online_id", ""))
    # Preserve the legacy coercion while this response's team-name slot is unresolved.
    _team_name = str((team or {}).get("name", ""))
    ac_data = registration.get("ac_data", b"")
    if not isinstance(ac_data, bytes):
        ac_data = b""
    values = [int(value) for value in registration.get("result_values", [])[:4]]
    values.extend([0] * (4 - len(values)))
    return (
        proto_varint(1, int(registration.get("player_id", 0)))
        + proto_bytes(2, online_id)
        + proto_bytes(3, online_id)
        + proto_bytes(4, ac_data)
        + b"".join(proto_varint(number, values[number - 5]) for number in range(5, 9))
        + proto_bytes(
            9,
            proto_varint(1, 0) + b"".join(proto_varint(number, 0) for number in range(2, 8)),
        )
        + proto_varint(10, 0)
    )


def _mercenary_search_response(entries: list[tuple[dict, dict | None]]) -> bytes:
    return b"".join(
        proto_bytes(1, _mercenary_search_result(registration, team))
        for registration, team in entries
    )


def _mercenary_message(message: dict) -> bytes:
    matching_info = (
        proto_varint(1, int(message.get("employer_player_id", 0)))
        + proto_varint(2, int(message.get("target_player_id", 0)))
        + b"".join(proto_varint(number, 0) for number in range(3, 8))
    )
    return (
        proto_varint(1, int(message.get("message_id", 0)))
        + proto_bytes(2, matching_info)
        + proto_bytes(3, str(message.get("employer_online_id", "")))
        + proto_bytes(4, str(message.get("employer_team_name", "")))
        + proto_bytes(5, "Hire request")
        + proto_bytes(7, str(message.get("target_online_id", "")))
    )


def _mercenary_message_list_response(messages: list[dict]) -> bytes:
    return b"".join(proto_bytes(1, _mercenary_message(message)) for message in messages)
