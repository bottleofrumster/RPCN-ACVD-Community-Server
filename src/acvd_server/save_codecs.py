"""Decode, compare and summarize opaque player snapshots without mutation.

Compare decompressed meaning, not zlib byte choices. Network projections omit
local savedata regions; equality here does not prove whole-save preservation.
Preseed promotion policy belongs to the request lifecycle, not these codecs.
"""

from __future__ import annotations

import base64
import hashlib
import zlib

from .protocol import ProtocolError, decode_proto, first_field, proto_varint


def _own_unac_list_response(slots: list[int]) -> bytes:
    """RequestGetOwnUnacListResponse is repeated uint32 field 1."""
    return b"".join(proto_varint(1, max(0, int(slot))) for slot in sorted(set(slots)))


def _ac_data_response_payload(stored_payload: bytes) -> bytes:
    """Return the retail application-encoded AC snapshot."""
    if not stored_payload:
        # Retail rejects a marker-only response during a new player's Step 2
        # bootstrap (3050010).  This is a neutral, valid 26,120-byte AC
        # assembly snapshot extracted from the local BLUS31194 save format;
        # the surrounding player name/design metadata is deliberately not
        # included.  The client can subsequently replace it through
        # RequestRegisterACData.
        default_stored = base64.b64decode(
            "AXja7dO9DcIwEAZQpwGUlp6KHWjTIiTEJM4KtEipmCsda7CDcfgJEqKIRBrgPdu6u0/X"
            "OtThVKxCfT+derKZzrumSI9ubIfz0M22bVNZlmm3rVIerzU36V0eRsxjjH3eNI1c/hX5gC"
            "+1GLgHAAAAAAAAAAAAAAAAAAAAAAAAAAAAwA8qXioA/JV9PiHMbkOV3/q4zPe58Nl8AWhDf24="
        )
        return b"\x00" + zlib.decompress(default_stored[1:])
    if stored_payload[:1] == b"\x01":
        return b"\x00" + zlib.decompress(stored_payload[1:])
    return stored_payload


def _decoded_ai_info(payload: bytes) -> tuple[int, bytes, int, bytes]:
    """Return the semantic fields of one stored UploadAiInfo payload."""
    fields = decode_proto(payload)
    slot = first_field(fields, 1)
    name = first_field(fields, 2)
    value = first_field(fields, 3)
    application = first_field(fields, 4)
    if (
        slot is None
        or not isinstance(slot.value, int)
        or name is None
        or not isinstance(name.value, bytes)
        or value is None
        or not isinstance(value.value, int)
        or application is None
        or not isinstance(application.value, bytes)
        or not application.value
    ):
        raise ProtocolError("AI info is missing one or more required fields")
    if application.value[:1] == b"\x01":
        raw = zlib.decompress(application.value[1:])
    elif application.value[:1] == b"\x00":
        raw = application.value[1:]
    else:
        raise ProtocolError("AI info has an unknown application encoding")
    return int(slot.value), bytes(name.value), int(value.value), raw


def _ai_info_equivalent(first: bytes, second: bytes) -> bool:
    """Compare AI payload meaning rather than zlib encoder byte choices."""
    try:
        return _decoded_ai_info(first) == _decoded_ai_info(second)
    except (ProtocolError, zlib.error):
        return False


def _decoded_application_blob(payload: bytes) -> bytes:
    """Decode ACVD's marker-prefixed raw/zlib application representation."""
    if not payload:
        raise ProtocolError("application blob is empty")
    if payload[:1] == b"\x01":
        return zlib.decompress(payload[1:])
    if payload[:1] == b"\x00":
        return payload[1:]
    raise ProtocolError("application blob has an unknown encoding")


def _mission_savedata_equivalent(first: bytes, second: bytes) -> bool:
    """Compare mission/rank snapshot meaning across zlib encoder choices."""
    try:
        return _decoded_application_blob(first) == _decoded_application_blob(second)
    except (ProtocolError, zlib.error):
        return False


def _mission_snapshot_summary(payload: bytes, previous: bytes | None = None) -> dict:
    """Describe the fixed 32-byte flags + 64x18-byte mission snapshot safely."""
    summary = {
        "stored_length": len(payload),
        "stored_sha256": hashlib.sha256(payload).hexdigest(),
        "encoding_valid": False,
    }
    try:
        raw = _decoded_application_blob(payload)
    except (ProtocolError, zlib.error) as exc:
        summary["decode_error"] = str(exc)
        return summary
    summary.update(
        {
            "encoding_valid": True,
            "raw_length": len(raw),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
        }
    )
    if len(raw) != 1184:
        summary["layout_valid"] = False
        return summary
    flags = raw[:32]
    result_slots = [raw[32 + index * 6 : 32 + (index + 1) * 6] for index in range(192)]
    records = [raw[32 + index * 18 : 32 + (index + 1) * 18] for index in range(64)]
    grade_names = {2: "D", 3: "C", 4: "B", 5: "A", 6: "S"}
    summary.update(
        {
            "layout_valid": True,
            "nonzero_flag_indices": [index for index, value in enumerate(flags) if value],
            "nonzero_result_slot_indices": [
                index for index, slot in enumerate(result_slots) if any(slot)
            ],
            "nonzero_record_indices": [
                index for index, record in enumerate(records) if any(record)
            ],
            "grade_counts": {
                name: sum(slot[2] == code for slot in result_slots)
                for code, name in grade_names.items()
            },
        }
    )
    if previous is None:
        return summary
    try:
        old_raw = _decoded_application_blob(previous)
    except (ProtocolError, zlib.error) as exc:
        summary["previous_decode_error"] = str(exc)
        return summary
    summary["previous_raw_length"] = len(old_raw)
    summary["previous_raw_sha256"] = hashlib.sha256(old_raw).hexdigest()
    if len(old_raw) != 1184:
        return summary
    summary["changed_byte_count"] = sum(before != after for before, after in zip(old_raw, raw))
    summary["changed_flag_indices"] = [index for index in range(32) if old_raw[index] != raw[index]]
    summary["changed_result_slot_indices"] = [
        index
        for index in range(192)
        if old_raw[32 + index * 6 : 32 + (index + 1) * 6]
        != raw[32 + index * 6 : 32 + (index + 1) * 6]
    ]
    summary["changed_record_indices"] = [
        index
        for index in range(64)
        if old_raw[32 + index * 18 : 32 + (index + 1) * 18]
        != raw[32 + index * 18 : 32 + (index + 1) * 18]
    ]
    return summary
