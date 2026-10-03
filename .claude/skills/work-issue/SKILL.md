---
name: work-issue
description: Implement one GitHub issue end to end on its own branch and open a PR for the user to review. Use when the user asks to work on the next issue, a specific issue number, or to address review feedback on an open PR.
---

# Work an issue

One issue, one branch, one PR. `scripts/tracker.py` handles the git and GitHub
mechanics. The user reviews and merges every PR, so **never merge, and never
commit to `main`.**

## 1. Pick the issue

Run `scripts/tracker.py status`.

- If the user named an issue, use it.
- If an issue is **In progress** and its branch exists locally, resume it.
- Otherwise take the lowest-numbered **Ready** issue in the current phase.

Tell the user which issue you're taking. If the phase has no issues, use the
`plan-phase` skill instead.

## 2. Start

Run `scripts/tracker.py start N`. It branches from an up-to-date `origin/main`,
assigns the issue and prints its body. If it refuses because a dependency is
still open, tell the user and don't pass `--force` unless they say so.

Read every spec section the issue cites in full, and the safety invariants in
`docs/dev-environment.md` §7 if the work touches files, metadata or deletion.

## 3. Build

- Stay within the issue. If you find needed work outside it, note it for the PR
  under "Follow-ups" and don't do it. If the spec and the issue disagree, or the
  spec is silent on something that matters, stop and ask.
- Write tests alongside the code at the levels in dev-environment §6. Each
  acceptance criterion should map to something that checks it.
- Update the docs the change affects, as `CLAUDE.md` describes under "Keeping docs
  current". Propose any edit to `docs/plan.md` to the user and don't make it
  yourself.
- Commit in logical steps with clear messages. Run `just check` before
  finishing. `finish` runs it again and refuses to continue if it fails.

## 4. Verify each acceptance criterion

Go through the issue's criteria one by one and confirm each against the work:
run the test or command, or do the manual check. If a criterion can't be met as
written, tell the user. Don't quietly reinterpret it.

## 5. Finish

Write `.drafts/pr-N.md` with these sections:

- `## Summary`: what changed and why, plus anything surprising.
- `## Verification`: one bullet per acceptance criterion saying how it was
  checked (test name, command, or manual step and result).
- `## Follow-ups` (if any): work found but not done. Offer to draft issues for it.
- `## Notes for review` (optional): where to look first, risky spots, and
  decisions the reviewer might question.

Run `scripts/tracker.py finish .drafts/pr-N.md`. It checks the branch is
current with `origin/main`, runs `just check`, pushes, and opens the PR. The PR
gets the issue's acceptance criteria as a checklist for the reviewer, plus
`Closes #N`. If the PR already exists, `finish` updates its body instead.

Then watch CI with `gh pr checks --watch`. Fix any failure on the same branch and
run `finish` again. When CI is green, give the user the PR link and stop.

## Review feedback

When the user leaves comments, read them with `gh pr view N --comments`. Inline
review comments come from
`gh api repos/{owner}/{repo}/pulls/N/comments`. Address them on the same branch
and update `.drafts/pr-N.md` if the summary changed, then run `finish` again.

## After a merge

`start` always branches from `origin/main`, so a stale local `main` doesn't
matter. Once the PR is merged you can tidy up with
`git switch main && git pull --ff-only` and `git branch -d <branch>`.
