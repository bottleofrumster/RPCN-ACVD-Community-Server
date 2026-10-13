"""Story item/emblem selection and transactional settlement.

No item is inserted into an uploaded save. A durable receipt supplies pending
ownership until the client sends a matching inventory snapshot. The authenticated
Story emblem converter consumes only its recovered row ID.
"""

from __future__ import annotations

import hashlib
import time
from copy import deepcopy
from pathlib import Path

from .inventory import PART_CATEGORY_FOR_CLASS
from .owned_items import (
    OwnershipError,
    PurchaseInventoryAuthority,
    allocate_instances,
    authorized_inventory,
    purchase_inventory_authority,
)
from .protocol import ProtocolError
from .rewards import (
    StoryRewardPlan,
    _register_battle_result_response,
    _register_story_group_response,
)
from .story_emblem_data import STORY_EMBLEM_REWARDS
from .story_emblems import STORY_EMBLEM_IDENTITY, owned_keys, reward_specs as emblem_specs
from .story_item_data import STORY_PART_REWARDS
from .teams import TeamStore
from .udp import REGISTER_BATTLE_RESULT, GameMessage, GameSession

LEGACY_ITEM_POLICY = "story_items_v3"
PREVIOUS_ITEM_POLICY = "story_items_emblems_v4"
ITEM_POLICY = "story_items_emblems_request_v5"
GROUP_ITEM_POLICY = "story_coop_items_emblems_v2"
GROUP_ALIAS_WINDOW_NS = 60 * 1_000_000_000
SUPPORTED_ITEM_POLICIES = frozenset(
    (LEGACY_ITEM_POLICY, PREVIOUS_ITEM_POLICY, ITEM_POLICY, GROUP_ITEM_POLICY)
)


def _reward_inventory_authority(teams, session, path) -> PurchaseInventoryAuthority:
    """Reuse new-item allocation authority for earned parts, including sparse saves."""
    try:
        inventory, reservations = authorized_inventory(teams, session, path)
        return PurchaseInventoryAuthority(inventory, reservations, 0, 0, "complete_snapshot")
    except OwnershipError:
        return purchase_inventory_authority(teams, session, path)


def request_identity(session: GameSession, message: GameMessage) -> str:
    """Bind one Story settlement to its authenticated application request.

    The game increments the four-byte message index for each request. Packet
    counter is transport-local and can change on a resend, so it is excluded.
    A new authenticated request may have the same mission report bytes as a
    previous, legitimate mission; its receipt must still be independent.
    """
    if (
        not isinstance(session, GameSession)
        or not isinstance(message, GameMessage)
        or message.message_type != REGISTER_BATTLE_RESULT
        or not isinstance(session.auth_token, bytes)
        or len(session.auth_token) != 8
        or not isinstance(session.online_id, str)
        or not session.online_id
        or type(session.player_id) is not int
        or not 0 < session.player_id < 1 << 32
        or not isinstance(message.message_index, bytes)
        or len(message.message_index) != 4
        or not isinstance(message.payload, bytes)
    ):
        raise ValueError("Story request identity is invalid")
    return hashlib.sha256(
        b"acvd-story-result-request-v1\0"
        + session.auth_token
        + session.online_id.encode("utf-8")
        + b"\0"
        + session.player_id.to_bytes(4, "big")
        + message.message_index
        + message.payload
    ).hexdigest()


def item_specs(plan: StoryRewardPlan) -> tuple[dict, ...]:
    """Select only this completed mission and its reported earned subquests."""
    conditions = [(100, plan.mission_id, 0)]
    conditions.extend((1000, plan.mission_id, quest) for quest in plan.subquest_ids)
    result = []
    for condition in conditions:
        for category, part in STORY_PART_REWARDS.get(condition, ()):
            # Canonical side uses the first supported class in this category.
            # Weapon-side variants still need native reward qualification.
            classes = [key for key, value in PART_CATEGORY_FOR_CLASS.items() if value == category]
            if not classes or not 0 < part <= 0xFFFF:
                raise ValueError("Story item category or ID is not supported")
            result.append(
                dict(
                    condition=condition,
                    category=category,
                    part_id=part,
                    part_class=min(classes),
                    tuning=1000,
                )
            )
    return tuple(result)


