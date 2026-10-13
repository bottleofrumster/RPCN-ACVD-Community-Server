"""Panel-only address masking; source records and protocol correlation stay intact."""

import copy
import json

import pytest
from acvd_console.privacy import hide_ip_text, hide_network_addresses
from acvd_console.traffic import Catalog, decode_record, safe_record


@pytest.mark.parametrize(
    "address",
    [
        "203.0.113.17",
        "192.168.1.20",
        "127.0.0.1",
        "2001:db8::1234",
        "::1",
        "fe80::abcd%eth0",
        "::ffff:192.0.2.7",
    ],
)
def test_literal_addresses_are_hidden_in_endpoints_and_sentences(address):
    assert address not in hide_ip_text(f"Peer [{address}]:52103")
    assert hide_ip_text(f"Connected to {address}.") == "Connected to [IP hidden]."


def test_non_addresses_and_server_port_numbers_remain():
    assert (
        hide_ip_text("v0.0.42-19984 code 0x03a6 port 52103")
        == "v0.0.42-19984 code 0x03a6 port 52103"
    )
    assert hide_ip_text("invalid 999.999.999.999") == "invalid 999.999.999.999"


def test_nested_endpoints_and_keys_are_hidden_without_mutating_source():
    source = {
        "peer": ("203.0.113.17", 52103),
        "nested": [{"remote_ip": 123456, "2001:db8::1": "host 192.168.1.20"}],
    }
    before = copy.deepcopy(source)
    result = hide_network_addresses(source)
    assert result["peer"] == "[IP hidden]"
    assert "123456" not in json.dumps(result)
    for address in ("203.0.113.17", "2001:db8::1", "192.168.1.20"):
        assert address not in json.dumps(result)
    assert source == before


@pytest.mark.parametrize(
    "encoded", ["cb007111", "203.0.113.17".encode().hex(), "203.0.113.17".encode("utf-16-be").hex()]
)
def test_known_peer_address_in_packet_hex_is_withheld(encoded):
    record = safe_record({"peer": ["203.0.113.17", 52103], "payload": {"hex": "0801" + encoded}})
    assert record["payload"]["hex"] == "[payload hidden: contains an IP address]"


def test_client_log_addresses_and_credentials_are_hidden():
    result = safe_record({"event": "client_log", "line": "peer=[2001:db8::1]:3658 password=secret"})
    assert "2001:db8::1" not in str(result) and "secret" not in str(result)


def test_masking_does_not_mix_two_players_message_correlations(tmp_path):
    catalog = Catalog(tmp_path)
    for address, code in (("203.0.113.17", 5), ("203.0.113.18", 6)):
        decode_record(
            {
                "event": "message_received",
                "service": "login",
                "peer": [address, 4000],
                "message_index": 1,
                "message_type": code,
            },
            catalog,
        )
    for address, code in (("203.0.113.17", 5), ("203.0.113.18", 6)):
        result = decode_record(
            {
                "event": "message_sent",
                "service": "login",
                "peer": [address, 4000],
                "message_index": 1,
                "message_type": 0,
            },
            catalog,
        )
        assert result["description"]["code"] == code
        assert address not in json.dumps(result)
