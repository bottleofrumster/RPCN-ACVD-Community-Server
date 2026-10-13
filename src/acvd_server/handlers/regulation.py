"""Use the retail regulation-check retry path; never edit a player's save."""

import hmac
from typing import Callable

from ..protocol import ProtocolError, decode_proto
from ..regulation import RegulationData
from ..telemetry import Telemetry
from ..udp import GameMessage, GameSession

# Native check controller 1f48fc maps detail 51/52 to corrupted/updated
# regulation dialogs. Their OK action retrieves contents_101.bin again.
REGULATION_CORRUPTED = 51
REGULATION_UPDATED = 52
DATA_UNAVAILABLE = 28


def check_regulation(
    session: GameSession,
    message: GameMessage,
    *,
    get_regulation: Callable[[], RegulationData] | None,
    telemetry: Telemetry,
    reply: Callable[[bytes], None],
    reply_error: Callable[[int, int], None],
) -> None:
    """Only acknowledge a matching version and SHA1 of the authenticated BND.

    SHA1 is the retail compatibility checksum, not proof of client integrity.
    Missing server data is a service failure, not evidence of a corrupt client.
    """
    try:
        if get_regulation is None:
            raise ValueError("Regulation source is not configured")
        current = get_regulation()
    except (OSError, ValueError) as error:
        reply_error(1, DATA_UNAVAILABLE)
        telemetry.emit("game_regulation_data_unavailable", reason=str(error))
        return

    version = None
    detail = REGULATION_CORRUPTED
    outcome = "invalid_request"
    try:
        fields = decode_proto(message.payload)
        digests = [field for field in fields if field.number == 1]
        versions = [field for field in fields if field.number == 2]
        if len(digests) != 1 or len(versions) != 1:
            raise ProtocolError("Regulation check needs one digest and one version")
        digest, version_field = digests[0], versions[0]
        if digest.wire_type != 2 or len(digest.value) != 20:
            raise ProtocolError("Regulation checksum must be 20 bytes")
        if version_field.wire_type != 0 or not 0 <= version_field.value <= 0xFFFFFFFF:
            raise ProtocolError("Regulation version must be uint32")
        version = version_field.value
        if version != current.version:
            detail, outcome = REGULATION_UPDATED, "version_mismatch"
        elif not any(hmac.compare_digest(digest.value, value) for value in current.digests):
            outcome = "digest_mismatch"
        else:
            detail, outcome = 0, "matched"
    except ProtocolError:
        pass

    if detail:
        reply_error(1, detail)
    else:
        reply(b"")
    telemetry.emit(
        "game_regulation_data_checked",
        online_id=session.online_id,
        outcome=outcome,
        client_version=version,
        server_version=current.version,
        regulation_sha256=current.sha256,
        detail=detail,
    )
