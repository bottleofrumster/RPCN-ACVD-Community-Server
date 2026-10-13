"""Read bounded prefixes of rows in the supported big-endian PARAM format.

Parsing establishes structure, not provenance or player authority. Callers must
select a verified game/regulation version. No archive names are opened as paths.
"""

from __future__ import annotations

import struct

MAX_PARAMETER_BYTES = 16 * 1024 * 1024
MAX_ROWS = 4096
PARAM_HEADER_SIZE = 0x30


def bounded_data(data: bytes, minimum: int) -> None:
    if not minimum <= len(data) <= MAX_PARAMETER_BYTES:
        raise ValueError("Parameter size is outside the supported bounds")


def read_rows(data: bytes, kind: bytes, minimum_size: int) -> dict[int, bytes]:
    """Return the required row prefixes without assigning unknown fields."""
    bounded_data(data, PARAM_HEADER_SIZE)
    if type(minimum_size) is not int or not 1 <= minimum_size <= MAX_PARAMETER_BYTES:
        raise ValueError("Invalid parameter row-prefix size")
    if data[12:44].split(b"\0", 1)[0].rstrip(b" ") != kind or data[44:48] != b"\xff\x02\x00\xff":
        raise ValueError("Wrong parameter type or unsupported byte order")
    count = int.from_bytes(data[10:12], "big")
    end = PARAM_HEADER_SIZE + count * 12
    if count > MAX_ROWS or end > len(data):
        raise ValueError("Parameter index exceeds its input")
    strings = int.from_bytes(data[:4], "big")
    if strings and not end <= strings <= len(data):
        raise ValueError("Invalid parameter string-table offset")
    limit = strings or len(data)
    result = {}
    for index in range(count):
        key, offset, _name = struct.unpack_from(">III", data, PARAM_HEADER_SIZE + index * 12)
        if key in result or not end <= offset <= limit - minimum_size:
            raise ValueError("Duplicate parameter key or invalid row offset")
        result[key] = data[offset : offset + minimum_size]
    return result
