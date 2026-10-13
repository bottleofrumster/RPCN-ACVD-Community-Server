"""Reliable UDP packets, application envelopes, and per-connection state."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .crypto import CryptoError, CwcCipher
from .delivery_observation import DeliveryObservation
from .notice_messages import PersonalNotice

if TYPE_CHECKING:
    from .key_item_shop import KeyItemOffer
    from .shop_catalogue import CataloguePart

MAGIC = b"\xf5\x02"
INITIAL_PREFIX_SIZE = 35
SYN = 0x02
RETRANSMIT = 0x03
DAT = 0x04
HBT = 0x05
FIN = 0x06
RST = 0x07
ACK = 0x31
SYN_ACK = 0x32
DAT_ACK = 0x34
FIN_ACK = 0x36
WAIT_FOR_USER_LOGIN = 0x0386
GET_MY_USER_INFO = 0x038D
GET_PLAYER_EMBLEM = 0x0392
SET_PLAYER_EMBLEM_BY_UGC_ID = 0x03C7
REGISTER_AC_DATA = 0x03C5
REGISTER_EMBLEM = 0x03C4
GET_AC_DATA = 0x03C6
SYNC_MONEY = 0x03D9
BUY_PLAYER_SHOP_KEY_ITEM = 0x03D7
BUY_PLAYER_SHOP_FOR_PARTS = 0x03DD
SELL_PARTS_ITEM = 0x03DE
SYNC_OFFLINE_DATA = 0x0411
GET_AI_EXTEND = 0x040D
UPDATE_AI_EXTEND = 0x040E
GET_TRASH_PARTS_LIST = 0x041E
GET_GARAGE_CUSTOM_INFO = 0x03DB
REGISTER_GARAGE_CUSTOM_INFO = 0x03DC
GET_AI_INFO = 0x0400
UPLOAD_AI_INFO = 0x0402
GET_MISSION_SAVEDATA = 0x040F
UPLOAD_MISSION_SAVEDATA = 0x0410
GET_SERVER_DATETIME = 0x03D5
GET_WORLD_BASIC_INFO = 0x03A2
GET_FACTION_SELECTION = 0x03A3
GET_FACTION_STRUGGLE_RESULT = 0x03A4
GET_WORLD_CURRENT_INFO = 0x03A5
GET_NOTICE_LIST = 0x03A6
GET_WORLD_LOG = 0x03C3
REGISTER_BATTLE_RESULT = 0x03C2
GET_TEAM_RECORD = 0x03C0
SYNCHRONIZE_DLC_PARTS_LINEUP = 0x03D6
GET_TERRITORY_CANNON_LAYOUT = 0x03BB
NOTIFY_MATCH_ROOM_INFO = 0x03BC
UPLOAD_TERRITORY_CANNON_LAYOUT = 0x03BE
BATTLE_START = 0x03B9
REGISTER_PREPARE = 0x03BD
CANCEL_PREPARE = 0x03BA
REGISTER_COMBAT_RESULT = 0x03BF
REPORT_BATTLE_SESSION_DISCONNECT = 0x041B
REPORT_BRIEFING_END = 0x041C
GET_TERRITORY_INFO = 0x03CA
GET_MERCENARY_RANK = 0x03CF
GET_MERCENARY_SELF_RANKING = 0x03D1
UNREGISTER_MERCENARY = 0x03D3
SEND_MERCENARY_MESSAGE = 0x03D2
CONTINUE_MERCENARY_REGISTRATION = 0x03D4
GET_MERCENARY_RANKING = 0x03D0
REGISTER_MERCENARY = 0x03CB
SEARCH_MERCENARY = 0x03CC
GET_MERCENARY_DATA = 0x03CD
GET_MERCENARY_MESSAGE_LIST = 0x03CE
START_MERCENARY_NEGOTIATION = 0x0419
CANCEL_MERCENARY_NEGOTIATION = 0x041A
CHECK_REGULATION_DATA = 0x0415
GET_PERSONAL_ANNOUNCE = 0x0417
SET_PERSONAL_ANNOUNCE_ALREADY_READ = 0x0418
VERIFY_OWN_PARTS_LIST = 0x041D
CHECK_VALID_UGC_ID_LIST = 0x0420
GET_MY_ITEM_SIMPLE_INFO_LIST = 0x03D8
GET_PLAYER_KEY_ITEM_LIST = 0x03E2
GET_MY_ITEM_SIMPLE_INFO_LIST_ALT = 0x03E4
GET_WELCOME_MESSAGE_INFO_LIST = 0x03E8
GET_WORLD_TOP_SCREENSHOT = 0x03F5
GET_WORLD_TOP_SS_THUMBNAIL_LIST = 0x03F8
GET_WAIT_TIME_FOR_APPLY_WORLD_TOP_SS = 0x03F9
REGISTER_PLAYER_SCREENSHOT = 0x03F6
APPLY_WORLD_TOP_SCREENSHOT = 0x03F7
GET_MY_MEDAL_INFO = 0x03FC
GET_OWN_UNAC_LIST = 0x0401
GET_MY_TEAM_MEDAL_INFO = 0x03FD
CREATE_TEAM = 0x0390
GET_TEAM_INFO = 0x0391
GET_TEAM_EMBLEM = 0x0393
GET_USER_INFO_BY_PLAYER_ID = 0x0395
JOIN_TEAM_AUTO = 0x0396
LEAVE_TEAM = 0x0397
SEARCH_TEAM_ID_LIST = 0x0398
GET_TEAM_INFO_LIST = 0x03AA
GET_OWN_TEAM_VOTE_STATUS = 0x03B6
GET_JOINING_TEAM_STATUS = 0x03B5
SET_TEAM_EMBLEM_BY_UGC_ID = 0x0399
TRADE_MONEY = 0x039A
TRADE_PARTS = 0x039B
JOIN_FACTION = 0x039F
UPDATE_TEAM_FACTION_INFO = 0x039E
UPDATE_LEADER_TEAM_INFO = 0x039D
UPDATE_USER_PROPERTY = 0x039C
UPDATE_MEMBER_TEAM_INFO = 0x03A8
UPDATE_TEAM_MEMBER_COMMENT = 0x03A9
DELETE_TEAM_SESSION = 0x03F2
CHECK_VALID_PLAYER = 0x03F4
CREATE_TEAM_SESSION = 0x03EF
REPORT_DISCONNECTED = 0x03EE
SEARCH_TEAM_SESSION = 0x03F0
JOIN_TEAM_SESSION = 0x03F1
LEAVE_TEAM_SESSION = 0x03F3
REPORT_DISCONNECTED_TEAM_SESSION_MEMBER = 0x03FB
CHECK_VALID_PLAYER_BOOTSTRAP = 0x03EB
GET_TEAM_RANKING_INFO_LIST = 0x0406
GET_MY_TEAM_RANKING_INFO = 0x0407
GET_PLAYER_RANKING_LIST = 0x0403
GET_MY_PLAYER_RANKING_INFO = 0x0404
GET_DEFEAT_RANKING_LIST = 0x040A
GET_MY_DEFEAT_RANKING_INFO = 0x040B
GET_VOTE_KICK_MEMBER = 0x03AE
GET_BATTLE_RECORD = 0x03C1
GET_TEAM_MEMBER_COMMENT_LIST = 0x0394
GET_COMMENT = 0x038F
GET_PLAYER_SHOP_LINEUP_FOR_PARTS = 0x03DF


@dataclass(frozen=True)
class ReliablePacket:
    local_sequence: int
    remote_sequence: int
    opcode: int
    unknown: int = 0xFF
    payload: bytes = b""
    initial_prefix: bytes = b""


@dataclass(frozen=True)
class GameMessage:
    message_type: int
    message_index: bytes
    payload: bytes
    packet_counter: int = 0


@dataclass
class GameSession:
    auth_token: bytes
    game_key: bytes
    online_id: str
    player_id: int
    server_sequence: int = field(default_factory=lambda: secrets.randbelow(4096))
    remote_sequence: int = 0
    established: bool = False
    # A fresh authenticated transport is not yet a native team/signaling
    # session. Old team roster records must not bootstrap it prematurely.
    team_session_ready: bool = False
    peer: tuple[str, int] | None = None
    fragment_counter: int = 0
    last_seen_monotonic: float = field(default_factory=time.monotonic)
    last_seen_unix: float = field(default_factory=time.time)
    fragment_parts: dict[int, dict[int, bytes]] = field(default_factory=dict)
    fragment_totals: dict[int, int] = field(default_factory=dict)
    fragment_started_monotonic: dict[int, float] = field(default_factory=dict)
    fragment_updated_monotonic: dict[int, float] = field(default_factory=dict)
    ac_preseed_delivered: Path | None = None
    ai_preseeds_delivered: dict[int, Path] = field(default_factory=dict)
    mission_preseed_delivered: Path | None = None
    offline_preseed_guard_until_monotonic: float = 0.0
    # Offers and the last native inventory claim are per-login evidence, not
    # durable player data. A restarted service requires fresh listings for a
    # new sale; committed paid replies remain independently recoverable.
    shop_catalogue_version: str | None = None
    shop_offers: dict[int, CataloguePart] = field(default_factory=dict)
    # Buy Decal offer IDs are valid only for the current authenticated login
    # and the latest native 0x03D8 catalogue page.
    key_item_shop_offers: dict[int, KeyItemOffer] = field(default_factory=dict)
    owned_parts_claim: bytes | None = None
    # RequestGetTrashPartsList field 3 is the native maximum registered
    # instance ID. Bind it to a later ownership claim within this login; none
    # of this authority survives transport restoration.
    owned_parts_max_instance_id: int | None = None
    owned_parts_evidence_serial: int = 0
    owned_parts_claim_serial: int = -1
    # Proof of the source that matched this login's native ownership claim.
    # Server-owned transactions may change its effective view afterward.
    # Never serialize this proof or retain it across a client inventory report.
    verified_inventory_source: bytes | None = None
    verified_inventory_claim: bytes | None = None
    sale_cursor_floor: int = 0
    # Unacknowledged server packets, retained in send order so an opcode-3
    # gap request can replay the first sequence after the client's cumulative
    # acknowledgement. This transport state is intentionally not persisted
    # across process replacement.
    outbound_reliable: dict[int, ReliablePacket] = field(default_factory=dict)
    delivery_observation: DeliveryObservation = field(
        default_factory=DeliveryObservation, repr=False, compare=False
    )
    # Welcome read state belongs to this authenticated login, not player history.
    login_welcome: PersonalNotice | None = None
    login_welcome_checked: bool = False
    login_welcome_read: bool = False
    # A no-team account with no stored wallet receives the retail new-save
    # baseline during this login.  An intact legacy save may immediately send
    # SyncMoney; only the same authenticated session may replace that
    # provisional baseline once.  The flag is intentionally not durable.
    new_wallet_provisional: bool = False
    # Cumulative incoming prefix. Future packets stay on the client until ACKed.
    receive_sequence_initialized: bool = False
    receive_high_sequence: int | None = None
    # Last World Current response sent to this exact transport, not proof of
    # native consumption. Never persist across reconnect or source restart.
    test_war_view: dict | None = None


class PushSender(Protocol):
    def __call__(
        self, session: GameSession, push_proto: bytes, *, subtype: int | None = None
    ) -> None:
        """Send a typed or generic push through the existing reliable channel."""
        ...


def _validate_sequence(value: int) -> None:
    if not 0 <= value < 4096:
        raise ValueError("reliable UDP sequence must be between 0 and 4095")


def encode_reliable_packet(packet: ReliablePacket) -> bytes:
    _validate_sequence(packet.local_sequence)
    _validate_sequence(packet.remote_sequence)
    counters = bytes(
        (
            packet.local_sequence & 0xFF,
            ((packet.local_sequence >> 8) << 4) | (packet.remote_sequence >> 8),
            packet.remote_sequence & 0xFF,
        )
    )
    return (
        packet.initial_prefix
        + MAGIC
        + counters
        + bytes((packet.opcode, packet.unknown))
        + packet.payload
    )


def decode_reliable_packet(payload: bytes) -> ReliablePacket:
    initial_prefix = b""
    if not payload.startswith(MAGIC):
        if len(payload) < INITIAL_PREFIX_SIZE + 7:
            raise ValueError("initial reliable UDP packet is too short")
        initial_prefix = payload[:INITIAL_PREFIX_SIZE]
        payload = payload[INITIAL_PREFIX_SIZE:]
    if len(payload) < 7 or payload[:2] != MAGIC:
        raise ValueError("invalid reliable UDP magic")
    local = payload[2] | ((payload[3] & 0xF0) << 4)
    remote = payload[4] | ((payload[3] & 0x0F) << 8)
    return ReliablePacket(local, remote, payload[5], payload[6], payload[7:], initial_prefix)


def decode_game_message(payload: bytes) -> GameMessage:
    if len(payload) < 24:
        raise ValueError("game message fragment is too short")
    packet_counter = int.from_bytes(payload[:2], "big")
    compressed = payload[2]
    # Retail uses bytes 3-7 as one 40-bit total length. Bytes 3-5 are zero for
    # ordinary packets, but rich offline-data uploads set them once the body
    # exceeds 65,535 bytes.
    total_length = int.from_bytes(payload[3:8], "big")
    fragment_index = int.from_bytes(payload[8:10], "big")
    fragment_length = int.from_bytes(payload[10:12], "big")
    if compressed or fragment_index:
        raise ValueError("compressed or multi-part game messages are not supported yet")
    message = payload[12:]
    if total_length != len(message) or fragment_length != (len(message) & 0xFFFF):
        raise ValueError("game fragment length mismatch")
    if int.from_bytes(message[:4], "big") != 12:
        raise ValueError("invalid game message header size")
    return GameMessage(
        int.from_bytes(message[4:8], "big"),
        message[8:12],
        message[12:],
        packet_counter,
    )


def encode_game_reply(message: GameMessage) -> bytes:
    return encode_game_reply_fragments(message)[0]


def encode_game_reply_fragments(
    message: GameMessage,
    *,
    max_fragment_length: int = 900,
    error: tuple[int, int] | None = None,
) -> list[bytes]:
    if len(message.message_index) != 4:
        raise ValueError("game message index must be four bytes")
    if max_fragment_length <= 0:
        raise ValueError("maximum fragment length must be positive")
    # Preserve retail-qualified success bytes. Errors instead carry the recovered
    # big-endian result/detail words; the remaining header words stay zero.
    response_header = bytes.fromhex("00000000010000000000000000000000")
    if error is not None:
        result, detail = error
        if (
            type(result) is not int
            or type(detail) is not int
            or not 0 < result <= 0x7FFFFFFF
            or not 0 <= detail <= 0xFFFFFFFF
        ):
            raise ValueError("reply error requires positive int32 result and uint32 detail")
        if message.payload:
            raise ValueError("reply error cannot carry a success payload")
        response_header = result.to_bytes(4, "big") + detail.to_bytes(4, "big") + bytes(8)
    body = (
        (12).to_bytes(4, "big")
        + bytes(4)
        + message.message_index
        + response_header
        + message.payload
    )
    total_length = len(body)
    if total_length > 0xFFFFFFFFFF:
        raise ValueError("game reply exceeds the 40-bit fragment envelope")
    fragment_count = (total_length + max_fragment_length - 1) // max_fragment_length
    if fragment_count > 0x10000:
        raise ValueError("game reply exceeds the 16-bit fragment index envelope")
    fragments: list[bytes] = []
    for fragment_index, offset in enumerate(range(0, total_length, max_fragment_length)):
        fragment_body = body[offset : offset + max_fragment_length]
        fragment = (
            message.packet_counter.to_bytes(2, "big")
            + b"\x00"
            + total_length.to_bytes(5, "big")
            + fragment_index.to_bytes(2, "big")
            + len(fragment_body).to_bytes(2, "big")
        )
        fragments.append(fragment + fragment_body)
    return fragments


def encode_game_push(payload: bytes, packet_counter: int) -> bytes:
    body = (
        (12).to_bytes(4, "big") + (0x0320).to_bytes(4, "big") + bytes.fromhex("ffffffff") + payload
    )
    length = len(body)
    fragment = (
        packet_counter.to_bytes(2, "big")
        + bytes(4)
        + length.to_bytes(2, "big")
        + bytes(2)
        + length.to_bytes(2, "big")
    )
    return fragment + body


def encode_game_request(message: GameMessage) -> bytes:
    return encode_game_request_fragments(message)[0]


def encode_game_request_fragments(
    message: GameMessage, *, max_fragment_length: int = 1012
) -> list[bytes]:
    if len(message.message_index) != 4:
        raise ValueError("game message index must be four bytes")
    if max_fragment_length <= 0:
        raise ValueError("maximum fragment length must be positive")
    body = (
        (12).to_bytes(4, "big")
        + message.message_type.to_bytes(4, "big")
        + message.message_index
        + message.payload
    )
    total_length = len(body)
    fragment_count = (total_length + max_fragment_length - 1) // max_fragment_length
    if fragment_count > 0x10000:
        raise ValueError("game request exceeds the 16-bit fragment index envelope")
    fragments: list[bytes] = []
    for fragment_index, offset in enumerate(range(0, total_length, max_fragment_length)):
        fragment_body = body[offset : offset + max_fragment_length]
        fragments.append(
            message.packet_counter.to_bytes(2, "big")
            + b"\x00"
            + total_length.to_bytes(5, "big")
            + fragment_index.to_bytes(2, "big")
            + len(fragment_body).to_bytes(2, "big")
            + fragment_body
        )
    return fragments


def decrypt_client_datagram(
    data: bytes, key: bytes, library_path: Path
) -> tuple[bytes, int, bytes]:
    if len(data) < 37:
        raise CryptoError("client UDP datagram is too short")
    auth_token = data[:8]
    iv = data[8:19]
    tag = data[19:35]
    packet_type = data[35]
    ciphertext = data[36:]
    aad = iv + auth_token + bytes((packet_type,))
    plaintext = CwcCipher(key, library_path).decrypt(iv + tag + ciphertext, aad=aad)
    return auth_token, packet_type, plaintext


def encrypt_client_datagram(
    plaintext: bytes,
    key: bytes,
    auth_token: bytes,
    packet_type: int,
    library_path: Path,
    *,
    iv: bytes | None = None,
) -> bytes:
    if len(auth_token) != 8:
        raise ValueError("UDP auth token must be 8 bytes")
    iv = secrets.token_bytes(11) if iv is None else bytes(iv)
    aad = iv + auth_token + bytes((packet_type,))
    envelope = CwcCipher(key, library_path).encrypt(plaintext, iv=iv, aad=aad)
    return auth_token + envelope[:27] + bytes((packet_type,)) + envelope[27:]


def encrypt_server_datagram(plaintext: bytes, key: bytes, library_path: Path) -> bytes:
    return CwcCipher(key, library_path).encrypt(plaintext)


def decrypt_server_datagram(data: bytes, key: bytes, library_path: Path) -> bytes:
    return CwcCipher(key, library_path).decrypt(data)
