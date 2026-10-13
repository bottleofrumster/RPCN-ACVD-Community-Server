"""Owned-item projection and category-aware upload merging.

Pure wire logic: no filesystem or session mutation. Instance IDs identify
individual owned parts. Strict persistence validation and tolerant readback are
intentionally different. Key-item snapshots replace only their known namespace.
"""

from __future__ import annotations

import hashlib
from types import MappingProxyType

from .protocol import (
    ProtocolError,
    ProtoField,
    decode_proto,
    first_field,
    proto_bytes,
    proto_varint,
)

# Owned-record class includes weapon side. Catalogue categories group sides.
PART_CATEGORY_FOR_CLASS = MappingProxyType(
    {
        0: 0,
        1: 1,
        2: 2,
        3: 3,
        4: 4,
        5: 5,
        6: 6,
        9: 13,
        10: 10,
        11: 10,
        12: 11,
        14: 12,
    }
)


def _sync_offline_data_response(*, reject_local_items: bool = False) -> bytes:
    """Tell the client whether server policy rejects locally acquired items."""
    return proto_varint(1, 1 if reject_local_items else 0)


def _offline_inventory_summary(payload: bytes) -> dict[str, int]:
    """Summarize SyncOfflineData without interpreting ownership policy.

    Retail sends repeated field-1 records with five scalar fields. Field 4 is
    the per-instance identifier: zero-valued catalog/placeholders are distinct
    from instantiated owned entries. Keeping both counts in telemetry makes a
    destructive 2,000-entry-to-single-digit transition visible while history
    retention preserves the exact previous bytes for recovery.
    """
    total = instantiated = malformed = auxiliary = 0
    categories: set[int] = set()
    try:
        fields = decode_proto(payload)
    except ProtocolError:
        return {
            "record_count": 0,
            "instantiated_count": 0,
            "category_count": 0,
            "auxiliary_record_count": 0,
            "malformed_count": 1,
        }
    for field in fields:
        if field.number != 1:
            if isinstance(field.value, bytes):
                auxiliary += 1
            else:
                malformed += 1
            continue
        if not isinstance(field.value, bytes):
            malformed += 1
            continue
        try:
            record = decode_proto(field.value)
        except ProtocolError:
            malformed += 1
            continue
        values = [first_field(record, number) for number in range(1, 6)]
        if any(value is None or not isinstance(value.value, int) for value in values):
            malformed += 1
            continue
        total += 1
        categories.add(int(values[0].value))
        if int(values[3].value) != 0:
            instantiated += 1
    return {
        "record_count": total,
        "instantiated_count": instantiated,
        "category_count": len(categories),
        "auxiliary_record_count": auxiliary,
        "malformed_count": malformed,
    }


def _offline_inventory_records(
    payload: bytes, *, require_complete: bool = False
) -> list[tuple[int, bytes]]:
    """Return exact repeated records paired with their authoritative category."""
    result: list[tuple[int, bytes]] = []
    fields = decode_proto(payload)
    for field in fields:
        if field.number != 1:
            continue
        if not isinstance(field.value, bytes):
            raise ProtocolError("SyncOfflineData inventory field is not length-delimited")
        record = decode_proto(field.value)
        category = first_field(record, 1)
        if category is None or not isinstance(category.value, int):
            raise ProtocolError("SyncOfflineData record is missing category field 1")
        if require_complete:
            values = [first_field(record, number) for number in range(1, 6)]
            if any(
                value is None or value.wire_type != 0 or not isinstance(value.value, int)
                for value in values
            ):
                raise ProtocolError("SyncOfflineData record requires five scalar ownership fields")
        result.append((int(category.value), bytes(field.value)))
    return result


