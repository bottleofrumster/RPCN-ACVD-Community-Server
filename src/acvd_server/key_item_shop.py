"""Authoritative key-item catalogues and atomic paid ownership receipts.

Buy Decal and Buy Formation use opcode 0x03D8 for parameter-backed offers,
then buy through opcode 0x03D7. Keep these five-field paid offers separate
from the 0x03E2 owned-KeyItem readback and from Story emblem rewards.
"""

from __future__ import annotations

import hashlib
import struct
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .parameter_tables import read_rows
from .protocol import ProtocolError, decode_proto, proto_bytes, proto_varint
from .teams import MAX_BALANCE

if TYPE_CHECKING:
    from .config import ServerConfig
    from .teams import TeamStore
    from .udp import GameMessage, GameSession

EMBLEM_PIECE_FILE = "emblempieceparam.bin"
EMBLEM_PIECE_TYPE = b"EMBLEM_PIECE_PARAM_ST"
EMBLEM_SELECTOR_BASE = 0x20000
FORMATION_SELECTOR = 0xA0000
FORMATION_FILE = "customlayoutareadata.bin"
FORMATION_TYPE = b"CUSTOMLAYOUT_AREADATA_ST"
SHOP_NAMESPACES = frozenset((2, 10))
BUY_KEY_ITEM = 0x03D7
MAX_KEY_ITEM_ROWS = 2048


class KeyItemShopError(ProtocolError):
    """Reject a malformed, stale, unaffordable or unauthoritative purchase."""


@dataclass(frozen=True, slots=True)
class KeyItemOffer:
    offer_id: int
    selector: int
    row_id: int
    price: int

    def __post_init__(self) -> None:
        if (
            type(self.offer_id) is not int
            or type(self.selector) is not int
            or type(self.row_id) is not int
            or type(self.price) is not int
            or not 0 < self.offer_id < 1 << 32
            or not (
                EMBLEM_SELECTOR_BASE <= self.selector < EMBLEM_SELECTOR_BASE + 0x10000
                or self.selector == FORMATION_SELECTOR
            )
            or not 0 < self.row_id < 1 << 32
            or not 0 <= self.price <= 0xFFFFFFFF
        ):
            raise ValueError("Key-item offer is outside the native integer domains")


@dataclass(frozen=True, slots=True)
class KeyItemShopCatalogue:
    entries: tuple[KeyItemOffer, ...]
    source_sha256: str
    input_hashes: tuple[tuple[str, str], ...] = ()

    @classmethod
    def load(cls, config: ServerConfig) -> KeyItemShopCatalogue | None:
        path = config.shop_parameter_path / EMBLEM_PIECE_FILE
        formation_path = config.shop_parameter_path / FORMATION_FILE
        if not path.is_file() and not formation_path.is_file():
            return None
        data = path.read_bytes() if path.is_file() else b""
        rows = read_rows(data, EMBLEM_PIECE_TYPE, 16) if data else {}
        inputs = [(EMBLEM_PIECE_FILE, hashlib.sha256(data).hexdigest())] if data else []
        entries = []
        for row_id, body in rows.items():
            category = body[0]
            price = struct.unpack_from(">I", body, 4)[0]
            initial_type = body[8]
            if initial_type not in (0, 1, 2, 3):
                raise ValueError("Unknown emblem-piece initial type")
            if initial_type != 1:
                continue
            entries.append(
                KeyItemOffer(
                    offer_id=row_id,
                    selector=EMBLEM_SELECTOR_BASE + category,
                    row_id=row_id,
                    price=price,
                )
            )
        if formation_path.is_file():
            formations = formation_path.read_bytes()
            inputs.append((FORMATION_FILE, hashlib.sha256(formations).hexdigest()))
            for row_id, body in read_rows(formations, FORMATION_TYPE, 32).items():
                # CustomLayoutAreaData.def: u16 mapID, u8 mapLevel,
                # u8 presetCustomLayoutID; u32 price at byte 24.
                # Level 1 is the built-in Formation A, not a paid unlock.
                if not 0 < row_id < 0x10000 or not 1 <= body[2] <= 5:
                    raise ValueError("Formation row identity or level is invalid")
                price = struct.unpack_from(">I", body, 24)[0]
                if body[2] == 1:
                    continue
                entries.append(
                    KeyItemOffer(FORMATION_SELECTOR | row_id, FORMATION_SELECTOR, row_id, price)
                )
        entries.sort(key=lambda item: (item.selector, item.row_id))
        if not entries or len(entries) > MAX_KEY_ITEM_ROWS:
            raise ValueError("Key-item shop catalogue is empty or too large")
        if len({item.offer_id for item in entries}) != len(entries):
            raise ValueError("Key-item shop offer IDs are not unique")
        digest = hashlib.sha256(repr(inputs).encode()).hexdigest()
        return cls(tuple(entries), digest, tuple(inputs))

    def page(
        self,
        selectors: frozenset[int],
        *,
        offset: int,
        limit: int,
        owned: frozenset[tuple[int, int]],
    ) -> tuple[bytes, tuple[KeyItemOffer, ...]]:
        if (
            not selectors
            or any(selector >> 16 not in SHOP_NAMESPACES for selector in selectors)
            or not 0 <= offset <= 0xFFFFFFFF
            or not 0 <= limit <= MAX_KEY_ITEM_ROWS
        ):
            raise KeyItemShopError("Key-item shop page request is outside supported bounds")
        rows = [
            item
            for item in self.entries
            if item.selector in selectors and (item.selector, item.row_id) not in owned
        ][offset : offset + limit]
        payload = b"".join(
            proto_bytes(
                1,
                b"".join(
                    proto_varint(number, value)
                    for number, value in (
                        (1, item.offer_id),
                        (2, item.row_id),
                        (3, item.selector),
                        (4, item.row_id),
                        (5, item.price),
                    )
                ),
            )
            for item in rows
        )
        return payload, tuple(rows)


