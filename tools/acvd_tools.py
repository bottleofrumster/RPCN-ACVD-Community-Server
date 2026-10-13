#!/usr/bin/env python3
"""Human-facing entry point for safe analysis and explicit save extraction.

No command starts a server, controls a game, applies recovery, or runs GDB.
Advanced tools are described as source text, never imported or executed here.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
for directory in (ROOT, ROOT / "src", ROOT / "desktop"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

EXTRACTORS = {
    "parts": "extract_offline_inventory.py",
    "ac": "extract_ac_snapshot.py",
    "missions": "extract_mission_snapshot.py",
    "unacs": "extract_ai_snapshots.py",
}


def catalog(args):
    """Use the existing protocol catalog; do not infer new opcode meanings."""
    from tools.analysis.protocol_catalog import filtered_entries, load_catalog

    query = argparse.Namespace(code=args.code, search=args.search, layer=None, confidence=None)
    entries = filtered_entries(load_catalog(), query)
    shown = entries if args.all else entries[:25]
    for entry in shown:
        print(f"{entry['code']:8} {entry['name']} [{entry['confidence']}]")
        print(f"  {entry['summary']}")
    print(f"Shown: {len(shown)} of {len(entries)} matching codes.")
    if len(shown) < len(entries):
        print("Use --search, --code, or --all to see the remaining matches.")


def decode(args):
    """Inspect plaintext protobuf wire fields without guessing their meaning."""
    from acvd_server.protocol import decode_proto

    if len(args.hex) > 3 * 65536:
        raise ValueError("Use at most 64 KiB of hexadecimal data")
    payload = bytes.fromhex(args.hex)
    if len(payload) > 65536:
        raise ValueError("Use at most 64 KiB of hexadecimal data")
    fields = decode_proto(payload)
    print(
        "Plaintext protobuf fields only. This does not decrypt packets or identify field meanings."
    )
    for field in fields:
        value = field.value.hex() if isinstance(field.value, bytes) else field.value
        print(f"Field {field.number}; wire type {field.wire_type}; value {value}")


def pcap(args):
    """Reuse the bounded PCAP reader without importing Qt or attaching to clients."""
    from acvd_console.pcap import read_pcap

    if not 1 <= args.max_packets <= 1000:
        raise ValueError("Packet limit must be between 1 and 1000")
    events, summary = read_pcap(args.path, max_packets=args.max_packets)
    print(json.dumps(summary, indent=2))
    print("Saved IPv4/UDP packets only; no decryption, reassembly, or delivery proof.")
    for event in events:
        print(
            f"{event['timestamp_utc']} {event['source_endpoint']} -> "
            f"{event['destination_endpoint']}: {event['payload']['length']} bytes; "
            f"truncated={event['payload']['truncated']}; NXRV={event['nxrv_signature']}"
        )
        if args.show_bytes:
            print(event["payload"]["hex"])
    print("Review output before sharing. Unknown packet formats can contain private data.")


def tool_records():
    """Build the advanced index from source headers, without executing tools."""
    for path in sorted(TOOLS.rglob("*")):
        relative = path.relative_to(TOOLS)
        if (
            not path.is_file()
            or path.is_symlink()
            or path.suffix not in {".py", ".gdb", ".java", ".c", ".sh"}
            or set(relative.parts) & {"__pycache__", "SPRXPatcher"}
            or path.name == "__init__.py"
        ):
            continue
        summary = "Advanced game-specific source; inspect its requirements before use."
        if path.suffix == ".py":
            try:
                summary = (ast.get_docstring(ast.parse(path.read_text())) or summary).splitlines()[
                    0
                ]
            except (SyntaxError, UnicodeError):
                summary = "Source header could not be read. Inspect this tool manually."
        group = (
            "analysis"
            if relative.parts[0] == "analysis"
            else "save extraction"
            if relative.parts[0] == "migration"
            else "advanced ACVD"
        )
        yield {"path": relative.as_posix(), "group": group, "summary": summary}
    ghidra = TOOLS.parent / "ghidra_scripts"
    for path in sorted(ghidra.glob("*.java")):
        if path.is_file() and not path.is_symlink():
            yield {
                "path": f"ghidra_scripts/{path.name}",
                "group": "advanced Ghidra",
                "summary": "Run through Ghidra Script Manager on an owned, exact-version program. Read the script's arguments first.",
            }


def browse(args):
    rows = [row for row in tool_records() if args.search.casefold() in json.dumps(row).casefold()]
    shown = rows if args.all else rows[:25]
    for row in shown:
        print(f"{row['path']} [{row['group']}]\n  {row['summary']}")
    print(f"Shown: {len(shown)} of {len(rows)} matching source files.")
    print("Use browse --search WORD or --all. Listing a tool does not mean it is safe to run.")


def describe(args):
    is_ghidra = args.tool.startswith("ghidra_scripts/")
    base = TOOLS.parent if is_ghidra else TOOLS
    boundary = TOOLS.parent / "ghidra_scripts" if is_ghidra else TOOLS
    path = (base / args.tool).resolve()
    if not path.is_relative_to(boundary.resolve()) or not path.is_file():
        raise ValueError("Choose a source file listed by browse")
    rows = {row["path"]: row for row in tool_records()}
    relative = path.relative_to(base).as_posix()
    if relative not in rows:
        raise ValueError("This path is not an indexed tool source")
    print(f"File: {path}\nGroup: {rows[relative]['group']}")
    if path.suffix == ".py":
        print(ast.get_docstring(ast.parse(path.read_text())) or rows[relative]["summary"])
    else:
        print(rows[relative]["summary"])
    print(
        "Not executed. Read the source before using a client-control, debugger, or migration tool."
    )
    print("ACVD addresses, save offsets, and packet IDs must be recovered again for another game.")


def menus(args):
    """Show reviewed menu states and routes without controlling a client."""
    from tools.acvd_menu_navigator import STATES, route_record

    if args.list_states:
        for state in STATES.values():
            print(f"{state.name:28} [{state.group}; {state.coverage}] {state.note}")
        return
    if not args.source or not args.target:
        raise ValueError("Use --list-states or supply both --from and --to")
    print(json.dumps(route_record(args.source, args.target), indent=2))


def extract(args):
    """Stage new files only; no force, apply, or live-store destination is exposed."""
    source, output = args.input.resolve(), args.output.resolve()
    if not source.exists():
        raise ValueError("The input path does not exist")
    if args.kind == "unacs":
        if not source.is_dir():
            raise ValueError(
                "UNAC extraction needs the folder containing AI000.DAT through AI008.DAT"
            )
    elif not source.is_file() or source.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Choose the expected save file, no larger than 16 MiB")
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("Choose a new output path; existing files are never overwritten")
    for name in ("data", "keys", "state", "src", "desktop", "client", "dist", "tests", "tools"):
        if output.is_relative_to((ROOT / name).resolve()):
            raise ValueError("Use a separate staging folder, not a server or source directory")
    if output == source or output.is_relative_to(source):
        raise ValueError("Keep output outside the source save path")
    command = [
        sys.executable,
        str(TOOLS / "migration" / EXTRACTORS[args.kind]),
        str(source),
        str(output),
    ]
    if args.kind in ("missions", "unacs"):
        if args.player_id is None or args.player_id <= 0:
            raise ValueError("This extraction requires the known --player-id; do not guess it")
        command += ["--player-id", str(args.player_id)]
    print(
        "Read input and create new staging output only. Nothing will be installed into the server."
    )
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise ValueError(detail[-1] if detail else "The extractor rejected this input")
    print(result.stdout, end="")


def interactive():
    print("ACVD TOOLS — no programming is required for these common checks.")
    print("1  Find a protocol code\n2  Decode plaintext hex fields\n3  Inspect a saved PCAP")
    print("4  Find an advanced tool\n5  Show save-extraction instructions")
    print("6  List mapped game-menu states\n0  Exit")
    choice = input("Choose: ").strip()
    if choice == "1":
        return ["catalog", "--search", input("Search word (for example, mercenary): ").strip()]
    if choice == "2":
        return ["decode", "--hex", input("Plaintext hex bytes: ").strip()]
    if choice == "3":
        return ["pcap", input("Path to a classic PCAP file: ").strip(), "--max-packets", "25"]
    if choice == "4":
        return ["browse", "--search", input("Search word (for example, capture): ").strip()]
    if choice == "5":
        return ["extract", "--help"]
    if choice == "6":
        return ["menus", "--list-states"]
    if choice in ("", "0"):
        return None
    raise ValueError("Choose a number from 0 to 6")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    c = commands.add_parser("catalog", help="Find known codes and their confidence")
    c.add_argument("--search", default="")
    c.add_argument("--code")
    c.add_argument("--all", action="store_true")
    c.set_defaults(run=catalog)
    d = commands.add_parser("decode", help="Inspect plaintext protobuf fields; no decryption")
    d.add_argument("--hex", required=True)
    d.set_defaults(run=decode)
    p = commands.add_parser("pcap", help="Read a saved capture, not live traffic")
    p.add_argument("path", type=Path)
    p.add_argument("--max-packets", type=int, default=25)
    p.add_argument("--show-bytes", action="store_true")
    p.set_defaults(run=pcap)
    b = commands.add_parser("browse", help="Find advanced source files without running them")
    b.add_argument("--search", default="")
    b.add_argument("--all", action="store_true")
    b.set_defaults(run=browse)
    d = commands.add_parser("describe", help="Read a tool's purpose and safety boundary")
    d.add_argument("tool")
    d.set_defaults(run=describe)
    m = commands.add_parser("menus", help="List or plan reviewed game-menu routes")
    m.add_argument("--list-states", action="store_true")
    m.add_argument("--from", dest="source")
    m.add_argument("--to", dest="target")
    m.set_defaults(run=menus)
    e = commands.add_parser("extract", help="Stage ACVD save data; never install or overwrite it")
    e.add_argument("kind", choices=EXTRACTORS)
    e.add_argument("input", type=Path)
    e.add_argument("output", type=Path)
    e.add_argument("--player-id", type=int)
    e.set_defaults(run=extract)
    try:
        arguments = sys.argv[1:] if argv is None else argv
        if not arguments and sys.stdin.isatty():
            arguments = interactive()
            if arguments is None:
                return 0
        args = parser.parse_args(arguments)
        if not args.command:
            parser.print_help()
            return 0
        args.run(args)
        return 0
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError, EOFError) as error:
        print(f"Cannot complete this check: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
