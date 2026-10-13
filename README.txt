ACVD COMMUNITY SERVER

Experimental online server for Armored Core: Verdict Day.
Client target: RPCS3, BLUS31194, game update v1.02.
Native PS3 crossplay is not qualified.

SERVER UI
Windows: Double-click Launch Server UI.cmd.
Requires WSL 2 with Linux GUI support and Python 3.11+ with venv installed
inside the default Linux distribution. The server controls use Linux/systemd;
this is not a native Windows server. Keep the launcher with the other files.
WSL setup: https://learn.microsoft.com/en-us/windows/wsl/tutorials/gui-apps

Linux: Double-click Launch Server UI.desktop. Select Allow Launching if prompted.
Or run: bash "Launch Server UI.sh"
First launch installs UI dependencies. It does not start or reset the server.
The UI uses your last selected server folder.

INSTALL (LINUX)
Requires Python 3.11+, venv support, GCC and make.

1. Run in this directory:
   python3 -m venv .venv
   .venv/bin/python -m pip install -e .
   .venv/bin/acvd-server init
2. Start a local server:
   .venv/bin/acvd-server serve --bind 127.0.0.1 --advertised-host 127.0.0.1 --login-ports 50011

For public service, set --bind 0.0.0.0 and --advertised-host YOUR_HOST.
Allow TCP 50011, 52102 and 8080; allow UDP 52103.
Configure clients with the same address and this host's PUBLIC key.
The supplied community client kit does not configure a new self-hosted server.

CAUTION
Keep keys/server-private.pem private.
Do not run init --force on an existing host. Do not run two servers on one
data directory. Use --data-root PATH to select an existing host.
Stop with Ctrl+C or SIGTERM. Do not delete saves to correct connection errors.

INPUTS
init builds the native library and installs regulation r1.03 for this host.
The CDN serves a signed manifest, then a signed regulation archive.
Game update v1.02 and regulation r1.03 are different versions.
Supply compatible shop tables in data/shop. Formation purchases require
customlayoutareadata.bin. Retail executables, saves and shop tables are not
included. This source checkout is required; a wheel alone is not sufficient.

CLIENT
See client/README.txt. Client files and server source are separate.
Player install files are in "client user patch files".
No live server address is included. Before installing client files, run:
   python3 client/configure_endpoint.py SERVER_IPV4
Obtain SERVER_IPV4 from the operator. Do not publish your configured copy.
RPCN provides emulator identity, rooms and peer signaling.

STATUS
See PENDING.txt for known issues and tests still required.
Authentication checks ticket structure, not its signature or expiry.
Do not treat this experimental server as hardened against hostile clients.
The admin socket is local-only. Never expose it through an open network proxy.
Logs contain player identifiers and addresses. Do not publish host logs.

DEVELOPMENT
   .venv/bin/python -m pip install -e '.[test,dev]'
   .venv/bin/python -m pytest -q -p no:cacheprovider
   .venv/bin/ruff check --no-cache src tests

scripts/build_distribution.py packages an existing reviewed client kit.
It does not generate game assets from source.

CREDITS AND LICENSE
Initial reverse engineering and first login: NatsuDragneelTheFireDragon.
Free to use and modify with credit to bottleofrumster and
NatsuDragneelTheFireDragon. See LICENSE.txt. Third-party terms still apply.
