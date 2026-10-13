"""One persisted operator policy for active war and the native grace period.

The effective mode is derived from UTC time on each read. No desktop process,
background model, season reset, or second scheduler is required.
"""

from __future__ import annotations

import time

ACTIVE_PHASE = 2
GRACE_PHASE = 0
MAX_UTC_SECONDS = 253402300799
WORLD_DEFAULTS = {
    "world_mode": "active",
    "grace_start_utc": None,
    "grace_end_utc": None,
}


def validate_world_settings(values: dict) -> None:
    if values["world_mode"] not in ("active", "grace", "scheduled"):
        raise ValueError("World mode must be active, grace, or scheduled")
    start, end = values["grace_start_utc"], values["grace_end_utc"]
    if start is None and end is None:
        if values["world_mode"] == "scheduled":
            raise ValueError("Scheduled mode requires a start and end time")
        return
    if any(type(value) is not int or not 0 <= value <= MAX_UTC_SECONDS for value in (start, end)):
        raise ValueError("Schedule times must be UTC Unix seconds")
    if start >= end:
        raise ValueError("The grace-period end must follow its start")


def world_state(settings: dict, now_unix: float | None = None) -> dict:
    """Return a read-only effective state from validated operator settings."""
    now = time.time() if now_unix is None else now_unix
    mode = settings.get("world_mode", "active")
    start, end = settings.get("grace_start_utc"), settings.get("grace_end_utc")
    phase, next_change, schedule_state = ACTIVE_PHASE, None, "not_scheduled"
    if mode == "grace":
        phase = GRACE_PHASE
    elif mode == "scheduled":
        if now < start:
            next_change, schedule_state = start, "pending"
        elif now < end:
            phase, next_change, schedule_state = GRACE_PHASE, end, "running"
        else:
            schedule_state = "finished"
    return {
        "phase": phase,
        "mode": mode,
        "label": "Season Reset Grace Period" if phase == GRACE_PHASE else "Active war",
        "grace_start_utc": start,
        "grace_end_utc": end,
        "schedule_state": schedule_state,
        "next_change_utc": next_change,
        "source": "Operator world policy; season simulation and rewards remain incomplete",
    }
