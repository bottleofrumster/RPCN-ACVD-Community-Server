"""Captured medal-title selection must survive closing/reopening Team Data."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from acvd_server.handlers.team_updates import update_team_properties
from acvd_server.protocol import decode_proto, first_field, proto_varint
from acvd_server.team_messages import _team_info, _team_info_list_response
from acvd_server.teams import TeamStore
from acvd_server.udp import GameMessage, GameSession


def make_store(tmp_path, *, earned=True):
    store = TeamStore(tmp_path / "teams")
    store.create(
        online_id="Leader", player_id=11, name="Test", operation_base=2,
        language=2, play_type=4, enlistment_method=2,
    )
    if earned:
        # Retention cases model titles whose medals have already been earned.
        store.team_for("Leader")["team_medal_ids"] = list(range(1, 33))
        store._save()
    return store


def read_title(team):
    return first_field(decode_proto(_team_info(team, datetime.now(timezone.utc))), 31).value


@pytest.mark.parametrize("title", [0, 1, 2, 16, 32])
def test_native_title_update_reopens_without_changing_play_style(tmp_path, title):
    store = make_store(tmp_path)
    actor = GameSession(b"title001", bytes(16), "Leader", 11)
    payload = proto_varint(1, 3) + proto_varint(13, title)
    if title == 1:
        # Exact payload from Dupliaxe at2026-10-04T04:51:19.866724Z.
        assert payload.hex() == "08036801"
    before = deepcopy(store.team_for("Leader"))
    replies = []
    for _ in range(2):
        update_team_properties(
            actor, GameMessage(0x039E, bytes(4), payload), team_store=store,
            telemetry=SimpleNamespace(emit=lambda *a, **k: None), reply=replies.append,
        )
    store.update_team_properties("Leader", scalar_fields={26: 2, 8: 3, 20: 7})
    team = TeamStore(store.root).team_for("Leader")
    assert replies == [b"", b""]
    assert team["title_id"] == read_title(team) == title
    assert team["play_type"] == before["play_type"] == 4
    assert team["desired_faction_id"] == 2 and team["language"] == 3
    assert team["team_medal_ids"] == before["team_medal_ids"]
    assert team["team_fame_points"] == before["team_fame_points"]


@pytest.mark.parametrize("title", [0, 1, 32])
def test_prior_selection_recovers_read_only_in_both_team_views(tmp_path, title):
    store = make_store(tmp_path)
    team = store.team_for("Leader")
    # Old code retained the native selection but assigned it to play_type.
    team["title_id"] = 0 if title else 2
    team["changeable_properties"] = {"13": title, "20": 7}
    store._save()
    disk_before = store.state_path.read_bytes()
    before = deepcopy(team)
    assert read_title(team) == title
    outer = decode_proto(_team_info_list_response([team], datetime.now(timezone.utc)))
    assert first_field(decode_proto(outer[0].value), 31).value == title
    assert team == before
    assert store.state_path.read_bytes() == disk_before


def test_missing_legacy_title_never_migrates_from_field20(tmp_path):
    store = make_store(tmp_path)
    team = store.team_for("Leader")
    team.pop("title_id")
    team["changeable_properties"] = {"20": 7}
    store._save()
    reloaded = TeamStore(store.root).team_for("Leader")
    assert "title_id" not in reloaded
    assert read_title(reloaded) == 0


def test_legacy_title_readback_preserves_missing_canonical_field_and_other_settings(tmp_path):
    store = make_store(tmp_path)
    team = store.team_for("Leader")
    team.pop("title_id")
    team["changeable_properties"] = {"13": 1, "20": 7}
    store._save()
    reloaded = TeamStore(store.root).team_for("Leader")
    assert "title_id" not in reloaded
    assert read_title(reloaded) == 1 and reloaded["play_type"] == 4


@pytest.mark.parametrize("bad", [True, -1, 33, "1", None])
def test_unqualified_retained_value_does_not_override_canonical_title(tmp_path, bad):
    team = make_store(tmp_path).team_for("Leader")
    team["title_id"] = 2
    team["changeable_properties"] = {"13": bad}
    assert read_title(team) == 2


@pytest.mark.parametrize("title", [2, 11, 32])
def test_unearned_title_is_rejected_atomically_and_hidden_in_legacy_readback(tmp_path, title):
    store = make_store(tmp_path, earned=False)
    before = store.state_path.read_bytes()
    state_before = deepcopy(store.state)
    with pytest.raises(ValueError, match="has not been earned"):
        store.update_team_properties("Leader", scalar_fields={13: title, 8: 3})
    assert store.state == state_before and store.state_path.read_bytes() == before
    team = store.team_for("Leader")
    team["title_id"] = title
    team["changeable_properties"] = {"13": title}
    assert read_title(team) == 0
    assert team["title_id"] == title  # Readback does not rewrite stored history.


def test_unearned_title_request_returns_error_instead_of_success(tmp_path):
    store = make_store(tmp_path, earned=False)
    replies, errors = [], []
    update_team_properties(
        GameSession(b"title001", bytes(16), "Leader", 11),
        GameMessage(0x039E, bytes(4), proto_varint(13, 32)), team_store=store,
        telemetry=SimpleNamespace(emit=lambda *a, **k: None), reply=replies.append,
        reply_error=lambda *error: errors.append(error),
    )
    assert not replies and errors == [(1, 28)]
