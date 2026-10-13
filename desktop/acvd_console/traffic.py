"""Evidence-ranked protocol labels, raw events and schema-neutral field inspection."""

from __future__ import annotations

import json
import re
import time

from acvd_server.diagnostic_records import (
    SECRET_KEYS,
    build_catalog,
    parse_udp_constants,
    redact_record,
)

from .core import RESOURCES
from .privacy import hide_network_addresses

# Preserve the established public name without a second policy set.
SECRETS = SECRET_KEYS


def safe_record(record):
    """Use the shared disclosure policy with the panel's existing marker text."""
    return hide_network_addresses(redact_record(record, desktop_markers=True))


class Catalog:
    def __init__(self, root):
        constants, reviewed = root / "src/acvd_server/udp.py", root / "tools/protocol_catalog.json"
        if constants.exists() and reviewed.exists():
            # Treat selected repository files as data, never as Python modules.
            data = build_catalog(
                parse_udp_constants(constants.read_text()), json.loads(reviewed.read_text())
            )
        else:
            path = RESOURCES / "protocol-catalog.json"
            data = json.loads(path.read_text()) if path.exists() else {"entries": []}
        # A complete source catalog replaces the bundle. Do not mix stale facts.
        self.entries = {
            (entry["layer"], int(entry["code"], 16)): entry for entry in data["entries"]
        }
        self.correlations = {}

    def describe(self, record):
        event, layer, code = str(record.get("event", "unknown")), "event", None
        if event == "pcap_udp":
            return {
                "layer": "pcap_udp",
                "code": None,
                "name": "NXRV / UNKNOWN MESSAGE"
                if record.get("nxrv_signature")
                else "UNKNOWN UDP PAYLOAD",
                "summary": record.get("meaning", "Undecoded UDP payload"),
                "confidence": "unknown",
            }
        if event in ("message_received", "message_sent"):
            layer = "authentication" if record.get("service") == "auth" else "login_discovery"
            code = record.get("message_type")
            key = (str(record.get("peer")), record.get("message_index"), layer)
            if event == "message_received":
                self.correlations[key] = code
                if len(self.correlations) > 4096:
                    self.correlations.pop(next(iter(self.correlations)))
            elif code == 0:
                code = self.correlations.get(key)
            names = {
                5: "Authentication endpoint discovery",
                6: "Cryptographic handshake",
                2: "Service availability / application version",
                1: "Session-key exchange",
                3: "RPCN ticket authentication / game endpoint",
            }
            return {
                "layer": layer,
                "code": code,
                "name": names.get(code, "UNKNOWN"),
                "summary": names.get(code, "No correlated known handshake")
                + ". Credentials are redacted.",
                "confidence": "static_recovered" if code in names else "unknown",
            }
        if "message_type" in record:
            layer, code = "application_service", record["message_type"]
        elif "opcode" in record:
            layer, code = "reliable_udp", record["opcode"]
        elif "push_id" in record:
            layer, code = "application_push", record["push_id"]
        elif event == "client_log":
            line = str(record.get("line", ""))
            for (candidate_layer, number), entry in self.entries.items():
                if candidate_layer in ("rpcn_room", "rpcn_signaling", "nxr_p2p", "client_local"):
                    if candidate_layer == "client_local" and number < 0x100000:
                        continue
                    if re.search(r"0x0*" + f"{number:x}" + r"\b", line, re.I):
                        return {**entry, "code": number}
            for function, meaning in {
                "sceNpMatching2JoinRoom": "The client asks RPCN to join a room. This message does not prove that a battle started.",
                "sceNpMatching2CreateJoinRoom": "The client asks RPCN to create and join a room.",
                "sceNpMatching2LeaveRoom": "The client leaves the room. Earlier records can show the cause.",
                "sceNpSignalingActivateConnection": "The client requests a peer connection. This request does not prove that the connection is complete.",
                "DnsHook: DNS query": "The client requests a host address. This request does not prove that an HTTP reply arrived.",
            }.items():
                if function in line:
                    return {
                        "layer": "client_runtime",
                        "code": None,
                        "name": function,
                        "summary": meaning,
                        "confidence": "observed_api",
                    }
        if code is not None:
            try:
                entry = self.entries.get((layer, int(code)))
            except (TypeError, ValueError):
                entry = None
            if entry:
                return {**entry, "code": code}
            return {
                "layer": layer,
                "code": code,
                "name": "UNKNOWN",
                "confidence": "unknown",
                "summary": "The catalog has no meaning for this code. The source record is unchanged.",
            }
        return {
            "layer": layer,
            "code": None,
            "name": event,
            "confidence": "event_only",
            "summary": "This is a runtime log entry. The code meaning is not known.",
        }


def decode_record(record, catalog, source="Server"):
    record = redact_record(record, desktop_markers=True)
    # Correlate on the real endpoint before masking. Otherwise two players
    # with the same sequence number could acquire each other's message labels.
    description = catalog.describe(record)
    event = str(record.get("event", ""))
    return {
        "source": source,
        "record": hide_network_addresses(record),
        "description": hide_network_addresses(description),
        "received_at": time.strftime("%H:%M:%S %Z"),
        "direction": "← Client"
        if "received" in event
        else "→ Client"
        if "sent" in event or event == "game_reply_observed"
        else "• Observe",
    }


def protobuf_fields(data, limit=96):
    position, fields = 0, []

    def varint():
        nonlocal position
        value = 0
        for shift in range(0, 70, 7):
            if position >= len(data):
                raise ValueError("Incomplete varint / capture prefix")
            byte = data[position]
            position += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("Oversized varint")

    while position < len(data) and len(fields) < limit:
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("Not a protobuf field stream")
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            length = varint() if wire == 2 else (8 if wire == 1 else 4)
            if length > len(data) - position:
                raise ValueError("Incomplete field / truncated capture")
            part = data[position : position + length]
            position += length
            value = (
                {"bytes": length, "hex_preview": part[:128].hex()}
                if wire == 2
                else int.from_bytes(part, "little")
            )
        else:
            raise ValueError(f"Unsupported wire type {wire}")
        fields.append({"field": number, "wire_type": wire, "value": value})
    return {
        "fields": fields,
        "remaining_bytes": len(data) - position,
        "note": "These are field numbers and values. Their meanings require protocol evidence.",
    }
