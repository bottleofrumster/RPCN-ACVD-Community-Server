"""Pending part ownership shared by purchases and mission rewards.

The durable source receipt owns an item until a matching upload is stored.
Later disposal must not cause a confirmed item to return.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .inventory import (
    _offline_inventory_records,
    compare_owned_parts,
    decode_owned_parts_claim,
    inventory_instance_ids,
)
from .inventory_registration import batches, without_registered_placeholders
from .part_sales import receipts as sale_receipts, sold_ids, without_sold_items
from .player_storage import _requested_offline_snapshot
from .protocol import ProtocolError, decode_proto, first_field, proto_bytes, proto_varint

if TYPE_CHECKING:
    from .teams import TeamStore
    from .udp import GameSession

MAX_INSTANCE_ID = (1 << 64) - 1
# Retail 0x002142c8 returns zero for IDs <= 9999. Predicate 0x00214a44
# also rejects that range. Do not allocate local/special IDs as new ordinary
# owned parts, including when a player has no registered inventory yet.
MIN_INSTANCE_ID = 10000
# GetGarageCapacity (0x00447174 -> 0x0042f408) returns 2980. The native
# occupancy predicate counts owned and carry-over entries. A network-instance
# list is not automatically that full count. This is a necessary upper bound,
# not a remaining-slot count.
MAX_REGISTERED_PARTS = 2980
MAX_INVENTORY_SNAPSHOT_BYTES = 16 * 1024 * 1024


class OwnershipError(ProtocolError):
    """Known ownership cannot be changed safely with the supplied inputs."""


@dataclass(frozen=True, slots=True)
class PurchaseInventoryAuthority:
    inventory: bytes
    reservations: frozenset[int]
    capacity_floor: int
    cursor_floor: int
    mode: str


def _records(payload: bytes) -> list[tuple[int, int, int, int, int]]:
    result = []
    for _, raw in _offline_inventory_records(payload, require_complete=True):
        fields = decode_proto(raw)
        values = tuple(int(first_field(fields, n).value) for n in range(1, 6))
        if not 0 <= values[3] <= MAX_INSTANCE_ID:
            raise OwnershipError("Inventory instance ID is outside uint64")
        result.append(values)
    return result


def all_receipts(teams: TeamStore):
    """Yield durable owned items and their mutation paths, regardless of source."""
    for key, batch in batches(teams):
        for index, item in enumerate(batch["items"]):
            yield ("inventory_registrations", key, "items", index), item
    grants = teams.state.get("shop_purchases", {})
    rewards = teams.state.get("story_rewards", {})
    if not isinstance(grants, dict) or not isinstance(rewards, dict):
        raise OwnershipError("Owned-item ledger needs review")
    for key, receipt in grants.items():
        if not isinstance(receipt, dict):
            raise OwnershipError("Owned-item receipt needs review")
        yield ("shop_purchases", key), receipt
    for key, reward in rewards.items():
        if not isinstance(reward, dict) or not isinstance(reward.get("items"), list):
            raise OwnershipError("Story item receipt needs review")
        for index, item in enumerate(reward["items"]):
            if not isinstance(item, dict) or (item.get("online_id"), item.get("player_id")) != (
                reward.get("online_id"),
                reward.get("player_id"),
            ):
                raise OwnershipError("Story item owner does not match its receipt")
            yield ("story_rewards", key, "items", index), item


def _owned_receipts(teams: TeamStore, session: GameSession):
    for path, receipt in all_receipts(teams):
        if receipt["online_id"] == session.online_id and receipt["player_id"] == session.player_id:
            yield path, receipt


def _item(receipt: dict) -> tuple[int, int, int, int, int]:
    return tuple(
        receipt[key] for key in ("category", "part_id", "part_class", "instance_id", "tuning")
    )


def pending_inventory(
    teams: TeamStore,
    session: GameSession,
    snapshot: bytes,
    *,
    canonical_snapshot: bytes | None = None,
) -> bytes:
    """Add missing paid ownership without changing an uploaded record.

    A matching instance may have newer tuning in its uploaded snapshot. Keep
    that record. A different part identity with the same ID is an error, not
    permission to replace either item. No receipt is confirmed by a read.
    When a reviewed preseed overrides the normal snapshot, canonical_snapshot
    protects confirmed grants that remain owned. Removed items are not added.
    """
    removed = sold_ids(teams, session)
    snapshot = without_registered_placeholders(teams, session, snapshot)
    snapshot = without_sold_items(teams, session, snapshot)
    if canonical_snapshot is not None:
        canonical_snapshot = without_registered_placeholders(teams, session, canonical_snapshot)
        canonical_snapshot = without_sold_items(teams, session, canonical_snapshot)
    owned = [r for _, r in _owned_receipts(teams, session) if r["instance_id"] not in removed]
    if not owned:
        return snapshot

    def indexed(payload):
        result = {}
        for row in _records(payload):
            if row[3]:
                result.setdefault(row[3], []).append(row)
        return result

    by_id = indexed(snapshot)
    canonical = indexed(canonical_snapshot) if canonical_snapshot is not None else {}
    additions = []
    for receipt in owned:
        item = _item(receipt)
        existing = by_id.get(item[3], [])
        confirmed = canonical.get(item[3], [])
        for rows in (existing, confirmed):
            if len(rows) > 1 or any(row[:3] != item[:3] for row in rows):
                raise OwnershipError("Granted instance conflicts with uploaded ownership")
        if existing:
            continue
        if receipt["in_snapshot"]:
            if not confirmed:
                continue
            item = confirmed[0]
        additions.append(
            proto_bytes(1, b"".join(proto_varint(n, value) for n, value in enumerate(item, 1)))
        )
    return snapshot + b"".join(additions)


def confirm_inventory(teams: TeamStore, session: GameSession, stored_snapshot: bytes) -> int:
    """Confirm a grant only after a matching ownership snapshot was stored.

    Do not call this for deferred or rejected uploads. Once integrated, normal
    snapshot handling owns later item changes; a receipt must not resurrect a
    part that the player subsequently removed through that path.
    """
    if not any(not r["in_snapshot"] for _, r in _owned_receipts(teams, session)):
        return 0
    pending_inventory(teams, session, stored_snapshot)  # Validate collisions first.
    ids = {row[3] for row in _records(stored_snapshot) if row[3]}
    keys = [
        key
        for key, r in _owned_receipts(teams, session)
        if not r["in_snapshot"] and r["instance_id"] in ids
    ]
    if not keys:
        return 0
    updated = deepcopy(teams.state)
    for key in keys:
        target = updated
        for component in key:
            target = target[component]
        target["in_snapshot"] = True
    teams.commit_state(updated)
    return len(keys)


def allocate_instances(
    teams: TeamStore,
    session: GameSession,
    inventory: bytes | None,
    reserved_instance_ids: frozenset[int],
    count: int,
    *,
    allow_partial: bool = False,
    capacity_floor: int = 0,
    cursor_floor: int = 0,
) -> tuple[int, ...]:
    """Reserve above the owner's cursor and exclude all known grant sources.

    This does not write state. The caller must commit the allocated IDs with
    ownership before replying, within the serialized TeamStore mutation path.
    """
    if type(count) is not int or not 1 <= count <= MAX_REGISTERED_PARTS:
        raise OwnershipError("Invalid part grant count")
    if inventory is None:
        raise OwnershipError("Owned inventory is not verified")
    combined = _records(pending_inventory(teams, session, inventory))
    if type(capacity_floor) is not int or not 0 <= capacity_floor <= MAX_REGISTERED_PARTS:
        raise OwnershipError("Inventory capacity floor is outside the garage bound")
    if type(cursor_floor) is not int or not 0 <= cursor_floor <= MAX_INSTANCE_ID:
        raise OwnershipError("Inventory instance cursor is outside uint64")
    occupancy = max(len(combined), capacity_floor)
    if allow_partial:
        count = min(count, max(0, MAX_REGISTERED_PARTS - occupancy))
        if count == 0:
            return ()
    elif occupancy + count > MAX_REGISTERED_PARTS:
        raise OwnershipError("Registered inventory reaches the garage upper bound")
    if any(
        type(value) is not int or not 0 <= value <= MAX_INSTANCE_ID
        for value in reserved_instance_ids
    ):
        raise OwnershipError("Reserved instance IDs need review")
    known = {r["instance_id"] for _, r in all_receipts(teams)}
    known.update(r["instance_id"] for r in sale_receipts(teams))
    own_ids = {row[3] for row in combined}
    if not (own_ids - {0}) <= reserved_instance_ids | known:
        raise OwnershipError("Instance reservation view omits known owned parts")
    own_ids.update(r["instance_id"] for _, r in _owned_receipts(teams, session))
    own_ids.update(sold_ids(teams, session))
    cursor = max(MIN_INSTANCE_ID - 1, cursor_floor, max(own_ids, default=0))
    used = reserved_instance_ids | known
    selected = []
    while len(selected) < count:
        cursor += 1
        if cursor > MAX_INSTANCE_ID:
            raise OwnershipError("No higher uint64 instance ID is available")
        if cursor not in used:
            selected.append(cursor)
    return tuple(selected)


def authorized_inventory(teams: TeamStore, session: GameSession, path: Path):
    """Require a native-verified source, then apply known server transactions.

    A sale can change the effective inventory without another native count
    report. Keep the verified source proof within this login, not a rewritten
    or invented client claim. A changed source or client report invalidates it.
    """
    if teams.player_profile(session.online_id).get("player_id") != session.player_id:
        raise OwnershipError("Item account identity is not established")
    selected = _requested_offline_snapshot(path, player_id=session.player_id)
    if selected.payload is None or session.owned_parts_claim is None:
        raise OwnershipError("Owned inventory and its native claim are required")
    inventory = pending_inventory(
        teams,
        session,
        selected.payload,
        canonical_snapshot=selected.canonical_payload if selected.preseed_path else None,
    )
    fingerprint = hashlib.sha256()
    fingerprint.update(len(selected.payload).to_bytes(8, "big"))
    fingerprint.update(selected.payload)
    fingerprint.update(b"\1" if selected.preseed_path else b"\0")
    if selected.preseed_path:
        fingerprint.update(selected.canonical_payload or b"")
    source_digest = fingerprint.digest()
    matches = any(
        compare_owned_parts(session.owned_parts_claim, view)["comparison_status"] == "match"
        for view in (selected.payload, inventory)
    )
    if matches:
        session.verified_inventory_source = source_digest
        session.verified_inventory_claim = session.owned_parts_claim
    elif not (
        session.verified_inventory_source == source_digest
        and session.verified_inventory_claim == session.owned_parts_claim
    ):
        session.verified_inventory_source = None
        session.verified_inventory_claim = None
        raise OwnershipError("Stored inventory does not match the native claim")
    return inventory, reserved_instances(path)


def sale_inventory_authority(
    teams: TeamStore,
    session: GameSession,
    path: Path,
    instance_id: int,
) -> tuple[bytes, str]:
    """Permit one positively stored sale despite an incomplete global upload.

    A native count/XOR mismatch cannot authorize missing item metadata or a
    purchase. It need not veto a sale of the exact owner-bound instance that
    the server already stored or paid for, provided the current login supplied
    a valid claim and maximum instance cursor. Only this sale receives the
    narrower view; no general inventory verification state is promoted.
    """
    try:
        inventory, _reservations = authorized_inventory(teams, session, path)
        return inventory, "verified_snapshot"
    except OwnershipError as error:
        if str(error) != "Stored inventory does not match the native claim":
            raise
    if (
        type(instance_id) is not int
        or not MIN_INSTANCE_ID <= instance_id <= MAX_INSTANCE_ID
        or session.owned_parts_claim is None
        or session.owned_parts_max_instance_id is None
        or type(session.owned_parts_max_instance_id) is not int
        or session.owned_parts_evidence_serial <= 0
        or session.owned_parts_claim_serial != session.owned_parts_evidence_serial
    ):
        raise OwnershipError("Sale requires current-login native ownership evidence")
    try:
        claimed_count, _claimed_xor = decode_owned_parts_claim(session.owned_parts_claim)
    except ProtocolError as error:
        raise OwnershipError("Sale native ownership claim is invalid") from error
    selected = _requested_offline_snapshot(path, player_id=session.player_id)
    if selected.payload is None:
        raise OwnershipError("Sale requires stored item metadata")
    server_issued_pending = any(
        key[0] in ("shop_purchases", "story_rewards")
        and (receipt.get("online_id"), receipt.get("player_id")) == (
            session.online_id,
            session.player_id,
        )
        and receipt.get("instance_id") == instance_id
        and receipt.get("in_snapshot") is False
        for key, receipt in all_receipts(teams)
    )
    if instance_id > session.owned_parts_max_instance_id and not server_issued_pending:
        raise OwnershipError("Sale instance exceeds the current native cursor")
    inventory = pending_inventory(
        teams,
        session,
        selected.payload,
        canonical_snapshot=selected.canonical_payload if selected.preseed_path else None,
    )
    records = _records(inventory)
    registered_count = sum(row[3] >= MIN_INSTANCE_ID for row in records)
    if claimed_count < registered_count:
        raise OwnershipError("Sale item is not positively owned by this account")
    if sum(row[3] == instance_id for row in records) == 1:
        return inventory, "positive_stored_instance"
    if claimed_count <= registered_count:
        raise OwnershipError("Sale item is not positively owned by this account")

    # A partial native upload can omit an older, still-owned instance. Retained
    # snapshots are not promoted into the canonical inventory, but they can
    # recover the metadata for this one sale when the current login proves
    # that additional registered instances exist and bounds the requested ID.
    history = path / ".history" / f"{session.player_id}.offline-data.bin"
    historical_rows = set()
    if history.is_dir():
        for candidate in history.glob("*.bin"):
            try:
                raw = candidate.read_bytes()
                if len(raw) > MAX_INVENTORY_SNAPSHOT_BYTES:
                    continue
                historical_rows.update(row for row in _records(raw) if row[3] == instance_id)
            except (OSError, ProtocolError, ValueError):
                continue
    if len(historical_rows) != 1:
        raise OwnershipError("Sale item is not positively owned by this account")
    recovered = historical_rows.pop()
    inventory += proto_bytes(
        1,
        b"".join(proto_varint(number, value) for number, value in enumerate(recovered, 1)),
    )
    return inventory, "positive_owner_history_instance"


def purchase_inventory_authority(
    teams: TeamStore, session: GameSession, path: Path
) -> PurchaseInventoryAuthority:
    """Authorize new allocation from a full snapshot or same-login claim/cursor.

    Existing-item mutations still require ``authorized_inventory`` because the
    sparse path proves only registered count/XOR and an allocation ceiling. It
    does not prove part metadata or ownership of any particular old instance.
    """
    try:
        inventory, reservations = authorized_inventory(teams, session, path)
        return PurchaseInventoryAuthority(
            inventory=inventory,
            reservations=reservations,
            capacity_floor=0,
            cursor_floor=0,
            mode="complete_snapshot",
        )
    except OwnershipError as error:
        if str(error) not in {
            "Owned inventory and its native claim are required",
            "Stored inventory does not match the native claim",
        }:
            raise

    if teams.player_profile(session.online_id).get("player_id") != session.player_id:
        raise OwnershipError("Item account identity is not established")
    if (
        session.owned_parts_claim is None
        or session.owned_parts_max_instance_id is None
        or session.owned_parts_claim_serial != session.owned_parts_evidence_serial
        or session.owned_parts_evidence_serial <= 0
    ):
        raise OwnershipError("Same-login ownership claim and instance cursor are required")
    try:
        claimed_count, _claimed_xor = decode_owned_parts_claim(session.owned_parts_claim)
    except ProtocolError as error:
        raise OwnershipError(str(error)) from error
    maximum_instance = session.owned_parts_max_instance_id
    if (
        type(maximum_instance) is not int
        or not 0 <= maximum_instance <= MAX_INSTANCE_ID
        or not 0 <= claimed_count <= MAX_REGISTERED_PARTS
        or (claimed_count > 0 and maximum_instance < MIN_INSTANCE_ID)
    ):
        raise OwnershipError("Ownership claim and instance cursor are inconsistent")

    selected = _requested_offline_snapshot(path, player_id=session.player_id)
    snapshot = selected.payload or b""
    inventory = pending_inventory(
        teams,
        session,
        snapshot,
        canonical_snapshot=selected.canonical_payload if selected.preseed_path else None,
    )
    # The sparse snapshot and pending receipts may overlap the native claim.
    # Counting both is conservative: it can reject near-capacity accounts but
    # cannot authorize capacity by undercounting unknown existing ownership.
    capacity_floor = claimed_count + len(_records(inventory))
    if capacity_floor > MAX_REGISTERED_PARTS:
        raise OwnershipError("Sparse ownership evidence reaches the garage upper bound")
    return PurchaseInventoryAuthority(
        inventory=inventory,
        reservations=reserved_instances(path),
        capacity_floor=capacity_floor,
        cursor_floor=maximum_instance,
        mode="same_login_claim_and_cursor",
    )


def reserved_instances(path: Path) -> frozenset[int]:
    """Use the same cross-account reservation view for purchases and imports."""
    reservations = set()
    for pattern in ("*.offline-data.bin", "*.offline-data.preseed.bin"):
        for file in path.glob(pattern):
            with file.open("rb") as stream:
                raw = stream.read(MAX_INVENTORY_SNAPSHOT_BYTES + 1)
            if len(raw) > MAX_INVENTORY_SNAPSHOT_BYTES:
                raise OwnershipError("Inventory reservation input exceeds the supported bound")
            reservations.update(inventory_instance_ids(raw))
    return frozenset(reservations)
