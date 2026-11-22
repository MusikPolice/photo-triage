#!/usr/bin/env bash
# `just e2e` (dev-environment §6): Playwright against the production Compose stack.
#
#   scripts/e2e.sh [PLAYWRIGHT_ARGS...]
#
# Builds the image from this checkout, as `just stack` does (or uses E2E_IMAGE, an
# image already built from it, as CI does), and starts the stack's
# app and worker (not Ollama) under its own project name, photo-triage-e2e, on a
# free port, as the UID running this script (APP_UID). The library is a fresh copy
# of the tier-1 fixtures in a scratch folder, and the worker embeds with FakeClip
# (WORKER_FAKE_CLIP), so no model weights are needed. A scan is queued once the app answers. Playwright then runs the specs in
# frontend/e2e at desktop and phone sizes; they wait for the worker to finish.
# The stack and the scratch folder are removed afterwards, pass or fail. Traces of
# failed tests are in frontend/test-results/.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
image="${E2E_IMAGE:-photo-triage:e2e}"
export COMPOSE_PROJECT_NAME=photo-triage-e2e

scratch="$(mktemp -d -t photo-triage-e2e.XXXXXX)"
mkdir "$scratch/photos" "$scratch/data" "$scratch/models"
cp -r backend/tests/fixtures/synthetic/. "$scratch/photos/"
# Its scan fails and waits minutes to retry, so the scan stage would never show
# done; the backend's tests cover how it's handled.
rm "$scratch/photos/broken/truncated.jpg"
cat >"$scratch/app.env" <<'EOF'
# Settings for `just e2e`'s containers, in place of `.env`.
WORKER_FAKE_CLIP=true
EOF

port="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
export PHOTO_TRIAGE_IMAGE="$image" PHOTO_DIR="$scratch/photos" DATA_DIR="$scratch/data" \
  MODELS_DIR="$scratch/models" APP_ENV_FILE="$scratch/app.env" APP_PORT="$port"
# As whoever runs this, so the containers can write the scratch folders and the
# cleanup can remove what they wrote. On CI, that's UID 1001, not the image's 1000.
APP_UID="$(id -u)" APP_GID="$(id -g)"
export APP_UID APP_GID
compose() { docker compose -f compose.yaml "$@"; }

# shellcheck disable=SC2329  # called by the EXIT trap
cleanup() {
  local status=$?
  trap '' INT TERM
  if ((status != 0)); then
    echo "The stack's logs, for the failure:" >&2
    compose logs --no-color --tail 50 migrate app worker >&2 || true
  fi
  compose down --volumes --timeout 5 >/dev/null 2>&1 || true
  rm -rf "$scratch"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -z "${E2E_IMAGE:-}" ]]; then
  # As `just image`, without the lint and smoke test.
  # shellcheck disable=SC2046  # one word per --build-arg and value
  docker build $(scripts/image_pins.sh --build-arg) -f docker/Dockerfile -t "$image" .
fi
compose up --detach --wait app worker

url="http://localhost:$port"
curl -fsS -X POST "$url/api/scan" >/dev/null
echo "e2e stack at $url, scanning the tier-1 fixtures" >&2

cd frontend
E2E_BASE_URL="$url" pnpm exec playwright test --config e2e/playwright.config.ts "$@"