def settle(
    teams: TeamStore,
    session: GameSession,
    plan: StoryRewardPlan,
    payload: bytes,
    *,
    player_data_path: Path | None = None,
    inventory: bytes | None = None,
    reserved_instance_ids: frozenset[int] = frozenset(),
    request_digest: str | None = None,
) -> tuple[bytes, dict, int]:
    """Reuse committed replies; new items, cash and receipt use one state write.

    The supplied inventory/reservations form is for isolated transaction tests.
    The live handler always supplies player_data_path and verifies native claims.
    Existing cash-only receipts are not upgraded or credited retrospectively.
    Live callers supply the authenticated request digest. Direct isolated
    callers may omit it and retain payload-keyed compatibility behavior.
    """
    if plan.player_id != session.player_id:
        raise ValueError("Story result owner does not match the authenticated player")
    payload_digest = hashlib.sha256(payload).hexdigest()
    if request_digest is not None:
        if type(request_digest) is not str:
            raise ValueError("Story request digest must be SHA-256")
        try:
            digest_bytes = bytes.fromhex(request_digest)
        except ValueError as error:
            raise ValueError("Story request digest must be SHA-256") from error
        if len(digest_bytes) != 32 or request_digest != digest_bytes.hex():
            raise ValueError("Story request digest must be canonical SHA-256")
    digest = request_digest or payload_digest
    key = session.online_id + ":" + digest
    ledger = teams.state.get("story_rewards", {})
    if not isinstance(ledger, dict):
        raise ValueError("Story item ledger needs review")
    prior = ledger.get(key)
    if prior is not None:
        if (prior.get("online_id"), prior.get("player_id")) != (
            session.online_id,
            session.player_id,
        ) or prior.get("policy_version") not in SUPPORTED_ITEM_POLICIES:
            raise ValueError("Story item receipt identity needs review")
        return (
            bytes.fromhex(prior["reply_hex"]),
            dict(
                prior["settlement"],
                applied=False,
                pending_item_count=len(prior.get("pending_specs", [])),
                emblem_reward_count=len(prior.get("emblems", [])),
            ),
            prior.get("reply_item_count", len(prior["items"])),
        )
    legacy = request_digest is None and any(
        row.get("online_id") == session.online_id
        and row.get("result_digest") == payload_digest
        and row.get("policy_version") != ITEM_POLICY
        for row in teams.state.get("battle_settlements", [])
    )
    if legacy:
        digest = payload_digest
    specs = () if legacy else item_specs(plan)
    earned_emblems = () if legacy else emblem_specs(plan)
    reason = ""
    capacity_floor = cursor_floor = 0
    if player_data_path is not None and (specs or earned_emblems):
        try:
            authority = _reward_inventory_authority(teams, session, player_data_path)
            inventory, reserved_instance_ids = authority.inventory, authority.reservations
            capacity_floor, cursor_floor = authority.capacity_floor, authority.cursor_floor
        except (OSError, ProtocolError) as error:
            # Missing ownership cannot erase independently valid cash or an
            # idempotent emblem row. Parts stay pending until ownership exists.
            inventory = None
            reserved_instance_ids = frozenset()
            reason = str(error) if isinstance(error, ProtocolError) else type(error).__name__
    known_emblems = _retained_emblem_keys(teams, session)
    if inventory is not None:
        known_emblems.update(owned_keys(inventory))
    emblems = tuple(
        dict(spec, identity=STORY_EMBLEM_IDENTITY)
        for spec in earned_emblems
        if (spec["selector"], spec["row_id"]) not in known_emblems
    )
    if legacy:
        settlement = teams.settle_battle_money(
            session.online_id,
            digest,
            mission_id=plan.mission_id,
            gross_earnings=plan.gross_pay,
            expenses=plan.expenses,
            policy_version=plan.policy_version,
        )
        return _register_battle_result_response(plan), dict(settlement, emblem_reward_count=0), 0
    try:
        identities = (
            allocate_instances(
                teams,
                session,
                inventory,
                reserved_instance_ids,
                len(specs),
                allow_partial=True,
                capacity_floor=capacity_floor,
                cursor_floor=cursor_floor,
            )
            if specs
            else ()
        )
        if len(identities) < len(specs):
            reason = "Inventory capacity is not available"
    except (OSError, ProtocolError) as error:
        # This catches only ownership preparation, not the state commit below.
        # Missing ownership cannot erase an independently valid cash reward.
        identities = ()
        reason = str(error) if isinstance(error, ProtocolError) else type(error).__name__
    items = _owned_items(session, specs[: len(identities)], identities)
    pending = list(specs[len(identities) :])
    response = _register_battle_result_response(plan, items=items, emblems=emblems)
    receipt = dict(
        online_id=session.online_id,
        player_id=session.player_id,
        result_digest=digest,
        payload_sha256=payload_digest,
        reply_hex=response.hex(),
        reply_item_count=len(items),
        items=list(items),
        pending_specs=pending,
        emblems=list(emblems),
        deferred_reason=reason,
    )
    settlement = teams.settle_battle_money(
        session.online_id,
        digest,
        mission_id=plan.mission_id,
        gross_earnings=plan.gross_pay,
        expenses=plan.expenses,
        policy_version=ITEM_POLICY,
        item_receipt=receipt,
    )
    return (
        response,
        dict(
            settlement,
            pending_item_count=len(pending),
            emblem_reward_count=len(emblems),
        ),
        len(items),
    )


