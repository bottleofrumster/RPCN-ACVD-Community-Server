"""Own service listeners, authenticated dispatch, and correlated wire replies.

Domain handlers own policy; message modules encode bytes. Same-name imports
below preserve tested legacy entry points. They are exports, not duplicate code.
See readme.txt for feature routes, known limits, and the investigation method."""

from __future__ import annotations

# Explicit same-name aliases below preserve legacy imports during module extraction.
import asyncio
import base64 as base64
import json as json
import logging
import os
import struct as struct
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives.asymmetric import rsa

from .administration import AdminEndpoint, OperatorSettings, server_snapshot
from .cdn import encode_response as encode_cdn_response
from .config import ServerConfig
from .connections import (
    Frpg2Connection,
    _advertised_host_for_connection,
    _advertised_host_for_route as _advertised_host_for_route,
    _game_server_info,
    _local_host_for_peer as _local_host_for_peer,
)
from .crypto import (
    CryptoError,
    CwcCipher,
    load_private_key,
    rsa_oaep_decrypt,
    rsa_private_encrypt_x931,
)
from .inventory import (
    _item_simple_info_list as _item_simple_info_list,
    _merge_offline_inventory as _merge_offline_inventory,
    _offline_inventory_records as _offline_inventory_records,
    _offline_inventory_summary as _offline_inventory_summary,
    _sync_offline_data_response as _sync_offline_data_response,
)
from .key_item_shop import KeyItemShopCatalogue
from .match_messages import (
    ACVD_MATCHING2_SESSION_PASSWORD as ACVD_MATCHING2_SESSION_PASSWORD,
    _match_room_info_push as _match_room_info_push,
    _matching_result_for_members as _matching_result_for_members,
    _matching_result_push as _matching_result_push,
    _register_prepare_response as _register_prepare_response,
)
from .matchmaking import MatchmakingCoordinator, _rpcn_create_room_tuple as _rpcn_create_room_tuple
from .mercenary_messages import (
    _mercenary_continuation_pairs as _mercenary_continuation_pairs,
    _mercenary_message as _mercenary_message,
    _mercenary_message_list_response as _mercenary_message_list_response,
    _mercenary_rank_response as _mercenary_rank_response,
    _mercenary_ranking_info as _mercenary_ranking_info,
    _mercenary_ranking_list_response as _mercenary_ranking_list_response,
    _mercenary_search_response as _mercenary_search_response,
    _mercenary_search_result as _mercenary_search_result,
    _mercenary_self_ranking_response as _mercenary_self_ranking_response,
)
from .player_reset import reset_player
from .player_storage import (
    _replace_player_blob as _replace_player_blob,
    _requested_ac_snapshot as _requested_ac_snapshot,
    _requested_ai_snapshot as _requested_ai_snapshot,
    _requested_mission_snapshot as _requested_mission_snapshot,
)
from .protocol import (
    ProtocolError,
    _proto_datetime as _proto_datetime,
    _protobuf_summary,
    _typed_push_payload,
    decode_proto,
    first_field,
    proto_bytes,
    proto_varint,
)
from .reassembly import assemble_request
from .regulation import DOWNLOAD_PATHS, RegulationData, RegulationDownloads, RegulationSource
from .reply_recovery import FragmentReplyRecovery
from .request_recovery import RequestRecovery
from .rewards import (
    _register_battle_result_response as _register_battle_result_response,
)
from .routing import INITIAL_SERVER_BALANCE as INITIAL_SERVER_BALANCE, dispatch_request
from .save_codecs import (
    _ac_data_response_payload as _ac_data_response_payload,
    _ai_info_equivalent as _ai_info_equivalent,
    _decoded_ai_info as _decoded_ai_info,
    _decoded_application_blob as _decoded_application_blob,
    _mission_savedata_equivalent as _mission_savedata_equivalent,
    _mission_snapshot_summary as _mission_snapshot_summary,
    _own_unac_list_response as _own_unac_list_response,
)
from .screenshots import (
    _proto_fixed64 as _proto_fixed64,
    _required_screenshot_registration as _required_screenshot_registration,
    _world_top_screenshot as _world_top_screenshot,
    _world_top_screenshot_id as _world_top_screenshot_id,
    _world_top_thumbnail_info as _world_top_thumbnail_info,
)
from .session_lifecycle import (
    LiveSessionCoordinator,
    _remove_team_session_member_from_state as _remove_team_session_member_from_state,
)
from .session_state import (
    SESSION_STATE_MAX_AGE_SECONDS as SESSION_STATE_MAX_AGE_SECONDS,
    _load_game_sessions,
    _load_team_sessions,
    _persist_game_sessions,
)
from .shop_data import ShopCatalogue
from .team_messages import (
    _team_color_argb32 as _team_color_argb32,
    _team_id_search_response as _team_id_search_response,
    _team_info as _team_info,
    _team_info_list_response as _team_info_list_response,
    _team_medal_info as _team_medal_info,
    _team_rank_info as _team_rank_info,
    _team_ranking_info as _team_ranking_info,
    _team_record as _team_record,
    _team_session_join_response as _team_session_join_response,
    _team_session_leave_push as _team_session_leave_push,
    _team_session_member_push as _team_session_member_push,
    _team_session_member_push_pair as _team_session_member_push_pair,
    _team_session_response as _team_session_response,
    _team_update_date_info as _team_update_date_info,
    _user_info as _user_info,
    _vote_kick_member_response as _vote_kick_member_response,
)
from .teams import TeamStore
from .telemetry import Telemetry
from .tickets import TicketError, parse_ticket
from .udp import (
    ACK,
    DAT,
    DAT_ACK,
    FIN,
    FIN_ACK,
    HBT,
    RETRANSMIT,
    RST,
    SYN,
    SYN_ACK,
    TRADE_PARTS as TRADE_PARTS,
    GameMessage,
    GameSession,
    ReliablePacket,
    decode_game_message as decode_game_message,
    decode_reliable_packet,
    decrypt_client_datagram,
    encode_game_push,
    encode_game_reply_fragments,
    encode_reliable_packet,
    encrypt_server_datagram,
)
from .ugc import (
    _emblem_response_payload as _emblem_response_payload,
    _register_emblem_response as _register_emblem_response,
    _requested_team_for_emblem as _requested_team_for_emblem,
    _ugc_id_validation_response as _ugc_id_validation_response,
)
from .world_economy import WORLD_REWARD_DEFAULTS
from .world_messages import (
    CURRENT_SEASON_ID as CURRENT_SEASON_ID,
    WORLD_PHASE_ACTIVE as WORLD_PHASE_ACTIVE,
    _canonical_faction_selection_id as _canonical_faction_selection_id,
    _empty_ranking_list_response as _empty_ranking_list_response,
    _faction_selection as _faction_selection,
    _faction_struggle_results as _faction_struggle_results,
    _my_player_ranking_response as _my_player_ranking_response,
    _ranking_basic_info as _ranking_basic_info,
    _territory_cannon_layout as _territory_cannon_layout,
    _territory_info as _territory_info,
    _territory_layout_update_response as _territory_layout_update_response,
    _world_basic_info as _world_basic_info,
    _world_current_info as _world_current_info,
    _world_log as _world_log,
)
from .world_policy import world_state as operator_world_state

