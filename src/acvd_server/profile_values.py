"""Validated stored profile values shared by self and public-peer readback."""

from __future__ import annotations

import base64


def personal_selector(profile: dict, number: int) -> int:
    """Read a player's chosen selector, never their team's or network location."""
    properties = profile.get("user_property_scalars", {})
    value = properties.get(str(number), 0) if isinstance(properties, dict) else 0
    return value if type(value) is int and 0 <= value <= 0xFFFFFFFF else 0


def pilot_name_bytes(profile: dict, online_id: str) -> bytes:
    """Return this profile's uploaded UTF-8 name, preserving an explicit blank."""
    properties = profile.get("user_property_bytes_b64")
    encoded = properties.get("2") if isinstance(properties, dict) else None
    if isinstance(encoded, str):
        try:
            name = base64.b64decode(encoded, validate=True)
            name.decode("utf-8")
            return name
        except ValueError:
            # Missing/corrupt history is not authority to invent a recovered name.
            pass
    return online_id.encode("utf-8")
