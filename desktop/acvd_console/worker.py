"""Poll status and process explicit operator requests on a background thread.

The request queue has no fixed capacity. Service calls can delay polling.
Do not treat the worker loop as a hard real-time deadline.
"""

from __future__ import annotations

import json
import queue
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .core import ServiceControl, TailReader, bounded_lines, read_store, rpc
from .pcap import read_pcap
from .traffic import Catalog, decode_record


class Monitor(QThread):
    snapshot = Signal(dict)
    events = Signal(list)
    result = Signal(str, bool)

    def __init__(self, root: Path):
        super().__init__()
        self.root, self.pending = root, queue.Queue()
        self.debug, self.cursor, self.boot = False, 0, None
        self.catalog = Catalog(root)
        emulator = root.parent / "Emulator"
        self.readers = {"Server": TailReader(root / "logs/protocol-events.jsonl")}
        for label, folder in (
            ("Primary RPCS3", "RPCS3-ACVD-Primary-Peer"),
            ("Secondary RPCS3", "RPCS3-ACVD-Second"),
        ):
            self.readers[label] = TailReader(emulator / folder / "cache/rpcs3/RPCS3.log", 0)
        self.service = ServiceControl()

    def submit(self, command, **arguments):
        self.pending.put((command, arguments))

    def run(self):
        try:
            while not self.isInterruptionRequested():
                begin = time.monotonic()
                while not self.pending.empty():
                    command, args = self.pending.get()
                    try:
                        if command == "service":
                            message = self.service.act(
                                args["action"], self.root, args.get("confirmed", False)
                            )
                        elif command == "configure":
                            rpc(self.root, "configure", settings=args["settings"])
                            message = "The server now uses the new settings."
                        elif command == "debug":
                            self.debug = args["enabled"]
                            try:
                                rpc(self.root, "debug", enabled=self.debug)
                                self.cursor = 0
                                message = "Debug capture " + (
                                    "enabled." if self.debug else "disabled."
                                )
                            except (OSError, ValueError):
                                message = "Local log followers " + (
                                    "enabled; live raw capture requires the admin-enabled release."
                                    if self.debug
                                    else "disabled."
                                )
                        elif command == "log":
                            path = Path(args["path"])
                            self.readers[f"Saved log: {path.name}"] = TailReader(path)
                            message = "Added local log source."
                        elif command == "shutdown":
                            rpc(self.root, "shutdown", confirmed=args.get("confirmed", False))
                            message = "The panel sent a stop request to the server."
                        elif command == "delete_player":
                            result = rpc(
                                self.root,
                                "delete_player",
                                online_id=args["online_id"],
                                player_id=args["player_id"],
                                confirmed=args.get("confirmed", False),
                                confirmation=args.get("confirmation", ""),
                            )
                            message = (
                                "Permanently deleted server save data for "
                                f"{result['online_id']} ({result['deleted_paths']} stored paths)."
                            )
                        elif command == "pcap":
                            rows, summary = read_pcap(Path(args["path"]))
                            self.events.emit(
                                [decode_record(row, self.catalog, "PCAP import") for row in rows]
                            )
                            message = "PCAP import: " + json.dumps(summary)
                        else:
                            raise ValueError("This operator command is not known.")
                        self.result.emit(message, True)
                    except Exception as error:
                        self.result.emit(str(error), False)
                state = {"admin": None, "error": ""}
                try:
                    state["service"] = self.service.status()
                    try:
                        live = rpc(self.root, "status")
                        if Path(live["root"]).resolve() != self.root.resolve():
                            raise ValueError("Administrative socket belongs to another root")
                        state["admin"] = live
                        boot = (live["pid"], live["started_monotonic"])
                        if boot != self.boot:
                            self.cursor, self.boot = 0, boot
                    except (OSError, ValueError, KeyError) as error:
                        state["error"] = str(error)
                    if not state["admin"]:
                        store = read_store(self.root)
                        state["stored_teams"] = list(store.get("teams", {}).values())
                        state["stored_settlements"] = store.get("battle_settlements", [])[-100:]
                    if self.debug:
                        rows = []
                        live_debug = state["admin"] and state["admin"].get("debug_enabled")
                        if live_debug:
                            feed = rpc(self.root, "events", after=self.cursor)
                            self.cursor = feed["cursor"]
                            if feed["gap"]:
                                rows.append(
                                    decode_record(
                                        {
                                            "event": "diagnostic_gap",
                                            "reason": "Bounded live ring overflow; inspect audit log",
                                        },
                                        self.catalog,
                                    )
                                )
                            rows.extend(decode_record(r, self.catalog) for r in feed["records"])
                        for source, reader in self.readers.items():
                            if source == "Server" and live_debug:
                                continue
                            lines, skipped = bounded_lines(reader.poll())
                            if skipped:
                                rows.append(
                                    decode_record(
                                        {
                                            "event": "console_feed_gap",
                                            "skipped_rows": skipped,
                                            "reason": "Bounded display batch; inspect original log for omitted rows",
                                        },
                                        self.catalog,
                                        source,
                                    )
                                )
                            for line in lines:
                                try:
                                    record = json.loads(line)
                                    if not isinstance(record, dict):
                                        raise ValueError("not object")
                                except (ValueError, TypeError):
                                    record = {
                                        "event": "client_log"
                                        if source != "Server"
                                        else "unparsed_log",
                                        "line": line[:65536],
                                    }
                                rows.append(decode_record(record, self.catalog, source))
                        if rows:
                            self.events.emit(rows)
                    state["sources"] = {name: str(r.path) for name, r in self.readers.items()}
                    self.snapshot.emit(state)
                except Exception as error:
                    self.result.emit(f"Monitor: {error}", False)
                while time.monotonic() - begin < 1 and not self.isInterruptionRequested():
                    self.msleep(100)
        finally:
            if self.debug:
                try:
                    rpc(self.root, "debug", enabled=False)
                except (OSError, ValueError):
                    pass
