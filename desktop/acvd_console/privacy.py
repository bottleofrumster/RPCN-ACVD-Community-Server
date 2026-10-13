"""Mask network addresses in panel records without changing source data."""

from __future__ import annotations

import ipaddress
import re

_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)")
_IPV6 = re.compile(r"(?<![\w:])(?:[a-fA-F0-9]{0,4}:){2,}[a-fA-F0-9:.]*(?:%[\w.-]+)?(?![\w:])")
_ADDRESS_FIELDS = {
    "peer",
    "client_ip",
    "remote_ip",
    "source_ip",
    "destination_ip",
    "src_ip",
    "dst_ip",
}
_HEX_FIELDS = {"hex", "hex_prefix", "payload_hex", "data_hex", "packet_hex"}


def _addresses(text):
    for expression in (_IPV6, _IPV4):
        for match in expression.finditer(text):
            candidate = match.group().rstrip(".")
            try:
                yield candidate, ipaddress.ip_address(candidate)
            except ValueError:
                continue


def hide_ip_text(text: str) -> str:
    """Mask valid IPv4/IPv6 literals; preserve ports and other text."""
    for candidate, _ in sorted(_addresses(text), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(candidate, "[IP hidden]")
    return text


def hide_network_addresses(record):
    """Mask structured endpoints and literal addresses, including copied payloads.

    Known addresses in raw hex cause that payload field to be withheld, not
    rewritten into fake protocol bytes. Unknown encodings are not anonymous;
    diagnostic exports must still be kept private.
    """
    known = set()

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                collect(str(key))
                collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)
        elif isinstance(value, str):
            known.update(_addresses(value))

    collect(record)
    encodings = set()
    for literal, address in known:
        encodings.add(address.packed.hex())
        for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
            encodings.add(literal.encode(encoding).hex())

    def clean(value, key=""):
        if key.lower() in _ADDRESS_FIELDS:
            return "[IP hidden]"
        if isinstance(value, dict):
            return {hide_ip_text(str(k)): clean(v, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        if isinstance(value, str):
            if key.lower() in _HEX_FIELDS and any(code in value.lower() for code in encodings):
                return "[payload hidden: contains an IP address]"
            return hide_ip_text(value)
        return value

    return clean(record)
