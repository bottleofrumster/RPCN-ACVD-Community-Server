"""Serve qualified online catalogue pages; never acknowledge an unpaid part."""

from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .. import part_sales
from ..inventory import PART_CATEGORY_FOR_CLASS, _offline_inventory_records, key_item_pairs
from ..key_item_shop import (
    KeyItemPurchaseRequest,
    KeyItemShopCatalogue,
    decode_catalogue_request,
    purchase as purchase_key_item,
    receipt_pairs,
)
from ..owned_items import (
    purchase_inventory_authority,
    sale_inventory_authority,
)
from ..player_medals import stored_rating_bonus
from ..player_storage import _requested_offline_snapshot
from ..protocol import ProtocolError, decode_proto, first_field
from ..shop_catalogue import SHOP_CATEGORIES
from ..shop_data import ShopCatalogue
from ..shop_transactions import (
    PurchaseRequest,
    ShopError,
    purchase,
    replay_purchase,
)
from ..shop_unlocks import remember_story_shop_unlocks
from ..teams import TeamStore
from ..telemetry import Telemetry
from ..udp import GameMessage, GameSession


def list_key_items(
    session: GameSession,
    message: GameMessage,
    *,
    catalogue: KeyItemShopCatalogue | None,
    player_data_path: Path,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    reply_error: Callable[[int, int], None],
) -> None:
    """Offer every unowned parameter-backed key-item row requested by 0x03D8."""
    try:
        selectors, offset, limit = decode_catalogue_request(message.payload)
        if catalogue is None:
            session.key_item_shop_offers.clear()
            reply(b"")
            telemetry.emit(
                "game_key_item_shop_lineup_sent",
                online_id=session.online_id,
                selector_count=len(selectors),
                offset=offset,
                limit=limit,
                count=0,
                catalogue_available=False,
            )
            return
        if teams.player_profile(session.online_id).get("player_id") != session.player_id:
            raise ValueError("Key-item shop account identity is not established")
        snapshot = _requested_offline_snapshot(player_data_path, player_id=session.player_id)
        owned = set(key_item_pairs(snapshot.payload or b"", selectors))
        owned.update(receipt_pairs(teams, session, selectors))
        payload, rows = catalogue.page(
            selectors,
            offset=offset,
            limit=limit,
            owned=frozenset(owned),
        )
        session.key_item_shop_offers.clear()
        session.key_item_shop_offers.update((row.offer_id, row) for row in rows)
        reply(payload)
        telemetry.emit(
            "game_key_item_shop_lineup_sent",
            online_id=session.online_id,
            selector_count=len(selectors),
            offset=offset,
            limit=limit,
            count=len(rows),
            owned_count=len(owned),
            catalogue_available=True,
            source_sha256=catalogue.source_sha256,
        )
    except (OSError, ValueError) as error:
        session.key_item_shop_offers.clear()
        telemetry.emit(
            "game_key_item_shop_lineup_rejected",
            online_id=session.online_id,
            reason=str(error),
        )
        reply_error(1, 28)


def buy_key_item(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    reply_error: Callable[[int, int], None],
) -> None:
    """Debit one native offered key item and retain its ownership atomically."""
    try:
        request = KeyItemPurchaseRequest.decode(message.payload)
        selectors = frozenset((request.selector,))
        snapshot = _requested_offline_snapshot(player_data_path, player_id=session.player_id)
        owned = set(key_item_pairs(snapshot.payload or b"", selectors))
        owned.update(receipt_pairs(teams, session, selectors))
        replayed, receipt = purchase_key_item(teams, session, message, owned=frozenset(owned))
    except (OSError, StopIteration, ValueError) as error:
        telemetry.emit(
            "game_key_item_shop_purchase_rejected",
            online_id=session.online_id,
            reason=str(error),
        )
        reply_error(1, 28)
        return
    reply(b"")
    telemetry.emit(
        "game_key_item_shop_purchase_replayed"
        if replayed
        else "game_key_item_shop_purchase_committed",
        online_id=session.online_id,
        message_index=message.message_index,
        offer_id=receipt.get("offer_id", 0),
        selector=receipt.get("selector", 0),
        row_id=receipt.get("row_id", 0),
        price=receipt.get("price", 0),
        balance_after=receipt.get("balance_after"),
    )


