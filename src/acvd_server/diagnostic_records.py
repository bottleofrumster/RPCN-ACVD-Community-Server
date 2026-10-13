"""Pure diagnostic data contracts shared by the server, CLI, and native panel.

Callers own files and trust boundaries. This module never opens a path, imports
selected repository code, contacts a service, or mutates the source records.
"""

from __future__ import annotations

import ast
import re
from typing import Any

TRANSPORT_NAMES = {
    "SYN",
    "RETRANSMIT",
    "DAT",
    "HBT",
    "FIN",
    "RST",
    "ACK",
    "SYN_ACK",
    "DAT_ACK",
    "FIN_ACK",
}
VALID_CONFIDENCE = {
    "native_proven",
    "integration_proven",
    "static_recovered",
    "inferred",
    "unknown",
    "rejected",
}
# This integer slices the framing prefix; it is not an application opcode.
NON_MESSAGE_CONSTANTS = {"INITIAL_PREFIX_SIZE"}


def _hex_code(value: int) -> str:
    width = 2 if value <= 0xFF else 4
    return f"0x{value:0{width}x}"


def _summary_from_name(name: str) -> str:
    words = name.lower().split("_")
    return " ".join(words).capitalize() + "."


def parse_udp_constants(source: str) -> dict[str, int]:
    """Return integer protocol constants without importing production code."""
    module = ast.parse(source)
    result: dict[str, int] = {}
    for statement in module.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name) or not target.id.isupper():
            continue
        try:
            value = ast.literal_eval(statement.value)
        except (TypeError, ValueError):
            continue
        if type(value) is int:
            result[target.id] = value
    return result


def build_catalog(constants: dict[str, int], reviewed: dict[str, Any]) -> dict[str, Any]:
    """Build one metadata view from caller-supplied data; do not read files."""
    overrides = reviewed["overrides"]
    entries: list[dict[str, Any]] = []
    for name, value in constants.items():
        if name in NON_MESSAGE_CONSTANTS:
            continue
        layer = "reliable_udp" if name in TRANSPORT_NAMES else "application_service"
        code = _hex_code(value)
        identifier = f"{layer}:{code}"
        entry: dict[str, Any] = {
            "id": identifier,
            "layer": layer,
            "code": code,
            "value": value,
            "name": name,
            "direction": (
                "bidirectional" if layer == "reliable_udp" else "client_to_application_service"
            ),
            "summary": _summary_from_name(name),
            "confidence": "static_recovered",
            "source": "src/acvd_server/udp.py",
        }
        entry.update(overrides.get(identifier, {}))
        entries.append(entry)

    for source in reviewed["extra_entries"]:
        entry = dict(source)
        entry["value"] = int(entry["code"], 16)
        entries.append(entry)

    entries.sort(key=lambda item: (item["layer"], item["value"], item["name"]))
    result = dict(reviewed)
    result["entries"] = entries
    validate_catalog(result)
    return result


def validate_catalog(catalog: dict[str, Any]) -> None:
    if catalog.get("schema_version") != 1:
        raise ValueError("unsupported protocol catalog schema")
    identifiers: set[str] = set()
    for entry in catalog["entries"]:
        identifier = entry["id"]
        if identifier in identifiers:
            raise ValueError(f"duplicate catalog id: {identifier}")
        identifiers.add(identifier)
        if not re.fullmatch(r"0x[0-9a-f]+", entry["code"]):
            raise ValueError(f"non-canonical code: {entry['code']}")
        if entry["confidence"] not in VALID_CONFIDENCE:
            raise ValueError(f"invalid confidence {entry['confidence']!r} for {identifier}")
    rejected_ids: set[str] = set()
    for rejected in catalog["rejected_interpretations"]:
        identifier = rejected["id"]
        if identifier in rejected_ids:
            raise ValueError(f"duplicate rejected interpretation: {identifier}")
        rejected_ids.add(identifier)


# Shared disclosure policy. The two marker styles preserve existing UI wording.
SECRET_KEYS = {
    "auth_token",
    "game_key",
    "key",
    "secret",
    "password",
    "private_key",
    "ticket",
    "ticket_bytes",
    "game_info",
    "handshake_key",
    "server_secret",
    "client_secret",
}


def redact_record(record: dict[str, Any], *, desktop_markers: bool = False) -> dict[str, Any]:
    """Return a sanitized diagnostic view. Keep persistent records unchanged."""

    def clean(value: Any, key: str = "") -> Any:
        if key.lower() in SECRET_KEYS:
            return "[credential redacted]"
        if isinstance(value, dict):
            return {key: clean(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    result = clean(record)
    if str(record.get("service", "")).startswith(("auth", "login")):
        marker = (
            "[authentication material redacted]"
            if desktop_markers
            else "[authentication material redacted; encrypted frame remains visible]"
        )
        for key in ("plaintext_payload", "payload", "fields"):
            if key in result:
                result[key] = marker
    if record.get("event") == "game_datagram_received":
        result["payload"] = (
            "[token-bearing frame redacted; see decoded packet]"
            if desktop_markers
            else "[token-bearing datagram redacted; see game_packet_decoded]"
        )
    if isinstance(result.get("line"), str):
        result["line"] = re.sub(
            r"(?i)(password|auth.?token|secret|ticket|game.?key)(\s*[:=]).*",
            r"\1\2 [redacted]",
            result["line"],
        )
    return result
