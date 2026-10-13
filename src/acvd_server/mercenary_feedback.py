"""Retail short-message IDs and strict post-contract request decoding.

The English strings are exact game quotations, not STE rewrites. The source
FMG and native Contract Report correlation are recorded in the feedback evidence.
No payment, rank, or entitlement value is derived from these IDs.
"""

from .protocol import ProtocolError, decode_proto

MAX_FEEDBACK_TARGETS = 4  # Server policy, not a recovered wire limit.
FEEDBACK_TEXT = {
    88800: "Your performance was superb!",
    88801: "Great work as usual. Thanks!",
    88802: "Let's work together again!",
    88803: "Want to join our team?",
    88804: "Message me and join our team!",
    88805: "Thank you.",
    88806: "Good job out there.",
    88807: "Thanks for risking your life in battle.",
    88808: "We would have lost without you.",
    88809: "It's my fault. Apologies.",
    88810: "Sorry for not performing up to snuff.",
    88811: "Good effort, but no cigar. Maybe next time!",
    88812: "Let's work together again!",
    88813: "What a marvelous emblem.",
    88814: "That's an impressive AC.",
}


def decode_mercenary_feedback(payload: bytes) -> list[tuple[int, int]]:
    fields = decode_proto(payload)
    if len(fields) > MAX_FEEDBACK_TARGETS:
        raise ProtocolError("Too many feedback recipients")
    result: dict[int, int] = {}
    for field in fields:
        if field.number != 1 or field.wire_type != 2:
            raise ProtocolError("Feedback requires embedded field-1 records")
        nested = decode_proto(field.value)
        if sorted((f.number, f.wire_type) for f in nested) != [(1, 0), (2, 0)]:
            raise ProtocolError("Feedback requires one target and one text ID")
        values = {f.number: int(f.value) for f in nested}
        target, code = values[1], values[2]
        if not 0 < target < 1 << 32 or code not in FEEDBACK_TEXT:
            raise ProtocolError("Feedback target or text ID is unsupported")
        if target in result and result[target] != code:
            raise ProtocolError("Feedback contains conflicting messages for one recipient")
        result[target] = code
    return list(result.items())
