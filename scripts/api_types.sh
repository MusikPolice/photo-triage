#!/usr/bin/env bash
# The frontend's API types, generated from the backend's OpenAPI schema with
# openapi-typescript (dev-environment §8, the `contract` CI job):
#   scripts/api_types.sh          regenerate frontend/src/lib/api-types.ts
#   scripts/api_types.sh --check  fail if the committed file is out of date
# Needs the backend's .venv and the frontend's node_modules.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
committed="$root/frontend/src/lib/api-types.ts"
check=false
case "${1:-}" in
  "") ;;
  --check) check=true ;;
  *)
    echo "usage: $0 [--check]" >&2
    exit 2
    ;;
esac

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

uv run --no-sync --project "$root/backend" python -m photo_triage.api.openapi >"$tmp/openapi.json"
cd "$root/frontend"
pnpm exec openapi-typescript "$tmp/openapi.json" --output "$tmp/raw.ts" >/dev/null
# Formatted like the rest of the frontend, so prettier --check passes on it.
pnpm exec prettier --stdin-filepath src/lib/api-types.ts <"$tmp/raw.ts" >"$tmp/api-types.ts"

if ! $check; then
  cp "$tmp/api-types.ts" "$committed"
  echo "Wrote frontend/src/lib/api-types.ts"
elif diff -u "$committed" "$tmp/api-types.ts"; then
  echo "frontend/src/lib/api-types.ts matches the API"
else
  echo "frontend/src/lib/api-types.ts is out of date with the API. Run scripts/api_types.sh and commit the result." >&2
  exit 1
fi
