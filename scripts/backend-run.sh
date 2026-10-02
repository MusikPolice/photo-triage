#!/usr/bin/env bash
# Run a command in backend/'s uv environment with the mise-pinned tools, even
# where mise isn't activated (git hooks started from an editor, CI).
# Uses --no-sync: the environment is whatever `uv sync` last installed, which
# `just doctor` checks against uv.lock.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../backend"
mise="$(command -v mise || echo "$HOME/.local/bin/mise")"
exec "$mise" exec -- uv run --no-sync "$@"
