"""Mercenary availability, profile and central negotiation request handling.

The dispatcher supplies the shared active-registration set and authoritative
live-ID query. Durable listings alone do not establish availability. Contract
queueing is not proof of P2P delivery, acceptance or mission readiness.
"""

from __future__ import annotations

from typing import Callable

from ..hire_bindings import HireBindings
from ..mercenary_messages import (
    _mercenary_continuation_pairs,
    _mercenary_message_list_response,
    _mercenary_rank_response,
    _mercenary_ranking_list_response,
    _mercenary_search_response,
    _mercenary_self_ranking_response,
)
from ..protocol import ProtocolError, decode_proto, first_field, proto_bytes, proto_varint
from ..rankings import decode_page, decode_self_season, mercenary_rows, own_row, page
from ..teams import TeamStore
from ..telemetry import Telemetry
from ..udp import START_MERCENARY_NEGOTIATION, GameMessage, GameSession


def get_rank(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    profile = team_store.player_profile(session.online_id)
    rank = profile.get("mercenary_rank", 0)
    if type(rank) is not int or not 0 <= rank <= 6:
        telemetry.emit(
            "game_ranking_rejected",
            peer=session.peer,
            online_id=session.online_id,
            message_type=message.message_type,
            reason="Stored mercenary rank needs review",
        )
        reply(_mercenary_rank_response())
        return
    reply(_mercenary_rank_response(rank))
    telemetry.emit(
        "game_mercenary_rank_sent",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        rank=rank,
    )


def get_self_ranking(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    team_name = str((team_store.team_for(session.online_id) or {}).get("name", ""))
    try:
        season_id = decode_self_season(message.payload)
        rows = mercenary_rows(team_store) if season_id == team_store.world_season_id() else ()
        row = own_row(rows, session.player_id)
    except (ProtocolError, ValueError) as error:
        reply(
            _mercenary_self_ranking_response(
                session.online_id, team_name, player_id=session.player_id
            )
        )
        telemetry.emit(
            "game_ranking_rejected",
            peer=session.peer,
            online_id=session.online_id,
            message_type=message.message_type,
            reason=str(error),
        )
        return
    reply(
        _mercenary_self_ranking_response(
            session.online_id, team_name, row, player_id=session.player_id
        )
    )
    telemetry.emit(
        "game_mercenary_self_ranking_sent",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        team_name=team_name,
        season_id=season_id,
        rank=(row or {}).get("rank", 0),
        score=(row or {}).get("score", 0),
    )


def get_rankings(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    try:
        request = decode_page(message.payload, mercenary=True)
        rows = (
            mercenary_rows(team_store) if request.season_id == team_store.world_season_id() else ()
        )
    except (ProtocolError, ValueError) as error:
        reply(_mercenary_ranking_list_response())
        telemetry.emit(
            "game_ranking_rejected",
            peer=session.peer,
            online_id=session.online_id,
            message_type=message.message_type,
            reason=str(error),
        )
        return
    selected = page(rows, request)
    reply(_mercenary_ranking_list_response(selected, len(rows)))
    telemetry.emit(
        "game_mercenary_ranking_list_sent",
        peer=session.peer,
        online_id=session.online_id,
        season_id=request.season_id,
        offset=request.offset,
        count=len(selected),
        total=len(rows),
    )


def register(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    active_mercenaries: set[str],
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    hire_bindings: HireBindings | None = None,
) -> None:
    fields = decode_proto(message.payload)
    ac_field = first_field(fields, 3)
    if ac_field is None or not isinstance(ac_field.value, bytes):
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason="RequestRegisterMercenary is missing bytes field 3",
        )
        return

    def integer(number: int) -> int:
        field = first_field(fields, number)
        return field.value if field is not None and isinstance(field.value, int) else 0

    registration = team_store.register_mercenary(
        online_id=session.online_id,
        player_id=session.player_id,
        ac_data=ac_field.value,
        result_values=[integer(number) for number in range(4, 8)],
    )
    active_mercenaries.add(session.online_id)
    if hire_bindings is not None:
        hire_bindings.discard(session.online_id)
    # RequestRegisterMercenaryResponse field 1 is the asynchronous
    # job-status boolean, not a generic success flag. Returning true
    # immediately makes the client consume its built-in R.I.P. fallback
    # job, repeat registration, and unregister seconds later. False
    # leaves the durable listing active while it waits for a real hire.
    reply(proto_varint(1, 0))
    telemetry.emit(
        "game_mercenary_registered",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        ac_data_length=len(ac_field.value),
        result_values=registration["result_values"],
        job_ready=False,
    )


def continue_registration(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    active_mercenaries: set[str],
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    pairs = _mercenary_continuation_pairs(message.payload)
    registration = team_store.mercenary_for_player_id(session.player_id)
    continued = (
        registration is not None and str(registration.get("online_id", "")) == session.online_id
    )
    if continued:
        active_mercenaries.add(session.online_id)
    # The retail caller exposes no response object; successful
    # continuation is an empty acknowledgement. A queued employer is
    # delivered through a separate matching/push path, not this reply.
    reply(b"")
    telemetry.emit(
        "game_mercenary_registration_continued",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        continued=continued,
        matching_pairs=pairs,
        request_payload=message.payload,
    )


def unregister(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    active_mercenaries: set[str],
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    hire_bindings: HireBindings | None = None,
) -> None:
    fields = decode_proto(message.payload)
    enabled_field = first_field(fields, 1)
    client_value = (
        bool(enabled_field.value)
        if enabled_field is not None and isinstance(enabled_field.value, int)
        else False
    )
    # The retail client sends field 1=false when leaving registration.
    # It is not an enable/disable guard: the request opcode itself is
    # the unambiguous removal operation. Treating false as a no-op left
    # departed pilots visible in durable search state.
    if hire_bindings is not None:
        flags = [field for field in fields if field.number == 1]
        accepted = len(flags) == 1 and flags[0].wire_type == 0 and flags[0].value == 1
        bound = hire_bindings.unregister(session, accepted)
        telemetry.emit(
            "game_mercenary_hire_binding",
            online_id=session.online_id,
            accepted_signal=accepted,
            bound=bound,
        )
    active_mercenaries.discard(session.online_id)
    removed = team_store.unregister_mercenary(session.online_id)
    reply(b"")
    telemetry.emit(
        "game_mercenary_unregistered",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        client_value=client_value,
        removed=removed,
    )


def search(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    live_mercenary_online_ids: Callable[[], set[str]],
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    all_registrations = team_store.available_mercenaries(exclude_online_id=session.online_id)
    live_online_ids = live_mercenary_online_ids()
    live_registrations = [
        registration
        for registration in all_registrations
        if str(registration.get("online_id", "")) in live_online_ids
    ]
    requester_team = team_store.team_for(session.online_id)
    requester_team_id = int((requester_team or {}).get("team_id", 0))
    registrations = []
    ignored_same_team_online_ids = []
    for registration in live_registrations:
        candidate_online_id = str(registration.get("online_id", ""))
        candidate_team_id = int((team_store.team_for(candidate_online_id) or {}).get("team_id", 0))
        if requester_team_id > 0 and candidate_team_id == requester_team_id:
            ignored_same_team_online_ids.append(candidate_online_id)
            continue
        registrations.append(registration)
    entries = [
        (registration, team_store.team_for(str(registration.get("online_id", ""))))
        for registration in registrations
    ]
    reply(_mercenary_search_response(entries))
    telemetry.emit(
        "game_mercenary_search_sent",
        peer=session.peer,
        online_id=session.online_id,
        count=len(entries),
        candidate_online_ids=[registration.get("online_id", "") for registration in registrations],
        ignored_stale_count=len(all_registrations) - len(live_registrations),
        ignored_same_team_online_ids=ignored_same_team_online_ids,
        request_payload=message.payload,
    )


def get_data(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    live_mercenary_online_ids: Callable[[], set[str]],
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    player_field = first_field(fields, 1)
    player_id = (
        player_field.value
        if player_field is not None and isinstance(player_field.value, int)
        else 0
    )
    registration = team_store.mercenary_for_player_id(player_id)
    if str((registration or {}).get("online_id", "")) not in live_mercenary_online_ids():
        registration = None
    # RequestGetMercenaryData consumes the same registration/profile
    # payload advertised in MercenarySearchResultInfo field 4.  A
    # decoded garage AC snapshot is a different schema; native clients
    # reject it before opening the mercenary profile.
    ac_data = registration.get("ac_data", b"") if registration is not None else b""
    if not isinstance(ac_data, bytes):
        ac_data = b""
    reply(proto_bytes(1, ac_data))
    telemetry.emit(
        "game_mercenary_data_sent",
        peer=session.peer,
        online_id=session.online_id,
        requested_player_id=player_id,
        candidate_online_id=(registration or {}).get("online_id", ""),
        ac_data_length=len(ac_data),
        encoding=ac_data[:1].hex() if ac_data else "empty",
        found=registration is not None,
    )


def queue_negotiation(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    live_mercenary_online_ids: Callable[[], set[str]],
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    hire_bindings: HireBindings | None = None,
) -> None:
    if message.message_type != START_MERCENARY_NEGOTIATION:
        raise ProtocolError("Only StartNegotiate can create a hire offer")
    # Post-contract 0x03D2 belongs to feedback.py, never this offer inbox.
    fields = decode_proto(message.payload)
    target_field = first_field(fields, 1)
    target_pairs = [
        (
            target_field.value
            if target_field is not None and isinstance(target_field.value, int)
            else 0,
            0,
        )
    ]
    queued_messages = []
    target_online_ids = []
    rejected_same_team_player_ids = []
    employer_team_id = int((team_store.team_for(session.online_id) or {}).get("team_id", 0))
    for target_player_id, _message_kind in target_pairs:
        target = team_store.mercenary_for_player_id(target_player_id)
        if str((target or {}).get("online_id", "")) not in live_mercenary_online_ids():
            target = None
        target_online_id = str((target or {}).get("online_id", ""))
        target_online_ids.append(target_online_id)
        if target is None:
            continue
        target_team_id = int((team_store.team_for(target_online_id) or {}).get("team_id", 0))
        if employer_team_id > 0 and target_team_id == employer_team_id:
            rejected_same_team_player_ids.append(int(target_player_id))
            continue
        queued = team_store.queue_mercenary_message(
            target_player_id=target_player_id,
            employer_online_id=session.online_id,
            employer_player_id=session.player_id,
            employer_team_name=str((team_store.team_for(session.online_id) or {}).get("name", "")),
        )
        if queued is None:
            continue
        queued_messages.append(queued)
        if hire_bindings is not None:
            hire_bindings.offer(session, int(target_player_id))
    reply(b"")
    telemetry.emit(
        "game_mercenary_message_queued",
        peer=session.peer,
        online_id=session.online_id,
        request_type=message.message_type,
        target_pairs=target_pairs,
        target_online_ids=target_online_ids,
        message_ids=[item.get("message_id", 0) for item in queued_messages],
        queued_count=len(queued_messages),
        rejected_same_team_player_ids=rejected_same_team_player_ids,
    )


def get_messages(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    live_mercenary_online_ids: Callable[[], set[str]],
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    messages = (
        team_store.mercenary_messages_for(session.online_id)
        if session.online_id in live_mercenary_online_ids()
        else []
    )
    reply(_mercenary_message_list_response(messages))
    telemetry.emit(
        "game_mercenary_message_list_sent",
        peer=session.peer,
        online_id=session.online_id,
        message_ids=[item.get("message_id", 0) for item in messages],
        count=len(messages),
    )


def cancel_negotiation(
    session: GameSession,
    message: GameMessage,
    *,
    team_store: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    hire_bindings: HireBindings | None = None,
) -> None:
    if hire_bindings is not None:
        hire_bindings.discard(session.online_id)
    removed = team_store.cancel_mercenary_messages(session.online_id)
    reply(b"")
    telemetry.emit(
        "game_mercenary_negotiation_cancelled",
        peer=session.peer,
        online_id=session.online_id,
        removed=removed,
    )
