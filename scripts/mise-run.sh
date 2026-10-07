#!/usr/bin/env bash
# Run a mise-pinned tool (shellcheck, hadolint, ...), even where mise isn't
# activated (git hooks started from an editor, CI).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mise="$(command -v mise || echo "$HOME/.local/bin/mise")"
exec "$mise" exec -- "$@"
