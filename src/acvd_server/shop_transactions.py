"""Purchase receipts and pending ownership, stored with the wallet.

No handler may guess a catalogue, player level, wallet or allocation boundary.
A complete matching inventory remains the strongest authority. Returning saves
whose category uploads are sparse may instead use the same authenticated login's
native count/XOR claim plus maximum instance cursor for new allocation only.
That weaker pair never authorizes sale or metadata claims about an old item.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .inventory import PART_CATEGORY_FOR_CLASS
from .owned_items import (
    MAX_INSTANCE_ID as MAX_INSTANCE_ID,
    OwnershipError as ShopError,
    allocate_instances,
    confirm_inventory as confirm_inventory,
    pending_inventory as pending_inventory,
)
from .protocol import decode_proto, proto_varint
from .shop_catalogue import CataloguePart
from .teams import MAX_BALANCE

if TYPE_CHECKING:
    from .teams import TeamStore
    from .udp import GameMessage, GameSession


def _validate_tuning(code: int, part_class: int, budget: int) -> None:
    """Validate a new purchase allocation, not existing saved-item growth.

    The decimal code is 1000 + 100*Power + 10*RapidFire + Accuracy. Native
    purchase controls prove 1000 and 1003. Preserved native saves use the same
    three digits in metadata byte 4's nibbles and byte 5's high nibble.
    The caller must qualify the offered part's budget; class alone is not
    sufficient because class 2 includes both normal arms and weapon arms.
    """
    if type(budget) is not int or not 0 <= budget <= 3:
        raise ShopError("Purchase tuning budget is not qualified")
    if not 1000 <= code <= 1333:
        raise ShopError("Unsupported purchase tuning code")
    power, remainder = divmod(code - 1000, 100)
    rapid_fire, accuracy = divmod(remainder, 10)
    if max(power, rapid_fire, accuracy) > 3 or power + rapid_fire + accuracy > budget:
        raise ShopError("Purchase tuning exceeds the verified budget")
    if code != 1000 and part_class not in (2, 10, 11):
        raise ShopError("Tuning is not qualified for this part class")


@dataclass(frozen=True, slots=True)
class PurchaseRequest:
    shop_key: int
    entry_key: int
    part_class: int
    price: int
    tuning: int

    @classmethod
    def decode(cls, payload: bytes) -> PurchaseRequest:
        if len(payload) > 64:
            raise ShopError("Purchase payload exceeds the supported scalar form")
        fields = decode_proto(payload)
        numbers = (2, 3, 4, 5, 7)
        if len(fields) != len(numbers) or {f.number for f in fields} != set(numbers):
            raise ShopError("Purchase requires unique fields 2, 3, 4, 5 and 7")
        if any(
            f.wire_type != 0 or not isinstance(f.value, int) or not 0 <= f.value <= 0xFFFFFFFF
            for f in fields
        ):
            raise ShopError("Purchase fields must be uint32 values")
        values = {f.number: f.value for f in fields}
        return cls(*(values[n] for n in numbers))


def _receipts(teams: TeamStore) -> dict:
    records = teams.state.get("shop_purchases", {})
    if not isinstance(records, dict):
        raise ShopError("Purchase ledger needs review")
    return records


def _purchase_request(session: GameSession, message: GameMessage) -> tuple[PurchaseRequest, str]:
    """Keep the existing request identity stable for already committed receipts."""
    request = PurchaseRequest.decode(message.payload)
    if message.message_type != 0x03DD:
        raise ShopError("Wrong message type for a part purchase")
    if (
        not isinstance(session.auth_token, bytes)
        or len(session.auth_token) != 8
        or not isinstance(message.message_index, bytes)
        or len(message.message_index) != 4
        or type(message.packet_counter) is not int
        or not 0 <= message.packet_counter <= 0xFFFF
        or type(session.player_id) is not int
        or not 0 < session.player_id <= MAX_INSTANCE_ID
        or not isinstance(session.online_id, str)
        or not session.online_id
    ):
        raise ShopError("Purchase request identity is outside the native format")
    key = hashlib.sha256(
        b"acvd-shop-v1\0"
        + session.auth_token
        + session.online_id.encode("utf-8")
        + b"\0"
        + session.player_id.to_bytes(8, "big")
        + message.message_index
        + message.packet_counter.to_bytes(2, "big")
        + message.payload
    ).hexdigest()
    return request, key


def _receipt_reply(teams: TeamStore, session: GameSession, key: str) -> bytes | None:
    receipts = _receipts(teams)
    if key not in receipts:
        return None
    receipt = receipts[key]
    if (
        not isinstance(receipt, dict)
        or receipt.get("online_id") != session.online_id
        or type(receipt.get("player_id")) is not int
        or receipt["player_id"] != session.player_id
    ):
        raise ShopError("Stored purchase receipt identity requires review")
    instance_id = receipt.get("instance_id")
    if type(instance_id) is not int or not 0 < instance_id <= MAX_INSTANCE_ID:
        raise ShopError("Stored purchase instance ID requires review")
    return proto_varint(1, instance_id)


def replay_purchase(teams: TeamStore, session: GameSession, message: GameMessage) -> bytes | None:
    """Read a paid reply without a catalogue, inventory write, or second charge."""
    _, key = _purchase_request(session, message)
    return _receipt_reply(teams, session, key)


def purchase(
    teams: TeamStore,
    session: GameSession,
    message: GameMessage,
    *,
    offer: CataloguePart | None,
    shop_key: int,
    verified_level: int | None,
    inventory: bytes | None,
    reserved_instance_ids: frozenset[int],
    tuning_budget: int = 0,
    capacity_floor: int = 0,
    cursor_floor: int = 0,
) -> tuple[bytes, bool]:
    """Commit wallet and receipt before ACK; retries return the same instance.

    None inventory means unknown, not empty. Supply the authenticated owner's
    complete, reviewed snapshot. Its highest instance ID is the cursor floor.
    reserved_instance_ids must cover all known stored and reviewed inventories.
    Execution must use the TeamStore's single serialized mutation path.
    """
    request, key = _purchase_request(session, message)
    previous = _receipt_reply(teams, session, key)
    if previous is not None:
        # The committed reply survives later catalogue/level changes.
        return previous, True
    if offer is None or request.shop_key != shop_key or request.entry_key != offer.lineup_id:
        raise ShopError("No matching authoritative shop offer")
    if type(verified_level) is not int or not 0 <= verified_level <= 0xFFFF:
        raise ShopError("Player shop level is not verified")
    if offer.minimum_level > verified_level:
        raise ShopError("Part is not unlocked at this level")
    if request.price != offer.price or not 0 <= offer.price <= 0xFFFFFFFF:
        raise ShopError("Purchase price does not match the catalogue")
    if PART_CATEGORY_FOR_CLASS.get(request.part_class) != offer.category:
        raise ShopError("Part class does not match the qualified offer")
    _validate_tuning(request.tuning, request.part_class, tuning_budget)
    profile = teams.player_profile(session.online_id)
    balance = profile.get("balance")
    if type(balance) is not int or not offer.price <= balance <= MAX_BALANCE:
        raise ShopError("Known sufficient funds are required")
    (instance_id,) = allocate_instances(
        teams,
        session,
        inventory,
        reserved_instance_ids,
        1,
        capacity_floor=capacity_floor,
        cursor_floor=cursor_floor,
    )
    receipt = {
        "online_id": session.online_id,
        "player_id": session.player_id,
        "shop_key": shop_key,
        "entry_key": offer.lineup_id,
        "category": offer.category,
        "part_id": offer.part_id,
        "part_class": request.part_class,
        "tuning": request.tuning,
        "instance_id": instance_id,
        "price": offer.price,
        "balance_before": balance,
        "balance_after": balance - offer.price,
        "in_snapshot": False,
    }
    updated = deepcopy(teams.state)
    updated.setdefault("shop_purchases", {})[key] = receipt
    updated["player_profiles"][session.online_id]["balance"] = receipt["balance_after"]
    teams.commit_state(updated)
    return proto_varint(1, instance_id), False
