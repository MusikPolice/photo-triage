#!/usr/bin/env bash
# `just map-stack` (dev-environment §5): the production image's app, serving a
# synthetic library of N items (default 100,000), for trying the map's speed on
# phones and desktops (#119, plan §6.4).
#
#   scripts/map_stack.sh [N]
#
# Builds the image from this checkout, as `just stack` does (or uses MAP_STACK_IMAGE),
# then in a scratch folder: runs the migrate step, seeds N items with a layout,
# thumbnails and atlases (`scripts/seed_map.py`, inside the image), and starts the
# app alone, with no worker, since a scan of the empty library would mark every
# item missing. The app listens on every interface, on MAP_STACK_PORT (default
# 8100), so a phone on the LAN can open it; from WSL that needs a port forward
# (dev-environment §5, "Trying the map on a phone"). Add `?fps` to the URL for a
# frame-rate readout. Ctrl-C stops it and removes the scratch folder. The real
# library, DATA_DIR and `.stack/` are never touched.
set -euo pipefail

count="${1:-100000}"
[[ "$count" =~ ^[1-9][0-9]*$ ]] || { echo "usage: $0 [N]" >&2; exit 2; }
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
image="${MAP_STACK_IMAGE:-photo-triage:map-stack}"
port="${MAP_STACK_PORT:-8100}"
export COMPOSE_PROJECT_NAME=photo-triage-map-stack

scratch="$(mktemp -d -t photo-triage-map-stack.XXXXXX)"
mkdir "$scratch/photos" "$scratch/data" "$scratch/models"
: >"$scratch/app.env"
APP_UID="$(id -u)" APP_GID="$(id -g)"
export APP_UID APP_GID PHOTO_TRIAGE_IMAGE="$image" PHOTO_DIR="$scratch/photos" \
  DATA_DIR="$scratch/data" MODELS_DIR="$scratch/models" APP_ENV_FILE="$scratch/app.env" \
  APP_PORT="$port"
compose() { docker compose -f compose.yaml "$@"; }

# shellcheck disable=SC2329  # called by the EXIT trap
cleanup() {
  trap '' INT TERM
  compose down --volumes --timeout 5 >/dev/null 2>&1 || true
  rm -rf "$scratch"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -z "${MAP_STACK_IMAGE:-}" ]]; then
  # shellcheck disable=SC2046  # one word per --build-arg and value
  docker build $(scripts/image_pins.sh --build-arg) -f docker/Dockerfile -t "$image" .
fi
compose run --rm migrate
echo "Seeding $count synthetic items" >&2
compose run --rm --no-deps --volume "$root/scripts/seed_map.py:/seed_map.py:ro" \
  migrate python /seed_map.py "$count"
compose up --detach --wait --no-deps app

echo "Map stack at http://localhost:$port (add ?fps for a frame-rate readout)" >&2
wsl_address="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [[ -n "${WSL_DISTRO_NAME:-}" && -n "$wsl_address" ]]; then
  cat >&2 <<EOF
For a phone on the LAN, in an admin PowerShell (dev-environment §5):
  netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=$port connectaddress=$wsl_address connectport=$port
  New-NetFirewallRule -DisplayName "photo-triage map-stack" -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow -Profile Private
then open http://<the PC's LAN address, from ipconfig>:$port/?fps
EOF
fi
echo "Ctrl-C to stop" >&2
compose logs --follow --no-log-prefix app >/dev/null 2>&1 || true
