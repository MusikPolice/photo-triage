# Task runner (docs/dev-environment.md §5). `just` lists recipes.
# Recipes for the app itself (dev, api, worker, web, stack, ...) arrive with the
# Phase 1 scaffold.

# List recipes
default:
    @just --list --unsorted

# Verify the dev environment (tool versions, lockfiles, weights, Docker, mounts)
doctor:
    ./scripts/bootstrap.sh --check

# Install backend dependencies exactly as locked
[working-directory: 'backend']
sync:
    uv sync --locked

# Format and auto-fix lint where possible
[working-directory: 'backend']
fmt:
    uv run --no-sync ruff format .
    uv run --no-sync ruff check --fix .

# Formatting, lint, file-mutation guard, import contracts
[working-directory: 'backend']
lint:
    uv run --no-sync ruff format --check .
    uv run --no-sync ruff check .
    python3 ../scripts/check_file_mutation.py
    uv run --no-sync lint-imports

# pyright (strict for src/, basic for tests/)
[working-directory: 'backend']
typecheck:
    uv run --no-sync pyright

# Unit, integration, and safety tests with coverage gates (fake ML; fast)
[working-directory: 'backend']
test *args:
    uv run --no-sync pytest --cov {{args}}

# Tests that run the real ML models on cached weights (slow)
[working-directory: 'backend']
test-models *args:
    uv run --no-sync pytest -m models {{args}}

# Known vulnerabilities in runtime dependencies
[working-directory: 'backend']
audit:
    #!/usr/bin/env bash
    set -euo pipefail
    req="$(mktemp)"; trap 'rm -f "$req"' EXIT
    uv export --locked --no-dev --no-group export --no-emit-project --format requirements-txt -o "$req" >/dev/null
    uv run --no-sync pip-audit --disable-pip --require-hashes -r "$req"

# Every pre-commit hook against every file
pre-commit:
    pre-commit run --all-files

# Everything CI runs on a PR (except audit, which needs the network)
check: pre-commit lint typecheck test