def inventory_instance_ids(payload: bytes) -> frozenset[int]:
    """Reserve nonzero IDs from a stored inventory without altering its rows.

    Repeated IDs in another account's legacy snapshot still reserve that ID.
    This is not proof that the snapshot is complete or fit for a new purchase.
    """
    result = set()
    for _, raw in _offline_inventory_records(payload, require_complete=True):
        values = [field for field in decode_proto(raw) if field.number == 4]
        if len(values) != 1 or not 0 <= values[0].value < 1 << 64:
            raise ProtocolError("Stored inventory has an invalid instance ID")
        if values[0].value:
            result.add(values[0].value)
    return frozenset(result)


def decode_owned_parts_claim(claim: bytes) -> tuple[int, int]:
    """Return the native registered count and instance-ID XOR."""
    if not isinstance(claim, bytes) or len(claim) > 32:
        raise ProtocolError("Owned-parts claim exceeds its scalar form")
    fields = decode_proto(claim)
    values = {field.number: field.value for field in fields}
    if (
        len(fields) != 2
        or set(values) != {1, 2}
        or any(field.wire_type != 0 or type(field.value) is not int for field in fields)
        or not 0 <= values[1] <= 0xFFFFFFFF
        or not 0 <= values[2] < 1 << 64
    ):
        raise ProtocolError("Owned-parts claim requires uint32 count and uint64 XOR")
    return values[1], values[2]


def compare_owned_parts(claim: bytes, snapshot: bytes | None) -> dict:
    """Compare 0x041d count/XOR with known ownership, without granting authority.

    Count and XOR are consistency checks, not authentication or metadata proof.
    Missing data is not an empty inventory. Duplicate IDs must not be silently
    normalized: native reports differ between live acquisition and cold load.
    This function does not request deletion, reconstruction, or an upload.
    """
    result = {
        "comparison_status": "invalid_claim",
        "comparison_match": False,
        "proof_scope": "count_and_instance_xor_only",
        "authorizes_purchase": False,
    }
    try:
        claimed_count, claimed_xor = decode_owned_parts_claim(claim)
    except (ProtocolError, TypeError):
        return result
    result.update(claimed_count=claimed_count, claimed_xor=claimed_xor)
    if snapshot is None:
        result["comparison_status"] = "snapshot_missing"
        return result
    result["snapshot_sha256"] = hashlib.sha256(snapshot).hexdigest()
    try:
        _merge_offline_inventory(b"", snapshot)
        rows = _offline_inventory_records(snapshot, require_complete=True)
        identities = []
        for _, record in rows:
            items = [f for f in decode_proto(record) if f.number == 4]
            if len(items) != 1 or not 0 <= items[0].value < 1 << 64:
                raise ProtocolError("Invalid ownership identity")
            identities.append(items[0].value)
    except (ProtocolError, TypeError):
        result["comparison_status"] = "snapshot_invalid"
        return result
    # The native claim covers registered instances, not every stored row.
    # Zero-ID upload rows have no registered identity. Keep their metadata
    # in storage and capacity accounting; do not count them as owned IDs or
    # treat repeated zero as a collision. Sixteen captured checks against
    # exact snapshots prove this distinction (2824 rows, 2734 registered).
    registered = [identity for identity in identities if identity != 0]
    checksum = 0
    for identity in registered:
        checksum ^= identity
    result.update(
        snapshot_count=len(identities),
        registered_instance_count=len(registered),
        snapshot_xor=checksum,
        duplicate_instances=len(registered) - len(set(registered)),
        zero_instances=identities.count(0),
    )
    if result["duplicate_instances"]:
        result["comparison_status"] = "ambiguous_instances"
    else:
        matches = claimed_count == len(registered) and claimed_xor == checksum
        result.update(
            comparison_status="match" if matches else "mismatch", comparison_match=matches
        )
    return result