def list_parts(
    session: GameSession,
    message: GameMessage,
    *,
    catalogue: ShopCatalogue | None,
    player_data_path: Path,
    teams: TeamStore,
    telemetry: Telemetry,
    additional_points: int | None,
    reply: Callable[[bytes], None],
    reply_error: Callable[[int, int], None],
) -> None:
    try:
        if len(message.payload) > 32:
            raise ProtocolError("Shop page request exceeds its scalar form")
        fields = decode_proto(message.payload)
        if len(fields) != 3 or {f.number for f in fields} != {2, 3, 4}:
            raise ProtocolError("Shop page requires unique fields 2, 3 and 4")
        if any(f.wire_type != 0 or type(f.value) is not int for f in fields):
            raise ProtocolError("Shop page fields must be integers")
        values = {f.number: f.value for f in fields}
        category, offset, limit = (values[n] for n in (2, 3, 4))
        if (
            category not in SHOP_CATEGORIES
            or not 0 <= offset <= 0xFFFFFFFF
            or not 0 <= limit <= 512
        ):
            raise ProtocolError("Shop page is outside its supported bounds")
        if catalogue is None:
            session.shop_catalogue_version = None
            session.shop_offers.clear()
            # Keep the old empty listing only when inputs are not installed.
            # Do not pretend this is a valid rank-zero player's whole stock.
            reply(b"")
            telemetry.emit(
                "game_player_shop_parts_lineup_sent",
                online_id=session.online_id,
                category=category,
                offset=offset,
                limit=limit,
                count=0,
                catalogue_available=False,
                reason="shop_inputs_not_loaded",
            )
            return
        if teams.player_profile(session.online_id).get("player_id") != session.player_id:
            raise ValueError("Shop account identity is not established")
        if additional_points is None:
            additional_points = stored_rating_bonus(teams, session)
        unlocked_parts = remember_story_shop_unlocks(teams, session, player_data_path)
        payload, rows, level = catalogue.page(
            player_data_path=player_data_path,
            player_id=session.player_id,
            additional_points=additional_points,
            category=category,
            offset=offset,
            limit=limit,
            unlocked_parts=unlocked_parts,
        )
        reply(payload)
        if session.shop_catalogue_version != catalogue.version:
            session.shop_offers.clear()
        session.shop_catalogue_version = catalogue.version
        session.shop_offers.update((row.lineup_id, row) for row in rows)
        telemetry.emit(
            "game_player_shop_parts_lineup_sent",
            online_id=session.online_id,
            category=category,
            offset=offset,
            limit=limit,
            count=len(rows),
            catalogue_available=True,
            calculation=asdict(level),
            regulation_sha256=catalogue.regulation_digest,
            shop_key=catalogue.shop_key,
            story_unlocked_part_count=len(unlocked_parts),
        )
    except (OSError, ValueError) as error:
        telemetry.emit(
            "game_shop_catalogue_rejected", online_id=session.online_id, reason=str(error)
        )
        reply_error(1, 28)


