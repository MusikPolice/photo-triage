---
name: plan-phase
description: Break a phase of docs/plan.md §11 into PR-sized GitHub issues. Use when the current phase milestone has no issues yet, or when the user asks to plan a phase or add issues to one.
---

# Plan a phase

Turn one phase of `docs/plan.md` §11 into issues, each of which one PR can close.
`scripts/tracker.py` does every GitHub write. This skill covers the judgement:
how to split the phase and what each issue says.

## 1. Find the phase

Run `scripts/tracker.py status`. Plan the phase the user named, or else the
current one. If its milestone already has issues, plan only what's missing and
say which existing issues you're building on.

## 2. Read before splitting

- The phase paragraph in plan §11, then every section it relies on, in full.
  Phase paragraphs are summaries, and the requirements live in §5–§10.
- `docs/dev-environment.md` §6 (testing strategy) and §7 (safety invariants).
  These apply to every issue that touches them.
- plan §12 (open questions) for anything in this phase that's still undecided.
- What already exists in the repo, so issues don't repeat finished work.

## 3. Split

Each issue must:

- **Fit in one reviewable PR.** The user reads every PR, so aim for roughly a
  day of work and a diff someone can read in one sitting. Split anything bigger.
- **Leave `main` working.** It's tested and passes `just check` by itself, with
  no half-built features behind it.
- **Be thin and vertical where possible.** A small end-to-end slice (schema,
  then logic, then API, then UI) is better than one issue per layer, unless a
  layer is a real foundation that other issues build on.
- **Trace to the spec.** Cite at least one section. Don't add requirements the
  spec doesn't have. If the spec is ambiguous or silent, collect the question
  for the user and don't guess.
- **Turn plan §12 questions into work only on purpose.** Either make an explicit
  spike or calibration issue, or name the question under "Out of scope".

Order the issues so foundations come first, and use `Depends on` only for real
ordering constraints. Infrastructure goes first in a phase only when the next
issues need it.

## 4. Write drafts

Write one file per issue to `.drafts/phase-<N>/NN-short-slug.md`. The directory
is gitignored, and `NN` is the filing order. The format is in the docstring of
`scripts/tracker.py`. Section by section:

- **Spec:** list the sections as `plan §6.11` or `dev-environment §7`. Add a
  short note per line when only part of a section applies.
- **Deliverable:** describe in a few sentences what exists when the issue is
  done, from the user's point of view where that applies.
- **Acceptance criteria:** use `- [ ]` items that a test, a command or a short
  manual check can verify. Name the test level when it matters (unit,
  integration, safety). Avoid "works well" and "is fast" unless there's a
  number to check against.
- **Out of scope:** list what a reader might expect here that comes later, and
  which issue or phase covers it.
- **Depends on** (optional): use `draft:NN-short-slug` for drafts in this batch
  and `#N` for issues that are already filed.

Run `scripts/tracker.py new .drafts/phase-<N>/*.md --dry-run` until it passes.

## 5. Review with the user, then file

Show the user a table (order, title, labels, depends on) and the drafts
directory, along with any spec questions from step 3. **Don't file until they
approve.** Make the changes they ask for and run the dry run again.

Once they approve, run `scripts/tracker.py new .drafts/phase-<N>/*.md`. The shell
glob passes the files in `NN` order, which is the filing order. Report the issue
numbers it prints.
