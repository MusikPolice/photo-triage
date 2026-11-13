#!/usr/bin/env bash
# `just dry-run-full` (dev-environment §4 tier 3, §5): the production Compose
# stack against the whole library at /mnt/pictures, to measure the pipeline at
# full scale. Nothing can write to the library:
#   - it refuses to start unless /mnt/pictures is a read-only mount;
#   - docker/compose.dry-run.yaml mounts it read-only in the containers too, sets
#     EXIF_WRITES_ENABLED=false, and puts the trash in the scratch folder.
#
# DATA_DIR and the trash are in a scratch folder outside the library,
# ~/.cache/photo-triage/dry-run by default (DRY_RUN_DIR to change it), so a run
# can be stopped and resumed. Delete the folder to start again from nothing.
#
#   scripts/dry_run_full.sh                 build the image, then start the stack
#                                           (detached) on DRY_RUN_PORT (default 8010)
#   scripts/dry_run_full.sh COMPOSE_ARGS... any other Compose command on it, e.g.
#     logs -f worker | stats --no-stream | down
#   scripts/dry_run_full.sh report          the numbers so far: items, walk time,
#                                           items/min per stage, the worker's memory
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
library="${DRY_RUN_LIBRARY:-/mnt/pictures}"
scratch="${DRY_RUN_DIR:-$HOME/.cache/photo-triage/dry-run}"
image="photo-triage:dev"

die() { echo "dry-run-full: $*" >&2; exit 1; }

options="$(findmnt --noheadings --output OPTIONS --target "$library" 2>/dev/null)" \
  || die "$library isn't mounted (see README §5)"
[[ ",$options," == *",ro,"* ]] || die "$library is mounted read-write ($options). Remount it read-only first."
[[ "$(findmnt --noheadings --output TARGET --target "$library")" == "$library" ]] \
  || die "$library isn't a mount point of its own"
case "$scratch/" in
  "$library"/*) die "DRY_RUN_DIR ($scratch) is inside the library" ;;
esac

mkdir -p "$scratch/data" "$scratch/trash"
models="$HOME/.cache/photo-triage/models"
[[ -d "$models" ]] || die "no models in $models; run scripts/bootstrap.sh first"
[[ -f "$scratch/app.env" ]] || cat >"$scratch/app.env" <<'EOF'
# Settings for `just dry-run-full`'s containers (plan §9). PHOTO_DIR, DATA_DIR,
# MODELS_DIR, TRASH_DIR, APP_PORT and EXIF_WRITES_ENABLED are set by the compose
# files and ignored here.
TZ=America/Toronto
EOF

export COMPOSE_PROJECT_NAME=photo-triage-dry-run
export PHOTO_TRIAGE_IMAGE="$image"
export PHOTO_DIR="$library" DATA_DIR="$scratch/data" TRASH_DIR="$scratch/trash" \
  MODELS_DIR="$models" APP_ENV_FILE="$scratch/app.env" APP_PORT="${DRY_RUN_PORT:-8010}"
compose() { docker compose -f compose.yaml -f docker/compose.dry-run.yaml "$@"; }

if [[ "${1:-}" == report ]]; then
  compose exec -T worker python - <"$root/scripts/dry_run_report.py"
  exit
fi
if (($#)); then
  compose "$@"
  exit
fi

# shellcheck disable=SC2046  # one word per --build-arg and value
docker build $(scripts/image_pins.sh --build-arg) -f docker/Dockerfile -t "$image" .
compose up --detach --wait migrate app worker
echo "Dry run started on http://localhost:$APP_PORT/activity, data in $scratch." >&2
echo "Numbers so far: just dry-run-full report. Stop it: just dry-run-full down." >&2
