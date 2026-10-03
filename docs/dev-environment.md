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

All versions are pinned. `mise.toml` pins the language runtimes and CLI tools; lockfiles pin libraries; the Dockerfile pins system tools. `just doctor` verifies a local environment matches.

| Tool | Version | Pinned in | Purpose |
|---|---|---|---|
| mise | latest | — (installed once) | Installs/activates the tools below per-directory |
| Python | 3.13.x | `mise.toml` | Backend. Not 3.14: ML wheels (onnxruntime, numba/UMAP, InsightFace) lag new releases. The Phase 1 dependency spike confirmed every ML library below works on 3.13 (§10). |
| uv | 0.10.x | `mise.toml` | Python deps, venv, lockfile (`uv.lock`) |
| Node.js | 24.x LTS | `mise.toml` | Frontend tooling |
| pnpm | 10.x | `mise.toml` + `packageManager` | Frontend deps, lockfile (`pnpm-lock.yaml`) |
| just | 1.x | `mise.toml` | Task runner (`justfile`) |
| exiftool | 13.x | Dockerfile (tarball from exiftool.org, by version); local via `scripts/bootstrap.sh` | Metadata read/write. Ubuntu's apt version is too old for reliable HEIC/MWG writes. |
| ffmpeg | 7.x | Dockerfile (Debian trixie); local static build via `scripts/bootstrap.sh` (Ubuntu 24.04's apt version is 6.1) | Video frames, posters |
| Docker Engine + Compose | 29.x / v2 plugin | Docker Desktop (WSL integration) | Integration stack, e2e |
| Ollama | pinned image tag | `compose.yaml` | LLM tagging service |

Libraries of note (exact versions live in the lockfiles): FastAPI, SQLAlchemy 2, Alembic, Pydantic 2, onnxruntime, open_clip (export to ONNX), insightface, umap-learn, scikit-learn (including its HDBSCAN), Pillow + pillow-heif, OpenCV (headless); Svelte 5, Vite, TypeScript, deck.gl.

`backend/pyproject.toml` has three dependency sets:

- **Runtime** (`[project.dependencies]`): what the app and worker import. This set goes into the Docker image.
- **`dev`** group: pytest, Hypothesis, ruff, pyright, import-linter, pip-audit.
- **`export`** group: open_clip, torch, torchvision, onnx, onnxscript. These are only for the one-off CLIP→ONNX export (`scripts/export_clip_onnx.py`) and stay out of the image (`uv sync --no-group export`).

Both groups are `default-groups`, so a plain `uv sync` installs everything locally, which comes to about 2 GB. torch and torchvision come from the PyTorch CPU-only index, and both must be listed directly, because uv applies an index source only to direct dependencies. A torchvision pulled from PyPI fails at import with `operator torchvision::nms does not exist`.

### Bootstrap (fresh WSL)

`scripts/bootstrap.sh` — idempotent, and assumes nothing on a fresh Ubuntu 24.04 but git: installs apt prerequisites (curl, build-essential, cifs-utils; no libheif, since the pillow-heif wheel bundles its own), mise (plus `~/.bashrc` activation), and the pinned exiftool, ffmpeg, and pre-commit; runs `mise install`, `uv sync`, `pnpm install`, `pre-commit install`; downloads model weights to `~/.cache/photo-triage/models`. Downloads are verified against pinned SHA-256 checksums. It also checks what it can't install: Docker is reachable, and the photo mounts are present and read-only.

### Verifying the environment: `--check` and `just doctor`

`scripts/bootstrap.sh --check` runs the same detection as a bootstrap but changes nothing: no sudo, no network. It prints each problem with its fix and exits 1 if anything needs fixing. That includes a missing or wrong-version tool, missing weights, a `uv.lock` out of date with `pyproject.toml`, a `.venv` or `node_modules` out of date with its lockfile, missing git hooks, Docker unreachable, or `/mnt/sample-pictures` absent. A photo mount that is **read-write** is always a failure. Steps whose project files don't exist yet (e.g. `backend/pyproject.toml` before Phase 1) are warnings, not failures.

`just doctor` runs `scripts/bootstrap.sh --check`, followed by app-level checks that need `.env` (e.g. `PHOTO_DIR` isn't writable when it points at `/mnt/pictures`). The tool and version checks live only in the bootstrap script, so installing and verifying can't drift apart.

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
  mise.toml  justfile  compose.yaml  compose.dev.yaml  .env.example
  backend/
    pyproject.toml  uv.lock  alembic.ini
    src/photo_triage/
      api/          FastAPI routers, current_actor dependency
      worker/       job queue, scheduler, stage runners
      pipeline/     scan, thumbnails, clip, quality, faces, tagging, layout, dupes
      ml/           pluggable adapters (embedder, face detector, tagger) + fakes
      files/        THE ONLY module allowed to mutate files: trash, restore, purge, exif writes
      db/           engine, models, migrations/ (Alembic), repositories
    tests/
      unit/  integration/  models/  safety/
      fixtures/synthetic/        committed, generated
  frontend/
    package.json  pnpm-lock.yaml  src/  tests/  e2e/
  docker/Dockerfile
  scripts/        bootstrap.sh, tracker.py (GitHub issues/PRs), check_file_mutation.py, ...
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
| `just dev` | API (`uvicorn --reload`), worker process, and Vite dev server (proxying `/api` to the API) natively in WSL; Ollama via `compose.dev.yaml`. Uses `.env` → `PHOTO_DIR=/mnt/sample-pictures` (read-only), local `data/` and `trash/` dirs. |
| `just api` | The API alone: `python -m photo_triage.api --reload`, on `APP_PORT`, from the repo root so `.env` and `./data` resolve there. Exits with a list of what's wrong if `PHOTO_DIR` or `TRASH_DIR` is missing. API docs at `/api/docs`. |
| `just worker` / `just web` | Each individually |
| `just stack` | Full production-like Compose stack (built image) against tier-1 fixtures |
| `just dry-run-full` | Stack against `/mnt/pictures` (read-only), writes disabled — for scale testing |
| `just db-reset` | Drop and re-migrate the dev database in `DATA_DIR`: Alembic downgrade to empty, then upgrade to head. For other Alembic commands, run `uv run --project backend alembic -c backend/alembic.ini …` from the repo root. A new migration starts from `revision --autogenerate`; read it before committing, since the tests fail if models and migrations disagree. |
| `just fixtures` | Regenerate tier-1 synthetic fixtures |
| `just doctor` | `scripts/bootstrap.sh --check` (tool versions, lockfile sync, model weights, Docker, mounts read-only), plus `PHOTO_DIR` isn't writable when it points at `/mnt/pictures` (§2) |

Worker-specific dev affordances: `WORKER_WINDOW` unset (always on), a `--once` flag to drain the queue and exit, and a controllable clock (`FAKE_NOW`) for exercising quiet-hours and ETA logic.

## 6. Testing strategy

| Layer | Tooling | Approach |
|---|---|---|
| Pure logic | pytest (+ Hypothesis) | Quality scoring, union-find grouping, content hashing, ETA math against working windows, schedule windows, search score blending, FTS query building. Property-based where there are invariants. |
| Database & migrations | pytest + Alembic | Every migration upgrades from empty and downgrades; `alembic check` confirms models and migrations agree. |
| Metadata write-back | pytest + real exiftool | Round-trip each field mapping (plan §6.10) per container type on tier-1 copies in a temp dir; read back and assert. |
| Pipelines & worker | pytest + fake ML adapters | Run the worker `--once` over tier-1 fixtures with fake embedders/detectors (deterministic vectors derived from file hashes). Assert job states, stage ordering, resumability (kill mid-run, restart), retry/park behaviour. |
| ML adapters (model tier) | pytest `-m models` | Real CLIP / InsightFace / UMAP on fixtures with **tolerance** assertions: burst pair cosine > threshold, distinct scenes below it, expected face counts, text query ranks the right fixture first. Weights cached. Not in the default suite. |
| API | pytest + httpx `AsyncClient` | Endpoint behaviour, `current_actor` in both `AUTH_MODE`s, SSE progress stream. |
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
- `prettier --check`, `eslint`
- `uv lock --check`, pnpm lockfile consistency
- **Media guard:** reject any image/video file added outside `backend/tests/fixtures/synthetic/`; reject files > 1 MB — prevents family photos (tier 2) from ever being committed
- File-mutation guard (`scripts/check_file_mutation.py`, §7)
- `gitleaks` (secrets, e.g. SMB credentials)
- Trailing whitespace / EOF / merge-conflict markers; LF line endings enforced via `.gitattributes`

Config: `.pre-commit-config.yaml`. Ruff covers `backend/` and `scripts/`. The ruff and uv hooks run through `scripts/backend-run.sh`, which finds mise even when the shell hasn't activated it (commits from an editor), and uses the existing `.venv` without syncing. `just pre-commit` runs every hook on every file; `just check` runs that plus lint, pyright, and the tests — everything CI runs except the audit.

### CI on every push / PR (GitHub Actions, ubuntu-24.04)

| Job | Checks |
|---|---|
| backend | `uv sync --locked --no-group export`; pre-commit (all hooks); ruff; **pyright strict** (tests and `scripts/` at basic); import-linter; pytest unit + integration + safety with real exiftool/ffmpeg and fake ML; coverage gates (≥ 90% branch on `files/`, identity/move detection, and purge; ≥ 75% overall; `ml/` adapters exempt) |
| migrations | upgrade from empty → downgrade → upgrade; `alembic check` |
| frontend | `pnpm install --frozen-lockfile`; svelte-check; eslint; prettier; Vitest; `vite build` |
| contract | Export OpenAPI from the app, regenerate TS types (`openapi-typescript`); fail if the committed types differ |
| docker | hadolint; build image; image smoke test (container starts, `/api/health` ok, `exiftool -ver` / `ffmpeg -version` match pinned versions) |
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
| `finish SUMMARY` | Requires a clean tree that's up to date with `origin/main`. Runs `just check`, pushes, and opens or updates the PR, adding the issue's acceptance criteria and `Closes #N` to the body. |

The Claude Code skills in `.claude/skills/` hold the decisions the script can't make:
- `grill-me` interviews the user one question at a time until a plan is fully agreed. It's a copy of a claude.ai skill, kept in the repo so every session can invoke it.
- `plan-phase` splits a phase into issues and gets approval before filing them.
- `new-issue` files a single issue, such as a PR follow-up, a bug, an idea or a spike. It first interviews the user, using the `grill-me` skill, until the what, why and how are agreed. The agreed decisions go in the issue's optional `Background` and `Approach` sections.
- `work-issue` goes from picking an issue to a PR with passing CI.

`CLAUDE.md` points every session at `tracker.py status`. Issues filed on the web use the same sections, through the issue form in `.github/ISSUE_TEMPLATE/`.

Changes that aren't part of a phase, such as docs or tooling, still go through a branch and a PR. They don't need an issue, so the PR is opened with `gh pr create` rather than `finish`.

### Repository settings

These are set in the GitHub repo settings, not in files:

- **`main` is protected.** The `backend` and `audit` CI jobs must pass, and the branch must be up to date with `main` before merging. This applies to admins too, so nothing reaches `main` without a green PR.
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
   - CI jobs: frontend, contract, docker; model tier and e2e on `main`/nightly. (The migrations job landed on 2026-10-03.)
   - Pre-commit: prettier, eslint, and pnpm lockfile checks (with the frontend).
   - The ≥ 90% branch-coverage gates on `files/`, identity/move detection, and purge (the 75% overall gate is live).
   - Pinned exiftool and ffmpeg in CI, once tests call them.
   - The `.env` check in `just doctor`, and the app recipes in §5 (`dev`, `worker`, `web`, `stack`, ...). `just api` and `.env.example` landed on 2026-10-03.
5. ~~Decide how to track work toward the spec.~~ Done on 2026-10-03. Milestones, issues, and PRs on GitHub, driven by `scripts/tracker.py` (§9).
6. ~~Plan Phase 1 into issues with the `plan-phase` skill, then start building.~~ Done on 2026-10-03: issues #3–#12 in the Phase 1 milestone.
