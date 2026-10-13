"""Regressions for the 0x03FD required fields and native @HopeFaction source."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from acvd_server.handlers.team_updates import update_team_properties
from acvd_server.protocol import decode_proto, first_field, proto_varint
from acvd_server.team_medals import initialize, owned_medals
from acvd_server.team_messages import _team_info, _team_medal_info
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession


@pytest.mark.parametrize("ids", [[], [1], [1, 2, 26], list(range(1, 33))])
def test_medal_summary_has_all_required_scalars_and_retains_owned_ids(ids):
    team = {"team_id": 1}
    initialize(team)
    team["team_medal_ids"] = ids
    before = deepcopy(team)
    fields = decode_proto(_team_medal_info(team))
    assert all(f.wire_type == 0 for f in fields)
    assert [(f.number, f.value) for f in fields if f.number != 7] == [
        (n, 0) for n in (1, 2, 3, 4, 5, 6, 8)
    ]
    assert [f.value for f in fields if f.number == 7] == ids
    assert team == before


@pytest.mark.parametrize("choice", [0, 1, 2, 3])
def test_next_faction_survives_update_points_unrelated_edit_and_reload(tmp_path, choice):
    store = TeamStore(tmp_path / "teams")
    store.create(
        online_id="Leader", player_id=11, name="Test", operation_base=0,
        language=2, play_type=1, enlistment_method=2,
    )
    actor = GameSession(b"test0001", bytes(16), "Leader", 11)
    replies = []
    update_team_properties(
        actor, GameMessage(0x039E, bytes(4), proto_varint(26, choice)),
        team_store=store, telemetry=SimpleNamespace(emit=lambda *a, **k: None),
        reply=replies.append, phase=2,
    )
    assert replies == [b""]
    store.update_team_properties("Leader", scalar_fields={8: 3})
    reloaded = TeamStore(store.root)
    team = reloaded.team_for("Leader")
    before = deepcopy(team)
    for points in (50, 1200, 1205, 1_000_000):
        # The old field7 encoding used points as a faction FMG index. In
        # particular1200 selects a season-time template instead of a faction.
        projected = {**team, "team_fame_points": points}
        fields = decode_proto(_team_info(projected, datetime.now(timezone.utc)))
        assert first_field(fields, 7).value == choice
        assert first_field(fields, 11).value == 3
        assert first_field(fields, 6).value == team.get("current_faction_id", 0)
    assert owned_medals(team) == owned_medals(before)
    assert team == before and team["desired_faction_id"] == choice


@pytest.mark.parametrize("play_type,sortie_text", [(3, 2), (5, 1), (0, 0)])
def test_play_type_and_sortie_text_survive_sparse_edits_and_reload(tmp_path, play_type, sortie_text):
    store = TeamStore(tmp_path / "teams")
    store.create(online_id="Leader", player_id=11, name="Test", operation_base=0,
                 language=2, play_type=1, enlistment_method=2)
    # Native 2026-10-13 controls: Casual Player -> field10=3;
    # Commencing mission -> field20=2. Neither is a faction or title.
    for change in ({10: play_type}, {20: sortie_text}, {26: 3}, {8: 2}):
        store.update_team_properties("Leader", scalar_fields=change)
    reloaded = TeamStore(store.root)
    team = reloaded.team_for("Leader")
    before = reloaded.state_path.read_bytes()
    fields = decode_proto(_team_info(team, datetime.now(timezone.utc)))
    assert [first_field(fields, n).value for n in (9, 25, 7, 31)] == [play_type, sortie_text, 3, 0]
    assert reloaded.state_path.read_bytes() == before
    # Old releases retained the correct sparse values but not their canonical
    # projections. Reads must recover them without a deployment-time rewrite.
    team["play_type"] = 1
    team.pop("sortie_text_id")
    fields = decode_proto(_team_info(team, datetime.now(timezone.utc)))
    assert first_field(fields, 9).value == play_type
    assert first_field(fields, 25).value == sortie_text