def buy_parts(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    catalogue: ShopCatalogue | None = None,
    player_data_path: Path | None = None,
    additional_points: int | None = 0,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    reply_error: Callable[[int, int], None],
) -> None:
    # Paid requests must remain recoverable even while new sales are closed.
    # A replay reads the durable receipt; it never recalculates price or rank.
    try:
        response = replay_purchase(teams, session, message)
        replayed = response is not None
        authority = "receipt_replay"
        if response is None:
            if catalogue is None or player_data_path is None:
                raise ShopError("Shop inputs are not loaded")
            if teams.player_profile(session.online_id).get("player_id") != session.player_id:
                raise ShopError("Shop account identity is not established")
            if additional_points is None:
                additional_points = stored_rating_bonus(teams, session)
            request = PurchaseRequest.decode(message.payload)
            offer = catalogue.offer(request.entry_key)
            unlocked_parts = remember_story_shop_unlocks(teams, session, player_data_path)
            if offer is not None:
                offer = catalogue.unlocked_offer(offer, unlocked_parts)
            if (
                session.shop_catalogue_version != catalogue.version
                or offer is None
                or session.shop_offers.get(request.entry_key) != offer
            ):
                raise ShopError("Purchase requires the current offered item; reopen Shop")
            level, final_cleared = catalogue.progression_for(
                player_data_path=player_data_path,
                player_id=session.player_id,
                additional_points=additional_points,
            )
            if offer.category == 11 and (
                offer not in catalogue.ultimate_entries or not final_cleared
            ):
                raise ShopError("Ultimate weapon purchase requires Story Mission 10 clear")
            inventory_authority = purchase_inventory_authority(teams, session, player_data_path)
            authority = inventory_authority.mode
            response, replayed = purchase(
                teams,
                session,
                message,
                offer=offer,
                shop_key=catalogue.shop_key,
                verified_level=level.level,
                inventory=inventory_authority.inventory,
                reserved_instance_ids=inventory_authority.reservations,
                tuning_budget=catalogue.tuning_budget(offer.category, offer.part_id),
                capacity_floor=inventory_authority.capacity_floor,
                cursor_floor=inventory_authority.cursor_floor,
            )
    except (OSError, ValueError) as error:
        telemetry.emit(
            "game_shop_purchase_rejected", online_id=session.online_id, reason=str(error)
        )
        reply_error(1, 28)
        return
    reply(response)
    telemetry.emit(
        "game_shop_purchase_replayed" if replayed else "game_shop_purchase_committed",
        online_id=session.online_id,
        message_index=message.message_index,
        inventory_authority=authority,
    )


def sell_parts(
    session, message, *, teams, catalogue, player_data_path, telemetry, reply, reply_error
):
    """Sell one authenticated registered instance; never accept a quoted price."""
    try:
        instance = part_sales.sale_instance(message.payload)
        response = part_sales.replay_sale(teams, session, instance)
        repeated = response is not None
        authority = "receipt_replay"
        if response is None:
            if catalogue is None:
                raise ProtocolError("Shop inputs are not loaded")
            inventory, authority = sale_inventory_authority(
                teams, session, player_data_path, instance
            )
            selected = []
            for _, raw in _offline_inventory_records(inventory, require_complete=True):
                fields = decode_proto(raw)
                if first_field(fields, 4).value == instance:
                    selected.append(tuple(first_field(fields, n).value for n in range(1, 6)))
            if len(selected) != 1:
                raise ProtocolError("Sale requires exactly one owned instance")
            row = selected[0]
            if PART_CATEGORY_FOR_CLASS.get(row[2]) != row[0]:
                raise ProtocolError("Owned part class does not match its category")
            credit = catalogue.sale_credit(row[0], row[1])
            response, repeated = part_sales.sell(
                teams,
                session,
                instance,
                inventory=inventory,
                credit=credit,
                price_source=catalogue.regulation_digest + ":ServerSystemParam+0",
                cursor_floor=session.sale_cursor_floor,
            )
    except (OSError, ValueError) as error:
        telemetry.emit("game_shop_sale_rejected", online_id=session.online_id, reason=str(error))
        reply_error(1, 28)
        return
    reply(response)
    session.sale_cursor_floor = max(
        session.sale_cursor_floor, first_field(decode_proto(response), 1).value
    )
    telemetry.emit(
        "game_shop_sale_replayed" if repeated else "game_shop_sale_committed",
        online_id=session.online_id,
        message_index=message.message_index,
        inventory_authority=authority,
    )


def get_trash_parts(session, message, *, teams, telemetry, reply, reply_error):
    try:
        response, cursor, count = part_sales.trash_page(teams, session, message.payload)
    except ValueError as error:
        telemetry.emit("game_trash_parts_rejected", online_id=session.online_id, reason=str(error))
        reply_error(1, 28)
        return
    fields = decode_proto(message.payload)
    maximum_instance = first_field(fields, 3).value
    session.owned_parts_max_instance_id = maximum_instance
    session.owned_parts_evidence_serial += 1
    session.owned_parts_claim_serial = -1
    session.sale_cursor_floor = max(session.sale_cursor_floor, cursor)
    reply(response)
    telemetry.emit(
        "game_trash_parts_sent",
        online_id=session.online_id,
        count=count,
        cursor=cursor,
        maximum_instance_id=maximum_instance,
        inventory_evidence_serial=session.owned_parts_evidence_serial,
    )
