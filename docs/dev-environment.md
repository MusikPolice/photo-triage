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

`scripts/install_media_tools.sh DEST` is the one place exiftool and ffmpeg are downloaded from. It installs the pinned versions into `DEST/exiftool` and `DEST/ffmpeg`. exiftool comes from this repo's `media-tools` GitHub Release first, then SourceForge. ffmpeg comes from johnvansickle.com's old-releases URL, then its rolling release URL. Every download retries all errors (TLS ones too) and gives up on a URL within about 3 minutes, so a flaky host retries and a dead one fails rather than hanging. The script checks each download's SHA-256, and checks `exiftool -ver`, `ffmpeg -version` and `ffprobe -version` once they're installed. It exits 1 naming the tool and the problem. The pins come from the environment when all four are set, else from `versions.env`. It's run by:
- the bootstrap, into `~/.local/share/photo-triage/tools/media-tools`, symlinked from `~/.local/bin`;
- the Dockerfile's `tools` stage, into `/opt` (`.dockerignore` lets that one script in, and the stage installs `perl` so the script can check exiftool);
- the CI backend job, into `~/media-tools`, on a cache miss.

**The mirror.** `scripts/mirror_media_tools.sh` downloads the pinned exiftool, checks it against `versions.env`, and uploads it to the `media-tools` release. Run it after bumping `EXIFTOOL_VERSION`. A pin that's already there is left alone. Since the checksum lives only in `versions.env`, a tampered mirror can't pass. ffmpeg isn't mirrored: re-hosting a GPL binary means publishing its full corresponding source, and johnvansickle.com's source folders don't match the 7.0.2 build's libraries (#76). `MEDIA_TOOLS_MIRROR` overrides the mirror's URL, for testing the fallback.

So a bump is a one-line edit (plus the checksum, for exiftool and ffmpeg). `just doctor` verifies a local environment matches.

The model weights' checksums live in `scripts/bootstrap.sh`, their only user. `frontend/package.json`'s `packageManager` still repeats the pnpm version from `mise.toml`.

| Tool | Version | Pinned in | Purpose |
|---|---|---|---|
| mise | latest | — (installed once) | Installs/activates the tools below per-directory |
| Python | 3.13.x | `mise.toml` | Backend. Not 3.14: ML wheels (onnxruntime, numba/UMAP, InsightFace) lag new releases. The Phase 1 dependency spike confirmed every ML library below works on 3.13 (dev-reference §4). |
| uv | 0.10.x | `mise.toml` | Python deps, venv, lockfile (`uv.lock`) |
| Node.js | 24.x LTS | `mise.toml` | Frontend tooling |
| pnpm | 10.x | `mise.toml` + `packageManager` | Frontend deps, lockfile (`pnpm-lock.yaml`) |
| just | 1.x | `mise.toml` | Task runner (`justfile`) |
| shellcheck | 0.11.x | `mise.toml` | Lints the shell scripts (pre-commit) |
| hadolint | 2.x | `mise.toml` | Lints `docker/Dockerfile` (pre-commit, `just image`) |
| pre-commit | 4.x | `versions.env` (installed with `uv tool install`) | Git hooks and their checks (§8) |
| exiftool | 13.x | `versions.env`: one tarball, by version and checksum, for the bootstrap and the image (from SourceForge, which keeps old releases) | Metadata read/write. Ubuntu's apt version is too old for reliable HEIC/MWG writes. |
| ffmpeg | 7.0.x | `versions.env`: one static build, by version and checksum, so dev, CI and the image run one binary. Ubuntu 24.04's apt version is 6.1. Debian trixie's (7.1) was the plan for the image, but an apt version pin breaks whenever Debian ships a security update, and it wouldn't match dev. The image's smoke test checks both tools against `versions.env`. | Video frames, posters |
| Playwright + headless Chromium | 1.x | `frontend/pnpm-lock.yaml` (`@playwright/test`); the bootstrap installs the matching Chromium headless shell into `~/.cache/ms-playwright`, and its apt libraries | `just shot`, `just preview` and `just e2e` (dev-reference §2, §3) |
| Docker Engine + Compose | 29.x / v2 plugin | Docker Desktop (WSL integration) | Integration stack, e2e |
| Ollama | pinned image tag | `docker/ollama.yaml`, which `compose.yaml` and `compose.dev.yaml` extend | LLM tagging service |

Libraries of note (exact versions live in the lockfiles): FastAPI, SQLAlchemy 2, Alembic, Pydantic 2, onnxruntime, open_clip (export to ONNX), regex and ftfy (CLIP's tokenizer, ported from open_clip into `photo_triage.ml.tokenizer` so the runtime needs no torch), insightface, umap-learn, scikit-learn (including its HDBSCAN), Pillow + pillow-heif, OpenCV (headless); Svelte 5, Vite, TypeScript, deck.gl.

**New npm releases wait 7 days.** `frontend/pnpm-workspace.yaml` sets `minimumReleaseAge: 10080` (minutes), so `pnpm install`, `pnpm add` and `pnpm update` resolve only versions published at least 7 days ago. A range falls back to the newest version that old, and an exact pin to a newer one fails, naming the setting. Hijacked npm releases (the chalk/debug hijack, the Shai-Hulud worm, the nx release) were pulled within hours to a couple of days, so the wait keeps them out of the lockfile; 14 days would double the wait for wanted releases for little more protection (#131). It only applies when resolving: `pnpm install --frozen-lockfile` (CI, the bootstrap and the Docker build) installs the lockfile as it is, whatever its versions' ages, on pnpm 10. pnpm 11 checks the lockfile too, so revisit this when upgrading.

- **An urgent fix can skip the wait.** List the package under `minimumReleaseAgeExclude` in `pnpm-workspace.yaml`, in the PR that upgrades it, and remove it once the release is 7 days old. List any new transitive dependency it pulls in too: excluding `svelte` alone wasn't enough to install a day-old `svelte`, which needed a 4-day-old `esrap`.

TypeScript is pinned to 6.0 (`~6.0` in `frontend/package.json`). TypeScript 7 is out, but svelte-check and typescript-eslint don't support it yet.

`backend/pyproject.toml` has three dependency sets:

- **Runtime** (`[project.dependencies]`): what the app and worker import. This set, and no group, goes into the Docker image.
- **`dev`** group: pytest, Hypothesis, ruff, pyright, import-linter, pip-audit.
- **`export`** group: open_clip, torch, torchvision, onnx, onnxscript. These are only for the one-off CLIP→ONNX export (`scripts/export_clip_onnx.py`) and the model tier's comparisons with open_clip, and stay out of the image (`uv sync --no-group export`). The export writes `MODELS_DIR/clip/<CLIP_MODEL>/`: `image.onnx` and `text.onnx` (dynamic batch axis), the tokenizer's vocabulary, and `clip.json` with the preprocessing settings, so `photo_triage.ml.OnnxClip` needs only onnxruntime, numpy and Pillow. `CLIP_MODEL=ViT-B-32` uses OpenAI's weights, which the aesthetic predictor was trained on; open_clip's download checks their SHA-256.

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

The folder-by-folder layout is in [dev-reference.md](dev-reference.md) §1. The decision it reflects: only `backend/src/photo_triage/files/` may mutate files (§7), and the other subpackages (`api`, `worker`, `pipeline`, `ml`, `db`) existed from the first line of feature code, so the import contracts applied from the start (dev-reference §4).

## 4. Test data tiers

Automated tests **never** touch the real library.

| Tier | What | Where | Used by |
|---|---|---|---|
| 1. Synthetic | 31 generated files, 1.8 MB: JPEG/HEIC/MOV/MP4; a burst set (small shifts); blurred/over/underexposed variants; a screenshot-like image; files with pre-existing keywords, an MWG face region, and odd EXIF (no dates, only `CreateDate`, only `ModifyDate`, a 90° and an invalid orientation); a JPEG cut off before its picture data (unreadable) and one cut off halfway (damaged, #95); a PNG (unsupported type). Videos are 2–3 s at 320×240. Faces from openly licensed stock images are still to come (Phase 6). | `backend/tests/fixtures/synthetic/`, committed; regenerated deterministically by `scripts/make_synthetic_fixtures.py` (`just fixtures`) | Unit, integration, safety, e2e, CI |
| 2. Curated sample | Existing ~680-file curated subset on the Windows host (`C:\Users\jonfr\Pictures`, 3.1 GB): 666 JPEG, 1 HEIC, plus a few PNG/BMP (unsupported types — useful for testing that the scanner skips them cleanly). `Videos/` holds 10 library videos, added for #79: 4 iPhone MOV (HEVC and H.264, one shot in Calgary at UTC−6), 4 Android MP4 (three with only a UTC `CreateDate`), and an AVI and an MTS from older cameras (unsupported types, skipped like the PNGs). Summer and winter, so DST shows. | Bind-mounted **read-only** in WSL at `/mnt/sample-pictures`; lives outside the repo and is never committed (contains family photos) | Local dev, model-tier tests, threshold calibration |
| 3. Full library | Read-only mount at `/mnt/pictures`: 32,983 items (32,631 photos, 352 videos) on 2026-11-12 | — | Occasional full-scale dry runs with `just dry-run-full` (dev-reference §2): scan speed, throughput per stage, memory, the layout fit (51 s and a 1.0 GiB worker peak for 32,983 items on 2026-11-18), and the atlases (13.2 s for 9 sheets, 45 MB, on 2026-11-18). Writes off, the trash in a scratch folder |

Because tiers 2 and 3 are read-only, anything that needs to write (trash, EXIF write-back) runs against a throwaway copy: `just dev` can seed `~/photo-triage-data/scratch/` from `/mnt/sample-pictures` (`just seed-scratch`) and point `PHOTO_DIR` there when testing mutations.

## 5. Running each piece

Every recipe, the dev-only switches, the Compose stack's services and settings, trying the map on a phone, and logging's mechanics are in [dev-reference.md](dev-reference.md) §2. `just` lists the recipes. The decisions behind them:

- **Dev runs natively in WSL.** `just dev` runs the API, the worker and Vite in WSL, and only Ollama in Docker, from `compose.dev.yaml`, which shares `docker/ollama.yaml` with the production stack so its pin is written once (§2).
- **The API and worker never migrate.** They exit if the database isn't at the head revision. The migrate step backs the database up before applying a migration, and in the Compose stack the app and worker start only after it has succeeded.
- **Production is `compose.yaml`** (plan §3, §4): one image runs the migrate step, the app and the worker.
  - The containers run as `APP_UID`:`APP_GID` (default `1000:1000`), so they can be the library's owner without anyone changing who owns the photos.
  - Compose doesn't create the host folders, since Docker would make them owned by root.
  - The trash is always inside the library's mount, so trashing is a rename (plan §6.8).
  - Model weights aren't in the image, which would need torch to export CLIP and grow by about 600 MB. They're mounted read-only from `MODELS_DIR`.
  - The CPU and memory caps (plan §6.11) are first guesses, changed in a `compose.override.yaml` on the server.
  - There's no registry yet, so the server builds the image from a checkout.
- **Scratch stacks never see real data.** `just preview`, `just shot`, `just e2e`, `just stack` and `just map-stack` run against throwaway folders under their own Compose projects or ports, and read no `.env`. Only scratch-stack screenshots can be published (§9).
- **Dev-only switches** (`FAKE_NOW`, `ENV_FILE`, `WORKER_FAKE_CLIP`, the noop stage) are read like plan §9's settings but aren't listed there or in `.env.example`.

### Logging

Logs are for the operator finding out why something happened. The UI reads errors, progress and history from the database (plan §7), not from the logs. Each process writes plain-text lines with UTC timestamps to stderr and to its own rotating file in `DATA_DIR/logs`; the format, rotation and library loggers are in dev-reference §2, "Logging".

Use these levels, so `INFO` stays readable through a first pass of hundreds of thousands of jobs:

| Level | For |
|---|---|
| `DEBUG` | One line per job and per request; SQL statements. Off by default. |
| `INFO` | Startup, with the settings (secrets are `SecretStr` fields, so they're masked; `test_every_setting_is_public_or_secret` makes each new setting declare which it is); state changes such as pause/resume or a scan starting; batch jobs; periodic progress summaries. Never one line per item. |
| `WARNING` | A failure that will be retried, e.g. a job going to `error` with a backoff. |
| `ERROR` | A failure that needs a person: a job that's parked, or an unhandled error. Always with the traceback (`logger.exception`). |

Get a logger with `logging.getLogger(__name__)` (in a `__main__` module, name it, since `__name__` is `"__main__"` there) and pass values as arguments (`logger.info("scanned %d files", n)`), not f-strings. Alembic's CLI (`just db-reset`) keeps the logging set up in `alembic.ini`. The migrate step (`just db-migrate`) runs Alembic itself and tells `env.py` to skip that (`config.attributes["configure_logger"] = False`), so Alembic's lines reach `migrate.log` in this format.

## 6. Testing strategy

Automated tests never touch the real library (§4). The tests are layered:

- **Pure logic:** pytest, with Hypothesis where there are invariants.
- **Database and migrations:** every migration upgrades from empty and downgrades, `alembic check` confirms models and migrations agree, and migrations import no app code.
- **Metadata write-back:** round trips through real exiftool on tier-1 copies.
- **Pipelines and worker:** the worker `--once` over tier-1 fixtures with fake ML adapters.
- **ML adapters (model tier):** `pytest -m models`, real models with tolerance assertions, not in the default suite.
- **API:** httpx `AsyncClient`, with uvicorn in a thread for the event stream.
- **Performance:** `pytest -m bench`, against targets, on the dev machine only, since timings depend on the machine. Before and after numbers go in the PR.
- **Safety invariants:** `tests/safety/` (§7), the most important tests in the repo.
- **Frontend:** Vitest for logic and svelte-check for strict types.
- **End-to-end:** Playwright against the production Compose stack with the fake embedder, at desktop and phone sizes. The map is asserted through a test-only hook exposing deck.gl's state, not pixels.

What each layer covers, the e2e coverage list, the map's test hook and how to run a selection are in [dev-reference.md](dev-reference.md) §3. The coverage gates are in §8.

Determinism: fixed seeds for UMAP (`random_state`) and any sampling; injected clock; fake adapters derive outputs from content hashes; tests never depend on wall-clock time or network (Ollama is faked except in the model tier).

## 7. Safety invariants

File mutation is confined to `photo_triage.files`. These are enforced both statically and by tests:

**Static (fail the build):**
- Ruff `flake8-tidy-imports` banned-API rules (TID251, in `backend/pyproject.toml`): `os.remove`, `os.unlink`, `os.rmdir`, `os.removedirs`, `os.rename`, `os.renames`, `os.replace`, `os.utime`, `shutil.move`, and `shutil.rmtree` are banned in `backend/src` **except** `photo_triage/files/`.
- Ruff can't resolve method calls on `Path` objects or the arguments to `subprocess`, so `scripts/check_file_mutation.py` covers those by syntax, with the same scope: `.unlink()`, `.rename()`, `.rmdir()`, one-argument `.replace()` (`Path.replace`; `str.replace` takes two), and any string literal naming the `exiftool` executable. It runs in pre-commit and `just lint`.
- `import-linter` contracts: `api` and `pipeline` may import `photo_triage.files` (its public interface) but none of its submodules; `ml` imports nothing from `db`/`api`.
- Tests are exempt from both, since they build and tear down fixture copies in temp dirs.
- Log rotation renames and deletes files, so the rotating handler (`RotatingLogHandler`) lives in `photo_triage.files` too. It refuses any log file outside `DATA_DIR/logs` (dev-reference §2, "Logging").
- So does the worker's exiftool process (`ExifTool`), which reads and writes metadata, since naming the executable is banned elsewhere. Other packages see it as a `MetadataReader`, and the `metadata_write` stage as a `MetadataWriter`. A write is in place (`-overwrite_original_in_place`, so no `_original` copies and the same inode), and the exact mtime is put back afterwards, since `-P` keeps it only to the second. What to write, and the queue and log around it, are `photo_triage.pipeline.writeback`'s (plan §6.10, #152).
- So does the worker's lock file (`exclusive_lock`), which `python -m photo_triage.worker` creates in `DATA_DIR` and holds with `flock`, so only one worker runs (dev-reference §2, `just worker`). Creating it is the only mutation; it's never deleted.
- So does replacing derived files (`replace_derived`, `remove_derived`): the `frames` stage writes a video's new frames into a staging folder, then swaps it in for the old set, deleting it, and the `thumbnail` stage writes each thumbnail beside the old one and renames it over it, so a half-written one is never served. Both refuse any path outside `DATA_DIR/derived`, or the folder itself, and remove a link without following it.
- So does moving files to the trash and back (`move_to_trash`, `restore_from_trash`, plan §6.8). Each is a rename to the same relative path under `TRASH_DIR` or `PHOTO_DIR`, and refuses: a destination that exists; a path that leads out of its folder, or from the library side into the trash; a link or folder; and a move across filesystems, rather than copying. A restore removes the trash folders it empties, never the library's. The API moves a request's files inside one transaction, and moves them back if it doesn't commit.
- So does pruning the migrate step's database backups (`prune_backups`), which deletes all but the newest `*.db` files in `DATA_DIR/backups` (dev-reference §2, `just db-migrate`). The backups themselves are written by SQLite's `VACUUM INTO`, which only creates a file.

**Tests (`tests/safety/`, run on every PR):**
- **Dry run is inert:** with `EXIF_WRITES_ENABLED=false`, run every pipeline and every UI action over a fixture copy; every file's bytes and mtime are identical afterwards. So far `test_no_escape.py` runs the worker's whole pipeline (`main`: a walk, then `--once`, with the fake embedder) and checks every library file's bytes, size and mtime, and every library folder's mtime, so a temporary file created and removed there shows too. `test_write_back.py` queues a tags and reviewed-marker write for every tier-1 item and runs the worker with `EXIF_WRITES_ENABLED` unset: each is logged as a dry run, and nothing outside `DATA_DIR` changes (#152). The UI actions that queue writes are checked as they're added. Moving to the trash isn't blocked by the dry run: it's a rename that a restore undoes (`test_trash_round_trip.py`). The purge is: with writes off it logs each file it would delete and deletes nothing (`test_purge_retention.py`, #151).
- **No escape:** across a full worker run, no file is created, modified, or removed outside `PHOTO_DIR`, `TRASH_DIR`, and `DATA_DIR` (filesystem snapshot diff of the temp root). `test_no_escape.py` snapshots a temp root holding those folders plus `HOME`, `TMPDIR` and a bystander folder, so a write to any of them shows; until Phase 4 writes to the library or the trash, everything changed must be under `DATA_DIR`. Each check has tests that plant a stray write (beside the library, in `~`, in `$TMPDIR`, in the trash, a touched photo, a temporary file in the library) and confirm it's caught. After the run, it also shows every item in the lightbox through the API (`GET /api/items/{id}/view`, converting the HEICs and an HEVC video with ffmpeg) and checks that nothing changed outside `DATA_DIR` and the library is as it was (#114).
- **Pixel preservation:** after metadata writes, every file's content hash (pixel data) is unchanged and mtime is preserved. `test_write_back.py` does the same writes with `EXIF_WRITES_ENABLED=true`, and checks every item's content hash and exact mtime (#152).
- **Trash round-trip:** trash → restore yields byte-identical files at their original paths; purge only removes items past retention. `test_trash_round_trip.py` trashes every tier-1 item through the API, with the trash inside the library as by default, and restores them: each file is the same bytes, size and mtime in the trash and back at its path, the library's folders stay, and nothing changes outside the library and `DATA_DIR` (#149). `test_purge_retention.py` trashes every tier-1 item, half of them 31 days before the purge's clock and half 29, and purges with writes on: exactly the first half's files leave `TRASH_DIR`, with the trash folders they empty, and nothing else changes outside `DATA_DIR` (#151).
- **Identity stability:** content hash is stable across metadata writes and moves; a moved file keeps its history (`test_identity_stability.py`: exiftool writes to every tier-1 file, then every file moves, and each keeps its item id and hash).
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
| backend | loads `versions.env`; installs the exiftool and ffmpeg it pins with `scripts/install_media_tools.sh` on a cache miss (cached by checksum; exiftool from the repo's mirror), and on every run checks `exiftool -ver`, `ffmpeg -version` and `ffprobe -version` match; `uv sync --locked --no-group export`; pre-commit (all hooks except the frontend ones, so including shellcheck and hadolint); ruff; **pyright strict** (tests and `scripts/` at basic); import-linter; pytest unit + integration + safety with real exiftool/ffmpeg and fake ML; coverage gates (≥ 90% branch on `files/`, identity/move detection, and purge; ≥ 75% overall; `ml/` adapters exempt). `just test` checks the identity gate, on `pipeline/identity.py` and `pipeline/scan.py` together, and the purge gate, on `pipeline/purge.py`, `worker/purge_schedule.py` and `files/trash.py`, each with `coverage report --fail-under=90` after pytest |
| migrations | upgrade from empty → downgrade → upgrade; `alembic check`; on PRs, migrations already on `main` aren't modified, renamed or deleted (add a new one instead) |
| frontend | `pnpm install --frozen-lockfile`; svelte-check (strict, fails on warnings); eslint (typescript-eslint `strictTypeChecked`); prettier; Vitest; `vite build`. The backend job's pre-commit run skips the frontend hooks, since it has no `node_modules`. |
| contract | `scripts/api_types.sh --check` (`just contract`): export OpenAPI from the app, regenerate the TS types with `openapi-typescript`, and fail if they differ from the committed `frontend/src/lib/api-types.ts` |
| docker | build the image with the build args from `scripts/image_pins.sh` (layers cached in GitHub Actions); `scripts/docker_smoke.sh`: the container starts, `/api/health` is ok, `/` and `/activity` serve the frontend, its script is served, it runs as a non-root user, `exiftool -ver`, `ffmpeg -version` and `ffprobe -version` match the pins in `versions.env`, there's no Node toolchain and no `dev` or `export` packages, the runtime dependencies import, the app refuses an unmigrated database, the migrate step runs, and the app and then the worker (`--once`) run against the database it migrated; then `compose.yaml`'s migrate, app and worker services (not Ollama) on temporary bind-mounted folders, as `APP_UID` 1234, a UID with no name in the image: the app gets healthy after the migrate service, answers on `APP_PORT`, the database lands in the `DATA_DIR` mount, owned by UID 1234, the trash is `/photos/.photo-triage-trash` with no filesystem warning, and a second worker exits saying one is already running |
| audit | `pip-audit` on the runtime dependencies (`just audit`; fails on any known vulnerability, since pip-audit has no severity filter), then `pnpm audit --prod --audit-level high` on the frontend's (deck.gl and its dependencies), which fails on high and critical. Fixes come from Dependabot's grouped security-update PRs (§9) |

### CI on `main` and nightly

- **Model tier:** `.github/workflows/models.yml` runs `just test-models` on pushes to `main`, nightly, by hand (`workflow_dispatch`), and on PRs that touch `photo_triage/ml`, `tests/models`, `uv.lock`, the export script or the workflow. It installs every group (the tests compare with open_clip), and caches open_clip's pretrained weights and the ONNX export, keyed on the export script and `uv.lock`, so a cache miss runs the export (about 30 s). Not a required check.
- **E2E:** `.github/workflows/e2e.yml` runs `just e2e` (§6) on pushes to `main`, nightly and by hand. It builds the image with the docker job's cache and passes it in as `E2E_IMAGE`, and uploads Playwright's traces (`frontend/test-results/`) when a test fails. Not a required check.

## 9. Tracking work and merging

Work toward the spec is tracked on GitHub:

- Each phase in plan §11 is a **milestone**.
- Each deliverable is an **issue** small enough for one PR. An issue cites the spec sections it implements, lists checkable acceptance criteria, and says what's out of scope.
- Each issue gets its own **branch** and **PR**, and the PR closes the issue when merged.
- Claude Code merges its own PRs once CI is green, with `tracker.py merge N`, **except PRs that touch a sensitive path**. The user reviews and merges those. The paths are listed in `SENSITIVE_PATHS` in `scripts/tracker.py`: `docs/plan.md`, `docs/dev-environment.md`, `CLAUDE.md`, `.claude/`, `.github/`, `scripts/tracker.py`, `scripts/check_file_mutation.py`, `backend/src/photo_triage/files/` and `backend/tests/safety/`. They're the spec and the recorded decisions, the workflow and its gate, and the code and tests behind the safety invariants (§7). The list includes itself, so changing it needs review. (Decided 2026-11-09: reviewing routine diffs wasn't finding problems, and the user's feedback is most useful on something running in the browser.)
- Claude Code uses the user's GitHub account, so GitHub's required reviews can't tell its merges from the user's. The gate is therefore in the tracker, plus a Claude Code `PreToolUse` hook (`.claude/hooks/block_direct_merge.py`) that blocks `gh pr merge` (including `--auto`) and merge calls through `gh api` in the Bash tool. The tracker's own `gh` subprocess isn't seen by the hook. The hook matches `gh` only where it runs as a command (at the start of a line or after a shell separator, followed by its subcommand), and it ignores heredoc bodies, so a PR body or commit message that mentions these commands gets through. It guards against habit, not an adversary.

`scripts/tracker.py` does the mechanical steps, so they happen the same way every time. Its docstring has the issue draft format.

| Command | Does |
|---|---|
| `setup` | Creates the phase milestones from the plan §11 headings, plus the area labels (`backend`, `frontend`, `infra`, `documentation`), `bug`, and `in-progress`. Every issue needs at least one area label. Running it again only adds what's missing. |
| `status` | Shows the current phase and its issues (in progress, ready, or blocked on an open dependency), plus open PRs. |
| `new DRAFT...` | Validates issue drafts: required sections, spec citations that match a heading, checklist criteria, labels and phase. Then files them in order. If any draft is invalid, it files nothing. Use `--dry-run` to validate without filing. |
| `start N` | Creates branch `N-slug` from `origin/main`, assigns the issue and labels it `in-progress`. |
| `finish SUMMARY` | Requires a clean tree that's up to date with `origin/main`. Runs `just check`, pushes, publishes any screenshots in the summary's `Screenshots` section (below), and opens or updates the PR, adding the issue's acceptance criteria and `Closes #N` to the body. |
| `feedback [N]` | Prints everything reviewers have said on PR N, or on the current branch's PR: its state and review decision, each review, the conversation, and each inline thread with its file, line and whether it's resolved or outdated. |
| `merge N` | Refuses unless PR N is open, not a draft and targets `main`. Lists every path the PR touches (both sides of a rename) from GitHub. If any is sensitive, it labels the PR `needs-human-review`, prints those paths and stops. Otherwise it requires every check on the PR to have passed or been skipped, then squash-merges with `--match-head-commit`, so a push after the check makes GitHub refuse the merge. |

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

- **`main` is protected.** The `backend`, `migrations`, `frontend`, `audit`, `contract` and `docker` CI jobs must pass, and the branch must be up to date with `main` before merging. This applies to admins too, so nothing reaches `main` without a green PR.
  - A required job must run on every PR. GitHub waits indefinitely for a required check that never reports, so don't add `paths:` filters to these jobs; skip steps inside the job instead. Renaming a required job also needs this setting updated.
  - Because `audit` is required, a newly published vulnerability in a runtime dependency blocks merges until the dependency is upgraded or the advisory is dealt with.
  - This is deliberate. If it ever blocks urgent work, an admin can relax the rule temporarily.
- **Dependabot security updates are on, and grouped.** `.github/dependabot.yml` puts every open advisory in an ecosystem into one PR: one for `backend/` (uv) and one for `frontend/` (npm). Per-package PRs would each fail `audit` on the other packages' advisories, so none of them could merge. Version updates are off (`open-pull-requests-limit: 0`). Dependabot regenerates the lockfile with its own pnpm, which may not apply `minimumReleaseAge` (§2), so its PRs can bring in versions under 7 days old, and on pnpm 10 nothing in CI rejects them. That suits a security fix, which shouldn't wait. Check the lockfile diff for young versions before merging one.
- **Squash merges only.** Each PR lands as one commit whose message is the PR title and body.
- **Branches are deleted** automatically after merge.

## 10. Next steps

The setup history (the Phase 1 dependency spike, the checks and recipes as each landed) is in [dev-reference.md](dev-reference.md) §4.
