# Development Environment

How photo-triage is built, run, and verified. Companion to [plan.md](plan.md).

## 1. Where development happens

| Concern | Decision |
|---|---|
| Runtime | **WSL Ubuntu 24.04** on the Windows dev machine — Linux, like the NUC, so ML wheels, `exiftool`, and `ffmpeg` behave as in production. |
| Repo location | Inside the WSL filesystem: `~/src/photo-triage` (not `/mnt/c/...`, which is slow for `node_modules` and pytest). Claude Code and the editor (VS Code Remote-WSL) run inside WSL. |
| Photo library | The real library is an SMB share (`\\192.168.2.21\pictures`, `P:\` on Windows). In WSL it is mounted **read-only** at `/mnt/pictures` — the same path it has on the NUC. |
| Hosted CI | GitHub Actions on `ubuntu-24.04` runners (`MusikPolice/photo-triage`). |

### Mounting the library in WSL (read-only)

```
# /etc/fstab
//192.168.2.21/pictures  /mnt/pictures  cifs  ro,credentials=/etc/smb-pictures.cred,uid=1000,gid=1000,iocharset=utf8,noserverino,_netdev,nofail  0  0
```

The curated sample on the Windows host is exposed the same way:

```
# /etc/fstab
C:\134Users\134jonfr\134Pictures  /mnt/sample-pictures  drvfs  ro,noatime,uid=1000,gid=1000  0  0
```

`ro` is deliberate: during development, nothing can modify real photos at the OS level, regardless of bugs or config mistakes.

## 2. Toolchain

All versions are pinned, and each pin is written once. `mise.toml` pins the language runtimes and CLI tools. `versions.env` (plain `KEY=value` lines at the repo root) pins the rest: exiftool and ffmpeg with their SHA-256s, and pre-commit. Lockfiles pin libraries. Everything else reads those files:

- `scripts/bootstrap.sh` sources `versions.env`, and `mise install` reads `mise.toml`.
- The CI backend job loads `versions.env` into its environment. Its tools come from `mise.toml` through mise-action.
- `docker/Dockerfile` takes every version as a build arg with no default: Python, uv, Node and pnpm from `mise.toml`, and exiftool and ffmpeg from `versions.env`. `scripts/image_pins.sh` prints them, for `just image` and the CI docker job. A plain `docker build` without them fails, naming the missing arg.
- `scripts/docker_smoke.sh` checks the image's exiftool and ffmpeg against `versions.env`.

So a bump is a one-line edit (plus the checksum, for exiftool and ffmpeg). `just doctor` verifies a local environment matches.

The model weights' checksums live in `scripts/bootstrap.sh`, their only user. `frontend/package.json`'s `packageManager` still repeats the pnpm version from `mise.toml`.

| Tool | Version | Pinned in | Purpose |
|---|---|---|---|
| mise | latest | — (installed once) | Installs/activates the tools below per-directory |
| Python | 3.13.x | `mise.toml` | Backend. Not 3.14: ML wheels (onnxruntime, numba/UMAP, InsightFace) lag new releases. The Phase 1 dependency spike confirmed every ML library below works on 3.13 (§10). |
| uv | 0.10.x | `mise.toml` | Python deps, venv, lockfile (`uv.lock`) |
| Node.js | 24.x LTS | `mise.toml` | Frontend tooling |
| pnpm | 10.x | `mise.toml` + `packageManager` | Frontend deps, lockfile (`pnpm-lock.yaml`) |
| just | 1.x | `mise.toml` | Task runner (`justfile`) |
| shellcheck | 0.11.x | `mise.toml` | Lints the shell scripts (pre-commit) |
| hadolint | 2.x | `mise.toml` | Lints `docker/Dockerfile` (pre-commit, `just image`) |
| pre-commit | 4.x | `versions.env` (installed with `uv tool install`) | Git hooks and their checks (§8) |
| exiftool | 13.x | `versions.env`: one tarball, by version and checksum, for the bootstrap and the image (from SourceForge, which keeps old releases) | Metadata read/write. Ubuntu's apt version is too old for reliable HEIC/MWG writes. |
| ffmpeg | 7.0.x | `versions.env`: one static build, by version and checksum, so dev, CI and the image run one binary. Ubuntu 24.04's apt version is 6.1. Debian trixie's (7.1) was the plan for the image, but an apt version pin breaks whenever Debian ships a security update, and it wouldn't match dev. The image's smoke test checks both tools against `versions.env`. | Video frames, posters |
| Playwright + headless Chromium | 1.x | `frontend/pnpm-lock.yaml` (`@playwright/test`); the bootstrap installs the matching Chromium headless shell into `~/.cache/ms-playwright`, and its apt libraries | `just shot` and `just preview` now; e2e later (§6) |
| Docker Engine + Compose | 29.x / v2 plugin | Docker Desktop (WSL integration) | Integration stack, e2e |
| Ollama | pinned image tag | `docker/ollama.yaml`, which `compose.yaml` and `compose.dev.yaml` extend | LLM tagging service |

Libraries of note (exact versions live in the lockfiles): FastAPI, SQLAlchemy 2, Alembic, Pydantic 2, onnxruntime, open_clip (export to ONNX), insightface, umap-learn, scikit-learn (including its HDBSCAN), Pillow + pillow-heif, OpenCV (headless); Svelte 5, Vite, TypeScript, deck.gl.

TypeScript is pinned to 6.0 (`~6.0` in `frontend/package.json`). TypeScript 7 is out, but svelte-check and typescript-eslint don't support it yet.

`backend/pyproject.toml` has three dependency sets:

- **Runtime** (`[project.dependencies]`): what the app and worker import. This set, and no group, goes into the Docker image.
- **`dev`** group: pytest, Hypothesis, ruff, pyright, import-linter, pip-audit.
- **`export`** group: open_clip, torch, torchvision, onnx, onnxscript. These are only for the one-off CLIP→ONNX export (`scripts/export_clip_onnx.py`) and stay out of the image (`uv sync --no-group export`).

insightface requires `opencv-python`, the GUI build of OpenCV, which installs its own `cv2` over `opencv-python-headless` and needs X11 libraries. An `override-dependencies` entry in `pyproject.toml` leaves it out. Removing the GUI build from an older `.venv` also removes the `cv2` folder the two share. `just doctor` reports that, and `scripts/bootstrap.sh` (or `uv sync --reinstall-package opencv-python-headless`) fixes it.

Both groups are `default-groups`, so a plain `uv sync` installs everything locally, which comes to about 2 GB. torch and torchvision come from the PyTorch CPU-only index, and both must be listed directly, because uv applies an index source only to direct dependencies. A torchvision pulled from PyPI fails at import with `operator torchvision::nms does not exist`.

### Bootstrap (fresh WSL)

`scripts/bootstrap.sh` — idempotent, and assumes nothing on a fresh Ubuntu 24.04 but git: installs apt prerequisites (curl, build-essential, cifs-utils; no libheif, since the pillow-heif wheel bundles its own), mise (plus `~/.bashrc` activation), and the pinned exiftool, ffmpeg, and pre-commit; runs `mise install`, `uv sync`, `pnpm install`, `pre-commit install`; installs Playwright's headless Chromium and, through `playwright install-deps` (which uses sudo), the apt libraries it needs; downloads model weights to `~/.cache/photo-triage/models`. Downloads are verified against pinned SHA-256 checksums. It also checks what it can't install: Docker is reachable, and the photo mounts are present and read-only.

### Verifying the environment: `--check` and `just doctor`

`scripts/bootstrap.sh --check` runs the same detection as a bootstrap but changes nothing: no sudo, no network. It prints each problem with its fix and exits 1 if anything needs fixing. That includes a missing or wrong-version tool, missing weights, a `uv.lock` out of date with `pyproject.toml`, a `.venv` or `node_modules` out of date with its lockfile, a backend package with native code that doesn't import (`cv2`, `onnxruntime`, `pillow_heif`, `insightface`; a bootstrap reinstalls it), missing git hooks, Playwright's Chromium or its system libraries missing (found with `ldd`), Docker unreachable, or `/mnt/sample-pictures` absent. A photo mount that is **read-write** is always a failure. Steps whose project files don't exist yet (e.g. `backend/pyproject.toml` before Phase 1) are warnings, not failures.

`just doctor` runs `scripts/bootstrap.sh --check`, followed by `scripts/check_env.py`, the app-level checks that need `.env`: it exists, its settings are valid, `PHOTO_DIR` isn't writable when it's in `/mnt/pictures`, and the database is at the head revision (a warning). The tool and version checks live only in the bootstrap script, so installing and verifying can't drift apart.

### Claude Code session check

`.claude/settings.json` (committed, so every clone gets it) has a `SessionStart` hook that runs `scripts/bootstrap.sh --check --quiet` when a Claude Code session starts, resumes, or is cleared. It takes about 2 s. `--quiet` prints nothing when the environment is OK and only the problems list when it isn't. Each problem carries its own fix, for example:

```
Dev environment problems (from scripts/bootstrap.sh --check):
  - Docker is running, but this session isn't in the 'docker' group yet. Run 'wsl --shutdown' from Windows and reopen Ubuntu (or 'newgrp docker' for one shell).
```

That output lands in Claude's context, so the session starts already knowing that, say, Docker Desktop isn't running or the SMB share didn't mount after a reboot, instead of discovering it mid-task. The hook command ends in `|| true` because Claude Code only adds a SessionStart hook's output to context when it exits 0. The hook only reports; fixing is left to the developer, or to Claude when asked.

## 3. Repository layout

```
photo-triage/
  mise.toml  versions.env  justfile  compose.yaml  compose.dev.yaml  .env.example
  backend/
    pyproject.toml  uv.lock  alembic.ini
    src/photo_triage/
      api/          FastAPI routers, current_actor dependency
      worker/       job queue, scheduler, stage runners
      pipeline/     scan, thumbnails, clip, quality, faces, tagging, layout, dupes
      ml/           pluggable adapters (embedder, face detector, tagger) + fakes
      files/        THE ONLY module allowed to mutate files: trash, restore, purge, exif writes, log rotation, backup pruning, the worker's lock file
      logs.py       logging setup shared by every process (§5 "Logging")
      db/           engine, models, migrations/ (Alembic), the migrate step, repositories
    tests/
      unit/  integration/  models/  safety/
      fixtures/synthetic/        committed, generated
  frontend/
    package.json  pnpm-lock.yaml  src/  tests/  e2e/
    scripts/      screenshot.ts and shot-options.ts (`just shot`)
  docker/Dockerfile  ollama.yaml (the Ollama service both Compose files extend)  .dockerignore (an allowlist: only what the build copies)
  scripts/        bootstrap.sh, tracker.py (GitHub issues/PRs), check_file_mutation.py, docker_smoke.sh, image_pins.sh, api_types.sh, dev.sh, stack.sh, check_env.py (`just doctor`), host_report.sh (sizing the stack's caps), scratch_stack.sh, mise-run.sh, ...
  .claude/skills/ plan-phase, work-issue, new-issue, grill-me (see CLAUDE.md)
  docs/
```

## 4. Test data tiers

Automated tests **never** touch the real library.

| Tier | What | Where | Used by |
|---|---|---|---|
| 1. Synthetic | ~30 generated files: JPEG/HEIC/MOV/MP4; a burst set (small shifts); blurred/over/underexposed variants; a screenshot-like image; files with pre-existing keywords, MWG regions, and odd EXIF (missing dates, bad orientation); a corrupt file. Faces from openly licensed stock images. | `backend/tests/fixtures/synthetic/`, committed; regenerated deterministically by `scripts/make_synthetic_fixtures.py` | Unit, integration, safety, e2e, CI |
| 2. Curated sample | Existing ~680-file curated subset on the Windows host (`C:\Users\jonfr\Pictures`, 3.1 GB): 666 JPEG, 1 HEIC, plus a few PNG/BMP (unsupported types — useful for testing that the scanner skips them cleanly). **No videos yet** — add a handful of MOV/MP4 from the library before video work (Phase 2). | Bind-mounted **read-only** in WSL at `/mnt/sample-pictures`; lives outside the repo and is never committed (contains family photos) | Local dev, model-tier tests, threshold calibration |
| 3. Full library | Read-only mount at `/mnt/pictures` | — | Occasional full-scale dry runs (scan speed, UMAP at 100k, memory), with `EXIF_WRITES_ENABLED=false` and trash pointed at a scratch dir |

Because tiers 2 and 3 are read-only, anything that needs to write (trash, EXIF write-back) runs against a throwaway copy: `just dev` can seed `~/photo-triage-data/scratch/` from `/mnt/sample-pictures` (`just seed-scratch`) and point `PHOTO_DIR` there when testing mutations.

## 5. Running each piece

| Command | What it runs |
|---|---|
| `just dev` | `scripts/dev.sh`: runs the migrate step (`python -m photo_triage.db migrate`, as `just db-migrate`), then the API (`--reload`), the worker, the Vite dev server and Ollama, in one terminal, each line prefixed `api`, `worker`, `web` or `ollama`. The first three run natively in WSL. Ollama runs in Docker from `compose.dev.yaml`: the production stack's service and pinned image (`docker/ollama.yaml`) on `localhost:11434`, with its models in the `photo-triage-dev_ollama` volume. Its first start downloads the image, several GB. Before starting anything, `just dev` checks that Docker answers, and if it doesn't, it exits saying why (no `docker` command, not in the `docker` group, or Docker Desktop not running) and what to do. `just dev --no-ollama` leaves Ollama out, and skips that check; the scratch stack does this. Open the URL Vite prints (`http://localhost:5173`, which Windows browsers reach through WSL's localhost forwarding). The worker runs under `watchfiles`, which restarts it after its current job when backend code changes. Ctrl-C stops them all, the worker after its current job and Ollama's container through Compose, and if one exits the others are stopped. Extra arguments go to Vite, e.g. `just dev --host` to try it from a phone. Uses `.env` → `PHOTO_DIR=/mnt/sample-pictures` (read-only), local `data/` and `trash/` dirs. The API and worker each log a warning at startup that `TRASH_DIR` is on a different filesystem from `PHOTO_DIR`: expected in dev, where nothing is trashed (plan §6.8). To watch the Activity page at work, set `WORKER_NOOP_STAGE=true` in `.env` and run `just worker noop 500` in a second terminal. The migrate step backs the database up before applying a new migration, so after testing a branch that adds one, its revision stays in the dev database: back on a branch without it, `just dev` stops and says to downgrade from that branch, restore the backup from `DATA_DIR/backups`, or delete `photo-triage.db` in `DATA_DIR`. |
| `just api` | The API alone: `python -m photo_triage.api --reload`, on `APP_PORT`, from the repo root so `.env` and `./data` resolve there. Exits with a list of what's wrong if `PHOTO_DIR` is missing, or `TRASH_DIR` is `PHOTO_DIR` or contains it. `TRASH_DIR` defaults to `PHOTO_DIR/.photo-triage-trash`. Exits 1 without serving if the database isn't at the head revision, saying to run `just db-migrate`, or that the database is newer than the code: the API never migrates. API docs at `/api/docs`. `curl -N localhost:$APP_PORT/api/events` shows the Activity event stream. On a stop or reload, the API ends open event streams so it doesn't wait for them, and browsers reconnect. |
| `just web` | The Vite dev server for `frontend/`, with hot reload. It proxies `/api` to the API on `APP_PORT`, which it reads like the backend does: the environment first, then the repo-root `.env`, then 8000. Run `just api` alongside. Extra arguments go to Vite, e.g. `just web --host` to reach it from another device. |
| `just web-sync` / `just web-fmt` / `just web-check` | Install the frontend dependencies as locked; format and auto-fix lint; run svelte-check, eslint, prettier, Vitest and `vite build` (part of `just check`). |
| `just api-types` / `just contract` | Regenerate `frontend/src/lib/api-types.ts` from the API's OpenAPI schema (`scripts/api_types.sh`: `python -m photo_triage.api.openapi`, then `openapi-typescript` and prettier); fail if the committed file is out of date (part of `just check`, and the CI `contract` job). Run `just api-types` after changing an API model or route, and commit the result. The frontend takes its API types from that file (`src/lib/api.ts`). |
| `just worker` | The worker alone: `python -m photo_triage.worker`, from the repo root like `just api`. At startup it exits 1 if the database isn't at the head revision, as `just api` does, without claiming a job. Only one worker runs per `DATA_DIR`: it holds an `flock` on `DATA_DIR/worker.lock` while it runs, and a second one (`just worker` alongside `just dev` or the stack) exits 1 saying a worker is already running, before it touches the queue or `worker.log`. The kernel drops the lock when the process dies, even by SIGKILL, so a crash leaves no stale lock; the file itself stays. `noop`, `pause` and `resume` don't take it. Then it counts any job left `running` when it last stopped as a failed attempt: the job goes back to its place in the queue, or is parked after 5 attempts. Then it runs jobs in priority order until SIGINT or SIGTERM, finishing the job in progress first (a second signal stops at once). `just worker --once` runs every ready job and exits; jobs waiting out a retry backoff are left for later. Stages without a runner (all of them until Phase 2) stay in the queue. Before each claim it checks `worker_controls` and quiet hours, and logs at `INFO` when its state changes (`Worker paused`, `Quiet hours until …`, `Worker running; paused stages: …`). While it runs, a thread writes a heartbeat to `worker_heartbeat` every 5 s, and a clean stop records `stopped_at`, so the API can say when the worker isn't running. `just worker pause [STAGE]` and `just worker resume [STAGE]` pause and resume the whole worker, or one stage, as the Activity page does: a running worker picks the change up before its next claim, and a job already running finishes. They record your user name as the actor (`--actor NAME` to override) and log the resulting state. |
| `just image` | Lint `docker/Dockerfile` with hadolint (as pre-commit does), then build the image (`photo-triage:dev`, or `just image TAG`) with the pins from `scripts/image_pins.sh` (§2) and run `scripts/docker_smoke.sh` on it, as the CI `docker` job does. One image runs both: the API by default (`python -m photo_triage.api --host 0.0.0.0` on port 8000, as UID 1000, with `PHOTO_DIR=/photos` and `DATA_DIR=/data`; `TRASH_DIR` keeps its default, `/photos/.photo-triage-trash`, on the library's filesystem), and the worker with the command `python -m photo_triage.worker`, after the migrate step `python -m photo_triage.db migrate`. The smoke test also runs `compose.yaml`'s migrate, app and worker services on the image. FastAPI serves the API under `/api` and the built frontend at `/`, answering any other path without a file extension (a page such as `/activity`) with its `index.html`; unknown `/api` paths and missing files are still 404s. `just api` serves `frontend/dist` too, if you've run `vite build`. |
| `just stack` | `scripts/stack.sh`: the production stack, `compose.yaml`, on an image built from the checkout (`photo-triage:dev`, as `just image` builds it, without the lint and smoke test), against the test folder `.stack/` (gitignored). The Compose project is `photo-triage-stack`, so it can't touch a real deployment. `.stack/photos` is the library, with the trash inside it, and `.stack/data` is `DATA_DIR`, both bind-mounted. The containers read `.stack/app.env` in place of `.env`, created with `WORKER_NOOP_STAGE=true`; edit it to try other settings. The UI is on `APP_PORT`, from the environment or `.env`, so stop `just dev` first. Other arguments go to `docker compose` on the stack: `just stack up -d`, `just stack exec worker python -m photo_triage.worker noop 50` to queue jobs, `just stack restart worker`, `just stack logs -f`, `just stack down`. Deleting `.stack/data` starts it from an empty database. The library is empty until tier-1 fixtures exist (`just fixtures`). See "The Compose stack" below. |
| `just dry-run-full` | Stack against `/mnt/pictures` (read-only), writes disabled — for scale testing |
| `just preview` | `scripts/scratch_stack.sh`: a throwaway copy of the app for looking at pages. It runs `scripts/dev.sh` on two free ports against empty photo, trash and data directories in a new temporary folder, with `ENV_FILE=/dev/null` so it reads no `.env`: it never sees the developer's photo library or dev database, and doesn't touch a `just dev` that's running. The noop stage is on. Once the page answers it prints `Scratch stack ready: URL` and writes `.screenshots/preview.json` (its URL and environment) for `just shot`. Ctrl-C stops it and deletes the folder and that file. |
| `just shot PATH` | `frontend/scripts/screenshot.ts`: full-page screenshots of `PATH` in headless Chromium, at desktop (1280×800) and phone (390×844, touch, 2× pixels) widths, saved as `NAME-desktop.png` and `NAME-phone.png` and printed. It uses a running `just preview`, or else starts a scratch stack for this shot alone (about 5 s) and stops it afterwards, so it needs nothing running. `--noop N` queues noop jobs first, `--wait SELECTOR` waits for an element, `--click SELECTOR` (repeatable) clicks first, `--viewport desktop\|phone` takes one, `--name` names the files. Scratch-stack shots go to `.screenshots/scratch/`, which `tracker.py finish` can publish (§9). `--base URL` shoots a server that's already running, such as your `just dev`, into `.screenshots/local/`, which it won't publish, because that server can show real photos. `just shot --help` lists the options. |
| `just db-migrate` | The migrate step: `python -m photo_triage.db migrate`, which upgrades the database in `DATA_DIR` to head, creating it if there's none. The API and worker never migrate; they check the revision at startup and exit if it isn't head, so this runs first. In the Compose stack it's the one-shot `migrate` service, which the app and worker wait for. An existing database that needs upgrading is first copied with SQLite's `VACUUM INTO` to `DATA_DIR/backups/<UTC timestamp>-from-<revision>.db`, e.g. `20261008T142305Z-from-0003.db`, and only the 5 newest backups are kept. A database newer than the code is left alone, and the step exits 1. Logs to `migrate.log`, including Alembic's lines. **Restoring a backup** is done by hand: stop the API and worker, delete `photo-triage.db-wal` and `photo-triage.db-shm` in `DATA_DIR` if they exist, copy the backup over `photo-triage.db`, and run the code whose newest migration is the revision in the backup's name. |
| `just db-reset` | Drop and re-migrate the dev database in `DATA_DIR`: Alembic downgrade to empty, then upgrade to head. For other Alembic commands, run `uv run --project backend alembic -c backend/alembic.ini …` from the repo root. A new migration starts from `revision --autogenerate`; read it before committing, since the tests fail if models and migrations disagree. |
| `just fixtures` | Regenerate tier-1 synthetic fixtures |
| `just doctor` | `scripts/bootstrap.sh --check` (tool versions, lockfile sync, backend packages with native code import, model weights, Docker, mounts read-only), then `scripts/check_env.py`: `.env` exists (or the file `ENV_FILE` names), the settings it gives are valid, `PHOTO_DIR` isn't writable when it's in `/mnt/pictures` (§1), and a warning if the database isn't at the head revision. Exits 1 if either part fails (§2). |

Worker dev affordances, read from `.env` or the environment like the settings in plan §9 but not listed there:

- `WORKER_QUIET_HOURS` unset (always on). To try quiet hours, set it with `FAKE_NOW` and `TZ`: `just worker --once` then runs nothing and logs `Quiet hours until …`.
- `--once` to drain the queue and exit.
- **`FAKE_NOW`**, a controllable clock for exercising quiet hours and ETA logic. It needs a UTC offset (`FAKE_NOW=2026-10-03T21:59:00-04:00`). The worker's clock starts there and advances in real time. It sets `*_at` columns and backoff; durations in `job_stats` always come from a real monotonic timer. The API reads it too, so `GET /api/activity` shows the same quiet hours as the worker (each process starts its clock when it starts, so the two differ by the time between their starts). The worker heartbeat always uses real time.
- **`ENV_FILE`** names the file read in place of `.env`. The scratch stack sets it to `/dev/null`, so a developer's `FAKE_NOW`, quiet hours or paths can't reach it.
- **The `noop` stage**, off unless `WORKER_NOOP_STAGE=true`. `just worker noop 500` queues 500 jobs that each take 0.2 s, to watch the worker and the Activity page at work. Unlike other stages, every finished noop job is kept, so the page counts them; `just db-reset` clears them. It logs to stderr only, since a running worker owns `worker.log`.

`WORKER_THREADS` sets `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS` and `NUMBA_NUM_THREADS` when the worker starts, before any ML library loads.

### The Compose stack

`compose.yaml` is the production stack (plan §3, §4). `just stack` runs it in dev; on the server it's `docker compose up -d` in a folder holding `compose.yaml`, `docker/ollama.yaml` and a `.env`.

- **Services.** `migrate` runs the migrate step and exits; `app` and `worker` start only once it has succeeded (`service_completed_successfully`), so an upgrade is `docker compose up -d` with the new image, and the database is backed up before any migration. `app` serves the UI and API on host port `APP_PORT`, with a healthcheck on `/api/health`. `worker` gets 60 s to finish its job on a stop (`stop_grace_period`). A second worker, e.g. `docker compose run worker`, exits at once (`just worker`). `ollama` is defined in `docker/ollama.yaml`, which `compose.dev.yaml` shares, so its pinned tag is written once (§2).
- **`.env`**, next to `compose.yaml`, is read twice. Compose takes the host side from it: `PHOTO_DIR` and `DATA_DIR` are the host folders bind-mounted at `/photos` and `/data`, and `APP_PORT` is the host port. The app containers also get it as their environment, for the rest of plan §9, with the container paths set over it. `TRASH_DIR` is always the default, `/photos/.photo-triage-trash`, so the trash is inside the library's mount and trashing is a rename (plan §6.8); there's no trash mount. `APP_ENV_FILE` names another file for the containers, as `just stack` does.
- **Host folders.** `PHOTO_DIR` and `DATA_DIR` must exist and be writable by UID 1000, the image's user (`sudo chown 1000:1000 …`). Compose doesn't create them (`create_host_path: false`), since Docker would make them owned by root. `DATA_DIR` holds the database, its backups and the logs: back that folder up. Ollama's models are in the named volume `photo-triage_ollama`.
- **Caps** (plan §6.11), so the media server's other services keep running: app 1 CPU and 1 GB, worker 4 CPUs and 6 GB, Ollama 4 CPUs and 4 GB. They're first guesses, to revisit once Phase 2 measures real work; change them in a `compose.override.yaml` on the server. To size them, copy `scripts/host_report.sh` to the server and run `bash host_report.sh [MINUTES]` there, once when it's busiest and once when it's quiet. It only reads: the CPU, memory, swap and out-of-memory kills, disks, the other containers' limits and usage, the top processes, `sar` history if sysstat is installed, and `vmstat` and `docker stats` sampled over MINUTES (default 10). It writes `~/host-report-<host>-<time>.txt`. `WORKER_THREADS` sets the worker's threads within its cap.
- **Logs.** Docker's copy of each container's stderr is capped with the `json-file` driver at 10 MB × 5 files. The app's own rotated logs are in `DATA_DIR/logs` ("Logging" below).
- **The image** is `PHOTO_TRIAGE_IMAGE`, by default `photo-triage:dev`, which `just image` builds. There's no registry yet, so on the server it's built from a checkout with `docker build $(scripts/image_pins.sh --build-arg) -f docker/Dockerfile -t photo-triage:dev .`, which needs only Docker.

### Logging

Logs are for the operator finding out why something happened. The UI reads errors, progress and history from the database (plan §7), not from the logs. Every process calls `photo_triage.logs.configure(settings, process)` at startup, which sends one plain-text line per event to stderr and to `DATA_DIR/logs/<process>.log` (`api.log`, `worker.log`, `migrate.log`):

```
2026-10-03T22:14:05.123Z WARNING photo_triage.worker message
```

- **Timestamps are UTC with `Z`**, whatever the process's timezone, so they compare directly with the `*_at` columns. A traceback follows its line.
- **`LOG_LEVEL`** (default `INFO`; `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`, any case) sets the level for every process. Set it in `.env` or the environment and restart the process.
- **Files** rotate at 10 MB and keep 5 old ones (`api.log.1` … `api.log.5`), so a process uses at most about 60 MB. Each process has its own file, because Python's rotating handler isn't safe with several writers. With `just api` (`--reload`), only the serving child process writes `api.log`; the reloader's few lines go to stderr. In Docker, the stderr copy is capped by Compose's log options (10 MB × 5 per container).
- **Libraries** (uvicorn, SQLAlchemy, Alembic) log through the same handlers, format and level. Uvicorn's access log and SQLAlchemy's SQL statements appear only at `DEBUG`. SQLAlchemy's other loggers, such as the ORM's setup lines, are held at `WARNING`.
- **Unhandled errors in requests** are logged at `ERROR` with their traceback by `photo_triage.api.errors`, and the client gets a 500.

Use these levels, so `INFO` stays readable through a first pass of hundreds of thousands of jobs:

| Level | For |
|---|---|
| `DEBUG` | One line per job and per request; SQL statements. Off by default. |
| `INFO` | Startup, with the settings (secrets are `SecretStr` fields, so they're masked; `test_every_setting_is_public_or_secret` makes each new setting declare which it is); state changes such as pause/resume or a scan starting; batch jobs; periodic progress summaries. Never one line per item. |
| `WARNING` | A failure that will be retried, e.g. a job going to `error` with a backoff. |
| `ERROR` | A failure that needs a person: a job that's parked, or an unhandled error. Always with the traceback (`logger.exception`). |

Get a logger with `logging.getLogger(__name__)` (in a `__main__` module, name it, since `__name__` is `"__main__"` there) and pass values as arguments (`logger.info("scanned %d files", n)`), not f-strings. Alembic's CLI (`just db-reset`) keeps the logging set up in `alembic.ini`. The migrate step (`just db-migrate`) runs Alembic itself and tells `env.py` to skip that (`config.attributes["configure_logger"] = False`), so Alembic's lines reach `migrate.log` in this format.

## 6. Testing strategy

| Layer | Tooling | Approach |
|---|---|---|
| Pure logic | pytest (+ Hypothesis) | Quality scoring, union-find grouping, content hashing, ETA math against quiet hours, quiet-hours parsing, search score blending, FTS query building. Property-based where there are invariants. |
| Database & migrations | pytest + Alembic | Every migration upgrades from empty and downgrades; `alembic check` confirms models and migrations agree. Migrations have a single head and import only Alembic, SQLAlchemy and the standard library, never app code, so later code changes can't alter what an old migration does. |
| Metadata write-back | pytest + real exiftool | Round-trip each field mapping (plan §6.10) per container type on tier-1 copies in a temp dir; read back and assert. |
| Pipelines & worker | pytest + fake ML adapters | Run the worker `--once` over tier-1 fixtures with fake embedders/detectors (deterministic vectors derived from file hashes). Assert job states, stage ordering, resumability (kill mid-run, restart), retry/park behaviour. |
| ML adapters (model tier) | pytest `-m models` | Real CLIP / InsightFace / UMAP on fixtures with **tolerance** assertions: burst pair cosine > threshold, distinct scenes below it, expected face counts, text query ranks the right fixture first. Weights cached. Not in the default suite. |
| API | pytest + httpx `AsyncClient` | Endpoint behaviour, `current_actor` in both `AUTH_MODE`s, SSE progress stream. The in-process client waits for a whole response, so the event stream is read from uvicorn running in a thread (`test_events.py`). |
| Safety invariants | pytest `tests/safety/` | See §7 — these are the most important tests in the repo. |
| Frontend logic | Vitest | LOD thresholds, atlas UV math, search/filter state, ETA/progress formatting, API client. |
| Frontend types | svelte-check | Strict TypeScript across `.svelte` and `.ts`. |
| End-to-end | Playwright (Chromium; desktop + Pixel-class viewport) | Against `just stack` with tier-1 fixtures: search highlights matches, lightbox + neighbor walk, quality review keep/delete, duplicate pick, trash + restore, Activity page progress. The map is asserted through a test-only hook exposing deck.gl layer state (visible IDs, highlighted IDs, zoom), not pixels. |

Determinism: fixed seeds for UMAP (`random_state`) and any sampling; injected clock; fake adapters derive outputs from content hashes; tests never depend on wall-clock time or network (Ollama is faked except in the model tier).

## 7. Safety invariants

File mutation is confined to `photo_triage.files`. These are enforced both statically and by tests:

**Static (fail the build):**
- Ruff `flake8-tidy-imports` banned-API rules (TID251, in `backend/pyproject.toml`): `os.remove`, `os.unlink`, `os.rmdir`, `os.removedirs`, `os.rename`, `os.renames`, `os.replace`, `os.utime`, `shutil.move`, and `shutil.rmtree` are banned in `backend/src` **except** `photo_triage/files/`.
- Ruff can't resolve method calls on `Path` objects or the arguments to `subprocess`, so `scripts/check_file_mutation.py` covers those by syntax, with the same scope: `.unlink()`, `.rename()`, `.rmdir()`, one-argument `.replace()` (`Path.replace`; `str.replace` takes two), and any string literal naming the `exiftool` executable. It runs in pre-commit and `just lint`.
- `import-linter` contracts: `api` and `pipeline` may import `photo_triage.files` (its public interface) but none of its submodules; `ml` imports nothing from `db`/`api`.
- Tests are exempt from both, since they build and tear down fixture copies in temp dirs.
- Log rotation renames and deletes files, so the rotating handler (`RotatingLogHandler`) lives in `photo_triage.files` too. It refuses any log file outside `DATA_DIR/logs` (§5 "Logging").
- So does the worker's lock file (`exclusive_lock`), which `python -m photo_triage.worker` creates in `DATA_DIR` and holds with `flock`, so only one worker runs (§5, `just worker`). Creating it is the only mutation; it's never deleted.
- So does pruning the migrate step's database backups (`prune_backups`), which deletes all but the newest `*.db` files in `DATA_DIR/backups` (§5, `just db-migrate`). The backups themselves are written by SQLite's `VACUUM INTO`, which only creates a file.

**Tests (`tests/safety/`, run on every PR):**
- **Dry run is inert:** with `EXIF_WRITES_ENABLED=false`, run every pipeline and every UI action over a fixture copy; every file's bytes and mtime are identical afterwards.
- **No escape:** across a full worker run, no file is created, modified, or removed outside `PHOTO_DIR`, `TRASH_DIR`, and `DATA_DIR` (filesystem snapshot diff of the temp root).
- **Pixel preservation:** after metadata writes, every file's content hash (pixel data) is unchanged and mtime is preserved.
- **Trash round-trip:** trash → restore yields byte-identical files at their original paths; purge only removes items past retention.
- **Identity stability:** content hash is stable across metadata writes and moves; a moved file keeps its history.
- **Keep decisions survive DB loss:** delete the DB, rescan, and no reviewed item re-enters a queue.

## 8. Deterministic checks

### Pre-commit (local, seconds)

- `ruff format --check`, `ruff check`
- `shellcheck` on shell scripts (`.shellcheckrc` lets it follow `source versions.env`), `hadolint` on `docker/Dockerfile`
- `prettier --check`, `eslint`
- `uv lock --check`, pnpm lockfile consistency
- **Media guard:** reject any image/video file added outside `backend/tests/fixtures/synthetic/`; reject files > 1 MB — prevents family photos (tier 2) from ever being committed
- File-mutation guard (`scripts/check_file_mutation.py`, §7)
- `gitleaks` (secrets, e.g. SMB credentials)
- Trailing whitespace / EOF / merge-conflict markers; LF line endings enforced via `.gitattributes`

Config: `.pre-commit-config.yaml`. Ruff covers `backend/` and `scripts/`; prettier and eslint cover `frontend/`. The ruff and uv hooks run through `scripts/backend-run.sh`, which finds mise even when the shell hasn't activated it (commits from an editor), and uses the existing `.venv` without syncing. The frontend hooks do the same through `scripts/frontend-run.sh`, using the existing `node_modules`, and the shellcheck and hadolint hooks through `scripts/mise-run.sh`. The pnpm lockfile hook runs `pnpm install --frozen-lockfile --lockfile-only --offline`, which fails if `pnpm-lock.yaml` doesn't match `package.json` and changes nothing. `just pre-commit` runs every hook on every file; `just check` runs that plus lint, pyright, the tests, `just web-check` and `just contract` — everything CI runs except the audit.

### CI on every push / PR (GitHub Actions, ubuntu-24.04)

| Job | Checks |
|---|---|
| backend | loads `versions.env`; `uv sync --locked --no-group export`; pre-commit (all hooks except the frontend ones, so including shellcheck and hadolint); ruff; **pyright strict** (tests and `scripts/` at basic); import-linter; pytest unit + integration + safety with real exiftool/ffmpeg and fake ML; coverage gates (≥ 90% branch on `files/`, identity/move detection, and purge; ≥ 75% overall; `ml/` adapters exempt) |
| migrations | upgrade from empty → downgrade → upgrade; `alembic check`; on PRs, migrations already on `main` aren't modified, renamed or deleted (add a new one instead) |
| frontend | `pnpm install --frozen-lockfile`; svelte-check (strict, fails on warnings); eslint (typescript-eslint `strictTypeChecked`); prettier; Vitest; `vite build`. The backend job's pre-commit run skips the frontend hooks, since it has no `node_modules`. |
| contract | `scripts/api_types.sh --check` (`just contract`): export OpenAPI from the app, regenerate the TS types with `openapi-typescript`, and fail if they differ from the committed `frontend/src/lib/api-types.ts` |
| docker | build the image with the build args from `scripts/image_pins.sh` (layers cached in GitHub Actions); `scripts/docker_smoke.sh`: the container starts, `/api/health` is ok, `/` and `/activity` serve the frontend, its script is served, it runs as a non-root user, `exiftool -ver`, `ffmpeg -version` and `ffprobe -version` match the pins in `versions.env`, there's no Node toolchain and no `dev` or `export` packages, the runtime dependencies import, the app refuses an unmigrated database, the migrate step runs, and the app and then the worker (`--once`) run against the database it migrated; then `compose.yaml`'s migrate, app and worker services (not Ollama) on temporary bind-mounted folders: the app gets healthy after the migrate service, answers on `APP_PORT`, the database lands in the `DATA_DIR` mount, the trash is `/photos/.photo-triage-trash` with no filesystem warning, and a second worker exits saying one is already running |
| audit | `pip-audit` on the runtime dependencies (`just audit`; fails on any known vulnerability, since pip-audit has no severity filter), `pnpm audit --prod` — fail on high/critical |

### CI on `main` and nightly

- **Model tier:** `pytest -m models` with cached weights (actions/cache)
- **E2E:** `just stack` + Playwright, desktop and mobile viewports; traces uploaded on failure

## 9. Tracking work and merging

Work toward the spec is tracked on GitHub:

- Each phase in plan §11 is a **milestone**.
- Each deliverable is an **issue** small enough for one PR. An issue cites the spec sections it implements, lists checkable acceptance criteria, and says what's out of scope.
- Each issue gets its own **branch** and **PR**, and the PR closes the issue when merged.
- Every PR is reviewed and merged by a person. Claude Code opens PRs but never merges them.

`scripts/tracker.py` does the mechanical steps, so they happen the same way every time. Its docstring has the issue draft format.

| Command | Does |
|---|---|
| `setup` | Creates the phase milestones from the plan §11 headings, plus the area labels (`backend`, `frontend`, `infra`, `documentation`), `bug`, and `in-progress`. Every issue needs at least one area label. Running it again only adds what's missing. |
| `status` | Shows the current phase and its issues (in progress, ready, or blocked on an open dependency), plus open PRs. |
| `new DRAFT...` | Validates issue drafts: required sections, spec citations that match a heading, checklist criteria, labels and phase. Then files them in order. If any draft is invalid, it files nothing. Use `--dry-run` to validate without filing. |
| `start N` | Creates branch `N-slug` from `origin/main`, assigns the issue and labels it `in-progress`. |
| `finish SUMMARY` | Requires a clean tree that's up to date with `origin/main`. Runs `just check`, pushes, publishes any screenshots in the summary's `Screenshots` section (below), and opens or updates the PR, adding the issue's acceptance criteria and `Closes #N` to the body. |
| `feedback [N]` | Prints everything reviewers have said on PR N, or on the current branch's PR: its state and review decision, each review, the conversation, and each inline thread with its file, line and whether it's resolved or outdated. |

**Screenshots on PRs.** A PR that changes how a page looks attaches screenshots of it. Take them with `just shot` (scratch stack, not `--base`) and list them in the PR draft under `## Screenshots` as `![caption](.screenshots/scratch/NAME.png)`. `finish` checks each is a scratch-stack PNG, commits them to the `screenshots` branch under `pr-N/` (N is the issue number, as in `.drafts/pr-N.md`), replacing what was there, and embeds them in the PR body by commit URL (`raw.githubusercontent.com/<repo>/<commit>/pr-N/NAME.png`), so a re-run never shows a stale copy. The branch is an orphan that holds only screenshots, kept out of `main`'s history. It uses a scratch git index, so the working tree is untouched. The repo is public, which is why only scratch-stack screenshots, which never show real photos, can be published.

The Claude Code skills in `.claude/skills/` hold the decisions the script can't make:
- `grill-me` interviews the user one question at a time until a plan is fully agreed. It's a copy of a claude.ai skill, kept in the repo so every session can invoke it.
- `plan-phase` splits a phase into issues and gets approval before filing them.
- `new-issue` files a single issue, such as a PR follow-up, a bug, an idea or a spike. It first interviews the user, using the `grill-me` skill, until the what, why and how are agreed. The agreed decisions go in the issue's optional `Background` and `Approach` sections.
- `work-issue` goes from picking an issue to a PR with passing CI.

`CLAUDE.md` points every session at `tracker.py status`. Issues filed on the web use the same sections, through the issue form in `.github/ISSUE_TEMPLATE/`.

Changes that aren't part of a phase, such as docs or tooling, still go through a branch and a PR. They don't need an issue, so the PR is opened with `gh pr create` rather than `finish`.

### Repository settings

These are set in the GitHub repo settings, not in files:

- **`main` is protected.** The `backend`, `migrations`, `frontend` and `audit` CI jobs must pass, and the branch must be up to date with `main` before merging. This applies to admins too, so nothing reaches `main` without a green PR.
  - A required job must run on every PR. GitHub waits indefinitely for a required check that never reports, so don't add `paths:` filters to these jobs; skip steps inside the job instead. Renaming a required job also needs this setting updated.
  - Because `audit` is required, a newly published vulnerability in a runtime dependency blocks merges until the dependency is upgraded or the advisory is dealt with.
  - This is deliberate. If it ever blocks urgent work, an admin can relax the rule temporarily.
- **Squash merges only.** Each PR lands as one commit whose message is the PR title and body.
- **Branches are deleted** automatically after merge.

## 10. Next steps

1. Push `main`, then clone into WSL at `~/src/photo-triage` and start Claude Code there.
2. Set up the read-only `/mnt/pictures` (CIFS) and `/mnt/sample-pictures` (drvfs) mounts in WSL.
3. ~~Phase 1 dependency spike: confirm Python 3.13 wheels for onnxruntime, insightface, umap-learn/numba, open_clip, pillow-heif; lock versions.~~ Done on 2026-10-02 (`backend/pyproject.toml`, `backend/uv.lock`). Every library has a cp313 or pure-Python wheel, so nothing compiles. Each was exercised on the curated sample:
   - **insightface 2.0** (pure Python; previously 0.7.3, which shipped only as source): `model_zoo.get_model` on the bootstrap's `buffalo_l` ONNX files detects faces and returns 512-d embeddings. The new major version adds a GUI and a "PrivateFrame" CLI we don't use. Load models through `model_zoo` instead of `FaceAnalysis`, which expects its own directory layout.
   - **pillow-heif 1.8** (bundles libheif 1.23.4) decodes the sample HEIC with its EXIF intact.
   - **umap-learn 0.5.12** with numba JIT, and **scikit-learn 1.9** `HDBSCAN`, both work on 3.13.
   - **open_clip 3.3 → ONNX:** `torch.onnx.export(..., dynamo=True)` needs `onnxscript`. A dynamic batch axis needs `dynamic_shapes` with an example batch of at least 2, because `dynamic_axes` gets specialised to the example. The onnxruntime output matches torch to within 3e-6.
   - Also resolved: numpy 2.5, onnxruntime 1.30, OpenCV 5.0 (headless), Pillow 12.3, torch 2.14 (CPU).
4. ~~Write `mise.toml`, `justfile`, `scripts/bootstrap.sh`, pre-commit config, and the CI workflow skeleton before any feature code, so every subsequent change lands with the checks already in place.~~ Done on 2026-10-02. The backend checks from §7–8 are live: pre-commit, `just lint`/`typecheck`/`test`/`audit`/`check`, and the `backend` and `audit` CI jobs in `.github/workflows/ci.yml`. The `backend/src/photo_triage` subpackages from §3 exist as empty packages so the import contracts apply from the first line of feature code. Still to add as their code lands:
   - CI jobs: model tier and e2e on `main`/nightly. (The migrations and frontend jobs landed on 2026-10-03, docker on 2026-10-06, and contract on 2026-10-07.)
   - ~~Pre-commit: prettier, eslint, and pnpm lockfile checks (with the frontend).~~ Done on 2026-10-03.
   - `pnpm audit --prod` in the audit job, once the frontend has runtime dependencies (it has none yet: Vite bundles everything).
   - The ≥ 90% branch-coverage gates on `files/`, identity/move detection, and purge (the 75% overall gate is live).
   - Pinned exiftool and ffmpeg in CI, once tests call them.
   - The remaining app recipes in §5: `just fixtures` and `just dry-run-full` (Phase 2). `just api`, `just web` and `.env.example` landed on 2026-10-03, `just worker` on 2026-10-04, and `just dev` on 2026-10-08, as did `just shot` and `just preview`, which brought in Playwright ahead of e2e. ~~The `.env` check in `just doctor`, `just stack`, and Ollama in `just dev`.~~ Done on 2026-10-08.
5. ~~Decide how to track work toward the spec.~~ Done on 2026-10-03. Milestones, issues, and PRs on GitHub, driven by `scripts/tracker.py` (§9).
6. ~~Plan Phase 1 into issues with the `plan-phase` skill, then start building.~~ Done on 2026-10-03: issues #3–#12 in the Phase 1 milestone.