def decode_catalogue_request(payload: bytes) -> tuple[frozenset[int], int, int]:
    """Decode the repeated-selector shop form, not an owned-item cursor."""
    if not 0 < len(payload) <= 4096:
        raise KeyItemShopError("Key-item shop request size is invalid")
    fields = decode_proto(payload)
    selectors = [field for field in fields if field.number == 2]
    offsets = [field for field in fields if field.number == 3]
    limits = [field for field in fields if field.number == 4]
    if (
        not selectors
        or len(selectors) > MAX_KEY_ITEM_ROWS
        or len(offsets) != 1
        or len(limits) != 1
        or len(fields) != len(selectors) + 2
        or any(field.wire_type != 0 or type(field.value) is not int for field in fields)
    ):
        raise KeyItemShopError("Key-item shop request requires selectors, offset and limit")
    requested = frozenset(int(field.value) for field in selectors)
    if len(requested) != len(selectors) or any(
        selector >> 16 not in SHOP_NAMESPACES for selector in requested
    ):
        raise KeyItemShopError("Key-item shop selector list needs review")
    return requested, int(offsets[0].value), int(limits[0].value)


@dataclass(frozen=True, slots=True)
class KeyItemPurchaseRequest:
    offer_id: int
    selector: int
    price: int

    @classmethod
    def decode(cls, payload: bytes) -> KeyItemPurchaseRequest:
        if len(payload) > 32:
            raise KeyItemShopError("Key-item purchase payload exceeds its scalar form")
        fields = decode_proto(payload)
        if len(fields) != 3 or {field.number for field in fields} != {2, 3, 4}:
            raise KeyItemShopError("Key-item purchase requires unique fields 2, 3 and 4")
        if any(
            field.wire_type != 0
            or type(field.value) is not int
            or not 0 <= field.value <= 0xFFFFFFFF
            for field in fields
        ):
            raise KeyItemShopError("Key-item purchase fields must be uint32")
        values = {field.number: int(field.value) for field in fields}
        return cls(values[2], values[3], values[4])


def purchased_pairs(teams: TeamStore, session: GameSession) -> set[tuple[int, int]]:
    records = teams.state.get("key_item_purchases", {})
    if not isinstance(records, dict):
        raise KeyItemShopError("Key-item purchase ledger needs review")
    result = set()
    for receipt in records.values():
        if not isinstance(receipt, dict):
            raise KeyItemShopError("Key-item purchase receipt needs review")
        if (receipt.get("online_id"), receipt.get("player_id")) != (
            session.online_id,
            session.player_id,
        ):
            continue
        pair = (receipt.get("selector"), receipt.get("row_id"))
        if (
            type(pair[0]) is not int
            or type(pair[1]) is not int
            or pair[0] >> 16 not in SHOP_NAMESPACES
            or not 0 < pair[1] < 1 << 32
        ):
            raise KeyItemShopError("Stored key-item purchase identity needs review")
        result.add(pair)
    return result


