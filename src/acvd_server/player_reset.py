"""Safe rebuild of client-republishable player state."""

from __future__ import annotations

import shutil
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from .teams import TeamStore

_IDENTITY_PROFILE_KEYS = frozenset(
    ("player_id", "member_id", "member_status", "member_team_secondary")
)
_REPUBLISHABLE_FILENAMES = frozenset(("ac-data.bin", "mission-savedata.bin"))
_PERSONAL_DICTIONARY_LEDGERS = (
    "story_rewards",
    "shop_purchases",
    "key_item_purchases",
    "part_sales",
    "inventory_registrations",
)


def player_directory(teams: TeamStore, player_data_path: Path) -> list[dict[str, Any]]:
    """Return bounded non-network identities and safe operator summaries."""
    result = []
    profiles = teams.state.get("player_profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("Player directory needs review")
    for online_id, profile in profiles.items():
        if not isinstance(online_id, str) or not isinstance(profile, dict):
            continue
        player_id = profile.get("player_id")
        if type(player_id) is not int or not 0 < player_id < 1 << 32:
            continue
        team = teams.team_for(online_id) or {}
        summary = _player_data_summary(
            teams.state, player_data_path, online_id, player_id, profile
        )
        result.append(
            {
                "online_id": online_id,
                "player_id": player_id,
                "team_id": int(team.get("team_id", 0)),
                "team_name": str(team.get("name", "")),
                "has_saved_data": _has_saved_data(
                    teams.state, player_data_path, online_id, player_id, profile
                ),
                **summary,
            }
        )
    return sorted(result, key=lambda row: row["online_id"].casefold())


def _player_data_summary(
    state: dict[str, Any],
    player_data_path: Path,
    online_id: str,
    player_id: int,
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Describe stored domains without returning private blobs or filesystem paths."""
    prefix = f"{player_id}."
    paths = [
        path
        for path in _player_paths(player_data_path, player_id)
        if path.is_file() and not path.is_symlink()
    ]
    names = [path.name.removeprefix(prefix) for path in paths]
    republishable = _client_republishable_paths(player_data_path, player_id)
    unac_count = sum(
        name.startswith("ai-")
        and name.endswith(".bin")
        and name.removeprefix("ai-").removesuffix(".bin").isdigit()
        for name in names
    )

    def matching_count(key: str) -> int:
        rows = state.get(key, {})
        values = rows.values() if isinstance(rows, dict) else ()
        return sum(_row_matches_player(row, online_id, player_id) for row in values)

    settlement_count = sum(
        _history_matches_player(row, online_id, player_id)
        for key in ("battle_settlements", "world_battle_settlements")
        for row in state.get(key, [])
        if isinstance(state.get(key, []), list)
    )
    transfer_count = sum(
        _history_matches_player(row, online_id, player_id)
        for row in state.get("money_transfers", [])
        if isinstance(state.get("money_transfers", []), list)
    )
    purchase_count = sum(
        matching_count(key)
        for key in (
            "story_rewards",
            "shop_purchases",
            "key_item_purchases",
            "part_sales",
            "inventory_registrations",
        )
    )
    wallet = profile.get("balance")
    wallet_balance = wallet if type(wallet) is int else None
    ac_data = "ac-data.bin" in names or any(name.endswith("ac-data.bin") for name in names)
    story_data = "mission-savedata.bin" in names
    inventory_data = "offline-data.bin" in names
    notice_data = "personal-notices.json" in names
    emblem_data = online_id in state.get("player_emblems", {})
    pending_emblem = online_id in state.get("pending_emblems", {})
    mercenary_registered = online_id in state.get("mercenaries", {})
    categories = []
    for enabled, name in (
        (bool(republishable), "Rebuildable save"),
        (ac_data, "AC data"),
        (story_data, "Story progress"),
        (unac_count > 0, "UNAC data"),
        (inventory_data, "Inventory"),
        (emblem_data or pending_emblem, "Emblem"),
        (notice_data, "Notices"),
        (wallet_balance is not None, "Wallet"),
        (settlement_count + transfer_count + purchase_count > 0, "History"),
        (mercenary_registered, "Mercenary"),
    ):
        if enabled:
            categories.append(name)
    return {
        "has_republishable_data": bool(republishable),
        "wallet_balance": wallet_balance,
        "member_id": int(profile.get("member_id", 0)),
        "member_status": int(profile.get("member_status", 0)),
        "member_team_secondary": int(profile.get("member_team_secondary", 0)),
        "ac_data": ac_data,
        "story_data": story_data,
        "unac_count": unac_count,
        "inventory_data": inventory_data,
        "notice_data": notice_data,
        "emblem_data": emblem_data,
        "pending_emblem": pending_emblem,
        "mercenary_registered": mercenary_registered,
        "server_file_count": len(paths),
        "server_bytes": sum(path.stat().st_size for path in paths),
        "settlement_count": settlement_count,
        "purchase_count": purchase_count,
        "transfer_count": transfer_count,
        "data_categories": categories,
    }


def reset_player(
    teams: TeamStore,
    player_data_path: Path,
    *,
    online_id: str,
    player_id: int,
) -> dict[str, Any]:
    """Remove only state proven to republish on the next native login.

    Files are first renamed into a same-filesystem quarantine. A failed state
    commit restores every rename. Wallet, owned inventory, emblem, team
    membership, profile settings, and transaction/reward history are retained:
    an already-enrolled retail save does not republish those domains. AC,
    mission progress, and the nine UNAC slots do republish and are rebuilt from
    the player's unchanged local save on the next login.
    """
    if not isinstance(online_id, str) or not online_id or len(online_id) > 64:
        raise ValueError("Player reset requires one bounded online ID")
    if type(player_id) is not int or not 0 < player_id < 1 << 32:
        raise ValueError("Player reset requires one valid player ID")
    profile = teams.player_profile(online_id)
    if profile.get("player_id") != player_id:
        raise ValueError("Player reset identity changed; refresh the directory")
    if not _has_saved_data(teams.state, player_data_path, online_id, player_id, profile):
        raise ValueError("This player has no server save data to delete")

    updated = deepcopy(teams.state)
    removable = _client_republishable_paths(player_data_path, player_id)
    if not removable:
        raise ValueError("This player has no client-republishable server data to rebuild")

    quarantine = Path(tempfile.mkdtemp(prefix=".player-reset-", dir=player_data_path))
    moved: list[tuple[Path, Path]] = []
    committed = False
    try:
        for number, path in enumerate(sorted(set(removable), key=lambda item: str(item))):
            if path.is_symlink():
                raise ValueError("Refusing to reset a symlinked player path")
            target = quarantine / f"{number:04d}-{path.name}"
            path.replace(target)
            moved.append((path, target))
        teams.commit_state(updated)
        committed = True
    except BaseException:
        for original, target in reversed(moved):
            if target.exists() and not original.exists():
                target.replace(original)
        raise
    finally:
        if committed:
            shutil.rmtree(quarantine)
        elif quarantine.exists():
            quarantine.rmdir()
    return {
        "online_id": online_id,
        "player_id": player_id,
        "deleted_republishable_paths": len(moved),
        "team_membership_retained": online_id in updated.get("members", {}),
        "wallet_retained": "balance" in updated["player_profiles"][online_id],
        "inventory_retained": (player_data_path / f"{player_id}.offline-data.bin").is_file(),
        "emblem_retained": online_id in updated.get("player_emblems", {}),
        "transaction_history_retained": True,
    }


def _client_republishable_paths(player_data_path: Path, player_id: int) -> list[Path]:
    """Return only native-login mirrors proven to be re-uploaded by retail."""
    prefix = f"{player_id}."
    result = []
    for path in player_data_path.glob(prefix + "*"):
        suffix = path.name.removeprefix(prefix)
        if suffix in _REPUBLISHABLE_FILENAMES or (
            suffix.startswith("ai-")
            and suffix.endswith(".bin")
            and suffix.removeprefix("ai-").removesuffix(".bin").isdigit()
        ):
            result.append(path)
    history = player_data_path / ".history" / f"{player_id}.ac-data.bin"
    if history.exists():
        result.append(history)
    return result


def _player_paths(player_data_path: Path, player_id: int) -> list[Path]:
    prefix = f"{player_id}."
    result = [path for path in player_data_path.glob(prefix + "*") if path.exists()]
    history = player_data_path / ".history"
    if history.is_dir() and not history.is_symlink():
        result.extend(path for path in history.glob(prefix + "*") if path.exists())
    return result


def _row_matches_player(row: Any, online_id: str, player_id: int) -> bool:
    return isinstance(row, dict) and (
        row.get("online_id") == online_id or row.get("player_id") == player_id
    )


def _history_matches_player(row: Any, online_id: str, player_id: int) -> bool:
    if not isinstance(row, dict):
        return False
    if _row_matches_player(row, online_id, player_id):
        return True
    if row.get("sender_online_id") == online_id or row.get("recipient_online_id") == online_id:
        return True
    if row.get("recipient_player_id") == player_id:
        return True
    return any(
        _row_matches_player(participant, online_id, player_id)
        for participant in row.get("participants", [])
    )


def _has_saved_data(
    state: dict[str, Any],
    player_data_path: Path,
    online_id: str,
    player_id: int,
    profile: dict[str, Any],
) -> bool:
    if set(profile) - _IDENTITY_PROFILE_KEYS or _player_paths(player_data_path, player_id):
        return True
    if any(
        online_id in state.get(key, {})
        for key in ("mercenaries", "mercenary_messages", "pending_emblems", "player_emblems")
    ):
        return True
    for key in _PERSONAL_DICTIONARY_LEDGERS:
        if any(
            _row_matches_player(row, online_id, player_id)
            for row in state.get(key, {}).values()
        ):
            return True
    groups = state.get("story_group_rewards", {})
    if any(
        isinstance(row, dict)
        and any(
            _row_matches_player(participant, online_id, player_id)
            for participant in row.get("participants", [])
        )
        for row in groups.values()
    ):
        return True
    return any(
        _history_matches_player(row, online_id, player_id)
        for key in ("battle_settlements", "world_battle_settlements", "money_transfers")
        for row in state.get(key, [])
    )
