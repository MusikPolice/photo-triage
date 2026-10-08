# photo-triage

A self-hosted photo library explorer and triage tool.

- `docs/plan.md` is the spec. §11 lists the phases of work.
- `docs/dev-environment.md` covers tooling, testing strategy (§6), safety
  invariants (§7) and checks (§8).

## Tracking work

Each phase in plan §11 is a GitHub milestone, and each deliverable is an issue
that one PR closes. `scripts/tracker.py` does every mechanical step. Run it
rather than calling `gh` by hand, so issues, branches and PRs stay consistent.

- **Start every session with `scripts/tracker.py status`.** It shows the current
  phase, which issues are in progress, ready or blocked, and which PRs are open.
- To plan a phase, use the `plan-phase` skill. Draft issues and get the user's
  approval before filing.
- To file a single issue (a follow-up, bug, idea or spike), use the `new-issue`
  skill. It interviews the user with `grill-me` until the what, why and how
  are agreed.
- To work an issue, use the `work-issue` skill: `start N`, build, then
  `finish` opens the PR.
- **Never commit to `main` and never merge PRs.** The user reviews and merges
  every PR.
- Work that's outside the current issue goes in the PR's "Follow-ups" section,
  not into the branch.

## Keeping docs current

Update the docs in the same PR as the change, without being asked. Docs that
lag behind the code mislead the next session as well as the user.

- **`docs/dev-environment.md`, `README.md` and `CLAUDE.md`** describe how the
  project is built, run, tested and tracked. When a change affects any of
  that, update them. That includes new tools, recipes, checks, CI jobs, scripts,
  repo settings and workflow steps. Mark finished items in
  dev-environment "Next steps" as done, with the date.
- **`docs/plan.md` is the spec, and changing it is a decision.** If the
  implementation needs to diverge from it, or the work answers a question in
  plan §12, propose the edit to the user and don't change the spec quietly.
  Once they agree, update the spec in the same PR.
- `docs/dev-environment.md` also records decisions: where a tool comes from,
  what's pinned, how CI and the workflow are designed. Changing one of those is
  the user's call, like a spec change, so ask first. Describing how things
  currently work (a new recipe, a check that landed) just needs updating.
- In a PR, list doc updates in the summary so the reviewer sees them.

## Debugging

- Logs are in `DATA_DIR/logs/<process>.log` (and stderr). `LOG_LEVEL=DEBUG` adds a
  line per job and per request, and the SQL. See dev-environment §5 "Logging".
- The worker has dev-only switches, listed in dev-environment §5 and not in plan §9:
  `just worker --once` runs every ready job and exits, `FAKE_NOW` sets its clock,
  and `WORKER_NOOP_STAGE=true` with `just worker noop N` queues test jobs.

## Looking at pages

Claude Code can see the frontend without a browser: `just shot /activity`
saves screenshots at desktop and phone widths, and Claude reads the PNGs. Use
it to troubleshoot a layout the user reports, and to check any frontend change
before opening its PR.

- **Setup, once per machine:** run `scripts/bootstrap.sh`. It installs
  Playwright's headless Chromium, and asks for your sudo password once to
  install the system libraries it needs. Then `just doctor` should report
  "Chromium installed" and "Chromium's system libraries present". After a
  Playwright upgrade in `pnpm-lock.yaml`, `just doctor` says to run the
  bootstrap again.
- **Taking screenshots:** `just shot PATH` needs nothing running. It starts a
  scratch copy of the app (empty folders, no `.env`, free ports), shoots and
  stops it. `--noop N` queues work so the Activity page has something to show,
  and `--click`/`--wait` reach states like "paused". For many shots, keep
  `just preview` running in another terminal (Claude runs it in the
  background) and `just shot` uses it. `just shot --help` lists the options.
- **On PRs:** a PR that changes how a page looks lists its screenshots under
  `## Screenshots` in the PR draft, as
  `![caption](.screenshots/scratch/NAME.png)`, and `finish` publishes them.
  Only scratch-stack screenshots can be published. The repo is public, and
  `just shot --base` against your own `just dev` can show real photos, so those
  stay local.

## Checks

`just check` runs everything CI runs on a PR: pre-commit, lint, the
file-mutation guard, import contracts, pyright, tests, the frontend checks and
the API types contract. Run `just` to list all recipes. After changing an API
model or route, run `just api-types` and commit the regenerated types. Only `photo_triage.files` may move, delete or rewrite files
(dev-environment §7). The checks enforce this, so don't route around them.
