#!/usr/bin/env bash
# Run pnpm in frontend/ with the mise-pinned Node and pnpm, even where mise
# isn't activated (git hooks started from an editor). Uses the node_modules
# that `pnpm install` last installed, which `just doctor` checks against
# pnpm-lock.yaml.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../frontend"
mise="$(command -v mise || echo "$HOME/.local/bin/mise")"
exec "$mise" exec -- pnpm "$@"
