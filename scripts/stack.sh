#!/usr/bin/env bash
# `just stack` (dev-environment §5): the production Compose stack (compose.yaml)
# with an image built from this checkout, against a test folder, `.stack/`.
#   scripts/stack.sh                    build the image, then `docker compose up`
#   scripts/stack.sh COMPOSE_ARGS...    any other Compose command on the stack, e.g.
#     exec worker python -m photo_triage.worker noop 50
#     restart worker | logs -f app | down
# `up` (also with options, e.g. `up -d`) builds the image first.
#
# `.stack/` holds the library (photos/, with the trash inside it) and DATA_DIR
# (data/), bind-mounted as on the server, and app.env, the settings the
# containers get in place of `.env` (created with the noop stage on, so jobs can
# be queued by hand; edit it to try others). The host port is APP_PORT, from the
# environment or `.env`, as for `just dev`, so stop that first. The project name
# is photo-triage-stack, so this never touches a real deployment's containers.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
image="photo-triage:dev"
stack="$root/.stack"

mkdir -p "$stack/photos" "$stack/data"
if [[ ! -f "$stack/app.env" ]]; then
  cat >"$stack/app.env" <<'EOF'
# Settings for `just stack`'s containers, in place of `.env` (plan §9). PHOTO_DIR,
# DATA_DIR, TRASH_DIR and APP_PORT are set by compose.yaml and ignored here.
WORKER_NOOP_STAGE=true
EOF
fi
if [[ "$(id -u)" != 1000 ]]; then
  echo "Note: the containers run as UID 1000, and you're UID $(id -u), so they may not be able to write to $stack" >&2
fi

export COMPOSE_PROJECT_NAME=photo-triage-stack
export PHOTO_TRIAGE_IMAGE="$image"
export PHOTO_DIR="$stack/photos" DATA_DIR="$stack/data" APP_ENV_FILE="$stack/app.env"

(($#)) || set -- up
if [[ "$1" == up ]]; then
  # As `just image`, without the lint and smoke test.
  # shellcheck disable=SC2046  # one word per --build-arg and value
  docker build $(scripts/image_pins.sh --build-arg) -f docker/Dockerfile -t "$image" .
fi
exec docker compose -f compose.yaml "$@"
