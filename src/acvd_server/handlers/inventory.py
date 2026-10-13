"""Owned inventory synchronization, reviewed recovery and cursor readback.

Pure wire projection lives in inventory.py; atomic history lives in
player_storage.py. This handler owns the session-scoped preseed guard and
correlated acknowledgement. The VerifyOwnPartsList reply remains an ACK. Its
read-only count/XOR comparison does not authorize purchases or remove parts.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from ..inventory import (
    _item_simple_info_list,
    _merge_offline_inventory,
    _offline_inventory_summary,
    _sync_offline_data_response,
    compare_owned_parts,
    key_item_list_response,
    key_item_pairs,
)
from ..inventory_registration import prepare_registration
from ..key_item_shop import receipt_pairs
from ..owned_items import purchase_inventory_authority
from ..player_storage import _replace_player_blob, _requested_offline_snapshot
from ..protocol import ProtocolError, decode_proto, first_field, proto_bytes, proto_varint
from ..shop_transactions import confirm_inventory, pending_inventory
from ..story_items import resolve_pending
from ..teams import TeamStore
from ..telemetry import Telemetry
from ..udp import GET_MY_ITEM_SIMPLE_INFO_LIST_ALT, GameMessage, GameSession


def _resume_story_items(
    teams: TeamStore, session: GameSession, player_data_path: Path, telemetry: Telemetry
) -> None:
    """Prepare earned parts for normal cursor delivery, never a second payout."""
    if session.owned_parts_claim is None:
        return
    try:
        authority = purchase_inventory_authority(teams, session, player_data_path)
        count = resolve_pending(
            teams,
            session,
            inventory=authority.inventory,
            reserved_instance_ids=authority.reservations,
            capacity_floor=authority.capacity_floor,
            cursor_floor=authority.cursor_floor,
        )
    except (OSError, ValueError) as error:
        telemetry.emit(
            "game_story_items_still_pending",
            online_id=session.online_id,
            reason=str(error) if isinstance(error, ProtocolError) else type(error).__name__,
        )
        return
    if count:
        telemetry.emit(
            "game_story_items_prepared",
            online_id=session.online_id,
            count=count,
            delivery="next_owned_item_cursor_read",
            cash_changed=False,
            inventory_authority=authority.mode,
        )


def synchronize_dlc_lineup(
    session: GameSession,
    message: GameMessage,
    *,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    # Reflect the requested IDs; this does not certify local DLC entitlements.
    fields = decode_proto(message.payload)
    part_ids: list[int] = []
    response_parts: list[bytes] = []
    for field in fields:
        if field.number != 1 or not isinstance(field.value, bytes):
            continue
        nested = decode_proto(field.value)
        part_id_field = first_field(nested, 1)
        if part_id_field is None or not isinstance(part_id_field.value, int):
            continue
        part_id = int(part_id_field.value)
        part_ids.append(part_id)
        response_parts.append(proto_bytes(1, proto_varint(1, part_id)))
    reply(b"".join(response_parts))
    telemetry.emit(
        "game_dlc_parts_synchronized",
        peer=session.peer,
        online_id=session.online_id,
        part_ids=part_ids,
        count=len(part_ids),
    )


def get_key_items(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    reply_error: Callable[[int, int], None],
) -> None:
    """Return authenticated emblem/garage ownership for requested namespaces."""
    try:
        if not 0 < len(message.payload) <= 4096:
            raise ProtocolError("Key-item readback request size is invalid")
        fields = decode_proto(message.payload)
        selectors = [field.value for field in fields if field.number == 2]
        cursor_fields = [field for field in fields if field.number == 5]
        limit_fields = [field for field in fields if field.number == 6]
        if (
            not selectors
            or len(selectors) > 2048
            or any(field.wire_type != 0 for field in fields)
            or len(cursor_fields) != 1
            or len(limit_fields) != 1
            or len(fields) != len(selectors) + 2
            or type(cursor_fields[0].value) is not int
            or cursor_fields[0].value != 0
            or type(limit_fields[0].value) is not int
            or not 1 <= limit_fields[0].value <= 2048
        ):
            raise ProtocolError("Key-item readback request fields need review")
        requested = frozenset(int(value) for value in selectors)
        selected = _requested_offline_snapshot(
            player_data_path, player_id=session.player_id
        )
        owned = set(key_item_pairs(selected.payload or b"", requested))
        owned.update(receipt_pairs(teams, session, requested))
        pairs = tuple(sorted(owned))
        if len(pairs) > limit_fields[0].value:
            raise ProtocolError("Key-item readback exceeds the native page")
        response = key_item_list_response(pairs)
    except (OSError, ValueError) as error:
        telemetry.emit(
            "game_key_item_list_rejected",
            online_id=session.online_id,
            reason=str(error),
        )
        reply_error(1, 28)
        return
    reply(response)
    telemetry.emit(
        "game_key_item_list_sent",
        online_id=session.online_id,
        requested_selectors=len(requested),
        owned_count=len(pairs),
        namespaces=sorted({selector >> 16 for selector in requested}),
        identity_policy="inert-zero-v1",
    )


def sync_offline_data(
    session: GameSession,
    message: GameMessage,
    *,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    teams: TeamStore | None = None,
) -> None:
    session.owned_parts_claim = None
    session.owned_parts_max_instance_id = None
    session.owned_parts_evidence_serial += 1
    session.owned_parts_claim_serial = -1
    session.verified_inventory_source = None
    session.verified_inventory_claim = None
    target = player_data_path / f"{session.player_id}.offline-data.bin"
    previous_payload = target.read_bytes() if target.is_file() else b""
    previous_summary = _offline_inventory_summary(previous_payload) if previous_payload else {}
    inventory_summary = _offline_inventory_summary(message.payload)
    preseed = player_data_path / f"{session.player_id}.offline-data.preseed.bin"
    previous_backup = None
    applied_preseed = None
    deferred_upload = None
    deferred_reason = ""
    merged_categories: list[int] = []
    merged_field_numbers: list[int] = []
    try:
        canonical_payload, categories, field_numbers = _merge_offline_inventory(
            previous_payload, message.payload
        )
        if teams is not None:
            # Validate collisions before any snapshot is replaced. Do not
            # insert a pending purchase into an upload the client did not send.
            pending_inventory(teams, session, canonical_payload)
    except ProtocolError as exc:
        deferred_reason = str(exc)
    if deferred_reason:
        # Never replace known ownership with an undecodable snapshot,
        # including an exact-but-invalid preseed promotion. Preserve
        # both inputs for review; the normal false ACK does not order
        # deletion of the client's local items.
        deferred_target = player_data_path / f"{session.player_id}.offline-data.deferred.bin"
        _replace_player_blob(deferred_target, message.payload)
        deferred_upload = deferred_target
    elif preseed.is_file():
        if preseed.read_bytes() == message.payload:
            previous_backup = _replace_player_blob(target, message.payload)
            applied_preseed = preseed.with_name(
                f"{session.player_id}.offline-data.preseed.applied-{time.time_ns()}.bin"
            )
            preseed.replace(applied_preseed)
            # The reconstruction branch has been observed sending
            # several ownership uploads in one login. Preserve the
            # exact pre-reconstruction promotion through that bounded
            # window; later sessions/ordinary gameplay remain free to
            # update inventory normally.
            session.offline_preseed_guard_until_monotonic = time.monotonic() + 300.0
        else:
            # A reviewed pre-server inventory must not be replaced by
            # the reconstruction branch's smaller/default upload. Keep
            # the incoming bytes as versioned evidence; acknowledge
            # with reject_local_items=false so the server never asks
            # the client to delete additional local ownership.
            deferred_target = player_data_path / f"{session.player_id}.offline-data.deferred.bin"
            _replace_player_blob(deferred_target, message.payload)
            deferred_upload = deferred_target
    elif (
        target.is_file()
        and target.read_bytes() != message.payload
        and time.monotonic() < session.offline_preseed_guard_until_monotonic
    ):
        deferred_target = player_data_path / f"{session.player_id}.offline-data.deferred.bin"
        _replace_player_blob(deferred_target, message.payload)
        deferred_upload = deferred_target
    else:
        merged_categories = categories
        merged_field_numbers = field_numbers
        previous_backup = _replace_player_blob(target, canonical_payload)
    stored_payload = target.read_bytes() if target.is_file() else b""
    purchase_receipts_confirmed = 0
    if teams is not None and deferred_upload is None:
        purchase_receipts_confirmed = confirm_inventory(teams, session, stored_payload)
    reply(_sync_offline_data_response())
    telemetry.emit(
        "game_offline_data_synchronized",
        peer=session.peer,
        online_id=session.online_id,
        payload_length=len(message.payload),
        reject_local_items=False,
        inventory_summary=inventory_summary,
        canonical_inventory_summary=(
            _offline_inventory_summary(stored_payload) if stored_payload else {}
        ),
        merged_categories=merged_categories,
        merged_field_numbers=merged_field_numbers,
        stored_payload_length=len(stored_payload),
        purchase_receipts_confirmed=purchase_receipts_confirmed,
        previous_inventory_summary=previous_summary,
        previous_backup=str(previous_backup) if previous_backup is not None else "",
        applied_preseed=str(applied_preseed) if applied_preseed is not None else "",
        deferred_upload=str(deferred_upload) if deferred_upload is not None else "",
        deferred_reason=deferred_reason,
    )


def acknowledge_owned_parts(
    session: GameSession,
    message: GameMessage,
    *,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    player_data_path: Path | None = None,
    teams: TeamStore | None = None,
) -> None:
    # Keep the native ACK unchanged, even when local data is missing or invalid.
    session.owned_parts_claim = None
    session.owned_parts_claim_serial = -1
    session.verified_inventory_source = None
    session.verified_inventory_claim = None
    # A mismatch is not permission to reset a save or invent missing ownership.
    try:
        selected = (
            _requested_offline_snapshot(player_data_path, player_id=session.player_id)
            if player_data_path is not None
            else None
        )
        snapshot = selected.payload if selected else None
        if snapshot is not None and teams is not None:
            snapshot = pending_inventory(
                teams,
                session,
                snapshot,
                canonical_snapshot=(selected.canonical_payload or b"")
                if selected.preseed_path
                else None,
            )
        comparison = compare_owned_parts(message.payload, snapshot)
        session.owned_parts_claim = (
            bytes(message.payload) if comparison["comparison_status"] != "invalid_claim" else None
        )
        if session.owned_parts_claim is not None:
            session.owned_parts_claim_serial = session.owned_parts_evidence_serial
        comparison["snapshot_source"] = (
            "reviewed_preseed"
            if selected and selected.preseed_path
            else "canonical_with_pending_receipts"
            if selected and selected.payload is not None
            else "missing"
        )
    except (OSError, ProtocolError) as error:
        comparison = {
            "comparison_status": "snapshot_unavailable",
            "comparison_match": False,
            "authorizes_purchase": False,
            "reason": type(error).__name__,
        }
    reply(b"")
    telemetry.emit(
        "game_owned_parts_check_received",
        peer=session.peer,
        online_id=session.online_id,
        request_payload=message.payload,
        **comparison,
    )
    if session.owned_parts_claim is not None and teams is not None and player_data_path is not None:
        _resume_story_items(teams, session, player_data_path, telemetry)


def get_owned_items(
    session: GameSession,
    message: GameMessage,
    *,
    player_data_path: Path,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    teams: TeamStore | None = None,
) -> None:
    fields = decode_proto(message.payload)

    def integer(number: int, default: int = 0) -> int:
        field = first_field(fields, number)
        return field.value if field is not None and isinstance(field.value, int) else default

    start_item_id = integer(2)
    offset = integer(3)
    limit = integer(4, 64)
    after_instance_id = 0
    if message.message_type == GET_MY_ITEM_SIMPLE_INFO_LIST_ALT and start_item_id == 3000:
        # Native 0x03e4 uses field 4 as a uint64 instance cursor:
        # zero at enrollment, then the highest local instance ID.
        # Re-sending older items duplicates them and can displace
        # genuine parts at the retail inventory capacity. Keep the
        # separately used 0x03d8/territory query semantics unchanged.
        after_instance_id = integer(4)
        limit = 3000
    offline_inventory = b""
    canonical_inventory = b""
    inventory_preseed = None
    preseed_ignored_reason = ""
    if start_item_id == 3000:
        if teams is not None:
            _resume_story_items(teams, session, player_data_path, telemetry)
        selected = _requested_offline_snapshot(player_data_path, player_id=session.player_id)
        offline_inventory = selected.payload or b""
        canonical_inventory = selected.canonical_payload or b""
        inventory_preseed = selected.preseed_path
        preseed_ignored_reason = selected.preseed_ignored_reason
    purchase_overlay_applied = False
    if teams is not None and start_item_id == 3000:
        # Register the accepted stored upload before returning its cursor
        # page. A durable receipt makes read/reconnect retries idempotent.
        # Reviewed preseeds are a separate operator recovery path.
        if inventory_preseed is None and offline_inventory:
            registered_count = prepare_registration(
                teams, session, offline_inventory, player_data_path
            )
            if registered_count:
                telemetry.emit(
                    "game_offline_parts_registered",
                    online_id=session.online_id,
                    count=registered_count,
                    cash_changed=False,
                )
        combined = pending_inventory(
            teams,
            session,
            offline_inventory,
            canonical_snapshot=canonical_inventory if inventory_preseed is not None else None,
        )
        purchase_overlay_applied = combined != offline_inventory
        offline_inventory = combined
    response = _item_simple_info_list(
        start_item_id if start_item_id == 3000 else start_item_id + offset,
        limit,
        offset=offset if start_item_id == 3000 else 0,
        offline_inventory=offline_inventory,
        after_instance_id=after_instance_id,
    )
    response_fields = decode_proto(response) if response else []
    response_count = sum(
        field.number == 1 and isinstance(field.value, bytes) for field in response_fields
    )
    reply(response)
    telemetry.emit(
        "game_item_simple_info_list_sent",
        peer=session.peer,
        online_id=session.online_id,
        start_item_id=start_item_id,
        offset=offset,
        limit=limit,
        count=response_count,
        schema="acv-small-item-info",
        after_instance_id=after_instance_id,
        purchase_overlay_applied=purchase_overlay_applied,
        source=(
            "pending_purchases_and_inventory"
            if purchase_overlay_applied
            else "reviewed_offline_inventory_preseed"
            if inventory_preseed is not None
            else "canonical_offline_inventory"
            if start_item_id == 3000 and offline_inventory
            else "territory_catalog"
            if start_item_id >= 0x90000
            else "empty"
        ),
        preseed_ignored_reason=preseed_ignored_reason,
    )
