"""Preparation and room-handoff encoders, independent of match scheduling.

Keep the wire contract separate from role selection and release ordering.
The shared Extermination type passes private Normal gameplay. Result submission,
other modes, remote room discovery, and repeatability remain separate gates."""

from __future__ import annotations

from .protocol import proto_bytes, proto_varint

# Legacy synthesized-room helper only. Native 0x03BC archives pass unchanged.
ACVD_MATCHING2_SESSION_PASSWORD = b"PRVTPSWD"
NORMAL_BATTLE_TYPE_EXTERMINATION = 1
NORMAL_BATTLE_TYPE_STORED_TERRITORY = 3
NORMAL_BATTLE_TYPE_OFFLINE_TERRITORY = 4
NORMAL_BATTLE_TYPE_NORMAL_WEAPONS = 5
NORMAL_BATTLE_TYPE_SPECIAL_WEAPONS = 6
INTERWAR_BOSS_TYPE = 6


def interwar_boss_result(prepare_id: int, area_id: int) -> bytes:
    """Encode the native-qualified single-squad boss result without a peer key."""
    if type(prepare_id) is not int or not 0 < prepare_id < 1 << 64:
        raise ValueError("Preparation ID must be a positive uint64")
    if type(area_id) is not int or not 0 <= area_id <= 7:
        raise ValueError("Stored area must be an integer from 0 through 7")
    # In grace mode the type-6 builder uses the client's selected mission.
    # Counterpart zero is qualified for this path, not for paired PvP.
    values = {1: prepare_id, 2: 0, 3: 1, 4: INTERWAR_BOSS_TYPE, 5: 1, 6: area_id, 7: 0, 8: 0, 9: 1}
    return b"".join(proto_varint(number, value) for number, value in values.items())


def _register_prepare_response(
    prepare_id: int = 1,
    *,
    state: int = 1,
    token: str = "",
) -> bytes:
    """Initialized retail RegisterPrepare result."""
    prepare_id = max(1, int(prepare_id))
    return (
        proto_varint(1, prepare_id)
        + proto_varint(2, prepare_id)
        + proto_varint(3, max(0, int(state)))
        + proto_bytes(4, token)
    )


def _matching_result_push(
    prepare_id: int,
    counterpart_prepare_id: int,
    *,
    is_host: bool,
    criteria: dict[int, int] | None = None,
    result_code: int = 0,
    field_overrides: dict[int, int] | None = None,
    allow_missing_counterpart: bool = False,
) -> bytes:
    """Return a fully initialized nine-field PushRequestMatchingResult.

    Field 4 selects the battle type. Field 5 selects the side/role builder.
    Use the member encoder for the qualified Normal default. Zero battle type
    selects a fallback, not a playable unconstrained match.
    """
    # The retail message's IsInitialized implementation requires all nine
    # presence bits.  Fields 1-2 are 64-bit scalars, 3 and 9 are booleans,
    # fields 4-7 are unsigned scalars, and field 8 is the signed result code.
    # The member encoder supplies the qualified battle type and side fields.
    # Keep the low-level encoding available for explicit protocol tests.
    criteria = criteria or {}
    values = {
        # Keep recipient/counterpart prepare IDs rather than exposing one
        # replacement-server-private match ID.
        1: max(1, int(prepare_id)),
        2: (
            max(0, int(counterpart_prepare_id))
            if allow_missing_counterpart
            else max(1, int(counterpart_prepare_id))
        ),
        # Retain the best observed role/action values. Direct predicate traces
        # reject every non-error state code and both early/late result orderings.
        3: 1 if is_host else 0,
        4: 0,
        5: 0,
        6: 0,
        7: 0,
        8: max(0, int(result_code)),
        9: 1,
    }
    if field_overrides:
        values.update(
            {
                int(number): max(0, int(value))
                for number, value in field_overrides.items()
                if 3 <= int(number) <= 9
            }
        )
    return b"".join(proto_varint(number, value) for number, value in values.items())


