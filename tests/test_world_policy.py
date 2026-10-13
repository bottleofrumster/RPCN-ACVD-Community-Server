"""Operator schedules use one UTC policy and never change player state."""

import json
from datetime import datetime, timezone

import pytest

from acvd_server.administration import OperatorSettings
from acvd_server.protocol import decode_proto, first_field
from acvd_server.world_messages import _world_current_info, _world_log
from acvd_server.world_policy import world_state


def test_old_settings_default_to_the_qualified_active_phase(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"maintenance": True, "operator_notice": "Existing"}))
    settings = OperatorSettings(path)
    assert settings.values["world_mode"] == "active"
    assert world_state(settings.values, 100)["phase"] == 2
    assert not settings.accepts_login


@pytest.mark.parametrize("mode,phase", [("active", 2), ("grace", 0)])
def test_manual_modes_override_a_retained_schedule(tmp_path, mode, phase):
    settings = OperatorSettings(tmp_path / "settings.json")
    settings.update({"world_mode": mode, "grace_start_utc": 100, "grace_end_utc": 200})
    before = settings.path.read_bytes()
    for now in (99, 100, 199, 200, 1000):
        state = world_state(settings.values, now)
        assert state["phase"] == phase and state["next_change_utc"] is None
    assert settings.path.read_bytes() == before
    assert settings.accepts_login


@pytest.mark.parametrize(
    "now,phase,next_time,status",
    [
        (99, 2, 100, "pending"),
        (100, 0, 200, "running"),
        (199.9, 0, 200, "running"),
        (200, 2, None, "finished"),
        (250, 2, None, "finished"),
    ],
)
def test_schedule_boundaries_and_restart(tmp_path, now, phase, next_time, status):
    path = tmp_path / "settings.json"
    settings = OperatorSettings(path)
    settings.update({"world_mode": "scheduled", "grace_start_utc": 100, "grace_end_utc": 200})
    restarted = OperatorSettings(path)
    state = world_state(restarted.values, now)
    assert (state["phase"], state["next_change_utc"], state["schedule_state"]) == (
        phase,
        next_time,
        status,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"world_mode": "unknown"},
        {"world_mode": None},
        {"world_mode": []},
        {"world_mode": "scheduled"},
        {"grace_start_utc": 100},
        {"grace_start_utc": 200, "grace_end_utc": 100},
        {"grace_start_utc": 100, "grace_end_utc": 100},
        {"grace_start_utc": True, "grace_end_utc": 200},
        {"grace_start_utc": "100", "grace_end_utc": 200},
        {"grace_start_utc": -1, "grace_end_utc": 200},
        {"grace_start_utc": 0, "grace_end_utc": 253402300800},
    ],
)
def test_bad_schedule_cannot_replace_saved_settings(tmp_path, changes):
    settings = OperatorSettings(tmp_path / "settings.json")
    settings.update({"operator_notice": "Keep"})
    before, values = settings.path.read_bytes(), dict(settings.values)
    with pytest.raises(ValueError):
        settings.update(changes)
    assert settings.path.read_bytes() == before and settings.values == values


def test_grace_does_not_enable_or_disable_login(tmp_path):
    settings = OperatorSettings(tmp_path / "settings.json")
    settings.update({"world_mode": "grace", "login_enabled": False})
    assert world_state(settings.values)["phase"] == 0 and not settings.accepts_login


def test_wire_phase_changes_without_changing_season_or_looping_log():
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    for phase in (2, 0, 2):
        fields = decode_proto(_world_current_info(now, phase=phase))
        assert first_field(fields, 2).value == 3
        assert first_field(fields, 3).value == phase
        caught_up = decode_proto(_world_log(now, 2, phase=phase))
        assert not [f for f in caught_up if f.number in (4, 5)]


@pytest.mark.parametrize("phase", [True, -1, 1, 3, "0"])
def test_unqualified_phase_values_are_rejected(phase):
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        _world_current_info(now, phase=phase)
    with pytest.raises(ValueError):
        _world_log(now, phase=phase)
