#!/usr/bin/env bash
# The pins docker/Dockerfile takes as build args, one KEY=value per line: the
# runtimes from mise.toml and the tools from versions.env (dev-environment §2).
#   scripts/image_pins.sh               KEY=value lines (the CI docker job's build-args)
#   scripts/image_pins.sh --build-arg   the same as `--build-arg KEY=value` (just image)
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../versions.env
source "$root/versions.env"
mise_pin() { sed -n "/^\[tools\]/,/^\[/s/^$1 = \"\(.*\)\"$/\1/p" "$root/mise.toml"; }

pins=(
  "PYTHON_VERSION=$(mise_pin python)"
  "UV_VERSION=$(mise_pin uv)"
  "NODE_VERSION=$(mise_pin node)"
  "PNPM_VERSION=$(mise_pin pnpm)"
  "EXIFTOOL_VERSION=${EXIFTOOL_VERSION:-}"
  "EXIFTOOL_SHA256=${EXIFTOOL_SHA256:-}"
  "FFMPEG_VERSION=${FFMPEG_VERSION:-}"
  "FFMPEG_SHA256=${FFMPEG_SHA256:-}"
)

for pin in "${pins[@]}"; do
  [[ -n "${pin#*=}" ]] || { echo "No pin for ${pin%%=*} in mise.toml or versions.env" >&2; exit 1; }
  if [[ "${1:-}" == --build-arg ]]; then echo "--build-arg $pin"; else echo "$pin"; fi
done