def _offline_key_item_records(fields: list[ProtoField]) -> list[tuple[int, bytes]]:
    """Validate field-3 pairs and retain their exact nested bytes.

    Retail exports emblems, garage/decor and formations in separate full walks.
    Their namespace prefixes survive projection into the shared wire field.
    See evidence/2026-09-11-key-item-upload-boundary.md. This is not the
    three-field KeyItem readback schema and does not supply its extra identity.
    """
    records: list[tuple[int, bytes]] = []
    for field in fields:
        if field.number != 3:
            continue
        if field.wire_type != 2 or not isinstance(field.value, bytes):
            raise ProtocolError("SyncOfflineData key item must be length-delimited")
        nested = decode_proto(field.value)
        required = [[item for item in nested if item.number == number] for number in (1, 2)]
        if any(len(items) != 1 or items[0].wire_type != 0 for items in required):
            raise ProtocolError("SyncOfflineData key item requires two unique scalar fields")
        namespace = int(required[0][0].value) >> 16
        if namespace not in (2, 3, 10):
            raise ProtocolError("SyncOfflineData key-item namespace requires review")
        records.append((namespace, field.value))
    return records


def key_item_pairs(payload: bytes, selectors: frozenset[int]) -> tuple[tuple[int, int], ...]:
    """Project authenticated two-field uploads into requested key-item ownership."""
    if not isinstance(selectors, frozenset) or not selectors:
        raise ProtocolError("Key-item readback requires requested selectors")
    if any(
        type(selector) is not int
        or not 0 < selector < 1 << 32
        or selector >> 16 not in (2, 3, 9, 10)
        for selector in selectors
    ):
        raise ProtocolError("Key-item selector namespace needs review")
    pairs = []
    for _namespace, raw in _offline_key_item_records(decode_proto(payload)):
        nested = decode_proto(raw)
        values = {field.number: field.value for field in nested}
        selector, row_id = int(values[1]), int(values[2])
        if selector in selectors:
            pairs.append((selector, row_id))
    if len(pairs) != len(set(pairs)):
        raise ProtocolError("Stored key-item ownership repeats a selector/row")
    return tuple(sorted(pairs))


def key_item_list_response(pairs: tuple[tuple[int, int], ...]) -> bytes:
    """Encode repeated required KeyItem messages for opcode 0x03E2."""
    if (
        not isinstance(pairs, tuple)
        or len(pairs) > 2048
        or len(set(pairs)) != len(pairs)
        or any(
            type(selector) is not int
            or type(row_id) is not int
            or not 0 < selector < 1 << 32
            # Native garage/decor snapshots can use row zero. The server
            # already accepts and persists that exact uploaded uint32 value;
            # readback must not reject it and strand the player at Step 2.
            or not 0 <= row_id < 1 << 32
            for selector, row_id in pairs
        )
    ):
        raise ProtocolError("Key-item response is outside its bounds")
    return b"".join(
        proto_bytes(
            1,
            proto_varint(1, 0)
            + proto_varint(2, selector)
            + proto_varint(3, row_id),
        )
        for selector, row_id in pairs
    )


def _merge_offline_inventory(
    existing: bytes, incoming: bytes
) -> tuple[bytes, list[int], list[int]]:
    """Replace present inventory categories and known key-item collections.

    A key-item upload replaces its whole namespace, not only present selectors.
    An absent namespace carries no clear instruction. Other families retain
    their existing whole-family replacement rule.
    """
    incoming_fields = decode_proto(incoming)
    existing_fields = decode_proto(existing) if existing else []
    if any(field.wire_type != 2 for field in incoming_fields + existing_fields):
        raise ProtocolError("SyncOfflineData top-level fields must be length-delimited")
    incoming_numbers = sorted({field.number for field in incoming_fields})
    incoming_records = _offline_inventory_records(incoming, require_complete=True)
    # Corrupt persisted data needs reviewed recovery. Silently dropping an
    # unreadable category while accepting another batch can erase ownership.
    existing_records = (
        _offline_inventory_records(existing, require_complete=True) if existing else []
    )
    incoming_keys = _offline_key_item_records(incoming_fields)
    existing_keys = _offline_key_item_records(existing_fields)
    incoming_key_namespaces = {namespace for namespace, _record in incoming_keys}
    incoming_categories = sorted({category for category, _record in incoming_records})
    result = bytearray()
    for number in sorted({field.number for field in existing_fields} | set(incoming_numbers)):
        if number not in incoming_numbers:
            for field in existing_fields:
                if field.number == number and isinstance(field.value, bytes):
                    result.extend(proto_bytes(number, field.value))
            continue
        if number == 1:
            for category, record in existing_records:
                if category not in incoming_categories:
                    result.extend(proto_bytes(1, record))
            for _category, record in incoming_records:
                result.extend(proto_bytes(1, record))
            continue
        if number == 3:
            for namespace, record in existing_keys:
                if namespace not in incoming_key_namespaces:
                    result.extend(proto_bytes(3, record))
            for _namespace, record in incoming_keys:
                result.extend(proto_bytes(3, record))
            continue
        for field in incoming_fields:
            if field.number == number:
                result.extend(proto_bytes(number, field.value))
    return bytes(result), incoming_categories, incoming_numbers