def receipt_pairs(
    teams: TeamStore,
    session: GameSession,
    selectors: frozenset[int],
) -> set[tuple[int, int]]:
    """Return durable paid and Story emblem ownership for requested selectors."""
    result = {pair for pair in purchased_pairs(teams, session) if pair[0] in selectors}
    records = teams.state.get("story_rewards", {})
    if not isinstance(records, dict):
        raise KeyItemShopError("Story reward ledger needs review")
    for receipt in records.values():
        if not isinstance(receipt, dict) or (
            receipt.get("online_id"),
            receipt.get("player_id"),
        ) != (session.online_id, session.player_id):
            continue
        emblems = receipt.get("emblems", [])
        if not isinstance(emblems, list):
            raise KeyItemShopError("Stored Story emblem receipt needs review")
        for emblem in emblems:
            if not isinstance(emblem, dict):
                raise KeyItemShopError("Stored Story emblem receipt needs review")
            pair = (emblem.get("selector"), emblem.get("row_id"))
            if pair[0] not in selectors:
                continue
            if (
                type(pair[0]) is not int
                or type(pair[1]) is not int
                or pair[0] >> 16 != 2
                or not 0 < pair[1] < 1 << 32
            ):
                raise KeyItemShopError("Stored Story emblem identity needs review")
            result.add(pair)
    return result


def _request_key(session: GameSession, message: GameMessage) -> tuple[KeyItemPurchaseRequest, str]:
    request = KeyItemPurchaseRequest.decode(message.payload)
    if message.message_type != BUY_KEY_ITEM:
        raise KeyItemShopError("Wrong message type for an key-item purchase")
    if (
        not isinstance(session.auth_token, bytes)
        or len(session.auth_token) != 8
        or not isinstance(message.message_index, bytes)
        or len(message.message_index) != 4
        or type(message.packet_counter) is not int
        or not 0 <= message.packet_counter <= 0xFFFF
        or type(session.player_id) is not int
        or not 0 < session.player_id < 1 << 64
        or not session.online_id
    ):
        raise KeyItemShopError("Key-item purchase request identity is invalid")
    digest = hashlib.sha256(
        # Retain the existing receipt domain so old paid retries stay idempotent.
        b"acvd-emblem-shop-v1\0"
        + session.auth_token
        + session.online_id.encode("utf-8")
        + b"\0"
        + session.player_id.to_bytes(8, "big")
        + message.message_index
        + message.packet_counter.to_bytes(2, "big")
        + message.payload
    ).hexdigest()
    return request, digest


def purchase(
    teams: TeamStore,
    session: GameSession,
    message: GameMessage,
    *,
    owned: frozenset[tuple[int, int]],
) -> tuple[bool, dict]:
    """Debit and record one offered key item atomically; return replay and receipt."""
    request, key = _request_key(session, message)
    records = teams.state.get("key_item_purchases", {})
    if not isinstance(records, dict):
        raise KeyItemShopError("Key-item purchase ledger needs review")
    prior = records.get(key)
    if prior is not None:
        if not isinstance(prior, dict) or (prior.get("online_id"), prior.get("player_id")) != (
            session.online_id,
            session.player_id,
        ):
            raise KeyItemShopError("Stored key-item purchase receipt identity needs review")
        return True, prior
    offer = session.key_item_shop_offers.get(request.offer_id)
    if (
        offer is None
        or request.selector != offer.selector
        or request.price != offer.price
        or (offer.selector, offer.row_id) in owned
    ):
        raise KeyItemShopError("Key-item purchase requires a current unowned offer")
    profile = teams.player_profile(session.online_id)
    balance = profile.get("balance")
    if type(balance) is not int or not offer.price <= balance <= MAX_BALANCE:
        raise KeyItemShopError("Known sufficient funds are required")
    receipt = {
        "online_id": session.online_id,
        "player_id": session.player_id,
        "offer_id": offer.offer_id,
        "selector": offer.selector,
        "row_id": offer.row_id,
        "price": offer.price,
        "balance_before": balance,
        "balance_after": balance - offer.price,
    }
    updated = deepcopy(teams.state)
    updated.setdefault("key_item_purchases", {})[key] = receipt
    updated["player_profiles"][session.online_id]["balance"] = receipt["balance_after"]
    teams.commit_state(updated)
    return False, receipt
