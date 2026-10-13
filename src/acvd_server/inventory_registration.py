"""Register uploaded offline parts before the native cursor read.

An uploaded zero ID is not a part to discard. The native client removes its
local copy and reads the registered copy from the server. Keep the upload
unchanged. A durable receipt supplies the new ID and prevents a second grant.
This module does not recover history or change cash, rank, teams or tuning.
"""

import hashlib
import json
from collections import Counter
from copy import deepcopy

from .inventory import PART_CATEGORY_FOR_CLASS, _offline_inventory_records
from .protocol import ProtocolError, decode_proto, first_field, proto_bytes


def _rows(payload):
    rows = []
    for _, raw in _offline_inventory_records(payload, require_complete=True):
        fields = decode_proto(raw)
        if len(fields) != 5 or {f.number for f in fields} != set(range(1, 6)):
            raise ProtocolError("Import requires five unique ownership fields")
        rows.append(tuple(first_field(fields, n).value for n in range(1, 6)))
    return rows


def _digest(rows):
    # Order and auxiliary uploads do not create a new registration request.
    return hashlib.sha256(json.dumps(sorted(rows), separators=(",", ":")).encode()).hexdigest()


def batches(teams):
    ledger = teams.state.get("inventory_registrations", {})
    if not isinstance(ledger, dict):
        raise ProtocolError("Import registration ledger needs review")
    seen_ids = set()
    for key, batch in ledger.items():
        if (
            not isinstance(batch, dict)
            or not isinstance(batch.get("online_id"), str)
            or type(batch.get("player_id")) is not int
            or not isinstance(batch.get("source_digest"), str)
            or len(batch["source_digest"]) != 64
            or not isinstance(batch.get("items"), list)
            or not 1 <= len(batch["items"]) <= 2980
        ):
            raise ProtocolError("Import registration receipt needs review")
        for item in batch["items"]:
            if (
                not isinstance(item, dict)
                or (item.get("online_id"), item.get("player_id"))
                != (batch["online_id"], batch["player_id"])
                or type(item.get("instance_id")) is not int
                or not 9999 < item["instance_id"] < 1 << 64
                or type(item.get("in_snapshot")) is not bool
                or any(
                    type(item.get(name)) is not int
                    for name in ("category", "part_id", "part_class", "tuning")
                )
            ):
                raise ProtocolError("Import item identity needs review")
            if item["instance_id"] in seen_ids:
                raise ProtocolError("Import ledger repeats an instance ID")
            seen_ids.add(item["instance_id"])
        yield key, batch


def _owned(teams, session):
    return [
        (key, batch)
        for key, batch in batches(teams)
        if (batch["online_id"], batch["player_id"]) == (session.online_id, session.player_id)
    ]


def _pending_placeholders(owned, rows):
    """Count unconfirmed replacements whose new IDs are not yet uploaded."""
    present = {row[3] for row in rows if row[3]}
    return Counter(
        (item["category"], item["part_id"], item["part_class"], 0, item["tuning"])
        for _, batch in owned
        for item in batch["items"]
        if not item["in_snapshot"] and item["instance_id"] not in present
    )


def without_registered_placeholders(teams, session, payload):
    """Replace only placeholders tied to an exact recorded inventory batch.

    The common pending-item layer supplies unconfirmed registrations. Once
    confirmed, normal snapshots own their lifecycle; a stale upload must not
    recreate a part that was subsequently removed.
    """
    owned = _owned(teams, session)
    if not owned:
        return payload
    rows = _rows(payload)
    if not any(row[3] == 0 for row in rows):
        return payload
    digest = _digest(rows)
    if any(batch["source_digest"] == digest for _, batch in owned):
        covered = Counter(row for row in rows if row[3] == 0)
    else:
        # A later upload can confirm one category while another still holds
        # placeholders. Remove only the exact quantities already registered.
        covered = _pending_placeholders(owned, rows)
    output = []
    for field in decode_proto(payload):
        if field.number == 1:
            row = tuple(first_field(decode_proto(field.value), n).value for n in range(1, 6))
            if row[3] == 0 and covered[row]:
                covered[row] -= 1
                continue
        output.append(proto_bytes(field.number, field.value))
    return b"".join(output)


def prepare_registration(teams, session, snapshot, player_data_path):
    """Commit IDs before any readback; recover a missed upload ACK safely.

    The input is the current stored upload, not old history or a client claim.
    Re-entry after restart uses the same receipt. An overlapping unconfirmed
    batch is ambiguous and must not allocate more parts.
    """
    from .owned_items import allocate_instances, reserved_instances

    rows = _rows(snapshot)
    zeros = sorted(row for row in rows if row[3] == 0)
    if not zeros:
        return 0
    if teams.player_profile(session.online_id).get("player_id") != session.player_id:
        raise ProtocolError("Import account identity is not established")
    if not 1 <= len(rows) <= 2980:
        raise ProtocolError("Import exceeds the garage capacity")
    positive = [row[3] for row in rows if row[3]]
    if len(positive) != len(set(positive)):
        raise ProtocolError("Import repeats a registered instance ID")
    for category, part, kind, identity, tune in rows:
        if (
            PART_CATEGORY_FOR_CLASS.get(kind) != category
            or not 0 < part <= 65535
            or not 0 <= identity < 1 << 64
            or not 1000 <= tune <= 1333
        ):
            raise ProtocolError("Import part metadata needs review")
        digits = ((tune - 1000) // 100, (tune // 10) % 10, tune % 10)
        if max(digits) > 3 or sum(digits) > 3:
            raise ProtocolError("Import tuning exceeds three points")
    digest = _digest(rows)
    owned = _owned(teams, session)
    if any(batch["source_digest"] == digest for _, batch in owned):
        return 0
    # A fresh client can upload another partial inventory before it has stored
    # the IDs returned for an earlier batch. Exact metadata multiplicity lets
    # us distinguish the already-pending placeholders from genuinely new
    # zero-ID parts without allocating either one twice.
    uncovered = Counter(zeros) - _pending_placeholders(owned, rows)
    if not uncovered:
        return 0
    zeros = sorted(uncovered.elements())
    # Remove placeholders for capacity accounting: registration replaces a
    # row, it does not buy another part. All other stored rows remain exact.
    registered = b"".join(
        proto_bytes(f.number, f.value)
        for f in decode_proto(snapshot)
        if f.number != 1 or first_field(decode_proto(f.value), 4).value != 0
    )
    identities = allocate_instances(
        teams, session, registered, reserved_instances(player_data_path), len(zeros)
    )
    items = [
        dict(
            online_id=session.online_id,
            player_id=session.player_id,
            category=row[0],
            part_id=row[1],
            part_class=row[2],
            instance_id=identity,
            tuning=row[4],
            in_snapshot=False,
        )
        for row, identity in zip(zeros, identities, strict=True)
    ]
    key = hashlib.sha256(
        json.dumps([session.online_id, session.player_id, digest]).encode()
    ).hexdigest()
    updated = deepcopy(teams.state)
    updated.setdefault("inventory_registrations", {})[key] = dict(
        online_id=session.online_id,
        player_id=session.player_id,
        source_digest=digest,
        items=items,
    )
    teams.commit_state(updated)
    return len(items)
