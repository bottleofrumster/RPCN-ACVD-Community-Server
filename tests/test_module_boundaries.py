"""Keep production responsibilities independent while preserving old entry points."""

import ast
import importlib
from pathlib import Path

import pytest

from acvd_server import server

FORBIDDEN_PRODUCTION_ROOTS = {"tools", "tests", "client", "desktop", "acvd_console", "evidence"}

EXTRACTED = {
    "inventory": (
        "_offline_inventory_summary",
        "_offline_inventory_records",
        "_merge_offline_inventory",
        "_item_simple_info_list",
        "_sync_offline_data_response",
    ),
    "save_codecs": (
        "_decoded_ai_info",
        "_ai_info_equivalent",
        "_decoded_application_blob",
        "_mission_savedata_equivalent",
        "_mission_snapshot_summary",
        "_own_unac_list_response",
        "_ac_data_response_payload",
    ),
    "screenshots": (
        "_required_screenshot_registration",
        "_proto_fixed64",
        "_world_top_screenshot_id",
        "_world_top_thumbnail_info",
        "_world_top_screenshot",
    ),
    "session_state": ("_persist_game_sessions", "_load_game_sessions", "_load_team_sessions"),
    "reassembly": ("assemble_request",),
    "reply_recovery": ("FragmentReplyRecovery",),
    "matchmaking": ("MatchmakingCoordinator", "_rpcn_create_room_tuple"),
    "session_lifecycle": ("LiveSessionCoordinator", "_remove_team_session_member_from_state"),
    "protocol": ("_proto_datetime", "_typed_push_payload", "_protobuf_summary"),
    "player_storage": (
        "_requested_ac_snapshot",
        "_requested_ai_snapshot",
        "_requested_mission_snapshot",
        "_replace_player_blob",
    ),
    "ugc": (
        "_ugc_id_validation_response",
        "_requested_team_for_emblem",
        "_emblem_response_payload",
        "_register_emblem_response",
    ),
    "connections": (
        "Frpg2Connection",
        "_game_server_info",
        "_advertised_host_for_route",
        "_local_host_for_peer",
        "_advertised_host_for_connection",
    ),
    "world_messages": (
        "_world_basic_info",
        "_world_current_info",
        "_world_log",
        "_faction_selection",
        "_canonical_faction_selection_id",
        "_faction_struggle_results",
        "_territory_cannon_layout",
        "_territory_layout_update_response",
        "_territory_info",
        "_empty_ranking_list_response",
        "_ranking_basic_info",
        "_my_player_ranking_response",
    ),
    "team_messages": (
        "_team_rank_info",
        "_team_update_date_info",
        "_team_color_argb32",
        "_team_info",
        "_user_info",
        "_team_ranking_info",
        "_team_id_search_response",
        "_team_info_list_response",
        "_team_session_response",
        "_team_session_member_push",
        "_team_session_leave_push",
        "_team_session_join_response",
        "_team_session_member_push_pair",
        "_team_record",
        "_vote_kick_member_response",
        "_team_medal_info",
    ),
    "match_messages": (
        "_register_prepare_response",
        "_matching_result_push",
        "_matching_result_for_members",
        "_match_room_info_push",
    ),
    "mercenary_messages": (
        "_mercenary_rank_response",
        "_mercenary_ranking_info",
        "_mercenary_continuation_pairs",
        "_mercenary_self_ranking_response",
        "_mercenary_ranking_list_response",
        "_mercenary_search_result",
        "_mercenary_search_response",
        "_mercenary_message",
        "_mercenary_message_list_response",
    ),
    "rewards": ("_register_battle_result_response",),
}


@pytest.mark.parametrize("module,names", EXTRACTED.items())
def test_legacy_server_imports_reexport_one_implementation(module, names):
    extracted = importlib.import_module(f"acvd_server.{module}")
    for name in names:
        function = getattr(extracted, name)
        assert getattr(server, name) is function
        assert function.__module__ == extracted.__name__


@pytest.mark.parametrize("module", EXTRACTED)
def test_extracted_modules_do_not_depend_on_request_dispatch_or_tooling(module):
    extracted = importlib.import_module(f"acvd_server.{module}")
    tree = ast.parse(Path(extracted.__file__).read_text())
    forbidden = {"server", "tools", "tests", "client", "evidence"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports = [node.module or ""]
        else:
            continue
        assert not any(forbidden.intersection(name.split(".")) for name in imports)


def test_reliable_state_retention_default_did_not_change():
    from acvd_server.session_state import SESSION_STATE_MAX_AGE_SECONDS

    assert SESSION_STATE_MAX_AGE_SECONDS == server.SESSION_STATE_MAX_AGE_SECONDS == 600.0


def test_world_defaults_remain_shared_with_legacy_server_exports():
    from acvd_server import world_messages

    assert world_messages.WORLD_PHASE_ACTIVE == server.WORLD_PHASE_ACTIVE == 2
    assert world_messages.CURRENT_SEASON_ID == server.CURRENT_SEASON_ID == 3


def test_request_handlers_do_not_import_dispatcher_or_connection_transport():
    root = Path(server.__file__).parent / "handlers"
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(
                {"server", "connections"}.intersection(name.split(".")) for name in names
            ), path


def test_session_handler_reexports_the_canonical_member_removal_helper():
    from acvd_server.handlers import team_sessions
    from acvd_server.session_lifecycle import _remove_team_session_member_from_state

    assert (
        team_sessions._remove_team_session_member_from_state
        is _remove_team_session_member_from_state
    )


def _static_imports(source: str, module: str) -> set[str]:
    """Resolve relative aliases without importing the inspected module."""
    result = set()
    package = module.split(".")[:-1]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            if node.module:
                base += node.module.split(".")
            prefix = ".".join(base)
            if prefix:
                result.add(prefix)
            result.update(".".join(base + [alias.name]) for alias in node.names)
    return result


@pytest.mark.parametrize(
    "source,expected",
    [
        ("from . import server", "acvd_server.server"),
        ("from .. import server", "acvd_server.server"),
        ("from ..server import AcvdServer", "acvd_server.server"),
        ("from tools import analysis", "tools.analysis"),
        ("import desktop.acvd_console as panel", "desktop.acvd_console"),
        ("from client.free_battle_cache import patch_bytes", "client.free_battle_cache"),
    ],
)
def test_import_checker_resolves_aliases(source, expected):
    module = (
        "acvd_server.inventory"
        if source.startswith("from . import")
        else "acvd_server.handlers.inventory"
    )
    assert expected in _static_imports(source, module)


def test_every_production_module_preserves_product_boundaries():
    root = Path(server.__file__).parent
    for path in root.rglob("*.py"):
        module = "acvd_server." + ".".join(path.relative_to(root).with_suffix("").parts)
        imports = _static_imports(path.read_text(), module)
        assert not {name.split(".")[0] for name in imports} & FORBIDDEN_PRODUCTION_ROOTS, path
        if "handlers" in path.relative_to(root).parts:
            assert not any(
                name == forbidden or name.startswith(forbidden + ".")
                for name in imports
                for forbidden in ("acvd_server.server", "acvd_server.connections")
            ), path


def test_desktop_package_is_forbidden_even_without_desktop_prefix():
    imports = _static_imports("from acvd_console import core", "acvd_server.inventory")
    assert {name.split(".")[0] for name in imports} & FORBIDDEN_PRODUCTION_ROOTS == {"acvd_console"}