def settle_group(
    teams: TeamStore,
    sessions: tuple[GameSession, GameSession],
    plans: tuple[StoryRewardPlan, StoryRewardPlan],
    payload: bytes,
    *,
    player_data_path: Path,
    request_digest: str,
) -> tuple[bytes, dict]:
    """Settle two authenticated Story owners and all rewards atomically."""
    if (
        not isinstance(sessions, tuple)
        or not isinstance(plans, tuple)
        or len(sessions) != 2
        or len(plans) != 2
        or tuple(session.player_id for session in sessions)
        != tuple(plan.player_id for plan in plans)
        or len({session.online_id for session in sessions}) != 2
        or len({plan.player_id for plan in plans}) != 2
        or len({plan.mission_id for plan in plans}) != 1
    ):
        raise ValueError("Story co-op settlement requires two bound owners")
    try:
        request_bytes = bytes.fromhex(request_digest)
    except (TypeError, ValueError) as error:
        raise ValueError("Story co-op request digest must be SHA-256") from error
    if len(request_bytes) != 32 or request_digest != request_bytes.hex():
        raise ValueError("Story co-op request digest must be canonical SHA-256")
    prior = teams.state.get("story_group_rewards", {}).get(request_digest)
    if prior is not None:
        if (
            not isinstance(prior, dict)
            or prior.get("policy_version") != GROUP_ITEM_POLICY
            or not isinstance(prior.get("response_hex"), str)
        ):
            raise ValueError("Story co-op receipt needs review")
        response = bytes.fromhex(prior["response_hex"])
        replay = deepcopy(prior)
        replay["applied"] = False
        return response, replay

    payload_digest = hashlib.sha256(payload).hexdigest()
    prepared = []
    participant_rows = []
    for session, plan in zip(sessions, plans, strict=True):
        specs = item_specs(plan)
        earned_emblems = emblem_specs(plan)
        reason = ""
        capacity_floor = cursor_floor = 0
        try:
            authority = _reward_inventory_authority(teams, session, player_data_path)
            inventory, reserved = authority.inventory, authority.reservations
            capacity_floor, cursor_floor = authority.capacity_floor, authority.cursor_floor
        except (OSError, ProtocolError) as error:
            inventory, reserved = None, frozenset()
            reason = str(error) if isinstance(error, ProtocolError) else type(error).__name__
        known_emblems = _retained_emblem_keys(teams, session)
        if inventory is not None:
            known_emblems.update(owned_keys(inventory))
        emblems = tuple(
            dict(spec, identity=STORY_EMBLEM_IDENTITY)
            for spec in earned_emblems
            if (spec["selector"], spec["row_id"]) not in known_emblems
        )
        try:
            identities = (
                allocate_instances(
                    teams,
                    session,
                    inventory,
                    reserved,
                    len(specs),
                    allow_partial=True,
                    capacity_floor=capacity_floor,
                    cursor_floor=cursor_floor,
                )
                if specs
                else ()
            )
            if len(identities) < len(specs):
                reason = "Inventory capacity is not available"
        except (OSError, ProtocolError) as error:
            identities = ()
            reason = str(error) if isinstance(error, ProtocolError) else type(error).__name__
        items = _owned_items(session, specs[: len(identities)], identities)
        pending = list(specs[len(identities) :])
        prepared.append((plan, items, emblems))
        participant_digest = hashlib.sha256(
            b"acvd-story-coop-participant-v1\0"
            + request_bytes
            + plan.player_id.to_bytes(4, "big")
        ).hexdigest()
        participant_rows.append(
            {
                "session": session,
                "plan": plan,
                "digest": participant_digest,
                "items": items,
                "pending": pending,
                "emblems": emblems,
                "reason": reason,
            }
        )
    response = _register_story_group_response(tuple(prepared))
    payments = []
    for row in participant_rows:
        session, plan, digest = row["session"], row["plan"], row["digest"]
        receipt = {
            "online_id": session.online_id,
            "player_id": session.player_id,
            "result_digest": digest,
            "payload_sha256": payload_digest,
            "reply_hex": response.hex(),
            "reply_item_count": len(row["items"]),
            "items": list(row["items"]),
            "pending_specs": row["pending"],
            "emblems": list(row["emblems"]),
            "deferred_reason": row["reason"],
            "group_request_digest": request_digest,
        }
        payments.append(
            {
                "online_id": session.online_id,
                "player_id": session.player_id,
                "mission_id": plan.mission_id,
                "gross_earnings": plan.gross_pay,
                "expenses": plan.expenses,
                "reward_key": session.online_id + ":" + digest,
                "item_receipt": receipt,
            }
        )
    result = teams.settle_story_group_money(
        request_digest,
        tuple(payments),
        response_hex=response.hex(),
        policy_version=GROUP_ITEM_POLICY,
    )
    result["pending_item_count"] = sum(len(row["pending"]) for row in participant_rows)
    result["emblem_reward_count"] = sum(len(row["emblems"]) for row in participant_rows)
    result["reply_item_count"] = sum(len(row["items"]) for row in participant_rows)
    return response, result


