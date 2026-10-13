#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
venv_dir="$project_dir/.venv"

python3 -m venv "$venv_dir"
"$venv_dir/bin/pip" install --upgrade pip
"$venv_dir/bin/pip" install -e "$project_dir[test]"
"$venv_dir/bin/acvd-server" init

echo
echo "Setup complete. Run:"
echo "  $venv_dir/bin/acvd-server serve --advertised-host SERVER_IP"
