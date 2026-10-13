#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import PySide6, cryptography' >/dev/null 2>&1; then
    .venv/bin/python -m pip install -e '.[desktop-test]'
fi
exec .venv/bin/python desktop/run.py "$@"
