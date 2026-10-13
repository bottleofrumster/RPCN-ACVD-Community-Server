"""Money synchronization, transfers and battle settlement request handlers.

The transport supplies an already-decoded message, authenticated session and a
correlated reply callback. TeamStore owns atomic balance changes and settlement
deduplication. This extraction does not add reward grants or change validation.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Callable, Mapping

from ..hire_bindings import HireBindings
from ..protocol import ProtocolError, decode_proto, first_field, proto_varint
from ..rewards import (
    _register_battle_result_response,
    story_result_owner_ids,
    story_reward_plan,
    story_reward_plans,
)
from ..story_items import (
    replay_group as replay_story_group,
    request_identity,
    settle as settle_story_items,
    settle_group as settle_story_group,
)
from ..teams import MAX_BALANCE, TeamStore
from ..telemetry import Telemetry
from ..udp import GameMessage, GameSession


def trade_money(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    initial_balance: int,
    reply_error: Callable[[int, int], None] | None = None,
) -> None:
    """Validate the captured recipient/amount fields, then commit before ACK."""
    fields = decode_proto(message.payload)
    recipient_field = first_field(fields, 1)
    amount_field = first_field(fields, 3)
    if (
        recipient_field is None
        or not isinstance(recipient_field.value, int)
        or amount_field is None
        or not isinstance(amount_field.value, int)
    ):
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason=("RequestTradeMoney requires recipient player ID field 1 and amount field 3"),
        )
        return
    recipient_player_id = int(recipient_field.value)
    amount = int(amount_field.value)
    recipient = teams.member_by_player_id(recipient_player_id)
    if recipient_player_id == session.player_id or recipient is None:
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason="RequestTradeMoney does not identify one known non-self recipient",
            recipient_player_id=recipient_player_id,
            amount=amount,
        )
        return
    # Bind retries to this authenticated request, not just recipient/amount.
    # Distinct intentional payments and new auth sessions must remain distinct.
    request_digest = hashlib.sha256(
        b"acvd-trade-money-v1\0"
        + session.auth_token
        + session.online_id.encode("utf-8")
        + b"\0"
        + message.message_index
        + message.packet_counter.to_bytes(2, "big")
        + message.payload
    ).hexdigest()
    transfer = teams.transfer_money(
        session.online_id,
        recipient_player_id,
        amount,
        initial=initial_balance,
        request_digest=request_digest,
    )
    if transfer is None:
        sender_known = "balance" in teams.player_profile(session.online_id)
        recipient_known = "balance" in teams.player_profile(recipient[0])
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason="RequestTradeMoney transfer validation failed",
            recipient_player_id=recipient_player_id,
            amount=amount,
            sender_balance_stored=sender_known,
            recipient_balance_stored=recipient_known,
        )
        if reply_error is not None and amount > 0 and not (sender_known and recipient_known):
            # Shipped ServerRequestResultCode/ServerExtErrorCode: failure/data
            # not found. Unknown funds are not a zero balance or an overdraft.
            # Only this recovered case changes; other rejection policy is deferred.
            reply_error(1, 28)
        return
    # Retail's RequestTradeMoneyResponse is an empty acknowledgement;
    # the client applies its local display delta after success.
    reply(b"")
    telemetry.emit(
        "game_money_trade_replayed" if transfer.get("applied") is False else "game_money_traded",
        peer=session.peer,
        **transfer,
    )


def sync_money(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    """Import native offline cash on first enrollment; retain known wallets."""
    try:
        if len(message.payload) > 40:
            raise ProtocolError("SyncMoney exceeds the native scalar form")
        fields = decode_proto(message.payload)
        if len(fields) != 3 or {field.number for field in fields} != {2, 3, 4}:
            raise ProtocolError("SyncMoney requires unique fields 2, 3 and 4")
        if any(field.wire_type != 0 or type(field.value) is not int for field in fields):
            raise ProtocolError("SyncMoney requires integer fields")
        values = {field.number: field.value for field in fields}
        # The native builder sign-extends both 32-bit saved cash words to
        # 64-bit fields. Decode their two's-complement wire form before range
        # validation; DateTime field 2 remains unsigned.
        for number in (3, 4):
            if not 0 <= values[number] < 1 << 64:
                raise ProtocolError("SyncMoney cash field is outside int64")
            if values[number] >= 1 << 63:
                values[number] -= 1 << 64
        balance, enrolled = teams.enroll_balance(
            session.online_id,
            client_time=values[2],
            offline_balance=values[3],
            online_balance=values[4],
            replace_new_save_provisional=session.new_wallet_provisional,
        )
        session.new_wallet_provisional = False
    except (ProtocolError, ValueError) as error:
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason=str(error),
        )
        return
    reply(proto_varint(1, balance))
    telemetry.emit(
        "game_money_synchronized",
        peer=session.peer,
        online_id=session.online_id,
        balance=balance,
        offline_balance=values[3],
        previous_online_balance=values[4],
        client_time=values[2],
        wallet_enrolled=enrolled,
        policy="offline-first-v1",
    )


def register_battle_result(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    initial_balance: int,
    player_data_path: Path | None = None,
    game_sessions: Mapping[bytes, GameSession] | None = None,
    team_sessions: dict[int, dict] | None = None,
    purge_team_sessions: Callable[[], None] | None = None,
    hire_bindings: HireBindings | None = None,
) -> None:
    """Apply solo or authenticated two-human Story rewards atomically."""
    try:
        owner_ids = story_result_owner_ids(message.payload)
        if session.player_id not in owner_ids:
            raise ValueError("Story result submitter is not one of its owners")
        if len(owner_ids) == 2:
            if player_data_path is None:
                raise ValueError("Story co-op rewards require player storage")
            group_digest = request_identity(session, message)
            replayed = replay_story_group(
                teams,
                group_digest,
                payload=message.payload,
                player_ids=owner_ids,
            )
            if replayed is not None:
                response, settlement = replayed
                reply(response)
                telemetry.emit(
                    "game_story_coop_result_registered",
                    peer=session.peer,
                    online_id=session.online_id,
                    player_id=session.player_id,
                    participant_player_ids=list(owner_ids),
                    applied=False,
                    policy_version=settlement.get("policy_version"),
                    replay=True,
                )
                return
            owners = _story_group_sessions(
                session,
                owner_ids,
                game_sessions=game_sessions,
                team_sessions=team_sessions,
                purge_team_sessions=purge_team_sessions,
                hire_bindings=hire_bindings,
            )
            plans = story_reward_plans(message.payload, owner_ids)
            response, settlement = settle_story_group(
                teams,
                owners,
                plans,
                message.payload,
                player_data_path=player_data_path,
                request_digest=group_digest,
            )
            reply(response)
            telemetry.emit(
                "game_story_coop_result_registered",
                peer=session.peer,
                online_id=session.online_id,
                player_id=session.player_id,
                mission_id=plans[0].mission_id,
                participant_player_ids=list(owner_ids),
                gross_pay_by_player={str(plan.player_id): plan.gross_pay for plan in plans},
                expenses_by_player={str(plan.player_id): plan.expenses for plan in plans},
                subquest_ids=plans[0].subquest_ids,
                reply_item_count=settlement.get("reply_item_count", 0),
                deferred_item_count=settlement.get("pending_item_count", 0),
                emblem_reward_count=settlement.get("emblem_reward_count", 0),
                applied=settlement.get("applied", False),
                policy_version=settlement.get("policy_version"),
            )
            return

        plan = story_reward_plan(message.payload, session.player_id)
        profile = teams.player_profile(session.online_id)
        if type(profile.get("balance")) is not int or not -MAX_BALANCE <= profile["balance"] <= MAX_BALANCE:
            raise ValueError("Story settlement requires a known wallet balance")
        if player_data_path is not None:
            response, settlement, item_count = settle_story_items(
                teams,
                session,
                plan,
                message.payload,
                player_data_path=player_data_path,
                request_digest=request_identity(session, message),
            )
        else:
            response = _register_battle_result_response(plan)
            item_count = 0
            settlement = teams.settle_battle_money(
                session.online_id,
                hashlib.sha256(message.payload).hexdigest(),
                mission_id=plan.mission_id,
                gross_earnings=plan.gross_pay,
                expenses=plan.expenses,
                initial=0,
                policy_version=plan.policy_version,
            )
    except (ProtocolError, ValueError) as error:
        telemetry.emit("game_story_reward_rejected", online_id=session.online_id, reason=str(error))
        reply(b"")
        return
    reply(response)
    telemetry.emit(
        "game_battle_result_registered",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        mission_id=plan.mission_id,
        unit_count=plan.unit_count,
        battle_reward_count=1,
        quest_reward_count=len(plan.cash_subquests),
        subquest_ids=plan.subquest_ids,
        base_pay=plan.base_pay,
        bonus_pay=plan.bonus_pay,
        item_reward_count=item_count,
        emblem_reward_count=settlement.get("emblem_reward_count", 0),
        deferred_item_count=settlement.get("pending_item_count", 0),
        money_settlement=settlement,
        policy_version=settlement.get("policy_version", plan.policy_version),
    )


def _story_group_sessions(
    submitter: GameSession,
    owner_ids: tuple[int, int],
    *,
    game_sessions: Mapping[bytes, GameSession] | None,
    team_sessions: dict[int, dict] | None,
    purge_team_sessions: Callable[[], None] | None,
    hire_bindings: HireBindings | None,
) -> tuple[GameSession, GameSession]:
    """Bind a two-owner Story aggregate to one current live relationship."""
    if game_sessions is None:
        raise ValueError("Story co-op result has no live session directory")
    now = time.monotonic()
    resolved = []
    for player_id in owner_ids:
        candidates = [
            candidate
            for candidate in game_sessions.values()
            if candidate.player_id == player_id
            and candidate.established
            and candidate.peer is not None
            and now - candidate.last_seen_monotonic <= 90
        ]
        if len(candidates) != 1:
            raise ValueError("Story co-op owner is not uniquely live")
        resolved.append(candidates[0])
    if all(owner is not submitter for owner in resolved):
        raise ValueError("Story co-op submitter session changed")
    expected_names = {owner.online_id for owner in resolved}
    if purge_team_sessions is not None:
        purge_team_sessions()
    same_team_session = any(
        isinstance(directory, dict)
        and isinstance(directory.get("members"), dict)
        # A Story squad can be a subset of the current team lobby. Other
        # authenticated teammates who stayed in the lobby did not participate
        # in the result and must not invalidate its two actual owners.
        and expected_names <= set(directory["members"])
        and set(owner_ids)
        <= {
            record.get("player_id")
            for record in directory["members"].values()
            if isinstance(record, dict)
        }
        for directory in (team_sessions or {}).values()
    )
    accepted_hire = False
    if hire_bindings is not None:
        accepted_hire = any(
            (
                binding := hire_bindings.resolve(employer, mercenary.player_id)
            ) is not None
            and binding.mercenary is mercenary
            for employer in resolved
            for mercenary in resolved
            if employer is not mercenary
        )
    if not same_team_session and not accepted_hire:
        raise ValueError("Story co-op owners lack one current team-session or hire binding")
    return resolved[0], resolved[1]
