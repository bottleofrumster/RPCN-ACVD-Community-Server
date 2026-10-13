"""Authenticated profile and garage request handling.

Storage and wire replies are explicit dependencies, not dispatcher internals.
Pilot-name readback follows the observed self-property/response mapping.
Reconstruction-time garage defaults remain unresolved; no migration flags are guessed.
"""

from __future__ import annotations

from typing import Callable

from ..profile_values import personal_selector, pilot_name_bytes as _stored_pilot_name
from ..protocol import decode_proto, first_field, proto_bytes, proto_varint
from ..teams import TeamStore
from ..telemetry import Telemetry
from ..udp import GameMessage, GameSession


def get_comment(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    profile = teams.player_profile(session.online_id)
    status_value = 0
    comment = str(profile.get("status", ""))
    reply(proto_varint(1, status_value) + proto_bytes(2, comment))
    telemetry.emit(
        "game_comment_sent",
        peer=session.peer,
        online_id=session.online_id,
        status_value=status_value,
        comment=comment,
    )


def get_my_user_info(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    # ACVD's generated RequestGetMyUserInfoResponse::IsInitialized
    # requires all 13 presence bits. The serializer identifies fields
    # 2, 3, and 12 as strings and the remaining fields as scalars.
    # Conservative zero values avoid claiming team/world progress.
    team = teams.team_for(session.online_id)
    profile = teams.player_profile(session.online_id)
    team_id = int(team["team_id"]) if team is not None else 0
    # Retain the observed14/15 pair: changing these booleans did not by
    # itself suppress reconstruction. Historical matching-account successes
    # do not certify all saves; name/garage/reversion failures remain open.
    # Do not turn these fields into a guessed migration-bypass mechanism.
    # Preserve the existing self-response selector layout. Peer UserInfo uses
    # a distinct layout; personal country persistence still needs qualification.
    scalar_fields = {
        6: 0,
        8: personal_selector(profile, 5),
        9: personal_selector(profile, 6),
        10: personal_selector(profile, 7),
        14: 1,
        15: 0,
    }
    balance_known = "balance" in profile
    wallet_provisioned = False
    wallet_provisioning_eligible = set(profile).issubset({"player_id"})
    # A native brand-new save never emits SyncMoney: it creates both local cash
    # words as 100,000, but MyUserInfo field 11 is applied before Workshop and
    # used to display online cash.  Establish the same baseline durably for an
    # account that has neither a wallet nor team membership.  Historical
    # intact-save enrollment with team_id=0 emits SyncMoney later in this same
    # login, so the session flag lets that request replace this provisional
    # baseline once.  Team-retained reset remains a separate recovery path.
    if not balance_known and team is None and wallet_provisioning_eligible:
        response_balance, wallet_provisioned = teams.provision_new_balance(session.online_id)
        session.new_wallet_provisional = session.new_wallet_provisional or wallet_provisioned
    else:
        response_balance = teams.balance(session.online_id, initial=0)
    response_proto = (
        proto_varint(1, session.player_id)
        + proto_bytes(2, session.online_id)
        # Native one-field probe writes response field 3 to SETTING.DAT's
        # 32-byte UTF-16BE pilot-name slot. Field 2 remains the account identity.
        + proto_bytes(3, _stored_pilot_name(profile, session.online_id))
        + proto_varint(5, team_id)
        + proto_varint(6, scalar_fields[6])
        + proto_varint(8, scalar_fields[8])
        + proto_varint(9, scalar_fields[9])
        + proto_varint(10, scalar_fields[10])
        + proto_varint(11, response_balance)
        + proto_bytes(12, str(profile.get("status", "")))
        # This is the member's two-digit team slot.  Retail accepts
        # zero for the leader but requires joined members to match
        # TeamInfo.MemberInfo field 4.
        + proto_varint(13, int(profile.get("member_id", 0)))
        + proto_varint(14, scalar_fields[14])
        + proto_varint(15, scalar_fields[15])
    )
    reply(response_proto)
    telemetry.emit(
        "game_user_info_sent",
        peer=session.peer,
        online_id=session.online_id,
        player_id=session.player_id,
        team_id=team_id,
        balance_known=balance_known,
        balance_sent=response_balance,
        wallet_provisioned=wallet_provisioned,
        wallet_provisioning_eligible=wallet_provisioning_eligible,
        wallet_provisional_for_login=session.new_wallet_provisional,
        field_6=scalar_fields[6],
        field_8=scalar_fields[8],
        field_9=scalar_fields[9],
        field_10=scalar_fields[10],
        field_14=scalar_fields[14],
        field_15=scalar_fields[15],
    )


def update_user_property(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    scalar_fields = {
        field.number: int(field.value) for field in fields if isinstance(field.value, int)
    }
    byte_fields = {field.number: field.value for field in fields if isinstance(field.value, bytes)}
    teams.update_user_properties(
        session.online_id,
        scalar_fields=scalar_fields,
        byte_fields=byte_fields,
    )
    reply(b"")
    telemetry.emit(
        "game_user_property_updated",
        peer=session.peer,
        online_id=session.online_id,
        scalar_fields=scalar_fields,
        byte_field_lengths={number: len(value) for number, value in byte_fields.items()},
    )


def update_member_team_info(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    notify_team_profile: Callable[..., list[str]] | None = None,
) -> None:
    fields = decode_proto(message.payload)
    member_field = first_field(fields, 1)
    secondary_field = first_field(fields, 2)
    member_id = (
        int(member_field.value)
        if member_field is not None and isinstance(member_field.value, int)
        else 0
    )
    secondary = (
        int(secondary_field.value)
        if secondary_field is not None and isinstance(secondary_field.value, int)
        else 0
    )
    team = teams.team_for(session.online_id)
    team_id = int((team or {}).get("team_id", 0))
    try:
        profile = teams.update_member_team_info(
            session.online_id,
            member_id=member_id,
            secondary=secondary,
        )
    except ValueError as error:
        # Finish the request, then make every live teammate reload the unchanged
        # canonical roster. Leaving the request unanswered produces only a
        # network warning and can strand the editor on its optimistic local ID.
        reply(b"")
        recipients = (
            notify_team_profile(
                team_id,
                reason="member_number_conflict",
                subject_online_id=session.online_id,
            )
            if notify_team_profile is not None and team_id > 0
            else []
        )
        telemetry.emit(
            "game_member_team_info_rejected",
            peer=session.peer,
            online_id=session.online_id,
            requested_member_id=member_id,
            requested_secondary=secondary,
            current_member_id=int(teams.player_profile(session.online_id).get("member_id", 0)),
            reason=str(error),
            profile_update_recipient_online_ids=recipients,
        )
        return
    reply(b"")
    recipients = (
        notify_team_profile(
            team_id,
            reason="member_number_changed",
            subject_online_id=session.online_id,
        )
        if notify_team_profile is not None and team_id > 0
        else []
    )
    telemetry.emit(
        "game_member_team_info_updated",
        peer=session.peer,
        online_id=session.online_id,
        member_id=int(profile.get("member_id", 0)),
        secondary=int(profile.get("member_team_secondary", 0)),
        profile_update_recipient_online_ids=recipients,
    )


def update_team_member_comment(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    comment_field = first_field(fields, 1)
    comment = (
        comment_field.value
        if comment_field is not None and isinstance(comment_field.value, bytes)
        else b""
    )
    profile = teams.update_member_comment(session.online_id, comment)
    reply(b"")
    telemetry.emit(
        "game_team_member_comment_updated",
        peer=session.peer,
        online_id=session.online_id,
        status=str(profile.get("status", "")),
    )


def get_garage_custom_info(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    stored = teams.garage_custom_info(session.online_id)
    garage_info = proto_varint(1, stored["primary"]) + proto_varint(2, stored["secondary"])
    reply(proto_bytes(1, garage_info))
    telemetry.emit(
        "game_garage_custom_info_sent",
        peer=session.peer,
        online_id=session.online_id,
        primary=stored["primary"],
        secondary=stored["secondary"],
    )


def register_garage_custom_info(
    session: GameSession,
    message: GameMessage,
    *,
    teams: TeamStore,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
) -> None:
    fields = decode_proto(message.payload)
    info_field = first_field(fields, 1)
    if info_field is None or not isinstance(info_field.value, bytes):
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason="RequestRegisterGarageCustomInfo requires embedded field 1",
        )
        return
    info_fields = decode_proto(info_field.value)
    primary_field = first_field(info_fields, 1)
    secondary_field = first_field(info_fields, 2)
    if (
        primary_field is None
        or not isinstance(primary_field.value, int)
        or secondary_field is None
        or not isinstance(secondary_field.value, int)
    ):
        telemetry.emit(
            "game_message_rejected",
            peer=session.peer,
            online_id=session.online_id,
            reason="GarageCustomInfo requires integer fields 1 and 2",
        )
        return
    stored = teams.set_garage_custom_info(
        session.online_id,
        int(primary_field.value),
        int(secondary_field.value),
    )
    # RequestRegisterGarageCustomInfoResponse has one required scalar.
    reply(proto_varint(1, 0))
    telemetry.emit(
        "game_garage_custom_info_registered",
        peer=session.peer,
        online_id=session.online_id,
        primary=stored["primary"],
        secondary=stored["secondary"],
    )