def replay_group(
    teams: TeamStore,
    request_digest: str,
    *,
    payload: bytes,
    player_ids: tuple[int, int],
) -> tuple[bytes, dict] | None:
    """Replay either participant's request for one already-settled co-op result.

    Retail sends the same aggregate from both human clients with different
    authenticated request indices. The first request settles both owners. The
    second request is bound as a durable alias only while the identical result
    and owner order are inside the bounded debrief window. Exact aliased
    retransmissions replay forever; a later identical mission remains a new run.
    """
    try:
        request_bytes = bytes.fromhex(request_digest)
    except (TypeError, ValueError) as error:
        raise ValueError("Story co-op request digest must be SHA-256") from error
    if (
        len(request_bytes) != 32
        or request_digest != request_bytes.hex()
        or not isinstance(payload, bytes)
        or not 0 < len(payload) <= 4096
        or not isinstance(player_ids, tuple)
        or len(player_ids) != 2
        or len(set(player_ids)) != 2
        or any(type(player_id) is not int or not 0 < player_id < 1 << 32 for player_id in player_ids)
    ):
        raise ValueError("Story co-op replay identity is invalid")
    payload_digest = hashlib.sha256(payload).hexdigest()
    ledger = teams.state.get("story_group_rewards", {})
    if not isinstance(ledger, dict):
        raise ValueError("Story group reward history needs review")

    direct = None
    compatible = []
    now = time.time_ns()
    for key, row in ledger.items():
        if not isinstance(row, dict) or row.get("policy_version") != GROUP_ITEM_POLICY:
            continue
        aliases = row.get("request_aliases")
        participant_ids = row.get("participant_player_ids")
        settled = row.get("settled_at_unix_ns")
        response_hex = row.get("response_hex")
        if (
            not isinstance(key, str)
            or not isinstance(aliases, list)
            or not 1 <= len(aliases) <= 4
            or any(not isinstance(alias, str) for alias in aliases)
            or len(set(aliases)) != len(aliases)
            or key not in aliases
            or not isinstance(participant_ids, list)
            or len(participant_ids) != 2
            or any(type(player_id) is not int for player_id in participant_ids)
            or type(settled) is not int
            or settled <= 0
            or not isinstance(response_hex, str)
        ):
            raise ValueError("Story co-op receipt needs review")
        try:
            alias_bytes = [bytes.fromhex(alias) for alias in aliases]
            response = bytes.fromhex(response_hex)
        except ValueError as error:
            raise ValueError("Story co-op receipt needs review") from error
        if (
            any(len(value) != 32 or alias != value.hex() for alias, value in zip(aliases, alias_bytes, strict=True))
            or not response
            or len(response) > 8192
            or response_hex != response.hex()
        ):
            raise ValueError("Story co-op receipt needs review")
        matches_result = (
            row.get("payload_sha256") == payload_digest
            and tuple(participant_ids) == player_ids
        )
        if request_digest in aliases:
            if not matches_result or direct is not None:
                raise ValueError("Story co-op request alias conflicts with retained history")
            direct = (key, row, response)
        elif matches_result and 0 <= now - settled <= GROUP_ALIAS_WINDOW_NS:
            compatible.append((key, row, response))

    if direct is not None:
        _key, row, response = direct
    else:
        if not compatible:
            return None
        if len(compatible) != 1:
            raise ValueError("Story co-op result matches multiple debriefs")
        key, row, response = compatible[0]
        updated = deepcopy(teams.state)
        updated_row = updated["story_group_rewards"][key]
        updated_row["request_aliases"].append(request_digest)
        teams.commit_state(updated)
        row = updated_row
    replay = deepcopy(row)
    replay["applied"] = False
    return response, replay


