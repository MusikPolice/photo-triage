## 1. Problem Statement

A family photo and video library of 10,000–100,000 items has accumulated several classes of problems that make it hard to use:

- **Low-quality photos**: bad lighting, soft focus, poor framing, very low resolution, or "technically fine but pointless" shots (screenshots, receipts, pocket shots). These are genuine candidates for deletion but require human judgment before acting.
- **Near-duplicates**: groups of 3–20 photos that are visually near-identical, typically the result of burst-shooting. These are not bitwise copies, so hash deduplication won't catch them.
- **No way to explore or search**: the library is organized chronologically by elodie, but there is no way to query it by content ("photos of Tony at the beach") or to browse it by what's *in* it. iPhones do this natively via on-device ML; the goal is to replicate — and go beyond — that capability against the full archival library.
- **Unidentified faces**: the library contains many people who have never been systematically tagged, so person-based search is impossible.

The library is managed using [elodie](https://github.com/jmathai/elodie), which organizes files by year/month based solely on EXIF data and maintains no external database. Any tooling built on top of it must respect this constraint: **permanent metadata must live in the files (EXIF/IPTC/XMP), not in a proprietary sidecar or database.**

---

## 2. Goals

1. **Explore the library visually**: a zoomable 2D similarity map of every photo and video, where visually/semantically similar items sit near each other, with search, filters, and overlays layered on top. This is the app's front door.
2. **Search by content** with free text, combining image embeddings (CLIP), LLM-generated tags, and face names.
3. Produce a human-reviewable **quality queue** of low-quality or junk photos, with a simple Keep / Skip / Delete flow and a stated reason for each flag.
4. Detect and surface **near-duplicate groups** with a side-by-side picker to choose which to keep.
5. **Identify faces** across the library: detect, cluster, label clusters by name, and write names and face regions to the files.
6. Use a **local vision LLM** to generate descriptive tags and captions and commit them to file metadata, so they outlive the app.
7. Make the (weeks-long) initial processing of the library **observable**: clear progress, throughput, and ETA for every pipeline.

### Non-goals

- Cloud sync or upload
- Replacing elodie's import/organization workflow
- A general-purpose photo editor
- Automatic deletion without human confirmation
- Authentication in v1 (but see §10 for the hooks that enable it later)

---

## 3. Deployment Environment

| Property | Value |
|---|---|
| Host OS | Ubuntu Linux |
| Hardware | Intel NUC-class machine, Intel Comet Lake UHD integrated graphics |
| GPU | None — CPU-only inference |
| Deployment | Docker Compose on the home media server (shared with other services) |
| Library size | 10,000–100,000 items (JPEG, HEIC, MOV/MP4) |
| Existing tooling | elodie (EXIF-based, no database) |
| Access pattern | Web UI from phone and desktop browsers, LAN only in v1 |

The absence of a GPU is the most significant constraint. All ML workloads must be CPU-viable, and all long-running work is done by a resumable background worker — never in a blocking API call. Because the host also serves media, background work must be throttled and schedulable (§6.11).

---

## 4. Architecture Overview

Two Docker Compose services:

**`app`** — Python/FastAPI application serving the REST API and the built frontend bundle, plus the background worker (same image, separate process or container via a `command:` override). Mounts the photo library read/write and a trash directory. Persists SQLite state and derived artifacts (thumbnails, atlases, embeddings, layout) to named volumes.

**`ollama`** — Ollama model server for LLM tagging. Persists models to a named volume. No GPU flags.

The app writes metadata directly into files using `exiftool`. Files are never copied; the only file moves are to/from the trash directory (§6.8).

### State storage philosophy

SQLite is **workflow scaffolding and index storage** — scan progress, job queues, embeddings, clusters, layout coordinates, write queue. It is not the source of truth for permanent metadata. Everything that should outlive the app is written into the files:

| Data | Lives in |
|---|---|
| Tags, captions | File metadata (§6.10) |
| Person names, face regions | File metadata (§6.10) |
| "Reviewed / keep" decisions | File metadata (custom XMP field) |
| Embeddings, layout, clusters, queues | SQLite / derived-data volume (regenerable) |

A complete database loss should cost only compute time, never human decisions.

---

## 5. Technology Choices

### Backend

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python | Best ecosystem for image processing and local ML; consistent with elodie. |
| Web framework | FastAPI | Async, dependency injection, OpenAPI docs, SSE support for live progress. |
| Database | SQLite via SQLAlchemy (WAL mode) | Zero-config, file-based, sufficient for a household tool. |
| Vector storage | Embeddings in SQLite blobs + in-memory numpy matrix (or `sqlite-vec`) | 100k × 512 float32 ≈ 200 MB; brute-force cosine via matrix multiply is milliseconds–seconds. No ANN index needed at this scale. |
| Metadata I/O | `exiftool` (persistent `-stay_open` process) | Gold standard; handles JPEG, HEIC, and QuickTime/MP4 metadata. |
| Image loading | Pillow + `pillow-heif`, OpenCV | Pillow for I/O and thumbnails (HEIC via plugin); OpenCV for blur/exposure scoring. |
| Video | `ffmpeg` / `ffprobe` | Frame sampling, poster frames, metadata. |
| Image embeddings | CLIP ViT-B/32 (open_clip) via ONNX Runtime | ~5–20 img/s on CPU. Powers the map, text search, duplicates, aesthetic and junk scoring. |
| Layout | UMAP (`umap-learn`) | Projects CLIP embeddings to 2D for the map. |
| Face detection/embedding | InsightFace (SCRFD + ArcFace) via ONNX Runtime, behind a pluggable interface | Much better than dlib HOG on children, profiles, and small faces; CPU-viable. Model weights are non-commercial licensed — fine for personal use. |
| Face clustering | HDBSCAN (or DBSCAN) on ArcFace embeddings | Density-based, noise-tolerant; no need to pre-specify the number of people. |
| LLM tagging | Ollama + small vision model (moondream to start; evaluate Qwen2.5-VL 3B / Gemma 3 4B) | Local, no data leaves the network. See §6.6 for throughput implications. |

### Frontend

| Concern | Choice |
|---|---|
| Build | Vite + TypeScript |
| UI framework | Svelte |
| Map rendering | deck.gl (WebGL) |

Built assets are produced in a multi-stage Docker build and served by FastAPI as static files. Responsive: the map supports exploration on phones; triage queues are fully phone-friendly (a second household member doing review is a goal).

### Why CLIP *and* an LLM?

They play different roles:

- **CLIP is an index.** Fast (whole library in hours), deterministic, and great at visual/semantic similarity and free-text queries ("sunset", "snowy", "birthday cake"). Its vectors are not human-readable and are regenerable, so they live in SQLite.
- **LLM tags are metadata.** Slow (weeks for the full library), sometimes wrong, but human-readable and portable. They're written into the files so other tools (digiKam, Lightroom, Immich) and future-you can use them.
- **Face names are hard filters.** "Tony" is an exact constraint that neither CLIP nor the LLM can supply.

Search blends all three (§6.5).

---

## 6. Feature Modules

### 6.1 Library Scanner

Walks `PHOTO_DIR` recursively and registers every supported file (JPEG, HEIC, MOV, MP4).

**Identity.** Each item is identified by a **content hash** of its decoded pixel data (for video: hash of the media stream, excluding metadata atoms). This is stable across metadata writes (ours and others') and across elodie moving/renaming files. `(path, mtime, size)` is used as a cheap "maybe changed?" pre-check; only when it changes is the content hash recomputed. A known content hash at a new path is treated as a move, not a new item — all history is preserved.

**Per item, the scanner records:** path, type, dimensions, duration (video), `taken_at_local` (`DateTimeOriginal` → `CreateDate` → `DateTime` → file mtime), existing keywords/people/regions/review markers already in the file.

The scanner itself is cheap; heavy per-item work (thumbnails, CLIP, quality, faces, tagging) is enqueued as separate pipeline stages (§6.11).

**Discovery.** A scheduled nightly incremental scan (`SCAN_SCHEDULE`, cron syntax) plus a "Scan now" button. Files that have disappeared are marked missing (not deleted from the DB) until a later scan confirms they're gone or finds them at a new path.

**Videos.** Get full parity with photos: `ffmpeg` samples `VIDEO_SAMPLE_FRAMES` frames (default 5, evenly spaced, skipping first/last 5%), and each downstream stage runs over those frames. Aggregation rules: CLIP embedding = mean of frame embeddings; quality = median of frame scores; faces = union across frames (deduplicated per person); LLM tagging uses the most representative frame (closest to mean embedding).

### 6.2 Thumbnails & Atlases

- Per-item thumbnails at `THUMBNAIL_SIZE` (default 400px long edge) plus a tiny map thumbnail (64px), stored in the derived-data volume.
- For the map, 64px thumbnails are packed into texture **atlases** (e.g. 4096×4096 sheets, 4,096 thumbs each) regenerated after layout changes, so the browser loads a few dozen images instead of 100k.

### 6.3 CLIP Embeddings

Every photo (and video, via frame mean) gets a 512-d L2-normalized CLIP embedding. Stored in SQLite; loaded into an in-memory numpy matrix by the API process for search and similarity. This single pass drives the map, text search, near-neighbor browsing, duplicate detection, aesthetic scoring, and junk detection.

### 6.4 Exploration Map (primary UI)

The app's home screen: every item as a point in a 2D layout produced by UMAP over CLIP embeddings, so similar images cluster together.

**Layout.** UMAP is **re-fit after every scan** that adds items (a few minutes on CPU for 100k points). Some reshuffling between re-fits is accepted. Layout coordinates are stored per item; the frontend fetches them as a compact binary array. During the initial backfill the map shows whatever has been embedded so far and grows as processing continues.

**Rendering (level of detail).** Zoomed out: colored dots. Zoomed in past a threshold: dots are replaced by thumbnails from the atlases. Tapping/clicking opens the lightbox.

**Search.** A search bar on the map. Results **highlight in place** — matches glow, everything else dims — so the layout (and your spatial memory of it) stays fixed. A result count and "fly to best match" affordance are shown.

**Filters.** Date-range slider and person chips; these stack with text search (dim non-matching items).

**Color-by overlays.** Year, person, quality score, duplicate group, pipeline status (e.g. "not yet tagged"), media type.

**Lightbox & similarity walk.** Opening an item shows it full-size with metadata, tags, people, and a strip of its nearest CLIP neighbors. Clicking a neighbor opens it and re-centers the map on it — a way to wander through the library.

**Lasso select → bulk actions** (desktop only). Draw around a region to select items, then: move to trash, add/remove a tag, assign to a person, or send to the quality review queue.

**Device split.** Phones: pan, pinch-zoom, search, filters, lightbox. Desktop: all of the above plus lasso and bulk actions. Performance target: smooth pan/zoom with 100k points on a mid-range phone.

### 6.5 Search

Hybrid query pipeline:

1. **Person filter** — names that match labeled people become hard filters (`PersonInImage`).
2. **Keyword match** — terms matched against LLM tags, captions, and existing keywords (SQLite FTS5).
3. **CLIP similarity** — the full query text is embedded with CLIP's text encoder and scored against all image embeddings.
4. Scores are blended (keyword hits boost CLIP rank); results above a similarity floor are "matches" for highlighting.

Search works from day one on CLIP alone and improves as LLM tagging and face labeling progress.

### 6.6 LLM Tagging

The lowest-priority pipeline stage. For each item, send an image (or representative video frame) to Ollama with a prompt requesting JSON:

- `description`: one plain-language sentence
- `tags`: flat list of lowercase keywords (setting, objects, activities, colors, weather, time of day; people by role/appearance)

If faces in the image are already labeled, names are included in the prompt context so the caption can use them.

Tags are written **flat, mixed with existing keywords** (§6.10), and accumulate across runs. SQLite records every tag the app has written (item, tag, source, model, timestamp) so that AI-added tags can be listed or removed later, even though the file doesn't distinguish them.

**Throughput reality check.** At 1–4 items/min on CPU, 100k items is **~17–70 days of continuous running**; restricted to an 8-hour overnight window it's **~2–6 months**. This is why search must not depend on tagging, why quiet hours are optional (§6.11), and why the progress UI (§7) must give an honest ETA. Model choice should be re-evaluated on real photos against speed.

### 6.7 Duplicate Detection

Uses CLIP embeddings only. Pairs with cosine similarity ≥ `DUPLICATE_SIMILARITY_THRESHOLD` (default 0.95, to be calibrated) are linked; connected components (union-find) of size ≥ 2 become duplicate groups. Computed blockwise via matrix multiply (100k² similarity in chunks) — seconds to minutes, no LSH needed.

Within a group, items are ranked by quality score to recommend the best keeper.

**Review UI.** Side-by-side grid (swipeable on phone), recommended pick highlighted, actions: keep selected / trash the rest, keep all (marks group reviewed), skip.

### 6.8 Quality Review

**Signals** (each normalized 0–1):

| Signal | Method |
|---|---|
| Blur | Laplacian variance on grayscale, `tanh(variance / 200)` |
| Exposure | Fraction of pixels clipped at 0 or 255; penalty beyond 2% either end |
| Resolution | Megapixels, logistic curve centered at 2 MP |
| Aesthetic | LAION aesthetic predictor (small MLP on CLIP embeddings) |
| Junk | CLIP zero-shot vs. prompts: screenshot, receipt, document, whiteboard, accidental/pocket shot, blank frame |

A composite score (weights configurable) plus junk probability decide entry into the queue (`QUALITY_THRESHOLD`, `JUNK_THRESHOLD`). Each flagged item displays **why** it was flagged ("blurry", "likely screenshot", "overexposed"), which also helps calibrate thresholds.

**Review UI.** One item at a time, worst-first, phone-friendly. Keep / Skip / Delete. Keep writes a reviewed marker to the file (§6.10) so it's never re-queued, even after a DB rebuild.

**Deletion = move to trash.** All deletes (quality, duplicates, lasso) move the file to `TRASH_DIR`, preserving its relative path. The trash view lists trashed items with restore. Items are permanently purged after `TRASH_RETENTION_DAYS` (default 30). `TRASH_DIR` must be outside `PHOTO_DIR` so elodie and the scanner never see it.

### 6.9 Face Identification

Face detection and embedding are behind a **pluggable interface** (`detect(image) → [bbox, landmarks, score]`, `embed(face) → vector`); v1 implementation is InsightFace (SCRFD + ArcFace, 512-d).

1. **Detect & embed** — per item (per sampled frame for video). Faces below `FACE_MIN_SIZE` or detection score threshold are dropped. Existing face regions found in the file (e.g. from Apple Photos/digiKam exports) are imported as pre-labeled seeds.
2. **Cluster** — HDBSCAN over unassigned embeddings (cosine distance). Noise points become singletons.
3. **Label** (UI) — cluster cards sorted by size, each a grid of face crops. Actions: name the cluster (autocomplete existing people), merge clusters (drag or multi-select), split out selected faces, mark "not a face" / "ignore this person". After naming, the UI suggests other clusters likely to be the same person ("Is this also Tony?").
4. **Recognize** — new/unassigned faces are matched to labeled people by nearest labeled faces (k-NN, more robust than centroids across age ranges). Matches within threshold are auto-assigned; borderline matches go to a confirmation queue. Runs automatically after each detection batch.

Naming writes to the file: `XMP-iptcExt:PersonInImage`, the person's name as a keyword, and the face bounding box as an MWG region (`XMP-mwg-rs:RegionInfo`, type `Face`).

**Limitations.** Very small background faces, heavy occlusion, and extreme angles will be missed. Age progression (a person photographed 20 years apart) may split into multiple clusters — the merge flow and k-NN recognition with labeled examples across ages mitigate this.

### 6.10 Metadata Write-back

All file metadata writes go through a **write queue** in SQLite, applied by the worker:

- `exiftool -overwrite_original_in_place` (no `_original` backup files littering the library; preserves inode/permissions), file mtime preserved.
- `EXIF_WRITES_ENABLED=false` dry-run mode: writes are queued and logged but not applied — for early testing against the real library.
- A **write log** records before/after values for every field changed, enabling audit and revert.
- Writes to the same file are coalesced.

**Field mapping:**

| Data | JPEG / HEIC | MOV / MP4 |
|---|---|---|
| Tags | `IPTC:Keywords` + `XMP-dc:Subject` | `XMP-dc:Subject` + `QuickTime:Keywords` |
| Caption | `XMP-dc:Description` + `EXIF:ImageDescription` | `XMP-dc:Description` |
| People | `XMP-iptcExt:PersonInImage` + keyword | same (XMP) |
| Face regions | `XMP-mwg-rs:RegionInfo` | `XMP-mwg-rs:RegionInfo` |
| Reviewed marker | `XMP-phototriage:Reviewed` (e.g. `quality,duplicates`) | same |

(Exact tag support per container to be verified with `exiftool` during Phase 2.)

### 6.11 Background Worker & Scheduling

A **single worker** processes a persistent, priority-ordered job queue in SQLite. Pipeline stages per item form a dependency chain:

```
scan ─► thumbnail ─► clip ─┬─► quality/junk
                           ├─► faces ─► recognize
                           └─► (layout re-fit, duplicate grouping, atlases — batch jobs)
                     llm_tag (lowest priority; waits for faces when available)
metadata writes (high priority, small)
```

- **Priority order:** metadata writes > scan > thumbnails > CLIP > quality > faces > batch jobs > LLM tagging. So the map and search become usable first, and tagging trickles in behind.
- **Resource limits:** CPU/memory caps via Compose (`cpus:`, `mem_limit:`); `WORKER_THREADS` for ONNX/BLAS.
- **Quiet hours:** optional `WORKER_WINDOW` (e.g. `22:00-07:00`) per stage class, so heavy stages (LLM tagging, optionally all ML) only run overnight.
- **Pause / resume** from the UI, globally or per stage.
- **Resumable:** job state is durable; the worker picks up where it left off after restart. Failed jobs retry with backoff, then park in an error list.

---

## 7. Progress & Monitoring

The initial processing of a large library will take **weeks** (LLM tagging possibly months). Progress must be visible at a glance and in detail.

**Global status indicator.** A compact indicator in the app header, visible from every view: overall "library readiness" and whether the worker is running, paused, or waiting for its quiet-hours window ("Idle until 22:00"). Tapping it opens the Activity page.

**Activity page.** One row per pipeline stage (scan, thumbnails, CLIP, quality, faces, recognition, LLM tagging, metadata writes, batch jobs):

- done / total, with a progress bar, and counts of pending, in-progress, errored, skipped
- **throughput** (rolling average, items/min) and **ETA** — computed against the configured working window, not wall-clock (e.g. "~23 nights remaining" rather than a misleading "~8 days")
- currently processing item (thumbnail + path)
- last run / next scheduled run for batch jobs (layout re-fit, duplicate grouping, nightly scan)
- per-stage pause/resume

**Library coverage summary.** "What can I do yet?" in plain terms: e.g. "Map & search: 100% · Faces detected: 64% · Tagged: 8% · Duplicates reviewed: 12 of 140 groups".

**History.** A daily chart of items processed per stage (so you can see whether overnight runs happened and how fast they went), stored in a small `job_stats` table.

**Errors.** A browsable list of failed items with the error, retry button, and "ignore" option (e.g. corrupt files).

**Live updates.** The frontend receives progress via Server-Sent Events (`/api/events`); no polling storm from multiple open tabs.

**Map integration.** The "pipeline status" color-by overlay (§6.4) shows spatially which items are still unprocessed.

**Optional notifications (later).** A webhook hook (`NOTIFY_WEBHOOK_URL`, e.g. ntfy/Gotify/Home Assistant) for milestones ("CLIP pass complete — map is ready") and error spikes.

---

## 8. Data Model

```
items
  id, content_hash (unique), path, media_type (photo | video)
  file_size_bytes, file_mtime_ns, width, height, duration_s
  taken_at_local, first_seen_at, last_seen_at, missing_since
  status (active | trashed | purged | missing)

item_frames                       -- videos only
  id, item_id, frame_index, timestamp_s

embeddings
  item_id, model, vector (blob)   -- CLIP; video = frame mean

layout
  item_id, x, y, layout_version

quality
  item_id, blur, exposure, resolution, aesthetic, junk_label, junk_score,
  composite, flag_reasons (json)

reviews                            -- quality + duplicate decisions
  id, item_id, kind (quality | duplicate), decision (keep | delete | skip),
  actor, decided_at

duplicate_groups
  id, status (pending | resolved | skipped), created_at, resolved_at, actor
duplicate_members
  group_id, item_id, rank

people
  id, name, created_at, actor

face_clusters
  id, person_id, created_at

faces
  id, item_id, frame_id (nullable), cluster_id, person_id,
  bbox_x, bbox_y, bbox_w, bbox_h (normalized), det_score,
  embedding (blob), source (detected | imported), assigned_by (cluster | recognition | manual), actor

tags
  item_id, tag, source (llm | user | imported), model, created_at, actor
captions
  item_id, text, model, created_at

jobs                               -- error: retries after retry_at; parked: gave up
  id, item_id (nullable), stage, priority, status (pending | running | done | error | parked),
  attempts, last_error, enqueued_at, retry_at, started_at, finished_at

job_stats                          -- for history charts / throughput
  date, stage, processed, errors, busy_seconds

metadata_writes                    -- write queue + log
  id, item_id, fields (json), before (json), after (json),
  status (queued | applied | dry_run | error), actor, created_at, applied_at

trash
  item_id, original_path, trash_path, trashed_at, actor, purge_after
```

Every human decision records an `actor` (see §10).

Column names say what their values mean. Every `*_at` column is a UTC timestamp, except `taken_at_local`, which is the capture time as the camera recorded it, with no timezone. Sizes, durations and raw timestamps carry their unit in the name (`file_size_bytes`, `duration_s`, `file_mtime_ns`). Image dimensions are in pixels.

---

## 9. Configuration

Environment variables, documented in `.env.example`:

| Variable | Default | Description |
|---|---|---|
| `PHOTO_DIR` | *(required)* | Photo library path (dev machine: `P:\`; NUC host: `/mnt/pictures`; in Docker: bind-mounted to `/photos`) |
| `TRASH_DIR` | *(required)* | Trash location, outside `PHOTO_DIR` |
| `DATA_DIR` | `./data` | SQLite database and derived files (in Docker: `/data`, bind-mounted from a host folder) |
| `APP_PORT` | `8000` | Host port for the web UI |
| `SCAN_SCHEDULE` | `0 2 * * *` | Cron schedule for incremental scans |
| `WORKER_WINDOW` | *(unset = always)* | Quiet-hours window for heavy stages, e.g. `22:00-07:00` |
| `WORKER_THREADS` | `4` | Threads for ONNX/BLAS inference |
| `OLLAMA_MODEL` | `moondream` | Vision model for tagging |
| `CLIP_MODEL` | `ViT-B-32` | CLIP model |
| `QUALITY_THRESHOLD` | `0.35` | Composite quality score below which items are flagged |
| `JUNK_THRESHOLD` | `0.7` | Junk probability above which items are flagged |
| `DUPLICATE_SIMILARITY_THRESHOLD` | `0.95` | CLIP cosine similarity for duplicate linking |
| `FACE_MIN_SIZE` | `40` | Minimum face size in pixels |
| `FACE_MATCH_THRESHOLD` | *(tbd)* | Recognition distance threshold |
| `VIDEO_SAMPLE_FRAMES` | `5` | Frames sampled per video |
| `THUMBNAIL_SIZE` | `400` | Long-edge thumbnail size |
| `TRASH_RETENTION_DAYS` | `30` | Days before trashed files are purged |
| `EXIF_WRITES_ENABLED` | `false` | Apply queued metadata writes (off = dry run) |
| `AUTH_MODE` | `none` | `none` or `forward_auth` (§10) |
| `AUTH_USER_HEADER` | `X-Forwarded-Email` | Identity header when `AUTH_MODE=forward_auth` |
| `NOTIFY_WEBHOOK_URL` | *(unset)* | Optional milestone/error notifications |

---

## 10. Users & Future SSO

**v1:** no authentication; LAN-only; all users share the same state. Review queues lease items briefly so two people reviewing at once don't see the same item.

**Hooks for later SSO** (e.g. Google SSO via oauth2-proxy, Authelia, or Traefik/Caddy forward-auth — the pattern used across a home lab):

- The app never implements OAuth itself. With `AUTH_MODE=forward_auth`, it trusts an identity header set by the reverse proxy (`AUTH_USER_HEADER`) and rejects requests without it. With `AUTH_MODE=none`, the actor is `anonymous` (or an optional self-selected display name stored in a cookie).
- All identity resolution goes through one FastAPI dependency (`current_actor`), so switching modes is configuration only.
- Every human decision is recorded with an `actor` from day one, so per-user history and stats work retroactively once SSO is enabled.
- Only trust the header when requests come from the proxy (documented deployment: app port not exposed directly).

---

## 11. Phases of Work

### Phase 1 — Infrastructure & Scaffold
Compose stack (app, worker, ollama), SQLite schema and migrations (Alembic), FastAPI skeleton, `current_actor` dependency, Vite + Svelte frontend built into the image. Job queue and worker loop with priorities, pause/resume, and quiet-hours window. Activity page skeleton with SSE. Confirm it runs against a test directory.

### Phase 2 — Scanner, Thumbnails & CLIP
Directory walker, content hashing, move detection, EXIF/video metadata extraction (JPEG, HEIC, MOV/MP4), video frame sampling, thumbnails, CLIP embeddings. Stage progress, throughput, and ETA on the Activity page. Verify exiftool field support per container. Add the "No escape" safety test (dev-environment §7) here: it needs stages that touch files, so Phase 1 has nothing for it to check.

### Phase 3 — Exploration Map & Search (first usable milestone)
UMAP layout job, atlas generation, deck.gl map with LOD, CLIP text search with highlight-in-place, lightbox with nearest-neighbor walk, date filter, color-by overlays. Read-only. Mobile performance testing with 100k points.

### Phase 4 — Trash, Write-back & Duplicates
Trash directory moves, restore, auto-purge. Metadata write queue with dry-run and write log. CLIP duplicate grouping and review UI. Lasso select and bulk actions on desktop. Calibrate duplicate threshold.

### Phase 5 — Quality Review
Heuristic scores, aesthetic predictor, zero-shot junk detection, flag reasons, review queue, reviewed markers in files. Calibrate thresholds against the first 100 flagged items.

### Phase 6 — Faces
Pluggable detector/embedder with InsightFace, region import, HDBSCAN clustering, cluster-card labeling UI with merge/split/suggestions, k-NN recognition with confirmation queue, names + MWG regions written to files, person filter and overlay on the map.

### Phase 7 — LLM Tagging
Ollama service, model evaluation (moondream vs. alternatives) on real photos for quality and speed, tagging stage with face-name context, tag/caption write-back, FTS5 keyword index, hybrid search blending. Honest long-horizon ETA on the Activity page.

### Phase 8 — Hardening, SSO Hooks & Polish
`forward_auth` mode and deployment docs for running behind a proxy. Corrupt-file handling, error list UX, resumability and concurrency review (multiple tabs, scan during tagging), history charts, optional notification webhook, empty/loading states, mobile layout pass, deployment documentation.

---

## 12. Open Questions

- **Tagging model:** moondream vs. Qwen2.5-VL 3B vs. Gemma 3 4B — needs an empirical bake-off on real family photos for caption quality vs. items/min.
- **Threshold calibration:** duplicate similarity (0.95), quality/junk thresholds, face match distance, UMAP parameters (`n_neighbors`, `min_dist`) — all need tuning against the real library.
- **Video aggregation details:** whether 5 frames is enough, how long videos should be sampled, and whether per-frame quality should flag only the worst segment.
- **Layout churn:** "always re-fit" is the chosen behavior; if reshuffling after each scan proves disorienting, fall back to fitting once and placing new items with `umap.transform()`.
- **Vector storage:** plain SQLite blobs + numpy vs. `sqlite-vec` — decide during Phase 2 based on memory footprint and query latency.
- **exiftool coverage:** confirm MWG regions and custom XMP namespaces write cleanly to HEIC and MP4/MOV.
- **Mobile map performance:** whether 100k points + atlases are smooth on the household's actual phones; fallback is dots-only on mobile.
