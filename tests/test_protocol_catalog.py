from __future__ import annotations

import importlib.util
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "tools/acvd_protocol_catalog.py"
SPEC = importlib.util.spec_from_file_location("acvd_protocol_catalog", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
catalog_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalog_module)


def test_catalog_contains_every_production_udp_constant() -> None:
    catalog = catalog_module.load_catalog()
    entries = {
        (entry["layer"], entry["name"]): entry["value"]
        for entry in catalog["entries"]
        if entry.get("source") == "src/acvd_server/udp.py"
    }
    expected = {}
    for name, value in catalog_module.udp_constants().items():
        if name == "INITIAL_PREFIX_SIZE":
            continue
        layer = "reliable_udp" if name in catalog_module.TRANSPORT_NAMES else "application_service"
        expected[(layer, name)] = value
    assert entries == expected


def test_catalog_excludes_framing_size_but_retains_actual_message_codes():
    entries = {entry["name"]: entry for entry in catalog_module.load_catalog()["entries"]}
    assert "INITIAL_PREFIX_SIZE" not in entries
    assert entries["SYN"]["layer"] == "reliable_udp"
    assert entries["REGISTER_PREPARE"]["value"] == 0x03BD
    assert entries["NOTIFY_MATCH_ROOM_INFO"]["value"] == 0x03BC


def test_shop_catalog_separates_native_display_from_unqualified_purchase():
    entries = {entry["id"]: entry for entry in catalog_module.load_catalog()["entries"]}
    lineup = entries["application_service:0x03df"]
    purchase = entries["application_service:0x03dd"]
    assert "normal Buy Parts" in lineup["summary"]
    assert "Display population only" in lineup["limits"]
    assert "2,3,4,5,7" in purchase["payload"]
    assert "Production purchase settlement remains incomplete" in purchase["summary"]
    assert "new part instance ID" in purchase["payload"]


def test_story_nxr_entries_preserve_direction_timing_and_causal_result() -> None:
    catalog = catalog_module.load_catalog()
    entries = {entry["id"]: entry for entry in catalog["entries"]}
    assert entries["nxr_p2p:0xf20a"]["direction"] == "mercenary_to_employer"
    assert "4.00-4.01" in entries["nxr_p2p:0xf20a"]["timing"]
    result = entries["nxr_p2p:0xf222"]
    assert result["direction"] == "employer_to_mercenary"
    assert "15-byte guest datagram" in result["payload"]
    assert "not a false/not-ready boolean" in result["payload"]
    lookup = entries["client_local:0x00f1339c"]
    assert lookup["direction"] == "local_only"
    assert "five local AC entries" in lookup["summary"]


def test_rejected_interpretations_keep_known_dead_ends() -> None:
    catalog = catalog_module.load_catalog()
    rejected = {item["id"] for item in catalog["rejected_interpretations"]}
    assert "rejected:0x03cd-full-garage-snapshot" in rejected
    assert "rejected:f22201-is-ready" in rejected
    assert "rejected:f22200-is-not-ready" in rejected
    assert "rejected:index0-fallback-is-complete" in rejected
    assert "rejected:no-reverse-p2p-traffic" in rejected
    assert "rejected:normal-room-attribute-0x50" in rejected
    assert "rejected:normal-role-plus-counterpart-fields6-7" in rejected
