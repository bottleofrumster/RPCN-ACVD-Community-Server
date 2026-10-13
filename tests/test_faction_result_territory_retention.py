"""A repeated faction-results read must not replace an existing territory."""

from copy import deepcopy

import pytest

from acvd_server.handlers import world
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession


def make_store(tmp_path, faction, area):
    store = TeamStore(tmp_path / "teams")
    team = store.create(
        online_id="Owner",
        player_id=42,
        name="Example",
        operation_base=0,
        language=1,
        play_type=1,
        enlistment_method=1,
    )
    store.set_faction("Owner", 1, faction)
    team["territory_area_id"] = area
    store._save()
    store.save_native_territory_layout(
        "Owner",
        player_id=42,
        season_id=3,
        area_id=3,
        layout_version=7,
        payload=b"opaque layout fixture",
    )
    return store, team


@pytest.mark.parametrize("faction", [1, 2, 3])
@pytest.mark.parametrize("area", range(1, 8))
def test_results_preserve_all_assigned_regions_without_writes(tmp_path, monkeypatch, faction, area):
    store, team = make_store(tmp_path, faction, area)
    before = deepcopy(store.state)
    saved = store.state_path.read_bytes()

    def unexpected_write():
        pytest.fail("An unchanged faction-results read must not write team data")

    monkeypatch.setattr(store, "_save", unexpected_write)
    assert store.promote_faction("Owner") is team
    assert store.promote_faction("Owner") is team
    assert store.state == before and store.state_path.read_bytes() == saved


@pytest.mark.parametrize("faction", [1, 2, 3])
def test_unassigned_region_keeps_legacy_initial_assignment(tmp_path, faction):
    store, team = make_store(tmp_path, faction, 0)
    store.promote_faction("Owner")
    assert team["territory_area_id"] == faction
    reloaded = TeamStore(store.root).team_for("Owner")
    assert reloaded["territory_area_id"] == faction


def test_faction_promotion_preserves_different_existing_region(tmp_path):
    store, team = make_store(tmp_path, 2, 7)
    team["current_faction_id"] = 1
    store._save()
    layouts = deepcopy(team["native_territory_layouts"])
    store.promote_faction("Owner")
    reloaded = TeamStore(store.root).team_for("Owner")
    assert reloaded["current_faction_id"] == 2
    assert reloaded["territory_area_id"] == 7
    assert reloaded["native_territory_layouts"] == layouts


def test_results_handler_retains_the_assigned_territory(tmp_path):
    store, team = make_store(tmp_path, 1, 3)
    before = deepcopy(store.state)
    events, replies = [], []

    class Telemetry:
        def emit(self, event, **fields):
            events.append((event, fields))

    world.get_faction_struggle_result(
        GameSession(b"owner001", bytes(16), "Owner", 42),
        GameMessage(0, bytes(4), b"", 1),
        teams=store,
        telemetry=Telemetry(),
        reply=replies.append,
    )
    assert replies and store.state == before
    assert events[-1][1]["territory_area_id"] == 3


@pytest.mark.parametrize("area", [None, False, "0", -1, 99])
def test_results_do_not_repair_unknown_territory_values(tmp_path, area):
    store, team = make_store(tmp_path, 1, area)
    before = deepcopy(store.state)
    store.promote_faction("Owner")
    assert store.state == before


def test_unknown_player_does_not_create_or_write_a_team(tmp_path):
    store, _ = make_store(tmp_path, 1, 3)
    before = store.state_path.read_bytes()
    assert store.promote_faction("Unknown") is None
    assert store.state_path.read_bytes() == before