def _item_simple_info_list(
    start_item_id: int,
    limit: int,
    *,
    offset: int = 0,
    offline_inventory: bytes = b"",
    after_instance_id: int = 0,
) -> bytes:
    """Return the requested owned-item slice.

    ACVD reuses this request for two different stores.  Namespace ``3000`` is
    the login/reconstruction readback of the player's ordinary parts.  The
    five-field ``SyncOfflineData`` records use category in field 1. The
    six-field readback instead uses instance ID in field 1 and server item ID
    in field 2, followed by part ID, class, ownership and tuning. An empty
    readback leaves the reconstructed inventory empty. High namespaces are
    the separately synthesized Territory Custom defense inventory.
    """
    if start_item_id == 3000:
        if not offline_inventory:
            return b""
        try:
            records = []
            for _category, offline_record in _offline_inventory_records(offline_inventory):
                fields = decode_proto(offline_record)
                part_id = first_field(fields, 2)
                item_class = first_field(fields, 3)
                instance_id = first_field(fields, 4)
                tuning = first_field(fields, 5)
                if (
                    part_id is None
                    or not isinstance(part_id.value, int)
                    or item_class is None
                    or not isinstance(item_class.value, int)
                    or instance_id is None
                    or not isinstance(instance_id.value, int)
                    or int(instance_id.value) <= after_instance_id
                    or tuning is None
                    or not isinstance(tuning.value, int)
                ):
                    continue
                server_item_id = start_item_id + len(records)
                # Current Unbound builds parse repeated response field 1 as
                # ACVSmallItemInfo: uint64 instance identity, server item ID,
                # regulation part ID, signed class/side, owned flag, and
                # tuning. Native reconstruction proves this six-field
                # projection preserves the equipped AC, balance, duplicates,
                # inventory population, and tuning display.
                values = (
                    (1, int(instance_id.value)),
                    (2, server_item_id),
                    (3, int(part_id.value)),
                    (4, int(item_class.value)),
                    (5, 1),
                    (6, int(tuning.value)),
                )
                records.append(b"".join(proto_varint(number, value) for number, value in values))
        except ProtocolError:
            return b""
        first = max(0, int(offset))
        selected = records[first:]
        # The reconstruction request uses zero to mean the full canonical
        # list.  Positive values are ordinary bounded pages.
        if limit > 0:
            selected = selected[:limit]
        return b"".join(proto_bytes(1, record) for record in selected)

    # The historical defense-only compatibility record is not a shop offer.
    # Formation selector 0xA0000 belongs to the parameter-backed key-item
    # catalogue; never feed its shop consumer this six-field owned-part shape.
    if not 0x90000 <= start_item_id < 0xA0000 or limit <= 0:
        return b""

    # The same ACVSmallItemInfo response type is used for Territory Custom
    # namespaces. Synthetic defenses have no offline instance identity or
    # tuning record, so their requested namespace ID is a stable identity and
    # server item ID while tuning remains zero.
    item = b"".join(
        proto_varint(number, value)
        for number, value in (
            (1, start_item_id),
            (2, start_item_id),
            (3, 0x0C27),
            (4, 1),
            (5, 1),
            (6, 0),
        )
    )
    return proto_bytes(1, item)
