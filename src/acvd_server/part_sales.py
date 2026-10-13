"""Atomic sale receipts and the owner's removal-history view.

The sale reply contains a removal cursor, not money. A durable receipt owns
both the payment and the removal. Raw uploaded snapshots remain audit data;
all effective ownership views must exclude the recorded sold instances.
"""

from copy import deepcopy

from .inventory import _offline_inventory_records
from .protocol import ProtocolError, decode_proto, first_field, proto_bytes, proto_varint
from .teams import MAX_BALANCE

MAX_U64 = (1 << 64) - 1


def sale_instance(payload: bytes) -> int:
    if not 0 < len(payload) <= 11:
        raise ProtocolError("Sale request size is invalid")
    fields = decode_proto(payload)
    if len(fields) != 1 or fields[0].number != 1 or fields[0].wire_type != 0:
        raise ProtocolError("Sale requires one instance ID")
    value = fields[0].value
    if type(value) is not int or not 9999 < value <= MAX_U64:
        raise ProtocolError("Sale requires a registered instance ID")
    return value


def receipts(teams):
    ledger = teams.state.get("part_sales", {})
    if not isinstance(ledger, dict):
        raise ProtocolError("Sale ledger needs review")
    for row in ledger.values():
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("online_id"), str)
            or type(row.get("player_id")) is not int
            or type(row.get("instance_id")) is not int
            or not 9999 < row["instance_id"] <= MAX_U64
            or type(row.get("cursor")) is not int
            or not 0 < row["cursor"] <= MAX_U64
        ):
            raise ProtocolError("Stored sale identity needs review")
        yield row


def owned_sales(teams, session):
    result = [
        r
        for r in receipts(teams)
        if (r["online_id"], r["player_id"]) == (session.online_id, session.player_id)
    ]
    if len({r["cursor"] for r in result}) != len(result) or len(
        {r["instance_id"] for r in result}
    ) != len(result):
        raise ProtocolError("Sale ledger repeats an owner cursor or instance")
    return result


def trash_page(teams, session, payload: bytes) -> tuple[bytes, int, int]:
    """Read an owner-scoped removal page; no wallet or ownership mutation.

    Native request:1=limit,2=after removal cursor,3=maximum owned instance ID.
    Reply: repeated1=removed instances,2=page cursor,3=view upper bound.
    The client repeats until the two reply cursors are equal.
    """
    if teams.player_profile(session.online_id).get("player_id") != session.player_id:
        raise ProtocolError("Removal history account identity is not established")
    if not 0 < len(payload) <= 32:
        raise ProtocolError("Removal history request size is invalid")
    fields = decode_proto(payload)
    if (
        len(fields) != 3
        or {f.number for f in fields} != {1, 2, 3}
        or any(f.wire_type != 0 for f in fields)
    ):
        raise ProtocolError("Removal history requires three unique scalar fields")
    values = {f.number: f.value for f in fields}
    limit, after, maximum_instance = (values[n] for n in (1, 2, 3))
    if not 1 <= limit <= 512 or not 0 <= after < MAX_U64 or not 0 <= maximum_instance <= MAX_U64:
        raise ProtocolError("Removal history page is outside its bounds")
    rows = sorted(owned_sales(teams, session), key=lambda r: r["cursor"])
    upper = max(after, max((r["cursor"] for r in rows), default=0))
    eligible = [
        r for r in rows if after < r["cursor"] <= upper and r["instance_id"] <= maximum_instance
    ]
    page = eligible[:limit]
    cursor = page[-1]["cursor"] if len(eligible) > limit else upper
    response = b"".join(proto_varint(1, r["instance_id"]) for r in page)
    response += proto_varint(2, cursor) + proto_varint(3, upper)
    return response, cursor, len(page)


def sold_ids(teams, session):
    return frozenset(r["instance_id"] for r in owned_sales(teams, session))


def without_sold_items(teams, session, payload: bytes) -> bytes:
    removed = sold_ids(teams, session)
    if not removed:
        return payload
    output = []
    for field in decode_proto(payload):
        if field.wire_type != 2 or not isinstance(field.value, bytes):
            raise ProtocolError("Inventory must contain record fields")
        if field.number == 1:
            identity = first_field(decode_proto(field.value), 4)
            if identity is None or identity.wire_type != 0:
                raise ProtocolError("Inventory record has no instance ID")
            if identity.value in removed:
                continue
        output.append(proto_bytes(field.number, field.value))
    return b"".join(output)


def replay_sale(teams, session, instance: int) -> bytes | None:
    if teams.player_profile(session.online_id).get("player_id") != session.player_id:
        raise ProtocolError("Sale account identity is not established")
    matches = [r for r in owned_sales(teams, session) if r["instance_id"] == instance]
    if len(matches) > 1:
        raise ProtocolError("Sale ledger repeats an instance")
    return proto_varint(1, matches[0]["cursor"]) if matches else None


def sell(
    teams,
    session,
    instance: int,
    *,
    inventory: bytes,
    credit: int,
    price_source: str,
    cursor_floor: int = 0,
) -> tuple[bytes, bool]:
    """Commit once, before the reply. Call only with verified price/ownership."""
    previous = replay_sale(teams, session, instance)
    if previous is not None:
        return previous, True
    if type(instance) is not int or not 9999 < instance <= MAX_U64:
        raise ProtocolError("Sale requires a registered instance ID")
    if type(credit) is not int or not 0 <= credit <= MAX_BALANCE or not price_source:
        raise ProtocolError("Sale price is not qualified")
    if type(cursor_floor) is not int or not 0 <= cursor_floor < MAX_U64:
        raise ProtocolError("Sale cursor is invalid")
    rows = []
    for _, raw in _offline_inventory_records(inventory, require_complete=True):
        fields = decode_proto(raw)
        if first_field(fields, 4).value == instance:
            rows.append([first_field(fields, n).value for n in range(1, 6)])
    if len(rows) != 1:
        raise ProtocolError("Sale requires exactly one owned instance")
    balance = teams.player_profile(session.online_id).get("balance")
    if type(balance) is not int or not -MAX_BALANCE <= balance <= MAX_BALANCE:
        raise ProtocolError("Sale requires a known wallet")
    if balance + credit > MAX_BALANCE:
        # Do not remove the item if the full quoted credit cannot be retained.
        raise ProtocolError("Sale credit would exceed the supported wallet bound")
    cursor = (
        max(cursor_floor, max((r["cursor"] for r in owned_sales(teams, session)), default=0)) + 1
    )
    if cursor > MAX_U64:
        raise ProtocolError("Sale cursor is exhausted")
    row = dict(zip(("category", "part_id", "part_class", "instance_id", "tuning"), rows[0]))
    row.update(
        online_id=session.online_id,
        player_id=session.player_id,
        cursor=cursor,
        credit=credit,
        price_source=price_source,
        balance_before=balance,
        balance_after=balance + credit,
    )
    updated = deepcopy(teams.state)
    # A sold instance cannot be sold again in another login or retry window.
    key = f"{session.player_id}:{instance}"
    if key in updated.get("part_sales", {}):
        raise ProtocolError("Sale identity conflicts with an existing receipt")
    updated.setdefault("part_sales", {})[key] = row
    updated["player_profiles"][session.online_id]["balance"] = balance + credit
    teams.commit_state(updated)
    return proto_varint(1, cursor), False
