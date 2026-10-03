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
- To work an issue, use the `work-issue` skill: `start N`, build, then
  `finish` opens the PR.
- **Never commit to `main` and never merge PRs.** The user reviews and merges
  every PR.
- Work that's outside the current issue goes in the PR's "Follow-ups" section,
  not into the branch.

## Checks

`just check` runs everything CI runs on a PR: pre-commit, lint, the
file-mutation guard, import contracts, pyright and tests. Run `just` to list all
recipes. Only `photo_triage.files` may move, delete or rewrite files
(dev-environment §7). The checks enforce this, so don't route around them.