def active_single_team_result(
    prepare_id: int,
    *,
    battle_type: int,
    area_id: int,
    territory_layout_owner_id: int = 0,
) -> bytes:
    """Encode one active-war attacking squad without a fabricated counterpart."""
    if type(prepare_id) is not int or not 0 < prepare_id < 1 << 64:
        raise ValueError("Preparation ID must be a positive uint64")
    if type(battle_type) is not int or battle_type not in (
        NORMAL_BATTLE_TYPE_OFFLINE_TERRITORY,
        NORMAL_BATTLE_TYPE_NORMAL_WEAPONS,
        NORMAL_BATTLE_TYPE_SPECIAL_WEAPONS,
    ):
        raise ValueError("Unsupported single-team battle type")
    if type(area_id) is not int or not 1 <= area_id <= 7:
        raise ValueError("Active-war single-team area must be from 1 through 7")
    if type(territory_layout_owner_id) is not int or not 0 <= territory_layout_owner_id < 1 << 32:
        raise ValueError("Territory layout owner must be an unsigned 32-bit integer")
    if battle_type == NORMAL_BATTLE_TYPE_OFFLINE_TERRITORY:
        if territory_layout_owner_id == 0:
            raise ValueError("Offline-territory missions require an exact layout owner")
    elif territory_layout_owner_id != 0:
        raise ValueError("NPC weapon missions do not use a territory layout owner")
    return _matching_result_push(
        prepare_id,
        0,
        is_host=True,
        allow_missing_counterpart=True,
        field_overrides={
            4: battle_type,
            5: 1,
            6: area_id,
            7: territory_layout_owner_id,
            8: 0,
            # One squad fighting enemy NPCs is not an intra-faction drill.
            # The true bit incorrectly displays the Practice Battle notice.
            9: 0,
        },
    )


def _matching_result_for_members(
    recipient: dict,
    counterpart: dict,
    *,
    criteria: dict[int, int],
    is_host: bool,
    combat_area_id: int = 0,
    territory_layout_owner_id: int = 0,
    is_intra_faction: bool = True,
    battle_type: int = NORMAL_BATTLE_TYPE_EXTERMINATION,
) -> bytes:
    """Build a matching result from two canonical preparation records."""
    if (
        isinstance(combat_area_id, bool)
        or not isinstance(combat_area_id, int)
        or not 0 <= combat_area_id <= 7
    ):
        raise ValueError("combat area must be an integer from 0 through 7")
    if type(territory_layout_owner_id) is not int or not 0 <= territory_layout_owner_id < 1 << 32:
        raise ValueError("territory layout owner must be an unsigned 32-bit integer")
    if type(is_intra_faction) is not bool:
        raise ValueError("intra-faction classification must be a boolean")
    if type(battle_type) is not int or battle_type not in (1, 2, 3):
        raise ValueError("Unsupported paired battle type")
    return _matching_result_push(
        int(recipient["prepare_id"]),
        int(counterpart["prepare_id"]),
        is_host=is_host,
        criteria=criteria,
        field_overrides={
            # A shared type is separate from the asymmetric side/role fields.
            # Paired types exclude the attacker-only NPC and boss branches.
            4: battle_type,
            5: 1 if is_host else 0,
            # Field 6 selects the regional map row. Zero is the unassigned
            # compatibility path, not a qualified Data Hijacking region.
            6: combat_area_id,
            # Native type 3 copies this ID into its territory-read request.
            7: territory_layout_owner_id,
            # True selects category 7 and the intra-faction briefing rows.
            # The coordinator freezes this value from current team factions.
            9: int(is_intra_faction),
        },
    )


def _match_room_info_push(
    *,
    recipient_prepare_id: int,
    room_id: int,
    room_password: bytes,
    room_owner_online_id: str,
) -> bytes:
    """Build an initialized retail ``PushRequestMatchRoomInfo``.

    Static recovery and a live predicate trace prove that field 1 must match
    the receiving client's active prepare ID. The RPCN room ID and the room's
    fixed-size application binary follow. Retail asserts that this ACVBinary
    is exactly 512 bytes. The four-byte ``NXRV`` build signature and two-byte
    version precede an empty typed-record list and UTF-16 label, followed by a
    16-byte serialized identity, a uint16 connection-record length, and the
    connection record itself. The Normal
    host supplies an eight-byte session key plus the RPCN room ID. The live
    Normal-sortie decoder validates archive version ``0x03f2``. A leading zero
    selects the retail raw-data path.
    """
    session_key = bytes(room_password)
    if len(session_key) != 8:
        raise ValueError("room session key must be exactly eight bytes")
    owner_identity = room_owner_online_id.encode("ascii", "strict")
    if not 0 < len(owner_identity) <= 16:
        raise ValueError("room owner online ID must contain 1-16 ASCII bytes")
    connection_record = session_key + int(room_id).to_bytes(8, "big")
    room_archive = (
        b"NXRV"
        + b"\x03\xf2"  # live Normal-sortie archive version
        + b"\x00"  # empty typed-record list
        + b"\x00\x00"  # empty UTF-16 label
        + owner_identity.ljust(16, b"\x00")
        + len(connection_record).to_bytes(2, "big")
        + connection_record
    )
    room_binary = room_archive.ljust(512, b"\x00")
    return (
        proto_varint(1, max(1, int(recipient_prepare_id)))
        + proto_varint(2, max(0, int(room_id)))
        + proto_bytes(3, b"\x00" + room_binary)
    )
