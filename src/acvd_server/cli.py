"""Initialize a source-based host and run its service lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import subprocess
from pathlib import Path

from .audit_log import DEFAULT_BACKUP_COUNT, DEFAULT_MAX_BYTES
from .config import ServerConfig
from .crypto import generate_rsa_keypair
from .regulation import install_for_host
from .server import AcvdServer


def _config(args) -> ServerConfig:
    config = ServerConfig()
    if getattr(args, "data_root", None) is not None:
        config.project_root = args.data_root.resolve()
    if getattr(args, "bind", None):
        config.bind_host = args.bind
    if getattr(args, "advertised_host", None):
        config.advertised_host = args.advertised_host
    if getattr(args, "login_ports", None):
        config.login_ports = [int(value) for value in args.login_ports.split(",") if value]
    if getattr(args, "auth_port", None):
        config.auth_port = args.auth_port
    if getattr(args, "game_port", None):
        config.game_port = args.game_port
    if getattr(args, "cdn_port", None):
        config.cdn_port = args.cdn_port
    config.audit_capture_payloads = getattr(args, "capture_payloads", False)
    config.audit_max_bytes = getattr(args, "audit_max_bytes", config.audit_max_bytes)
    config.audit_backup_count = getattr(args, "audit_backups", config.audit_backup_count)
    return config


def initialize(config: ServerConfig, *, force: bool = False) -> None:
    if force and (config.regulation_path.exists() or config.regulation_path.is_symlink()):
        raise SystemExit(
            "key rotation requires an explicit regulation migration; no identity files changed"
        )
    private_exists = config.private_key_path.exists()
    public_exists = config.public_key_path.exists()
    if not force and private_exists != public_exists:
        raise SystemExit("partial keypair exists; preserve both identity files before recovery")
    build_script = config.project_root / "scripts" / "build_native.sh"
    subprocess.run(["bash", str(build_script)], check=True)
    if force or not private_exists:
        generate_rsa_keypair(config.private_key_path, config.public_key_path)
    bundled = config.project_root / "resources" / "regulation"
    source = bundled / "contents_101.bin"
    trusted_key = bundled / "publickey.pem"
    if source.exists() or trusted_key.exists():
        installed = install_for_host(
            source, trusted_key, config.private_key_path, config.regulation_path
        )
        print(
            "Regulation installed for this host." if installed else "Existing regulation preserved."
        )
    print(f"Server private key: {config.private_key_path}")
    print(f"Server public key: {config.public_key_path}")
    print("Build the reviewed bare helper kit with scripts/build_distribution.py")


async def _serve(config: ServerConfig) -> None:
    if not config.private_key_path.exists() or not config.native_library_path.exists():
        initialize(config)
    server = AcvdServer(config)
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, server.shutdown_requested.set)
    try:
        await server.serve_forever()
    finally:
        await server.close()
        loop.remove_signal_handler(signal.SIGTERM)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ACVD replacement service")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="build crypto and create a server/client keypair"
    )
    init_parser.add_argument("--force", action="store_true", help="replace the existing keypair")

    serve_parser = subparsers.add_parser(
        "serve", help="run login, auth, and game discovery services"
    )
    serve_parser.add_argument("--bind", default="0.0.0.0")
    serve_parser.add_argument("--advertised-host", default="127.0.0.1")
    serve_parser.add_argument("--login-ports", default="50011,52101,50036,50801,50050")
    serve_parser.add_argument("--auth-port", type=int, default=52102)
    serve_parser.add_argument("--game-port", type=int, default=52103)
    serve_parser.add_argument("--cdn-port", type=int, default=8080)
    serve_parser.add_argument("--verbose", action="store_true")
    serve_parser.add_argument(
        "--capture-payloads",
        action="store_true",
        help="persist non-authentication payload prefixes; logs remain private",
    )
    serve_parser.add_argument(
        "--audit-max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help="maximum size per audit log (minimum 1024 bytes)",
    )
    serve_parser.add_argument(
        "--audit-backups",
        type=int,
        choices=range(1, 11),
        default=DEFAULT_BACKUP_COUNT,
        help="number of rotated audit files to retain (1-10)",
    )
    serve_parser.add_argument(
        "--data-root",
        type=Path,
        help="use an existing host's keys, player data, settings and logs with this code release",
    )

    args = parser.parse_args(argv)
    config = _config(args)
    if config.audit_max_bytes < 1024:
        parser.error("--audit-max-bytes must be at least 1024")
    if args.command == "serve" and args.data_root is not None:
        # A code release must not create a replacement identity because of a
        # mistyped data path. Initialization remains a separate explicit step.
        required = (config.private_key_path, config.public_key_path, config.native_library_path)
        if not config.project_root.is_dir() or not all(path.is_file() for path in required):
            parser.error("--data-root must contain the existing keypair and native crypto library")
    if args.command == "init":
        initialize(config, force=args.force)
    elif args.command == "serve":
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        try:
            asyncio.run(_serve(config))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
