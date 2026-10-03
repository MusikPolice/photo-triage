---
name: new-issue
description: File one GitHub issue (a follow-up, bug, idea, or spike) after grilling the user until we agree on what is to be built, why, and how. Use when the user asks to create, file, or open an issue, or accepts an offer to turn a PR follow-up into one. For planning a whole phase, use plan-phase.
---

# New issue

Turn one piece of work into an issue that `work-issue` can pick up and finish
without guessing. Most of the effort goes into agreeing with the user on
**what** is to be built, **why**, and **how**. The draft only records what was
agreed. `scripts/tracker.py` does the validating and filing.

## 1. Gather context first

Before asking anything, collect what's already known, so the questions cover
only what the files can't answer:

- The request itself and the surrounding conversation. If it came from a PR's
  follow-ups, read that PR with `gh pr view N`.
- The spec sections it touches in `docs/plan.md`, any open question in plan §12
  that bears on it, and `docs/dev-environment.md` §6 and §7 if it involves tests
  or files.
- The current code in the area.
- Existing issues, from `scripts/tracker.py status` and
  `gh issue list --state all --search "<keywords>"`. If an issue already covers
  this, say so and offer to extend that one instead.

## 2. Grill

Invoke the `grill-me` skill (`.claude/skills/grill-me`) and interview the user
until every branch below is resolved. Don't ask what step 1 already answered.
State what you found and move on.

Work through these branches, letting earlier answers prune later ones:

- **Why.** What problem does this solve, for whom, and why now? For a bug: what
  happens, what should happen, and how to reproduce it.
- **What.** What exists when it's done, as the user would see it, and how big
  is it? If it won't fit in one reviewable PR, agree on how to split it, and
  file each part through this skill.
- **Spec fit.** Which plan or dev-environment sections does it implement?
  - If the spec doesn't cover it, or contradicts it, decide whether it belongs
    under an existing section or needs a spec change.
  - A spec change is a decision for the user. Propose the edit, and once they
    agree, add "update plan §X" to the acceptance criteria.
- **How.** The approach, and the alternatives you're rejecting and why. Ask
  only where the choice really matters: data model, interfaces, dependencies,
  anything touching the safety invariants, anything hard to undo. Leave the
  minor choices to whoever implements it.
- **Done.** Acceptance criteria someone can check with a test, a command, or a
  short manual step. Turn vague goals ("fast", "robust") into numbers or drop
  them.
- **Not this time.** What a reader might expect that's deliberately left out,
  and where it goes instead.
- **Where and when.** The phase (the current one by default, or a later one if
  it clearly belongs there), the area labels and `bug` if it applies, and any
  issues that must merge first.

Finish the grilling with grill-me's summary of decisions, and confirm it with
the user.

## 3. Draft

Write the agreed result to `.drafts/issue-<short-slug>.md`, in the draft format
documented in `scripts/tracker.py`.

- `## Background` holds the why.
- `## Approach` holds the how: the decisions made and the alternatives
  rejected, so whoever implements it doesn't reopen them.
- Every other section comes straight from the grilling. Don't add anything
  that wasn't agreed.

Run `scripts/tracker.py new .drafts/issue-<short-slug>.md --dry-run` and fix
anything it rejects.

## 4. File

Show the user the draft. If the grilling was long, highlight anything you had to
phrase on their behalf. File it only after they approve:
`scripts/tracker.py new .drafts/issue-<short-slug>.md`. Report the issue number
and link, and which phase it went into.
