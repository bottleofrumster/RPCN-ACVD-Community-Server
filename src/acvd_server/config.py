"""Define source-root paths, advertised endpoints, and listener settings."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .audit_log import DEFAULT_BACKUP_COUNT, DEFAULT_MAX_BYTES


@dataclass(slots=True)
class ServerConfig:
    bind_host: str = "0.0.0.0"
    advertised_host: str = "127.0.0.1"
    # 50011 is the retail BLUS31194 v1.02 login port. The remaining values
    # are preserved as diagnostic listeners for shipped development configs.
    login_ports: list[int] = field(default_factory=lambda: [50011, 52101, 50036, 50801, 50050])
    auth_port: int = 52102
    game_port: int = 52103
    cdn_port: int = 8080
    max_packet_length: int = 65535
    audit_capture_payloads: bool = False
    audit_max_bytes: int = DEFAULT_MAX_BYTES
    audit_backup_count: int = DEFAULT_BACKUP_COUNT
    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2])

    @property
    def private_key_path(self) -> Path:
        return self.project_root / "keys" / "server-private.pem"

    @property
    def public_key_path(self) -> Path:
        return self.project_root / "keys" / "publickey.pem"

    @property
    def telemetry_path(self) -> Path:
        return self.project_root / "logs" / "protocol-events.jsonl"

    @property
    def native_library_path(self) -> Path:
        return self.project_root / "native" / "libacvd_cwc.so"

    @property
    def player_data_path(self) -> Path:
        return self.project_root / "data" / "players"

    @property
    def team_data_path(self) -> Path:
        return self.project_root / "data" / "teams"

    @property
    def session_state_path(self) -> Path:
        return self.project_root / "data" / "sessions" / "active.json"

    @property
    def regulation_path(self) -> Path:
        return self.project_root / "data" / "cdn" / "regulation" / "contents_101.bin"

    @property
    def shop_parameter_path(self) -> Path:
        return self.project_root / "data" / "shop"