LOGGER = logging.getLogger("acvd_server")


# SERVICE LIFETIME: listeners, admission, login/authentication, and regulation HTTP.
# This layer owns sockets. It does not choose battle rewards or team membership.
class AcvdServer:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.telemetry = Telemetry(
            config.telemetry_path,
            capture_payloads=config.audit_capture_payloads,
            max_bytes=config.audit_max_bytes,
            backup_count=config.audit_backup_count,
        )
        self.private_key: rsa.RSAPrivateKey = load_private_key(config.private_key_path)
        self.regulation = RegulationSource(config.regulation_path, self.private_key.public_key())
        self.regulation_downloads: RegulationDownloads | None = None
        self.tcp_servers: list[asyncio.AbstractServer] = []
        self.udp_transport: asyncio.DatagramTransport | None = None
        self.game_protocol: GameDatagramProtocol | None = None
        self.game_sessions = _load_game_sessions(config.session_state_path)
        self.teams = TeamStore(config.team_data_path)
        self.operator_settings = OperatorSettings(
            config.project_root / "state/operator-settings.json"
        )
        self.operator_started = time.monotonic()
        self.shutdown_requested = asyncio.Event()
        self._stream_writers: set[asyncio.StreamWriter] = set()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._war_tick_handle: asyncio.TimerHandle | None = None
        self.admin_endpoint = AdminEndpoint(
            config.project_root / "state/operator.sock",
            lambda: server_snapshot(self, self.operator_started),
            self.operator_settings,
            self.telemetry,
            self.shutdown_requested.set,
            self._delete_player_save,
            self._validate_operator_settings_change,
            self._start_test_war,
        )
        self.telemetry.emit(
            "game_session_state_loaded",
            count=len(self.game_sessions),
            online_ids=sorted({session.online_id for session in self.game_sessions.values()}),
        )

    def _validate_operator_settings_change(self, changes: dict) -> None:
        """Protect session-cached CQ rows from changing under any live client."""
        if (
            self.teams.test_war_snapshot() is not None
            and {"world_mode", "grace_start_utc", "grace_end_utc", "cq_map_mode", "cq_map_pool"}
            & changes.keys()
        ):
            raise ValueError("The active test war owns phase and map progression")
        if not ({"cq_map_mode", "cq_map_pool"} & changes.keys()):
            return
        has_live_session = any(
            session.established and session.peer is not None
            for session in self.game_sessions.values()
        )
        protocol = self.game_protocol
        has_active_match = bool(
            protocol
            and any(
                not match.get("result_sha256")
                for match in protocol.matches.values()
            )
        )
        if has_live_session or has_active_match:
            raise ValueError(
                "CQ map policy can change only while every player is logged out "
                "and no match is active"
            )

    def _start_test_war(self, rules: dict, owners: list[int]) -> dict:
        """Activation is a separate confirmed action, never a startup migration."""
        if any(s.established and s.peer is not None for s in self.game_sessions.values()):
            raise ValueError("All players must log out before starting a test war")
        if self.game_protocol and any(
            not m.get("result_sha256") for m in self.game_protocol.matches.values()
        ):
            raise ValueError("An active match prevents test-war activation")
        result = self.teams.start_test_war(settings=rules, owners=owners, now=int(time.time()))
        self._tick_test_war()
        self.telemetry.emit(
            "operator_test_war_started",
            policy=result["policy"],
            season=result["season"],
            rules=result["rules"],
        )
        return result

    def _tick_test_war(self) -> None:
        if self._war_tick_handle is not None:
            self._war_tick_handle.cancel()
            self._war_tick_handle = None
        if self._closed:
            return
        delay = 1
        try:
            before = self.teams.test_war_snapshot()
            if before is None:
                return
            after = self.teams.advance_test_war(int(time.time()))
            if after != before:
                self.telemetry.emit(
                    "game_test_war_advanced",
                    season=after["season"],
                    phase=after["phase"],
                    revision=after["revision"],
                )
        except (ValueError, OSError, KeyError, TypeError) as error:
            delay = 30
            self.telemetry.emit("game_test_war_transition_failed", reason=str(error))
        self._war_tick_handle = asyncio.get_running_loop().call_later(delay, self._tick_test_war)

    def _delete_player_save(self, online_id: str, player_id: int) -> dict:
        """Run one confirmed offline reset from the serialized server event loop."""
        now = time.monotonic()
        if any(
            item.online_id == online_id
            and item.player_id == player_id
            and item.established
            and item.peer is not None
            and now - item.last_seen_monotonic <= 90
            for item in self.game_sessions.values()
        ):
            raise ValueError("Disconnect the selected player before permanent reset")
        protocol = self.game_protocol
        if protocol is not None:
            protocol._purge_stale_team_session_members()
            if any(not match.get("result_sha256") for match in protocol.matches.values()):
                raise ValueError("Wait until all active matches finish before player reset")
            if any(
                online_id in directory.get("members", {})
                for directory in protocol.team_sessions.values()
                if isinstance(directory, dict)
            ):
                raise ValueError("Wait for the selected player's team session to close")
        result = reset_player(
            self.teams,
            self.config.player_data_path,
            online_id=online_id,
            player_id=player_id,
        )
        self.game_sessions = {
            token: item
            for token, item in self.game_sessions.items()
            if (item.online_id, item.player_id) != (online_id, player_id)
        }
        if protocol is not None:
            protocol.sessions = self.game_sessions
            protocol.active_mercenaries.discard(online_id)
            protocol._matchmaking.hires.discard(online_id)
        self._persist_game_session_state()
        self.telemetry.emit("operator_player_save_deleted", **result)
        return result

    def _persist_game_session_state(self) -> None:
        try:
            _persist_game_sessions(
                self.config.session_state_path,
                self.game_sessions,
                self.game_protocol.team_sessions if self.game_protocol is not None else {},
            )
        except OSError as exc:
            self.telemetry.emit(
                "game_session_state_persist_failed",
                error=repr(exc),
                count=len(self.game_sessions),
            )

    async def start(self) -> None:
        seen: set[int] = set()
        for port in self.config.login_ports:
            if port in seen:
                continue
            seen.add(port)
            server = await asyncio.start_server(
                lambda reader, writer, listen_port=port: self._guarded_login(
                    reader, writer, listen_port
                ),
                self.config.bind_host,
                port,
            )
            self.tcp_servers.append(server)
            LOGGER.info("login discovery listening on %s:%d", self.config.bind_host, port)
        auth_server = await asyncio.start_server(
            self._guarded_auth, self.config.bind_host, self.config.auth_port
        )
        self.tcp_servers.append(auth_server)
        LOGGER.info(
            "authentication listening on %s:%d", self.config.bind_host, self.config.auth_port
        )
        cdn_server = await asyncio.start_server(
            self._handle_cdn, self.config.bind_host, self.config.cdn_port
        )
        self.tcp_servers.append(cdn_server)
        self.regulation_downloads = RegulationDownloads(
            self.regulation, self.private_key, self.config.advertised_host,
            cdn_server.sockets[0].getsockname()[1],
        )
        LOGGER.info(
            "regulation CDN listening on %s:%d", self.config.bind_host, self.config.cdn_port
        )
        loop = asyncio.get_running_loop()
        try:
            shop_catalogue = ShopCatalogue.load(self.config, self.private_key.public_key())
        except (OSError, ValueError) as error:
            shop_catalogue = None
            self.telemetry.emit("game_shop_inputs_rejected", reason=str(error))
        if shop_catalogue is not None:
            self.telemetry.emit(
                "game_shop_inputs_loaded",
                count=len(shop_catalogue.entries),
                regulation_sha256=shop_catalogue.regulation_digest,
                parameter_sha256=dict(shop_catalogue.input_hashes),
                normal_rate=shop_catalogue.normal_rate,
            )
        try:
            key_item_shop_catalogue = KeyItemShopCatalogue.load(self.config)
        except (OSError, ValueError) as error:
            key_item_shop_catalogue = None
            self.telemetry.emit("game_key_item_shop_inputs_rejected", reason=str(error))
        if key_item_shop_catalogue is not None:
            self.telemetry.emit(
                "game_key_item_shop_inputs_loaded",
                count=len(key_item_shop_catalogue.entries),
                source_sha256=key_item_shop_catalogue.source_sha256,
                parameter_sha256=dict(key_item_shop_catalogue.input_hashes),
            )
        protocol = GameDatagramProtocol(
            self.telemetry,
            self.game_sessions,
            self.config.native_library_path,
            self.config.player_data_path,
            self.teams,
            self._persist_game_session_state,
            get_world_state=lambda: operator_world_state(self.operator_settings.values),
            get_welcome_message=lambda: self.operator_settings.values["welcome_message"],
            get_world_reward_settings=lambda: self.operator_settings.values,
            get_cq_map_settings=lambda: self.operator_settings.values,
            shop_catalogue=shop_catalogue,
            key_item_shop_catalogue=key_item_shop_catalogue,
            get_regulation=self.regulation.load,
        )
        protocol.team_sessions = _load_team_sessions(
            self.config.session_state_path,
            self.game_sessions,
        )
        live_online_ids = {
            session.online_id for session in self.game_sessions.values() if session.established
        }
        protocol.active_mercenaries = {
            str(record.get("online_id", ""))
            for record in self.teams.available_mercenaries()
            if str(record.get("online_id", "")) in live_online_ids
        }
        self.telemetry.emit(
            "game_runtime_state_loaded",
            team_session_count=len(protocol.team_sessions),
            team_session_member_count=sum(
                len(team_session.get("members", {}))
                for team_session in protocol.team_sessions.values()
            ),
            active_mercenary_online_ids=sorted(protocol.active_mercenaries),
        )
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            local_addr=(self.config.bind_host, self.config.game_port),
        )
        self.udp_transport = transport
        self.game_protocol = protocol
        await self.admin_endpoint.start()
        LOGGER.info(
            "game UDP discovery listening on %s:%d", self.config.bind_host, self.config.game_port
        )
        self.telemetry.emit("server_started", config=asdict(self.config))
        self._tick_test_war()

    async def serve_forever(self) -> None:
        await self.start()
        # start_server already serves connections. Keep the loop alive until an
        # explicit same-user shutdown or process signal requests orderly closure.
        await self.shutdown_requested.wait()

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            if self._war_tick_handle is not None:
                self._war_tick_handle.cancel()
                self._war_tick_handle = None
            self.telemetry.emit(
                "server_stopping",
                tcp_listener_count=len(self.tcp_servers),
                stream_connection_count=len(self._stream_writers),
                game_session_count=len(self.game_sessions),
            )
            listeners, self.tcp_servers = tuple(self.tcp_servers), []
            for server in listeners:
                server.close()
            await self.admin_endpoint.close()

            writers = tuple(self._stream_writers)
            for writer in writers:
                writer.close()
            if listeners or writers:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            *(server.wait_closed() for server in listeners),
                            *(writer.wait_closed() for writer in writers),
                            return_exceptions=True,
                        ),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    self.telemetry.emit(
                        "server_stream_close_timeout",
                        connection_count=len(writers),
                    )
            self._stream_writers.clear()

            protocol = self.game_protocol
            if protocol is not None:
                protocol.close_runtime()
            transport, self.udp_transport = self.udp_transport, None
            if transport is not None:
                transport.close()
                if protocol is not None:
                    try:
                        await asyncio.wait_for(protocol.transport_closed.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        self.telemetry.emit("server_udp_close_timeout")
            self._persist_game_session_state()
            self.game_protocol = None
            self._closed = True
            self.telemetry.emit(
                "server_stopped",
                game_session_count=len(self.game_sessions),
            )

    # ADMISSION AND TCP SERVICES: maintenance blocks new clients, not live sessions.
    async def _guarded_login(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, port: int
    ) -> None:
        if not self.operator_settings.accepts_login:
            await self._deny_admission(writer, "login")
            return
        await self._guard(self._handle_login, reader, writer, f"login:{port}")

    async def _guarded_auth(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if not self.operator_settings.accepts_login:
            await self._deny_admission(writer, "auth")
            return
        await self._guard(self._handle_auth, reader, writer, "auth")

    async def _deny_admission(self, writer, service: str) -> None:
        self.telemetry.emit(
            "operator_admission_denied", service=service, peer=writer.get_extra_info("peername")
        )
        writer.close()
        await writer.wait_closed()

    async def _handle_cdn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        self._stream_writers.add(writer)
        status = 400
        method = ""
        target = ""
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            parts = request_line.decode("latin-1").strip().split()
            if len(parts) == 3:
                method, target, _ = parts
                while True:
                    header = await asyncio.wait_for(reader.readline(), timeout=5)
                    if header in (b"\r\n", b"\n", b""):
                        break
                path = target.split("?", 1)[0]
                if method not in ("GET", "HEAD"):
                    status = 405
                    body = b"method not allowed\n"
                elif path not in DOWNLOAD_PATHS:
                    status = 404
                    body = b"not found\n"
                elif not self.config.regulation_path.is_file():
                    status = 503
                    body = b"regulation file not installed\n"
                else:
                    try:
                        if self.regulation_downloads is None:
                            raise ValueError("Regulation download service is not initialized")
                        body = self.regulation_downloads.get(path)
                        status = 200
                    except (OSError, ValueError) as error:
                        status = 503
                        body = b"regulation file unavailable\n"
                        self.telemetry.emit("cdn_regulation_unavailable", reason=str(error))
                writer.write(encode_cdn_response(method, status, body))
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, UnicodeDecodeError):
            pass
        finally:
            self._stream_writers.discard(writer)
            self.telemetry.emit(
                "cdn_request",
                peer=peer,
                method=method,
                target=target,
                status=status,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def _guard(self, handler, reader, writer, service: str) -> None:
        self._stream_writers.add(writer)
        connection = Frpg2Connection(reader, writer, self.config, self.telemetry, service)
        self.telemetry.emit("client_connected", service=service, peer=connection.peer)
        try:
            await handler(connection)
        except (asyncio.IncompleteReadError, ConnectionError) as exc:
            self.telemetry.emit(
                "client_disconnected",
                service=service,
                peer=connection.peer,
                reason=type(exc).__name__,
            )
        except (ProtocolError, CryptoError, TicketError, ValueError) as exc:
            LOGGER.warning("%s protocol failure from %s: %s", service, connection.peer, exc)
            self.telemetry.emit(
                "protocol_failure", service=service, peer=connection.peer, error=str(exc)
            )
        except Exception as exc:
            LOGGER.exception("%s unexpected failure from %s", service, connection.peer)
            self.telemetry.emit(
                "unexpected_failure", service=service, peer=connection.peer, error=repr(exc)
            )
        finally:
            self._stream_writers.discard(writer)
            try:
                await connection.close()
            except (ConnectionError, asyncio.CancelledError):
                pass

    # LOGIN HANDSHAKES: establish the route, then verify authentication separately.
    async def _handle_login(self, connection: Frpg2Connection) -> None:
        request = await connection.read_message(
            lambda data: rsa_oaep_decrypt(self.private_key, data)
        )
        if request.msg_type != 5:
            raise ProtocolError(f"login expected message type 5, got {request.msg_type}")
        decode_proto(request.payload)  # Validate before logging or replying.
        self.telemetry.emit(
            "login_query", peer=connection.peer, fields=_protobuf_summary(request.payload)
        )
        advertised_host = _advertised_host_for_connection(connection)
        response = proto_varint(1, self.config.auth_port) + proto_bytes(2, advertised_host)
        await connection.send_message(
            rsa_private_encrypt_x931(self.private_key, response),
            msg_type=0,
            msg_index=request.msg_index,
        )

    async def _handle_auth(self, connection: Frpg2Connection) -> None:
        handshake = await connection.read_message(
            lambda data: rsa_oaep_decrypt(self.private_key, data)
        )
        if handshake.msg_type != 6:
            raise ProtocolError(f"auth expected handshake type 6, got {handshake.msg_type}")
        fields = decode_proto(handshake.payload)
        key_field = first_field(fields, 1)
        if not key_field or key_field.wire_type != 2 or not isinstance(key_field.value, bytes):
            raise ProtocolError("RequestHandshake did not contain bytes field 1")
        cwc = CwcCipher(key_field.value, self.config.native_library_path)
        self.telemetry.emit(
            "handshake_key_accepted", peer=connection.peer, key_length=len(key_field.value)
        )
        await connection.send_message(
            os.urandom(11) + bytes(16), msg_type=0, msg_index=handshake.msg_index
        )

        status = await connection.read_message(cwc.decrypt)
        if status.msg_type != 2:
            raise ProtocolError(f"auth expected service status type 2, got {status.msg_type}")
        status_fields = decode_proto(status.payload)
        app_version_field = first_field(status_fields, 4)
        app_version = (
            app_version_field.value
            if app_version_field and isinstance(app_version_field.value, int)
            else 0
        )
        status_response = (
            proto_varint(1, 2)
            + proto_bytes(2, b"\0")
            + proto_varint(3, 0)
            + proto_varint(4, app_version)
        )
        await connection.send_message(
            status_response,
            msg_type=0,
            msg_index=status.msg_index,
            encrypt=cwc.encrypt,
        )

        key_material = await connection.read_message(cwc.decrypt)
        if key_material.msg_type != 1 or len(key_material.payload) not in (8, 16):
            raise ProtocolError(
                f"auth expected 8- or 16-byte key material type 1, got type={key_material.msg_type} size={len(key_material.payload)}"
            )
        if len(key_material.payload) == 16 and key_material.payload[:2] == b"VD":
            # ACVD sends its 8-byte application-version identifier followed
            # by an 8-byte client secret. The response drops the identifier
            # and returns client-secret/server-secret, forming the game key.
            server_secret = os.urandom(8)
            key_response = key_material.payload[8:] + server_secret
            game_key = key_response
        else:
            # DS2/3 use a raw 8-byte client half and receive a 16-byte
            # client-half/server-half response.
            key_response = key_material.payload + os.urandom(len(key_material.payload))
            game_key = key_response
        await connection.send_message(
            key_response,
            msg_type=0,
            msg_index=key_material.msg_index,
            encrypt=cwc.encrypt,
        )

        ticket_message = await connection.read_message(cwc.decrypt)
        if ticket_message.msg_type != 3:
            raise ProtocolError("auth expected platform ticket type 3")
        if ticket_message.payload.startswith(b"\x21\x01\x00\x00"):
            # ACVD sends the PSN ticket directly.
            ticket_bytes = ticket_message.payload
        else:
            # DS2/3 prefix the ticket with the negotiated game key.
            if (
                len(ticket_message.payload) <= len(game_key)
                or ticket_message.payload[: len(game_key)] != game_key
            ):
                raise ProtocolError("platform ticket did not echo negotiated game key")
            ticket_bytes = ticket_message.payload[len(game_key) :]
        ticket = parse_ticket(ticket_bytes)
        # Drain can begin during an existing handshake. Do not issue a new session then.
        if not self.operator_settings.accepts_login:
            raise ProtocolError("New authentication disabled by operator")
        self.telemetry.emit("rpcn_ticket_accepted", peer=connection.peer, ticket=ticket)
        replaced_sessions = [
            token
            for token, existing in self.game_sessions.items()
            if existing.online_id == ticket.online_id
        ]
        for token in replaced_sessions:
            self.game_sessions.pop(token, None)
        auth_token = os.urandom(8)
        advertised_host = _advertised_host_for_connection(connection)
        game_info = _game_server_info(advertised_host, self.config.game_port, auth_token)
        self.game_sessions[auth_token] = GameSession(
            auth_token,
            game_key,
            ticket.online_id,
            ticket.account_id or 1,
        )
        self._persist_game_session_state()
        await connection.send_message(
            game_info,
            msg_type=0,
            msg_index=ticket_message.msg_index,
            encrypt=cwc.encrypt,
        )
        self.telemetry.emit(
            "authentication_complete",
            peer=connection.peer,
            online_id=ticket.online_id,
            service_id=ticket.service_id,
            advertised_host=advertised_host,
            replaced_session_count=len(replaced_sessions),
            game_key_length=len(game_key),
            auth_token_length=len(auth_token),
            game_info=game_info,
        )


# GAME TRANSPORT: authenticate datagrams before dispatching application requests.
# Domain modules own policy; this class keeps reliable sequencing and reply identity.
class GameDatagramProtocol(asyncio.DatagramProtocol):
    SYN_ACK_PAYLOAD = b"\x12\x10\x20\x20\x00\x01\x00\x00"

    def __init__(
        self,
        telemetry: Telemetry,
        sessions: dict[bytes, GameSession],
        library_path,
        player_data_path,
        teams: TeamStore,
        persist_sessions: Callable[[], None] | None = None,
        *,
        get_world_state: Callable[[], dict] | None = None,
        get_welcome_message: Callable[[], str] | None = None,
        get_world_reward_settings: Callable[[], dict] | None = None,
        get_cq_map_settings: Callable[[], dict] | None = None,
        shop_catalogue: ShopCatalogue | None = None,
        key_item_shop_catalogue: KeyItemShopCatalogue | None = None,
        get_regulation: Callable[[], RegulationData] | None = None,
    ) -> None:
        self.telemetry = telemetry
        self.sessions = sessions
        self.library_path = library_path
        self.player_data_path = player_data_path
        self.teams = teams
        self.persist_sessions = persist_sessions
        self.shop_catalogue = shop_catalogue
        self.key_item_shop_catalogue = key_item_shop_catalogue
        self._get_regulation = get_regulation
        self._get_world_state = get_world_state or (lambda: {"phase": WORLD_PHASE_ACTIVE})
        self._get_welcome_message = get_welcome_message or (lambda: "")
        self._get_world_reward_settings = get_world_reward_settings or (
            lambda: dict(WORLD_REWARD_DEFAULTS)
        )
        self._get_cq_map_settings = get_cq_map_settings or (lambda: {})
        self._session_lifecycle = LiveSessionCoordinator(
            get_sessions=lambda: self.sessions,
            get_teams=lambda: self.teams,
            can_refresh_peer=lambda recipient, peer: self._matchmaking.team_peer_recovery_allowed(
                recipient, peer
            ),
            send_push=lambda target, payload, subtype=None: self._send_game_push(
                target, payload, subtype=subtype
            ),
            telemetry=self.telemetry,
        )
        self.transport: asyncio.DatagramTransport | None = None
        self._reply_recovery = FragmentReplyRecovery(
            transport_available=lambda: self.transport is not None,
            is_current_session=lambda candidate: (
                self.sessions.get(candidate.auth_token) is candidate
            ),
            send_packet=lambda candidate, packet: self._send(candidate, packet, remember=False),
            telemetry=self.telemetry,
        )
        # Compatibility inspection view: the component owns this same dictionary.
        self._reply_retries = self._reply_recovery.timers
        self._request_recovery = RequestRecovery(
            is_current=lambda candidate: (
                self.transport is not None
                and self.sessions.get(candidate.auth_token) is candidate
            ),
            request_next=lambda candidate: self._send(
                candidate, ReliablePacket(0, candidate.remote_sequence, RETRANSMIT), remember=False
            ),
            telemetry=self.telemetry,
        )
        room_log_paths = os.environ.get("ACVD_MATCH_ROOM_LOG_PATHS", "").strip()
        legacy_room_bridge_enabled = os.environ.get("ACVD_ENABLE_LEGACY_ROOM_LOG_BRIDGE", "") == "1"
        configured_room_log_paths = (
            [
                Path(raw_path)
                for raw_path in room_log_paths.split(os.pathsep)
                if raw_path.strip()
            ]
            if legacy_room_bridge_enabled
            else []
        )
        if room_log_paths and not legacy_room_bridge_enabled:
            self.telemetry.emit(
                "game_match_room_log_bridge_disabled",
                reason=(
                    "Native authenticated room publication is authoritative; legacy log parsing "
                    "requires ACVD_ENABLE_LEGACY_ROOM_LOG_BRIDGE=1"
                ),
                configured_path_count=len(
                    [raw for raw in room_log_paths.split(os.pathsep) if raw.strip()]
                ),
            )
        elif configured_room_log_paths:
            self.telemetry.emit(
                "game_match_room_log_bridge_enabled",
                configured_path_count=len(configured_room_log_paths),
                warning="Diagnostic-only compatibility path; never enable on the public server",
            )
        self._matchmaking = MatchmakingCoordinator(
            get_test_war=lambda: self.teams.test_war_snapshot(),
            get_team_sessions=lambda: self.team_sessions,
            send_push=lambda target, payload, subtype=None: self._send_game_push(
                target, payload, subtype=subtype
            ),
            telemetry=self.telemetry,
            room_log_paths=configured_room_log_paths,
            get_world_phase=self.world_phase,
            get_sessions=lambda: self.sessions.values(),
            get_team_id=lambda online_id: int(
                (self.teams.team_for(online_id) or {}).get("team_id", 0)
            ),
            select_opposing_territory=lambda team_id, faction_id: (
                self.teams.select_opposing_native_territory(
                    attacker_team_id=team_id,
                    attacker_faction_id=faction_id,
                    season_id=self.teams.world_season_id(),
                )
            ),
            special_sortie_ready=self.teams.special_sortie_ready,
            consume_special_sortie=self.teams.consume_special_sortie,
            get_cq_map_settings=self._get_cq_map_settings,
        )
        self.transport_closed = asyncio.Event()

    def world_phase(self) -> int:
        war = self.teams.advance_test_war(int(time.time()))
        if war is not None:
            return war["phase"]
        phase = self._get_world_state()["phase"]
        if type(phase) is not int or phase not in (0, 2):
            raise ValueError("Unsupported operator world phase")
        return phase

    # COMPATIBILITY ACCESSORS: these delegate to the single state owner.
    # Keep them while old callers/tests still use the established protocol API.
    @property
    def active_mercenaries(self) -> set[str]:
        return self._session_lifecycle.active_mercenaries

    @active_mercenaries.setter
    def active_mercenaries(self, value: set[str]) -> None:
        self._session_lifecycle.active_mercenaries = value

    @property
    def team_sessions(self) -> dict[int, dict]:
        return self._session_lifecycle.team_sessions

    @team_sessions.setter
    def team_sessions(self, value: dict[int, dict]) -> None:
        self._session_lifecycle.team_sessions = value

    # Compatibility views forward to one canonical matchmaking state owner.
    @property
    def preparations(self) -> dict[str, dict]:
        return self._matchmaking.preparations

    @preparations.setter
    def preparations(self, value: dict[str, dict]) -> None:
        self._matchmaking.preparations = value

    @property
    def matches(self) -> dict[int, dict]:
        return self._matchmaking.matches

    @matches.setter
    def matches(self, value: dict[int, dict]) -> None:
        self._matchmaking.matches = value

    @property
    def signaling_ready_pairs(self) -> set[frozenset[str]]:
        return self._matchmaking.signaling_ready_pairs

    @signaling_ready_pairs.setter
    def signaling_ready_pairs(self, value: set[frozenset[str]]) -> None:
        self._matchmaking.signaling_ready_pairs = value

    @property
    def next_prepare_id(self) -> int:
        return self._matchmaking.next_prepare_id

    @next_prepare_id.setter
    def next_prepare_id(self, value: int) -> None:
        self._matchmaking.next_prepare_id = value

    @property
    def next_match_id(self) -> int:
        return self._matchmaking.next_match_id

    @next_match_id.setter
    def next_match_id(self, value: int) -> None:
        self._matchmaking.next_match_id = value

    @property
    def match_room_log_paths(self) -> list[Path]:
        return self._matchmaking.match_room_log_paths

    @match_room_log_paths.setter
    def match_room_log_paths(self, value: list[Path]) -> None:
        self._matchmaking.match_room_log_paths = value

    def _persist_session_state(self) -> None:
        if self.persist_sessions is None:
            return
        try:
            self.persist_sessions()
        except OSError as exc:
            self.telemetry.emit(
                "game_session_state_persist_failed",
                error=repr(exc),
                count=len(self.sessions),
            )

    def connection_made(self, transport) -> None:
        self.transport_closed.clear()
        self.transport = transport

    def connection_lost(self, exc) -> None:
        self.close_runtime()
        self.transport = None
        self.transport_closed.set()

    def close_runtime(self) -> None:
        """Cancel owned timers/tasks without mutating durable player state."""
        self._reply_recovery.close()
        self._request_recovery.close()
        self._matchmaking.close()

    def _schedule_reply_recovery(self, session, packets, attempt=0, key=None, peer=None) -> None:
        self._reply_recovery.schedule(session, packets, attempt, key, peer)

    def _recover_reply(self, session, packets, attempt, key, peer) -> None:
        self._reply_recovery.recover(session, packets, attempt, key, peer)

    def _live_mercenary_online_ids(self) -> set[str]:
        return self._session_lifecycle._live_mercenary_online_ids()

    def _notify_team_profile_updated(
        self,
        team_id: int,
        *,
        reason: str,
        subject_online_id: str,
    ) -> list[str]:
        return self._session_lifecycle._notify_team_profile_updated(
            team_id, reason=reason, subject_online_id=subject_online_id
        )

    def _remove_team_session_member_and_notify(
        self,
        online_id: str,
        requested_session_id: int = 0,
    ) -> tuple[list[int], list[int], list[str]]:
        return self._session_lifecycle._remove_team_session_member_and_notify(
            online_id, requested_session_id
        )

    def _purge_stale_team_session_members(self) -> None:
        return self._session_lifecycle._purge_stale_team_session_members()

    def _remove_matchmaking_member(self, online_id: str, *, reason: str) -> list[int]:
        return self._matchmaking._remove_matchmaking_member(online_id, reason=reason)

    def _purge_stale_matchmaking(self) -> None:
        return self._matchmaking._purge_stale_matchmaking()

    # RECEIVE PIPELINE: decode, authenticate, acknowledge, reassemble, dispatch.
    # A transport ACK is not proof that a gameplay action was applied.
    def datagram_received(self, data: bytes, addr) -> None:
        self.telemetry.emit("game_datagram_received", peer=addr, payload=data)
        LOGGER.info("game UDP datagram from %s: %d bytes", addr, len(data))
        auth_token = data[:8]
        session = self.sessions.get(auth_token)
        if session is None:
            self.telemetry.emit("game_datagram_rejected", peer=addr, reason="unknown_auth_token")
            return
        try:
            _, packet_type, plaintext = decrypt_client_datagram(
                data, session.game_key, self.library_path
            )
            packet = decode_reliable_packet(plaintext)
        except (CryptoError, ValueError) as exc:
            self.telemetry.emit("game_datagram_rejected", peer=addr, reason=str(exc))
            return
        session.last_seen_monotonic = time.monotonic()
        session.last_seen_unix = time.time()
        if session.peer != addr:
            self._request_recovery.discard(session)
            session.delivery_observation.pending.clear()
        session.peer = addr
        if packet.opcode == SYN:
            session.delivery_observation.pending.clear()
        delivery = session.delivery_observation.acknowledge(
            packet.remote_sequence, session.outbound_reliable, session.last_seen_monotonic
        )
        self._acknowledge_outbound(session, packet.remote_sequence)
        self.telemetry.emit(
            "game_packet_decoded",
            peer=addr,
            online_id=session.online_id,
            packet_type=packet_type,
            local_sequence=packet.local_sequence,
            remote_sequence=packet.remote_sequence,
            opcode=packet.opcode,
            initial_prefix=packet.initial_prefix,
            payload=packet.payload,
            **({"ack_delivery": delivery} if delivery else {}),
        )
        if packet.opcode == SYN:
            self._request_recovery.discard(session)
            session.remote_sequence = packet.local_sequence
            session.receive_sequence_initialized = True
            session.receive_high_sequence = None
            session.fragment_parts.clear()
            session.fragment_totals.clear()
            session.fragment_started_monotonic.clear()
            session.fragment_updated_monotonic.clear()
            self._clear_signaling_ready_for(session.online_id)
            self._send(
                session,
                ReliablePacket(
                    session.server_sequence,
                    packet.local_sequence,
                    SYN_ACK,
                    payload=self.SYN_ACK_PAYLOAD,
                ),
            )
            session.server_sequence = (session.server_sequence + 1) % 4096
            self._send(session, ReliablePacket(0, packet.local_sequence, ACK))
        elif packet.opcode == RETRANSMIT:
            requested_sequence = (packet.remote_sequence + 1) % 4096
            retained = session.outbound_reliable.get(requested_sequence)
            if retained is None:
                self.telemetry.emit(
                    "game_reliable_retransmit_unavailable",
                    peer=addr,
                    online_id=session.online_id,
                    acknowledged_sequence=packet.remote_sequence,
                    requested_sequence=requested_sequence,
                    retained_sequences=list(session.outbound_reliable),
                )
            else:
                self._send(session, retained, remember=False)
                self.telemetry.emit(
                    "game_reliable_packet_retransmitted",
                    peer=addr,
                    online_id=session.online_id,
                    acknowledged_sequence=packet.remote_sequence,
                    retransmitted_sequence=requested_sequence,
                    retransmitted_opcode=retained.opcode,
                )
        elif packet.opcode == ACK:
            session.established = True
            self.telemetry.emit("game_session_established", peer=addr, online_id=session.online_id)
        elif packet.opcode == HBT:
            self._send(session, ReliablePacket(0, packet.local_sequence, HBT))
        elif packet.opcode in (DAT, DAT_ACK):
            accepted = self._request_recovery.accept(session, packet)
            self._send(session, ReliablePacket(0, session.remote_sequence, ACK))
            if accepted:
                self.telemetry.emit(
                    "game_payload_received",
                    peer=addr,
                    online_id=session.online_id,
                    payload=packet.payload,
                )
                self._handle_game_payload(session, packet)
            self._request_recovery.refresh(session)
        elif packet.opcode in (FIN, RST):
            # Both paths retire transient state, never durable team membership.
            # FIN is acknowledged before cleanup; RST intentionally has no reply.
            reason = "fin" if packet.opcode == FIN else "rst"
            if packet.opcode == FIN:
                self._send(session, ReliablePacket(0, packet.local_sequence, FIN_ACK))
            session.established = False
            session.delivery_observation.pending.clear()
            self._session_lifecycle.discard_deferred_team_session_pushes(session)
            self._request_recovery.discard(session)
            self.active_mercenaries.discard(session.online_id)
            mercenary_removed = self.teams.unregister_mercenary(session.online_id)
            removed_match_ids = self._remove_matchmaking_member(session.online_id, reason=reason)
            self._clear_signaling_ready_for(session.online_id)
            removed_team_session_ids, deleted_team_session_ids, notified_online_ids = (
                self._remove_team_session_member_and_notify(session.online_id)
            )
            self.telemetry.emit(
                "game_session_closed",
                peer=addr,
                online_id=session.online_id,
                reason=reason,
                mercenary_removed=mercenary_removed,
                removed_match_ids=removed_match_ids,
                removed_team_session_ids=removed_team_session_ids,
                deleted_team_session_ids=deleted_team_session_ids,
                team_session_leave_notified_online_ids=notified_online_ids,
            )
        self._persist_session_state()

    @staticmethod
    def _acknowledge_outbound(session: GameSession, acknowledged_sequence: int) -> None:
        """Drop a cumulative reliable prefix only when its endpoint is retained."""
        if acknowledged_sequence not in session.outbound_reliable:
            return
        for sequence in list(session.outbound_reliable):
            session.outbound_reliable.pop(sequence, None)
            if sequence == acknowledged_sequence:
                break

    def _send(
        self,
        session: GameSession,
        packet: ReliablePacket,
        *,
        remember: bool = True,
    ) -> None:
        if self.transport is None or session.peer is None:
            return
        if remember and packet.opcode in (SYN_ACK, DAT, DAT_ACK):
            # A 12-bit sequence can wrap during a long session. Reinsert an
            # overwritten value at the end and retain a bounded half-window.
            session.outbound_reliable.pop(packet.local_sequence, None)
            session.outbound_reliable[packet.local_sequence] = packet
            while len(session.outbound_reliable) > 512:
                session.outbound_reliable.pop(next(iter(session.outbound_reliable)))
        plaintext = encode_reliable_packet(packet)
        datagram = encrypt_server_datagram(plaintext, session.game_key, self.library_path)
        self.transport.sendto(datagram, session.peer)
        session.delivery_observation.sent(packet, session.outbound_reliable, time.monotonic())
        self.telemetry.emit(
            "game_datagram_sent",
            peer=session.peer,
            online_id=session.online_id,
            local_sequence=packet.local_sequence,
            remote_sequence=packet.remote_sequence,
            opcode=packet.opcode,
            plaintext=plaintext,
            payload=datagram,
        )

    def _handle_game_payload(self, session: GameSession, packet: ReliablePacket) -> None:
        message = assemble_request(session, packet, telemetry=self.telemetry)
        if message is None:
            return
        self._dispatch_game_message(session, packet, message)

    # APPLICATION ROUTER: named opcodes lead to domain handlers or explicit stubs.
    # Do not move encryption/request correlation into the individual handlers.
    def _dispatch_game_message(
        self, session: GameSession, packet: ReliablePacket, message: GameMessage
    ) -> None:
        dispatch_request(self, session, packet, message)

    # LEGACY MATCH CALLERS: forward to MatchmakingCoordinator without copied policy.
    def _match_team_session_membership(self, preparation: dict) -> tuple[int, dict, dict] | None:
        return self._matchmaking._match_team_session_membership(preparation)

    def _clear_signaling_ready_for(self, online_id: str) -> None:
        return self._matchmaking._clear_signaling_ready_for(online_id)

    def _remove_matchmaking_state(self, match_id: int) -> list[str]:
        return self._matchmaking._remove_matchmaking_state(match_id)

    def _announce_cross_session_match_peers(self, match: dict) -> list[str]:
        return self._matchmaking._announce_cross_session_match_peers(match)

    def _match_preparation(self, preparation: dict) -> None:
        return self._matchmaking._match_preparation(preparation)

    def _cancel_match_room_log_watch(self, match: dict) -> None:
        return self._matchmaking._cancel_match_room_log_watch(match)

    async def _watch_match_room_logs(
        self, match_id: int, host_online_id: str, offsets: dict[str, int]
    ) -> None:
        return await self._matchmaking._watch_match_room_logs(match_id, host_online_id, offsets)

    def _publish_match_room(
        self,
        session: GameSession,
        *,
        room_id: int,
        room_password: bytes,
        source_values: dict[int, int],
        include_room_info: bool = True,
    ) -> None:
        return self._matchmaking._publish_match_room(
            session,
            room_id=room_id,
            room_password=room_password,
            source_values=source_values,
            include_room_info=include_room_info,
        )

    # REPLY PIPELINE: bind the response to the request, then frame/reliably send it.
    def _send_game_reply(
        self,
        session: GameSession,
        packet: ReliablePacket,
        request: GameMessage,
        response_proto: bytes,
        *,
        error: tuple[int, int] | None = None,
    ) -> None:
        if self.telemetry.diagnostics.enabled:
            self.telemetry.emit(
                "game_reply_observed",
                online_id=session.online_id,
                message_type=request.message_type,
                message_index=request.message_index,
                payload=response_proto,
                error=error,
            )
        responses = encode_game_reply_fragments(
            GameMessage(
                0,
                request.message_index,
                response_proto,
                session.fragment_counter,
            ),
            error=error,
        )
        session.fragment_counter = (session.fragment_counter + 1) & 0xFFFF
        sent_packets = []
        for fragment_index, response in enumerate(responses):
            outgoing = ReliablePacket(
                session.server_sequence,
                packet.local_sequence if fragment_index == 0 else 0,
                DAT_ACK if fragment_index == 0 else DAT,
                payload=response,
            )
            self._send(session, outgoing)
            sent_packets.append(outgoing)
            session.server_sequence = (session.server_sequence + 1) % 4096
        if len(responses) > 1:
            self._schedule_reply_recovery(session, tuple(sent_packets))
            self.telemetry.emit(
                "game_reply_fragmented",
                peer=session.peer,
                online_id=session.online_id,
                message_index=request.message_index,
                fragment_count=len(responses),
                total_length=sum(len(response) - 12 for response in responses),
            )

    def _send_game_error(
        self,
        session: GameSession,
        packet: ReliablePacket,
        request: GameMessage,
        result: int,
        detail: int,
    ) -> None:
        """Complete a rejected request without a protobuf success acknowledgement."""
        self._send_game_reply(session, packet, request, b"", error=(result, detail))
        self.telemetry.emit(
            "game_reply_error_sent",
            peer=session.peer,
            online_id=session.online_id,
            message_type=request.message_type,
            message_index=request.message_index,
            result_code=result,
            detail_code=detail,
        )

    def _send_game_push(
        self,
        session: GameSession,
        push_proto: bytes,
        *,
        subtype: int | None = None,
    ) -> None:
        wire_subtype = int(subtype) - 1 if subtype is not None else None
        wire_payload = (
            _typed_push_payload(subtype, push_proto) if subtype is not None else push_proto
        )
        payload = encode_game_push(wire_payload, session.fragment_counter)
        session.fragment_counter = (session.fragment_counter + 1) & 0xFFFF
        self._send(
            session,
            ReliablePacket(
                session.server_sequence,
                0,
                DAT,
                payload=payload,
            ),
        )
        session.server_sequence = (session.server_sequence + 1) % 4096
        self.telemetry.emit(
            "game_push_sent",
            peer=session.peer,
            online_id=session.online_id,
            push_id=0x038C,
            subtype=subtype,
            wire_subtype=wire_subtype,
        )

    def error_received(self, exc: Exception) -> None:
        self.telemetry.emit("game_datagram_error", error=repr(exc))
