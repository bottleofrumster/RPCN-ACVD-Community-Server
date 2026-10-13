"""Local administration and bounded log reading; no arbitrary command execution."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

SERVICE = "acvd-server.service"
RESOURCES = Path(__file__).with_name("resources")


def bounded_lines(lines, limit=500):
    """Report skipped overload rows explicitly; absence in a view is not evidence."""
    return lines[-limit:], max(0, len(lines) - limit)


class TailReader:
    """Rotation/truncation-safe, bounded reader that retains incomplete lines."""

    def __init__(self, path: Path, initial_bytes=65536):
        self.path, self.initial_bytes = path, initial_bytes
        self.identity, self.offset, self.partial = None, 0, b""

    def poll(self):
        try:
            with self.path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                identity = (info.st_dev, info.st_ino)
                if identity != self.identity:
                    self.offset = (
                        0
                        if self.initial_bytes is None
                        else max(0, info.st_size - self.initial_bytes)
                    )
                    self.identity, self.partial = identity, b""
                    stream.seek(self.offset)
                    if self.offset:
                        stream.readline()
                        self.offset = stream.tell()
                elif info.st_size < self.offset:
                    self.offset, self.partial = 0, b""
                stream.seek(self.offset)
                data = stream.read(262144)
                self.offset = stream.tell()
            lines = (self.partial + data).split(b"\n")
            self.partial = lines.pop()
            if len(self.partial) > 1024 * 1024:
                self.partial = b""
                lines.append(b'{"event":"console_line_dropped","reason":"line exceeds 1 MiB"}')
            return [line.decode("utf-8", "replace") for line in lines if line]
        except FileNotFoundError:
            return []


def rpc(root: Path, command: str, **values):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(root / "state/operator.sock"))
        connection.sendall(json.dumps({"command": command, **values}).encode() + b"\n")
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                raise ConnectionError("Administrative connection closed")
            data.extend(chunk)
            if len(data) > 4 * 1024 * 1024:
                raise ValueError("Administrative response exceeds limit")
    response = json.loads(data)
    if not response.get("ok"):
        raise ValueError(response.get("error", "Administrative error"))
    return response["data"]


class ServiceControl:
    def __init__(self, runner=subprocess.run):
        self.runner = runner

    def status(self):
        result = self.runner(
            [
                "systemctl",
                "--user",
                "show",
                SERVICE,
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "MainPID",
                "-p",
                "NRestarts",
                "-p",
                "LoadState",
                "-p",
                "WorkingDirectory",
            ],
            capture_output=True,
            text=True,
            timeout=4,
        )
        if result.returncode:
            return {"ActiveState": "unknown", "error": result.stderr.strip()}
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def act(self, action, root, confirmed=False):
        if action not in ("start", "stop", "restart"):
            raise ValueError("This server command is not available.")
        state = self.status()
        if state.get("LoadState") != "loaded":
            raise ValueError("The ACVD user service is not installed")
        directory = state.get("WorkingDirectory")
        if not directory or Path(directory).resolve() != root.resolve():
            raise ValueError("This service uses a different server folder. No command was sent.")
        if action == "start" and state.get("ActiveState") in ("active", "activating", "reloading"):
            return "Already running. No new server process was started."
        if action in ("stop", "restart") and not confirmed:
            raise ValueError("Confirm this command first.")
        result = self.runner(
            ["systemctl", "--user", action, SERVICE], capture_output=True, text=True, timeout=30
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Service command failed")
        return f"{action.capitalize()} requested; live status will confirm completion."


def read_store(root):
    path = root / "data/teams/teams.json"
    if not path.exists():
        return {}
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Team store exceeds desktop preview limit")
    return json.loads(path.read_text())
