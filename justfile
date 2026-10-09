# Task runner (docs/dev-environment.md §5). `just` lists recipes.
# The other app recipes (stack, ...) arrive with the rest of the Phase 1
# scaffold.

# List recipes
default:
    @just --list --unsorted

# Verify the dev environment (tool versions, lockfiles, weights, Docker, mounts)
doctor:
    ./scripts/bootstrap.sh --check

# Migrate, then the API, worker and Vite dev server together; Ctrl-C stops all three. Extra arguments go to Vite
dev *args:
    scripts/dev.sh {{args}}

# API on APP_PORT with reload. Runs from the repo root so `.env` and `./data` resolve here.
api *args:
    uv run --no-sync --project backend python -m photo_triage.api --reload {{args}}

# Worker, from the repo root like `just api`. `--once` drains the queue and exits; `noop N` queues N noop jobs
worker *args:
    uv run --no-sync --project backend python -m photo_triage.worker {{args}}

# Vite dev server, proxying /api to the API on APP_PORT (run `just api` alongside)
[working-directory: 'frontend']
web *args:
    pnpm exec vite {{args}}

# A scratch copy of the app on free ports, with empty photo, trash and data dirs and no .env; Ctrl-C stops it and deletes them
preview:
    scripts/scratch_stack.sh --state .screenshots/preview.json

# Screenshots of a page at desktop and phone widths into .screenshots/ (`just shot --help`). Uses a running preview, or a scratch stack of its own
[positional-arguments]  # so a selector with spaces stays one argument
shot *args:
    scripts/frontend-run.sh exec node scripts/screenshot.ts "$@"

# Drop and re-migrate the database in DATA_DIR (from `.env`, like `just api`)
db-reset:
    uv run --no-sync --project backend alembic -c backend/alembic.ini downgrade base
    uv run --no-sync --project backend alembic -c backend/alembic.ini upgrade head

# Install backend dependencies exactly as locked
[working-directory: 'backend']
sync:
    uv sync --locked

# Format and auto-fix lint where possible
[working-directory: 'backend']
fmt:
    uv run --no-sync ruff format . ../scripts
    uv run --no-sync ruff check --fix . ../scripts

# Formatting, lint, file-mutation guard, import contracts
[working-directory: 'backend']
lint:
    uv run --no-sync ruff format --check . ../scripts
    uv run --no-sync ruff check . ../scripts
    python3 ../scripts/check_file_mutation.py
    uv run --no-sync lint-imports

# pyright (strict for src/, basic for tests/ and scripts/)
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

# Install frontend dependencies exactly as locked
[working-directory: 'frontend']
web-sync:
    pnpm install --frozen-lockfile

# Format and auto-fix frontend lint where possible
[working-directory: 'frontend']
web-fmt:
    pnpm exec prettier --write .
    pnpm exec eslint --fix .

# Frontend: svelte-check (strict), eslint, prettier, Vitest, production build
[working-directory: 'frontend']
web-check:
    pnpm run check
    pnpm run lint
    pnpm run format:check
    pnpm run test
    pnpm run build

# Regenerate the frontend's API types from the backend's OpenAPI schema
api-types:
    scripts/api_types.sh

# Fail if the committed API types are out of date (as the CI contract job does)
contract:
    scripts/api_types.sh --check

# Lint the Dockerfile (as pre-commit does), build the image and smoke-test it (as the CI docker job does)
image tag="photo-triage:dev":
    scripts/mise-run.sh hadolint docker/Dockerfile
    docker build $(scripts/image_pins.sh --build-arg) -f docker/Dockerfile -t {{tag}} .
    scripts/docker_smoke.sh {{tag}}

# Every pre-commit hook against every file
pre-commit:
    pre-commit run --all-files

# Everything CI runs on a PR (except audit, which needs the network)
check: pre-commit lint typecheck test web-check contract
