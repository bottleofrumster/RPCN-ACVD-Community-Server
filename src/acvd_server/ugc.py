"""Emblem codecs and explicitly scoped user-generated-content validation.

Unknown imported IDs are not banned IDs: rejecting them strips legacy decals.
Team selection must honor the requested team, never an arbitrary stored emblem.
"""

from __future__ import annotations

import base64
import zlib

from .protocol import decode_proto, first_field, proto_bytes, proto_varint


def _ugc_id_validation_response(
    requested_ids: list[int], *, banned_ids: set[int] | frozenset[int] = frozenset()
) -> bytes:
    """Validate imported UGC unless it is explicitly banned.

    Official-server and locally saved decal IDs are not necessarily present in
    this replacement server's UGC store. Treating every unknown ID as banned
    makes ACVD irreversibly strip those decals during save migration.
    """
    return b"".join(
        proto_bytes(
            1,
            proto_varint(1, ugc_id)
            + proto_varint(2, 1 if ugc_id > 0 and ugc_id not in banned_ids else 0),
        )
        for ugc_id in requested_ids
    )


def _requested_team_for_emblem(team_store, online_id: str, payload: bytes) -> dict | None:
    """Resolve the team named by GetTeamEmblem, falling back only when absent."""
    fields = decode_proto(payload)
    team_field = first_field(fields, 1)
    if team_field is not None and isinstance(team_field.value, int) and int(team_field.value) > 0:
        return team_store.team_by_id(int(team_field.value))
    return team_store.team_for(online_id)


def _emblem_response_payload(stored_payload: bytes) -> bytes:
    """Return an emblem in the retail application-encoded form."""
    default_payload = base64.b64decode("AVjDYmAAA1VGCM0ApFUZRsEoGAWjYBSMghEGAAAAAP//AwAcFABN")
    if stored_payload[:1] == b"\x01":
        try:
            if len(zlib.decompress(stored_payload[1:])) == 1808:
                return stored_payload
        except zlib.error:
            pass
        return default_payload
    if stored_payload[:1] == b"\x00" and len(stored_payload) == 1809:
        return stored_payload
    if len(stored_payload) == 1808:
        return b"\x01" + zlib.compress(stored_payload)
    return default_payload


def _register_emblem_response(ugc_id: int) -> bytes:
    """Return the assigned server UGC ID in the response's sole field.

    The similarly named request has three fields (metadata, JPEG, and the
    caller's prior/local UGC ID).  Retail's response object is a different
    one-field message: its completion callback copies the uint64 stored at
    object offset 0x20.  Echoing the request layout left that value at zero,
    so the client uploaded AC data and then raised 3040020 before it could
    send RequestSetPlayerEmblemByUGCID.
    """
    return proto_varint(1, max(0, int(ugc_id)))