def _retained_emblem_keys(teams: TeamStore, session: GameSession) -> set[tuple[int, int]]:
    """Return rows already delivered by current owner-bound Story receipts."""
    result: set[tuple[int, int]] = set()
    ledger = teams.state.get("story_rewards", {})
    if not isinstance(ledger, dict):
        raise ValueError("Story item ledger needs review")
    for receipt in ledger.values():
        if not isinstance(receipt, dict):
            raise ValueError("Story item receipt needs review")
        if (receipt.get("online_id"), receipt.get("player_id")) != (
            session.online_id,
            session.player_id,
        ):
            continue
        if receipt.get("policy_version") not in (
            PREVIOUS_ITEM_POLICY,
            ITEM_POLICY,
            GROUP_ITEM_POLICY,
        ):
            continue
        emblems = receipt.get("emblems", [])
        if not isinstance(emblems, list):
            raise ValueError("Story emblem receipt needs review")
        for emblem in emblems:
            if not isinstance(emblem, dict):
                raise ValueError("Story emblem receipt needs review")
            condition = tuple(emblem.get("condition", ()))
            category, row_id = emblem.get("category"), emblem.get("row_id")
            if (
                type(category) is not int
                or type(row_id) is not int
                or (category, row_id) not in STORY_EMBLEM_REWARDS.get(condition, ())
                or emblem.get("selector") != 0x20000 + category
                or emblem.get("identity") != STORY_EMBLEM_IDENTITY
            ):
                raise ValueError("Story emblem receipt needs review")
            result.add((emblem["selector"], emblem["row_id"]))
    return result


def _owned_items(session: GameSession, specs, identities) -> tuple[dict, ...]:
    return tuple(
        dict(
            spec,
            online_id=session.online_id,
            player_id=session.player_id,
            instance_id=identity,
            in_snapshot=False,
        )
        for spec, identity in zip(specs, identities, strict=True)
    )


def resolve_pending(
    teams: TeamStore,
    session: GameSession,
    *,
    player_data_path: Path | None = None,
    inventory: bytes | None = None,
    reserved_instance_ids: frozenset[int] = frozenset(),
    capacity_floor: int = 0,
    cursor_floor: int = 0,
) -> int:
    """Allocate earned items once complete ownership becomes available.

    Keep the original mission reply immutable. Delivery uses the normal owned-
    item cursor read, not a second cash settlement or a replacement reward reply.
    Limit one call to 96 items. Capacity can advance a prefix; the rest stays due.
    """
    selected = []
    for key, receipt in teams.state.get("story_rewards", {}).items():
        if (receipt.get("online_id"), receipt.get("player_id")) != (
            session.online_id,
            session.player_id,
        ):
            continue
        specs = receipt.get("pending_specs", [])
        if not specs:
            continue
        if receipt.get("policy_version") not in SUPPORTED_ITEM_POLICIES or not isinstance(
            specs, list
        ):
            raise ValueError("Pending Story reward policy needs review")
        for spec in specs:
            condition = tuple(spec["condition"])
            if (
                (spec["category"], spec["part_id"]) not in STORY_PART_REWARDS.get(condition, ())
                or PART_CATEGORY_FOR_CLASS.get(spec["part_class"]) != spec["category"]
                or spec["tuning"] != 1000
            ):
                raise ValueError("Pending Story item needs review")
            # Each pending entry belongs to its own committed completion.
            # Global part-type deduplication loses legitimate repeat rewards
            # and breaks the prefix-removal transaction below.
            selected.append((key, spec))
            if len(selected) == 96:
                break
        if len(selected) == 96:
            break
    if not selected:
        return 0
    if player_data_path is not None:
        inventory, reserved_instance_ids = authorized_inventory(teams, session, player_data_path)
    identities = allocate_instances(
        teams,
        session,
        inventory,
        reserved_instance_ids,
        len(selected),
        allow_partial=True,
        capacity_floor=capacity_floor,
        cursor_floor=cursor_floor,
    )
    if not identities:
        return 0
    updated = deepcopy(teams.state)
    selected = selected[: len(identities)]
    items = _owned_items(session, [spec for _, spec in selected], identities)
    for (key, _), item in zip(selected, items, strict=True):
        receipt = updated["story_rewards"][key]
        receipt["items"].append(item)
        receipt["pending_specs"].pop(0)
        if not receipt["pending_specs"]:
            receipt["deferred_reason"] = ""
    teams.commit_state(updated)
    return len(items)
