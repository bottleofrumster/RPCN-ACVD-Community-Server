#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

gcc \
  -std=c11 \
  -O2 \
  -fPIC \
  -shared \
  -I"$project_dir/native" \
  -I"$project_dir/native/aes" \
  -I"$project_dir/native/aes_modes" \
  "$project_dir/native/wrapper.c" \
  "$project_dir/native/aes_modes/cwc.c" \
  "$project_dir/native/aes/aescrypt.c" \
  "$project_dir/native/aes/aeskey.c" \
  "$project_dir/native/aes/aestab.c" \
  -o "$project_dir/native/libacvd_cwc.so"

echo "Built $project_dir/native/libacvd_cwc.so"

