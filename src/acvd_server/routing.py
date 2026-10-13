"""Application request routing; transport and domain policy stay with their owners.

Keep opcode selection explicit. This module only wires request-scoped callbacks
and dependencies into handlers; it does not own sockets or durable state.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from . import player_medals
from .handlers import (
    economy,
    emblems,
    feedback,
    inventory as inventory_requests,
    matching,
    mercenaries,
    notices,
    profiles,
    regulation as regulation_requests,
    screenshots as screenshot_requests,
    shop as shop_requests,
    snapshots,
    team_queries,
    team_sessions as session_requests,
    team_updates,
    territory,
    world as world_requests,
)
from .protocol import decode_proto, proto_bytes, proto_varint
from .result_messages import BATTLE_RESULT_FIXED_PUSH, battle_result_fixed_push
from .team_messages import _vote_kick_member_response
from .udp import (
    APPLY_WORLD_TOP_SCREENSHOT,
    BATTLE_START,
    BUY_PLAYER_SHOP_FOR_PARTS,
    BUY_PLAYER_SHOP_KEY_ITEM,
    CANCEL_MERCENARY_NEGOTIATION,
    CANCEL_PREPARE,
    CHECK_REGULATION_DATA,
    CHECK_VALID_PLAYER,
    CHECK_VALID_PLAYER_BOOTSTRAP,
    CHECK_VALID_UGC_ID_LIST,
    CONTINUE_MERCENARY_REGISTRATION,
    CREATE_TEAM,
    CREATE_TEAM_SESSION,
    DELETE_TEAM_SESSION,
    GET_AC_DATA,
    GET_AI_EXTEND,
    GET_AI_INFO,
    GET_BATTLE_RECORD,
    GET_COMMENT,
    GET_DEFEAT_RANKING_LIST,
    GET_FACTION_SELECTION,
    GET_FACTION_STRUGGLE_RESULT,
    GET_GARAGE_CUSTOM_INFO,
    GET_JOINING_TEAM_STATUS,
    GET_MERCENARY_DATA,
    GET_MERCENARY_MESSAGE_LIST,
    GET_MERCENARY_RANK,
    GET_MERCENARY_RANKING,
    GET_MERCENARY_SELF_RANKING,
    GET_MISSION_SAVEDATA,
    GET_MY_DEFEAT_RANKING_INFO,
    GET_MY_ITEM_SIMPLE_INFO_LIST,
    GET_MY_ITEM_SIMPLE_INFO_LIST_ALT,
    GET_MY_MEDAL_INFO,
    GET_MY_PLAYER_RANKING_INFO,
    GET_MY_TEAM_MEDAL_INFO,
    GET_MY_TEAM_RANKING_INFO,
    GET_MY_USER_INFO,
    GET_NOTICE_LIST,
    GET_OWN_TEAM_VOTE_STATUS,
    GET_OWN_UNAC_LIST,
    GET_PERSONAL_ANNOUNCE,
    GET_PLAYER_EMBLEM,
    GET_PLAYER_KEY_ITEM_LIST,
    GET_PLAYER_RANKING_LIST,
    GET_PLAYER_SHOP_LINEUP_FOR_PARTS,
    GET_SERVER_DATETIME,
    GET_TEAM_EMBLEM,
    GET_TEAM_INFO,
    GET_TEAM_INFO_LIST,
    GET_TEAM_MEMBER_COMMENT_LIST,
    GET_TEAM_RANKING_INFO_LIST,
    GET_TEAM_RECORD,
    GET_TERRITORY_CANNON_LAYOUT,
    GET_TERRITORY_INFO,
    GET_TRASH_PARTS_LIST,
    GET_USER_INFO_BY_PLAYER_ID,
    GET_VOTE_KICK_MEMBER,
    GET_WAIT_TIME_FOR_APPLY_WORLD_TOP_SS,
    GET_WELCOME_MESSAGE_INFO_LIST,
    GET_WORLD_BASIC_INFO,
    GET_WORLD_CURRENT_INFO,
    GET_WORLD_LOG,
    GET_WORLD_TOP_SCREENSHOT,
    GET_WORLD_TOP_SS_THUMBNAIL_LIST,
    JOIN_FACTION,
    JOIN_TEAM_AUTO,
    JOIN_TEAM_SESSION,
    LEAVE_TEAM,
    LEAVE_TEAM_SESSION,
    NOTIFY_MATCH_ROOM_INFO,
    REGISTER_AC_DATA,
    REGISTER_BATTLE_RESULT,
    REGISTER_COMBAT_RESULT,
    REGISTER_EMBLEM,
    REGISTER_GARAGE_CUSTOM_INFO,
    REGISTER_MERCENARY,
    REGISTER_PLAYER_SCREENSHOT,
    REGISTER_PREPARE,
    REPORT_BATTLE_SESSION_DISCONNECT,
    REPORT_BRIEFING_END,
    REPORT_DISCONNECTED,
    REPORT_DISCONNECTED_TEAM_SESSION_MEMBER,
    SEARCH_MERCENARY,
    SEARCH_TEAM_ID_LIST,
    SELL_PARTS_ITEM,
    SEND_MERCENARY_MESSAGE,
    SET_PERSONAL_ANNOUNCE_ALREADY_READ,
    SET_PLAYER_EMBLEM_BY_UGC_ID,
    SET_TEAM_EMBLEM_BY_UGC_ID,
    START_MERCENARY_NEGOTIATION,
    SYNC_MONEY,
    SYNC_OFFLINE_DATA,
    SYNCHRONIZE_DLC_PARTS_LINEUP,
    TRADE_MONEY,
    UNREGISTER_MERCENARY,
    UPDATE_AI_EXTEND,
    UPDATE_LEADER_TEAM_INFO,
    UPDATE_MEMBER_TEAM_INFO,
    UPDATE_TEAM_FACTION_INFO,
    UPDATE_TEAM_MEMBER_COMMENT,
    UPDATE_USER_PROPERTY,
    UPLOAD_AI_INFO,
    UPLOAD_MISSION_SAVEDATA,
    UPLOAD_TERRITORY_CANNON_LAYOUT,
    VERIFY_OWN_PARTS_LIST,
    WAIT_FOR_USER_LOGIN,
    GameMessage,
    GameSession,
    ReliablePacket,
)
from .world_economy import build_world_settlement, world_result_identity

if TYPE_CHECKING:
    from .server import GameDatagramProtocol

INITIAL_SERVER_BALANCE = 1_000_000


def dispatch_request(
    runtime: GameDatagramProtocol,
    session: GameSession,
    packet: ReliablePacket,
    message: GameMessage,
) -> None:
    # Small inline bootstrap/compatibility replies are cataloged separately
    # in readme.txt; an ACK is not proof of feature completion.
    runtime.telemetry.emit(
        "game_message_received",
        peer=session.peer,
        online_id=session.online_id,
        message_type=message.message_type,
        message_index=message.message_index,
        payload=message.payload,
    )
    reply = partial(runtime._send_game_reply, session, packet, message)

    def reply_error(result: int, detail: int) -> None:
        runtime._send_game_reply(session, packet, message, b"", error=(result, detail))

    def reply_error_with_event(result: int, detail: int) -> None:
        runtime._send_game_error(session, packet, message, result, detail)

    if message.message_type == WAIT_FOR_USER_LOGIN:
        runtime.teams.remember_player(session.online_id, session.player_id)
        response_proto = proto_bytes(1, session.online_id) + proto_varint(2, session.player_id)
        runtime._send_game_reply(session, packet, message, response_proto)
        runtime.telemetry.emit(
            "game_login_accepted",
            peer=session.peer,
            online_id=session.online_id,
            player_id=session.player_id,
        )
        upload_config = proto_varint(2, 15)
        push_proto = (
            proto_varint(1, 0x038C)
            + proto_bytes(2, upload_config)
            + proto_varint(3, 600)
            + proto_varint(4, 300)
        )
        runtime._send_game_push(session, push_proto)
    elif message.message_type == GET_MY_USER_INFO:
        profiles.get_my_user_info(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == CREATE_TEAM:
        team_updates.create_team(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            reply=reply,
        )
    elif message.message_type == GET_TEAM_INFO:
        team_queries.get_team_info(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            reply=reply,
        )
    elif message.message_type == GET_TEAM_EMBLEM:
        emblems.get_team_emblem(
            session,
            message,
            telemetry=runtime.telemetry,
            teams=runtime.teams,
            reply=reply,
        )
    elif message.message_type == GET_USER_INFO_BY_PLAYER_ID:
        team_queries.get_user_info(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            game_sessions=runtime.sessions,
            reply=reply,
        )
    elif message.message_type == SEARCH_TEAM_ID_LIST:
        team_queries.search_team_ids(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            reply=reply,
        )
    elif message.message_type == LEAVE_TEAM:
        team_updates.leave_team(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            notify_team_profile=runtime._notify_team_profile_updated,
            reply=reply,
        )
    elif message.message_type == GET_TEAM_INFO_LIST:
        team_queries.get_team_info_list(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            reply=reply,
        )
    elif message.message_type == JOIN_TEAM_AUTO:
        team_updates.join_team_auto(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            notify_team_profile=runtime._notify_team_profile_updated,
            reply=reply,
        )
    elif message.message_type == GET_OWN_TEAM_VOTE_STATUS:
        team_queries.get_vote_status(
            session,
            message,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_JOINING_TEAM_STATUS:
        team_queries.get_joining_status(
            session,
            message,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == SET_TEAM_EMBLEM_BY_UGC_ID:
        emblems.set_team_emblem(
            session,
            message,
            telemetry=runtime.telemetry,
            teams=runtime.teams,
            reply=reply,
        )
    elif message.message_type == JOIN_FACTION:
        team_updates.join_faction(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            reply=reply,
        )
    elif message.message_type == UPDATE_TEAM_FACTION_INFO:
        team_updates.update_team_properties(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            reply=reply,
            phase=runtime.world_phase(),
            reply_error=reply_error,
        )
    elif message.message_type == TRADE_MONEY:
        economy.trade_money(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
            reply_error=partial(runtime._send_game_error, session, packet, message),
            initial_balance=INITIAL_SERVER_BALANCE,
        )
    elif message.message_type == UPDATE_USER_PROPERTY:
        profiles.update_user_property(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == UPDATE_MEMBER_TEAM_INFO:
        profiles.update_member_team_info(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
            notify_team_profile=runtime._notify_team_profile_updated,
        )
    elif message.message_type == UPDATE_TEAM_MEMBER_COMMENT:
        profiles.update_team_member_comment(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == UPDATE_LEADER_TEAM_INFO:
        team_updates.update_leader_settings(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            reply=reply,
        )
    elif message.message_type == JOIN_TEAM_SESSION:
        session_requests.join_session(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            session_directory=runtime.team_sessions,
            purge_stale_members=runtime._purge_stale_team_session_members,
            reply=reply,
        )
    elif message.message_type == LEAVE_TEAM_SESSION:
        session_requests.leave_session(
            session,
            message,
            telemetry=runtime.telemetry,
            remove_member=runtime._remove_team_session_member_and_notify,
            reply=reply,
        )
    elif message.message_type == REPORT_DISCONNECTED_TEAM_SESSION_MEMBER:
        session_requests.report_disconnected_member(
            session,
            message,
            telemetry=runtime.telemetry,
            retire_overlay=runtime._matchmaking.retire_reported_overlay,
            recover_peer=partial(
                runtime._session_lifecycle.recover_reported_peer,
                can_recover=runtime._matchmaking.team_peer_recovery_allowed,
            ),
            reply=reply,
        )
    elif message.message_type == DELETE_TEAM_SESSION:
        session_requests.delete_session(
            session,
            message,
            telemetry=runtime.telemetry,
            remove_member=runtime._remove_team_session_member_and_notify,
            reply=reply,
        )
    elif message.message_type == CHECK_VALID_PLAYER:
        session_requests.check_valid_player(
            session,
            message,
            telemetry=runtime.telemetry,
            session_directory=runtime.team_sessions,
            purge_stale_members=runtime._purge_stale_team_session_members,
            reply=reply,
        )
        runtime._session_lifecycle.flush_deferred_team_session_pushes(
            session, trigger="team_session_validated"
        )
    elif message.message_type == CREATE_TEAM_SESSION:
        runtime._session_lifecycle.discard_deferred_team_session_pushes(session)
        session_requests.create_session(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            session_directory=runtime.team_sessions,
            game_sessions=runtime.sessions,
            purge_stale_members=runtime._purge_stale_team_session_members,
            send_push=runtime._send_game_push,
            defer_push=runtime._session_lifecycle.defer_team_session_push,
            reply=reply,
        )
    elif message.message_type == CHECK_VALID_PLAYER_BOOTSTRAP:
        session_requests.check_bootstrap_player(
            session,
            message,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == SYNC_MONEY:
        economy.sync_money(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == SYNC_OFFLINE_DATA:
        inventory_requests.sync_offline_data(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            player_data_path=runtime.player_data_path,
            reply=reply,
        )
    elif message.message_type == GET_OWN_UNAC_LIST:
        snapshots.get_owned_unacs(
            session,
            message,
            player_data_path=runtime.player_data_path,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_AI_EXTEND:
        snapshots.get_ai_extension(
            session,
            message,
            player_data_path=runtime.player_data_path,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == UPDATE_AI_EXTEND:
        snapshots.update_ai_extension(
            session,
            message,
            player_data_path=runtime.player_data_path,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_TRASH_PARTS_LIST:
        shop_requests.get_trash_parts(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
            reply_error=reply_error,
        )
    elif message.message_type == GET_GARAGE_CUSTOM_INFO:
        profiles.get_garage_custom_info(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == REGISTER_GARAGE_CUSTOM_INFO:
        profiles.register_garage_custom_info(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_PLAYER_EMBLEM:
        emblems.get_player_emblem(
            session,
            message,
            telemetry=runtime.telemetry,
            teams=runtime.teams,
            reply=reply,
        )
    elif message.message_type == REGISTER_AC_DATA:
        snapshots.register_ac_data(
            session,
            message,
            player_data_path=runtime.player_data_path,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == REGISTER_EMBLEM:
        emblems.register_emblem(
            session,
            message,
            telemetry=runtime.telemetry,
            teams=runtime.teams,
            reply=reply,
        )
    elif message.message_type == SET_PLAYER_EMBLEM_BY_UGC_ID:
        emblems.set_player_emblem(
            session,
            message,
            telemetry=runtime.telemetry,
            teams=runtime.teams,
            reply=reply,
        )
    elif message.message_type == GET_AC_DATA:
        snapshots.get_ac_data(
            session,
            message,
            player_data_path=runtime.player_data_path,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_MISSION_SAVEDATA:
        snapshots.get_mission_data(
            session,
            message,
            player_data_path=runtime.player_data_path,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == UPLOAD_MISSION_SAVEDATA:
        snapshots.upload_mission_data(
            session,
            message,
            player_data_path=runtime.player_data_path,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_AI_INFO:
        snapshots.get_ai_info(
            session,
            message,
            player_data_path=runtime.player_data_path,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == UPLOAD_AI_INFO:
        snapshots.upload_ai_info(
            session,
            message,
            player_data_path=runtime.player_data_path,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_SERVER_DATETIME:
        world_requests.get_server_datetime(
            session,
            message,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_WORLD_BASIC_INFO:
        world_requests.get_world_basic_info(
            session,
            message,
            telemetry=runtime.telemetry,
            reply=reply,
            phase=runtime.world_phase(),
            cq_map_settings=runtime._get_cq_map_settings(),
            war=runtime.teams.test_war_snapshot(),
        )
        runtime._session_lifecycle.flush_deferred_team_session_pushes(
            session, trigger="world_basic_info"
        )
    elif message.message_type == GET_FACTION_SELECTION:
        world_requests.get_faction_selection(
            session,
            message,
            telemetry=runtime.telemetry,
            teams=runtime.teams,
            reply=reply,
        )
    elif message.message_type == GET_FACTION_STRUGGLE_RESULT:
        world_requests.get_faction_struggle_result(
            session,
            message,
            telemetry=runtime.telemetry,
            teams=runtime.teams,
            reply=reply,
        )
    elif message.message_type == GET_WORLD_CURRENT_INFO:
        world_requests.get_world_current_info(
            session,
            message,
            telemetry=runtime.telemetry,
            reply=reply,
            phase=runtime.world_phase(),
            cq_map_settings=runtime._get_cq_map_settings(),
            war=runtime.teams.test_war_snapshot(),
        )
        runtime._session_lifecycle.flush_deferred_team_session_pushes(
            session, trigger="world_current_info"
        )
    elif message.message_type == GET_NOTICE_LIST:
        world_requests.get_notice_list(
            session,
            message,
            telemetry=runtime.telemetry,
            reply=reply,
            teams=runtime.teams,
            reply_error=reply_error_with_event,
        )
    elif message.message_type == GET_WORLD_LOG:
        world_requests.get_world_log(
            session,
            message,
            telemetry=runtime.telemetry,
            reply=reply,
            phase=runtime.world_phase(),
            cq_map_settings=runtime._get_cq_map_settings(),
            war=runtime.teams.test_war_snapshot(),
        )
    elif message.message_type == REGISTER_BATTLE_RESULT:
        economy.register_battle_result(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
            initial_balance=INITIAL_SERVER_BALANCE,
            player_data_path=runtime.player_data_path,
            game_sessions=runtime.sessions,
            team_sessions=runtime.team_sessions,
            purge_team_sessions=runtime._purge_stale_team_session_members,
            hire_bindings=runtime._matchmaking.hires,
        )
    elif message.message_type == GET_TEAM_RECORD:
        world_requests.get_team_record(
            session,
            message,
            telemetry=runtime.telemetry,
            teams=runtime.teams,
            reply=reply,
            reply_error=reply_error_with_event,
        )
    elif message.message_type == SYNCHRONIZE_DLC_PARTS_LINEUP:
        inventory_requests.synchronize_dlc_lineup(
            session,
            message,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == BATTLE_START:
        # Exact generated metadata and a bounded optional-field trial show
        # RequestBattleStartResponse carries no stage assignment.  It is
        # an acknowledgement after the client has built battle context.
        runtime._send_game_reply(session, packet, message, b"")
        runtime.telemetry.emit(
            "game_battle_start_acknowledged",
            peer=session.peer,
            online_id=session.online_id,
            request_payload=message.payload,
        )
    elif message.message_type == REGISTER_PREPARE:
        matching.register_preparation(
            session,
            message,
            matchmaking=runtime._matchmaking,
            team_store=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
            reply_error=partial(runtime._send_game_error, session, packet, message),
        )
    elif message.message_type == CANCEL_PREPARE:
        matching.cancel_preparation(
            session,
            message,
            matchmaking=runtime._matchmaking,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == REPORT_BATTLE_SESSION_DISCONNECT:
        matching.report_disconnect(
            session,
            message,
            matchmaking=runtime._matchmaking,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == REPORT_DISCONNECTED:
        matching.report_disconnected(
            session,
            message,
            matchmaking=runtime._matchmaking,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == REPORT_BRIEFING_END:
        matching.report_briefing_end(
            session,
            message,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == NOTIFY_MATCH_ROOM_INFO:
        matching.notify_room(
            session,
            message,
            matchmaking=runtime._matchmaking,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_MERCENARY_RANK:
        mercenaries.get_rank(
            session,
            message,
            team_store=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_MERCENARY_SELF_RANKING:
        mercenaries.get_self_ranking(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            reply=reply,
        )
    elif message.message_type == GET_MERCENARY_RANKING:
        mercenaries.get_rankings(
            session,
            message,
            team_store=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == REGISTER_MERCENARY:
        mercenaries.register(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            active_mercenaries=runtime.active_mercenaries,
            hire_bindings=runtime._matchmaking.hires,
            reply=reply,
        )
    elif message.message_type == CONTINUE_MERCENARY_REGISTRATION:
        mercenaries.continue_registration(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            active_mercenaries=runtime.active_mercenaries,
            reply=reply,
        )
    elif message.message_type == UNREGISTER_MERCENARY:
        mercenaries.unregister(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            active_mercenaries=runtime.active_mercenaries,
            hire_bindings=runtime._matchmaking.hires,
            reply=reply,
        )
    elif message.message_type == SEARCH_MERCENARY:
        mercenaries.search(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            live_mercenary_online_ids=runtime._live_mercenary_online_ids,
            reply=reply,
        )
    elif message.message_type == GET_MERCENARY_DATA:
        mercenaries.get_data(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            live_mercenary_online_ids=runtime._live_mercenary_online_ids,
            reply=reply,
        )
    elif message.message_type == SEND_MERCENARY_MESSAGE:
        feedback.send_feedback(
            session,
            message,
            team_store=runtime.teams,
            hire_bindings=runtime._matchmaking.hires,
            player_data_path=runtime.player_data_path,
            telemetry=runtime.telemetry,
            reply=reply,
            reply_error=reply_error,
        )
    elif message.message_type == START_MERCENARY_NEGOTIATION:
        mercenaries.queue_negotiation(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            live_mercenary_online_ids=runtime._live_mercenary_online_ids,
            hire_bindings=runtime._matchmaking.hires,
            reply=reply,
        )
    elif message.message_type == GET_MERCENARY_MESSAGE_LIST:
        mercenaries.get_messages(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            live_mercenary_online_ids=runtime._live_mercenary_online_ids,
            reply=reply,
        )
    elif message.message_type == CANCEL_MERCENARY_NEGOTIATION:
        mercenaries.cancel_negotiation(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            hire_bindings=runtime._matchmaking.hires,
            reply=reply,
        )
    elif message.message_type == CHECK_REGULATION_DATA:
        regulation_requests.check_regulation(
            session,
            message,
            get_regulation=runtime._get_regulation,
            telemetry=runtime.telemetry,
            reply=reply,
            reply_error=reply_error,
        )
    elif message.message_type in (GET_PERSONAL_ANNOUNCE, SET_PERSONAL_ANNOUNCE_ALREADY_READ):
        notices.handle_personal_notices(
            session,
            message,
            player_data_path=runtime.player_data_path,
            telemetry=runtime.telemetry,
            reply=reply,
            reply_error=reply_error,
            acknowledge=message.message_type == SET_PERSONAL_ANNOUNCE_ALREADY_READ,
            welcome_message=runtime._get_welcome_message(),
        )
    elif message.message_type == VERIFY_OWN_PARTS_LIST:
        inventory_requests.acknowledge_owned_parts(
            session,
            message,
            telemetry=runtime.telemetry,
            reply=reply,
            player_data_path=runtime.player_data_path,
            teams=runtime.teams,
        )
    elif message.message_type == CHECK_VALID_UGC_ID_LIST:
        emblems.check_ugc_ids(
            session,
            message,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_VOTE_KICK_MEMBER:
        runtime._send_game_reply(
            session,
            packet,
            message,
            _vote_kick_member_response(),
        )
        runtime.telemetry.emit(
            "game_vote_kick_member_sent",
            peer=session.peer,
            online_id=session.online_id,
            player_id=session.player_id,
            member_id=0,
        )
    elif message.message_type == GET_TERRITORY_CANNON_LAYOUT:
        territory.get_layout(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            matchmaking=runtime._matchmaking,
            reply=reply,
            phase=runtime.world_phase(),
        )
    elif message.message_type == REGISTER_COMBAT_RESULT:
        team = runtime.teams.team_for(session.online_id)
        match = runtime._matchmaking.observe_combat_result(
            session, message.payload, team_id=int((team or {}).get("team_id", 0))
        )
        if not match:
            runtime.telemetry.emit(
                "game_message_rejected",
                online_id=session.online_id,
                message_type=message.message_type,
                reason="Combat result does not match one retained authenticated battle",
            )
            return
        try:
            report = match["combat_report"]
            receipt_digest = world_result_identity(match, report)
            receipt = runtime.teams.world_battle_settlement(receipt_digest)
            replay = receipt is not None
            reauthenticated_replay = match["squad"][session.player_id][0] is not session
            audit = None
            if receipt is None:
                if reauthenticated_replay:
                    raise ValueError("Reauthenticated World result has no durable receipt")
                settlement = build_world_settlement(
                    match,
                    report,
                    runtime.teams,
                    runtime._get_world_reward_settings(),
                )
                audit = settlement.audit
                receipt = runtime.teams.settle_world_battle_money(
                    settlement.result_digest,
                    settlement.payments,
                    policy_version=settlement.policy_version,
                    operator_win_player_ids=settlement.operator_win_player_ids,
                    boss_win_player_ids=settlement.boss_win_player_ids,
                    team_progress=settlement.team_progress,
                    team_medal_progress=settlement.team_medal_progress,
                    player_ranking_progress=settlement.player_ranking_progress,
                    war_impact=runtime._matchmaking.war_impact(match, report),
                )
            participant = next(
                value
                for value in receipt["participants"]
                if value.get("player_id") == session.player_id
                and value.get("online_id") == session.online_id
            )
            response = bytes.fromhex(participant["response_hex"])
            # Only a durable receipt authorizes retaining this match when
            # one participant disconnects before another gets its reply.
            match["reward_receipt_digest"] = receipt_digest
        except (KeyError, StopIteration, TypeError, ValueError, OSError) as error:
            runtime.telemetry.emit(
                "game_message_rejected",
                online_id=session.online_id,
                player_id=session.player_id,
                message_type=message.message_type,
                match_id=match.get("match_id"),
                reason=f"World reward settlement rejected: {error}",
            )
            return
        runtime._send_game_reply(session, packet, message, response)
        # Retail keeps World debrief pending until this distinct typed
        # push arrives.  The exact client callback ignores its values and
        # clears the post-battle wait flag.  The broken Mission Report was
        # observed while that retail transition was omitted entirely.
        runtime._send_game_push(
            session,
            battle_result_fixed_push(),
            subtype=BATTLE_RESULT_FIXED_PUSH,
        )
        social_team_id = int(match["squad"][session.player_id][1])
        same_team_players = {
            player_id
            for player_id, (_peer, team_id) in match.get("squad", {}).items()
            if int(team_id) == social_team_id
        }
        queued_refreshes = match.setdefault("post_battle_refresh_team_ids", set())
        if (
            len(same_team_players) > 1
            and social_team_id not in queued_refreshes
            and not reauthenticated_replay
        ):
            runtime._session_lifecycle.defer_post_battle_team_refreshes(social_team_id)
            queued_refreshes.add(social_team_id)
        runtime.telemetry.emit(
            "game_world_reward_settled",
            online_id=session.online_id,
            player_id=session.player_id,
            match_id=match.get("match_id"),
            result_digest=report.digest,
            receipt_digest=receipt_digest,
            policy_version=receipt["policy_version"],
            applied=bool(receipt.get("applied", False)),
            replay=replay,
            reauthenticated_replay=reauthenticated_replay,
            gross_earnings=participant["gross_earnings"],
            expenses=participant["expenses"],
            delta=participant["delta"],
            balance=participant["balance"],
            audit=audit,
        )
    elif message.message_type == UPLOAD_TERRITORY_CANNON_LAYOUT:
        territory.upload_layout(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            reply=reply,
            phase=runtime.world_phase(),
        )
    elif message.message_type == GET_TERRITORY_INFO:
        territory.get_info(
            session,
            message,
            telemetry=runtime.telemetry,
            team_store=runtime.teams,
            reply=reply,
        )
    elif message.message_type == GET_COMMENT:
        profiles.get_comment(
            session,
            message,
            telemetry=runtime.telemetry,
            teams=runtime.teams,
            reply=reply,
        )
    elif message.message_type == GET_PLAYER_SHOP_LINEUP_FOR_PARTS:
        shop_requests.list_parts(
            session,
            message,
            catalogue=runtime.shop_catalogue,
            player_data_path=runtime.player_data_path,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            additional_points=None,
            reply=reply,
            reply_error=reply_error,
        )
    elif message.message_type == SELL_PARTS_ITEM:
        shop_requests.sell_parts(
            session,
            message,
            teams=runtime.teams,
            catalogue=runtime.shop_catalogue,
            player_data_path=runtime.player_data_path,
            telemetry=runtime.telemetry,
            reply=reply,
            reply_error=reply_error,
        )
    elif message.message_type == BUY_PLAYER_SHOP_FOR_PARTS:
        shop_requests.buy_parts(
            session,
            message,
            teams=runtime.teams,
            catalogue=runtime.shop_catalogue,
            player_data_path=runtime.player_data_path,
            additional_points=None,
            telemetry=runtime.telemetry,
            reply=reply,
            reply_error=reply_error,
        )
    elif message.message_type == BUY_PLAYER_SHOP_KEY_ITEM:
        shop_requests.buy_key_item(
            session,
            message,
            teams=runtime.teams,
            player_data_path=runtime.player_data_path,
            telemetry=runtime.telemetry,
            reply=reply,
            reply_error=reply_error,
        )
    elif message.message_type == REGISTER_PLAYER_SCREENSHOT:
        screenshot_requests.register_screenshot(
            session,
            message,
            telemetry=runtime.telemetry,
            player_data_path=runtime.player_data_path,
            reply=reply,
        )
    elif message.message_type == APPLY_WORLD_TOP_SCREENSHOT:
        screenshot_requests.apply_screenshot(
            session,
            message,
            telemetry=runtime.telemetry,
            player_data_path=runtime.player_data_path,
            reply=reply,
        )
    elif message.message_type == GET_WORLD_TOP_SS_THUMBNAIL_LIST:
        screenshot_requests.get_thumbnails(
            session,
            message,
            telemetry=runtime.telemetry,
            player_data_path=runtime.player_data_path,
            reply=reply,
        )
    elif message.message_type == GET_WORLD_TOP_SCREENSHOT:
        screenshot_requests.get_screenshot(
            session,
            message,
            telemetry=runtime.telemetry,
            teams=runtime.teams,
            player_data_path=runtime.player_data_path,
            reply=reply,
        )
    elif message.message_type == GET_WAIT_TIME_FOR_APPLY_WORLD_TOP_SS:
        screenshot_requests.get_wait_time(
            session,
            message,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_TEAM_MEMBER_COMMENT_LIST:
        team_queries.get_team_member_comments(
            session,
            message,
            team_store=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_PLAYER_KEY_ITEM_LIST:
        inventory_requests.get_key_items(
            session,
            message,
            teams=runtime.teams,
            player_data_path=runtime.player_data_path,
            telemetry=runtime.telemetry,
            reply=reply,
            reply_error=reply_error,
        )
    elif message.message_type in (
        GET_WELCOME_MESSAGE_INFO_LIST,
        GET_BATTLE_RECORD,
    ):
        runtime._send_game_reply(session, packet, message, b"")
        runtime.telemetry.emit(
            "game_empty_collection_sent",
            peer=session.peer,
            online_id=session.online_id,
            message_type=message.message_type,
            count=0,
        )
    elif message.message_type == GET_MY_MEDAL_INFO:
        try:
            newly_awarded = runtime.teams.refresh_player_medal_state(
                session.online_id, session.player_id
            )
            medals = player_medals.load_records(runtime.teams, session)
            runtime._send_game_reply(
                session,
                packet,
                message,
                player_medals.encode_records(medals if medals is not None else ()),
            )
            runtime.telemetry.emit(
                "game_my_medal_info_sent",
                peer=session.peer,
                online_id=session.online_id,
                ownership_known=medals is not None,
                newly_awarded_medal_ids=list(newly_awarded),
                medal_count=sum(r.owned for r in medals) if medals is not None else 0,
            )
        except (ValueError, OSError) as error:
            runtime.telemetry.emit(
                "game_medal_readback_rejected", online_id=session.online_id, reason=str(error)
            )
            runtime._send_game_reply(session, packet, message, b"", error=(1, 28))
    elif message.message_type == GET_MY_ITEM_SIMPLE_INFO_LIST and any(
        field.number == 2 and type(field.value) is int and int(field.value) >> 16 in (2, 10)
        for field in decode_proto(message.payload)
    ):
        shop_requests.list_key_items(
            session,
            message,
            catalogue=runtime.key_item_shop_catalogue,
            player_data_path=runtime.player_data_path,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
            reply_error=reply_error,
        )
    elif message.message_type in (
        GET_MY_ITEM_SIMPLE_INFO_LIST,
        GET_MY_ITEM_SIMPLE_INFO_LIST_ALT,
    ):
        inventory_requests.get_owned_items(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            player_data_path=runtime.player_data_path,
            reply=reply,
        )
    elif message.message_type == GET_MY_TEAM_MEDAL_INFO:
        world_requests.get_team_medal_info(
            session,
            message,
            telemetry=runtime.telemetry,
            teams=runtime.teams,
            reply=reply,
        )
    elif message.message_type == GET_TEAM_RANKING_INFO_LIST:
        world_requests.get_team_ranking_list(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_PLAYER_RANKING_LIST:
        world_requests.get_player_ranking_list(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_DEFEAT_RANKING_LIST:
        world_requests.get_defeat_ranking_list(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_MY_PLAYER_RANKING_INFO:
        world_requests.get_my_player_ranking(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_MY_DEFEAT_RANKING_INFO:
        world_requests.get_my_defeat_ranking(
            session,
            message,
            teams=runtime.teams,
            telemetry=runtime.telemetry,
            reply=reply,
        )
    elif message.message_type == GET_MY_TEAM_RANKING_INFO:
        world_requests.get_my_team_ranking(
            session,
            message,
            telemetry=runtime.telemetry,
            teams=runtime.teams,
            reply=reply,
        )
    elif message.message_type not in (0, 0x0320):
        # Keep unimplemented requests observable rather than treating this
        # empty reply as successful domain handling. 0/0x0320 stay silent.
        runtime._send_game_reply(session, packet, message, b"")
        runtime.telemetry.emit(
            "game_request_stubbed",
            peer=session.peer,
            online_id=session.online_id,
            message_type=message.message_type,
        )
