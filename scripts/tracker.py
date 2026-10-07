#!/usr/bin/env python3
"""Track work toward the spec on GitHub (see CLAUDE.md, "Tracking work").

Each phase in docs/plan.md §11 is a milestone. Each deliverable is an issue
that fits in one PR, and each PR closes its issue. This script owns the
mechanical steps so they happen the same way every time:

  setup                 create or refresh phase milestones and labels (idempotent)
  status                current phase, its issues (in progress / ready / blocked), open PRs
  new DRAFT... [--dry-run]  validate issue drafts, then file them in the order given
  start N               branch N-slug off origin/main, assign the issue, label it in-progress
  finish SUMMARY        run `just check`, push, and open or update the PR closing the issue
  feedback [N]          print the reviews and comments on PR N (default: this branch's PR)

Issue draft format (docs/plan.md and docs/dev-environment.md sections are
cited as "plan §6.11" or "dev-environment §7"):

  ---
  title: Job queue with priorities and pause/resume
  phase: 1
  labels: backend
  ---
  ## Background          (optional)
  Why this is needed now, and the context a reader needs.
  ## Spec
  - plan §6.11
  ## Deliverable
  What exists when this is done.
  ## Acceptance criteria
  - [ ] A checkable statement.
  ## Out of scope
  What this issue deliberately leaves for later.
  ## Approach            (optional)
  How it will be built: decisions already agreed, and the alternatives rejected.
  ## Depends on          (optional)
  - #12
  - draft:01-alembic-schema

Labels: at least one area label, plus optionally "bug".

A "draft:<file stem>" dependency names another draft filed earlier in the same
`new` run. It's replaced with that draft's issue number as it's filed.

The PR summary file passed to `finish` needs "## Summary" and "## Verification"
sections. Verification says how each acceptance criterion was checked.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = {
    "plan": REPO_ROOT / "docs" / "plan.md",
    "dev-environment": REPO_ROOT / "docs" / "dev-environment.md",
}

IN_PROGRESS = "in-progress"
AREA_LABELS = {
    "backend": ("1d76db", "Python backend: API, worker, pipeline, files"),
    "frontend": ("5319e7", "Svelte and deck.gl frontend"),
    "infra": ("6e7781", "Docker, CI, tooling, dev environment"),
    "documentation": ("0075ca", "Improvements or additions to documentation"),
}
KIND_LABELS = {"bug": ("d73a4a", "Something isn't working")}
LABELS = {
    **AREA_LABELS,
    **KIND_LABELS,
    IN_PROGRESS: ("fbca04", "Being worked on in an open branch"),
}

REQUIRED_SECTIONS = ("Spec", "Deliverable", "Acceptance criteria", "Out of scope")
OPTIONAL_SECTIONS = ("Background", "Approach", "Depends on")
SUMMARY_SECTIONS = ("Summary", "Verification")

ATTRIBUTION = "🤖 Generated with [Claude Code](https://claude.com/claude-code)"

SPEC_REF = re.compile(r"\b(plan|dev-environment)\s+§(\d+(?:\.\d+)*)")
CHECKBOX = re.compile(r"^\s*- \[[ xX]\] \S", re.MULTILINE)
ISSUE_REF = re.compile(r"#(\d+)\b")
DRAFT_REF = re.compile(r"\bdraft:([\w.-]+)")


# ---------------------------------------------------------------------------
# Parsing (pure; covered by backend/tests/unit/test_tracker.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Phase:
    number: int
    name: str
    description: str

    @property
    def milestone(self) -> str:
        return f"Phase {self.number} — {self.name}"


@dataclass(frozen=True)
class Draft:
    title: str
    phase: int | None
    labels: list[str]
    body: str


def parse_phases(plan: str) -> list[Phase]:
    """Phases from the "Phases of Work" section of docs/plan.md."""
    match = re.search(r"^## \d+\. Phases of Work\n(.*?)(?=^## |\Z)", plan, re.M | re.S)
    if not match:
        return []
    phases: list[Phase] = []
    for m in re.finditer(
        r"^### Phase (\d+) — ([^\n]+)\n(.*?)(?=^### |^---|\Z)", match.group(1), re.M | re.S
    ):
        phases.append(Phase(int(m.group(1)), m.group(2).strip(), m.group(3).strip()))
    return phases


def phase_number(milestone_title: str) -> int | None:
    m = re.match(r"Phase (\d+) — ", milestone_title)
    return int(m.group(1)) if m else None


def sections(markdown: str) -> dict[str, str]:
    """Body text under each level-2 or level-3 heading (issue forms use level 3)."""
    found: dict[str, str] = {}
    parts = re.split(r"^#{2,3} +(.+?) *$", markdown, flags=re.M)
    for heading, text in zip(parts[1::2], parts[2::2], strict=True):
        found[heading.strip()] = text.strip()
    return found


def parse_draft(text: str) -> Draft:
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if not m:
        return Draft("", None, [], text.strip())
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    phase = meta.get("phase", "")
    labels = [x.strip() for x in meta.get("labels", "").split(",") if x.strip()]
    return Draft(
        title=meta.get("title", ""),
        phase=int(phase) if phase.isdigit() else None,
        labels=labels,
        body=m.group(2).strip(),
    )


def spec_refs(text: str) -> list[tuple[str, str]]:
    return SPEC_REF.findall(text)


def ref_exists(doc: str, section: str) -> bool:
    """True if `doc` has a heading numbered `section` ("6.11" matches "### 6.11 ...")."""
    pattern = rf"^#{{2,4}} {re.escape(section)}[. ]"
    return re.search(pattern, doc, re.M) is not None


def criteria(body: str) -> list[str]:
    text = sections(body).get("Acceptance criteria", "")
    return [line.strip() for line in text.splitlines() if CHECKBOX.match(line)]


def dependencies(body: str) -> list[int]:
    return [int(n) for n in ISSUE_REF.findall(sections(body).get("Depends on", ""))]


def validate_draft(draft: Draft, phases: list[Phase], docs: dict[str, str]) -> list[str]:
    errors: list[str] = []
    if not draft.title:
        errors.append("front matter: missing title")
    elif len(draft.title) > 80:
        errors.append("front matter: title is longer than 80 characters")
    if draft.phase not in {p.number for p in phases}:
        errors.append(f"front matter: phase must be one of 1-{len(phases)} (docs/plan.md §11)")
    if not any(label in AREA_LABELS for label in draft.labels):
        errors.append(f"front matter: labels must include one of {', '.join(AREA_LABELS)}")
    for label in draft.labels:
        if label not in AREA_LABELS and label not in KIND_LABELS:
            errors.append(f"front matter: unknown label {label!r}")

    found = sections(draft.body)
    for name in REQUIRED_SECTIONS:
        if not found.get(name):
            errors.append(f"section {name!r} is missing or empty")
    for name in found:
        if name not in REQUIRED_SECTIONS + OPTIONAL_SECTIONS:
            errors.append(f"unexpected section {name!r}")

    refs = spec_refs(found.get("Spec", ""))
    if found.get("Spec") and not refs:
        errors.append("section 'Spec' cites nothing; use e.g. 'plan §6.11'")
    for doc, section in refs:
        if not ref_exists(docs[doc], section):
            errors.append(f"spec reference '{doc} §{section}' has no matching heading")
    if found.get("Acceptance criteria") and not criteria(draft.body):
        errors.append("section 'Acceptance criteria' needs '- [ ] ...' checklist items")
    deps = found.get("Depends on")
    if deps is not None and not (ISSUE_REF.search(deps) or DRAFT_REF.search(deps)):
        errors.append("section 'Depends on' must list issues as #N or drafts as draft:<stem>")
    return errors


def draft_dependencies(body: str) -> list[str]:
    return DRAFT_REF.findall(sections(body).get("Depends on", ""))


def resolve_drafts(body: str, filed: dict[str, int]) -> str:
    """Replace draft:<stem> references with the issue numbers they were filed as."""
    return DRAFT_REF.sub(lambda m: f"#{filed[m.group(1)]}", body)


def validate_summary(text: str) -> list[str]:
    found = sections(text)
    return [
        f"section {name!r} is missing or empty" for name in SUMMARY_SECTIONS if not found.get(name)
    ]


def slugify(title: str, limit: int = 40) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", title.lower()).split()
    slug = ""
    for word in words:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > limit:
            break
        slug = candidate
    return slug or "issue"


def branch_name(number: int, title: str) -> str:
    return f"{number}-{slugify(title)}"


def issue_from_branch(branch: str) -> int | None:
    m = re.match(r"(\d+)-", branch)
    return int(m.group(1)) if m else None


def pr_body(summary: str, number: int, issue_body: str) -> str:
    items = "\n".join(criteria(issue_body))
    return (
        f"{summary.strip()}\n\n"
        f"## Acceptance criteria (from #{number})\n\n{items}\n\n"
        f"Closes #{number}\n\n{ATTRIBUTION}\n"
    )


def _author(node: dict[str, Any]) -> str:
    # A deleted account comes back as a null author.
    return f"@{(node.get('author') or {}).get('login', 'ghost')}"


def _indent(text: str, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" if line else "" for line in text.strip().splitlines())


def format_feedback(pr: dict[str, Any]) -> str:
    """Everything a reviewer has said on a PR, from the FEEDBACK_QUERY result."""
    decision = (pr.get("reviewDecision") or "none").replace("_", " ").lower()
    out = [
        f"PR #{pr['number']} {pr['title']}",
        f"{pr['url']}",
        f"State: {pr['state'].lower()}; review decision: {decision}",
    ]
    # A review with no body that only comments is the wrapper for inline
    # comments, which are listed under their threads instead.
    reviews = [r for r in pr["reviews"]["nodes"] if r["body"].strip() or r["state"] != "COMMENTED"]
    comments = pr["comments"]["nodes"]
    threads = pr["reviewThreads"]["nodes"]
    if not (reviews or comments or threads):
        out.append("\nNo reviews or comments yet.")
        return "\n".join(out)

    if reviews:
        out.append("\nReviews:")
        for r in reviews:
            state = r["state"].replace("_", " ").lower()
            out.append(f"  {_author(r)} {state} ({r['submittedAt']})")
            if r["body"].strip():
                out.append(_indent(r["body"], "    "))
    if comments:
        out.append("\nConversation:")
        for c in comments:
            out.append(f"  {_author(c)} ({c['createdAt']})")
            out.append(_indent(c["body"], "    "))
    if threads:
        out.append("\nInline comments:")
        for t in threads:
            line = t["line"] or t["originalLine"]
            start = t.get("startLine") or t.get("originalStartLine")
            where = (
                f"{t['path']}:{start}-{line}" if start and start != line else f"{t['path']}:{line}"
            )
            flags = [
                "resolved" if t["isResolved"] else "unresolved",
                *(["outdated"] if t["isOutdated"] else []),
            ]
            out.append(f"  {where} ({', '.join(flags)})")
            for c in t["comments"]["nodes"]:
                out.append(f"    {_author(c)} ({c['createdAt']})")
                out.append(_indent(c["body"], "      "))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# git and gh
# ---------------------------------------------------------------------------


def fail(message: str) -> NoReturn:
    print(f"tracker: {message}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], stdin: str | None = None) -> str:
    result = subprocess.run(
        cmd, cwd=REPO_ROOT, input=stdin, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        fail(f"{' '.join(cmd[:3])} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def gh(*args: str, stdin: str | None = None) -> str:
    return run(["gh", *args], stdin)


def gh_json(*args: str) -> Any:
    out = gh(*args)
    return json.loads(out) if out else None


def git(*args: str) -> str:
    return run(["git", *args])


def require_clean_tree() -> None:
    if git("status", "--porcelain"):
        fail("working tree has uncommitted changes; commit or stash them first")


def load_phases() -> list[Phase]:
    phases = parse_phases(DOCS["plan"].read_text(encoding="utf-8"))
    if not phases:
        fail("no phases found in docs/plan.md §11")
    return phases


def milestones() -> list[dict[str, Any]]:
    return gh_json("api", "repos/{owner}/{repo}/milestones?state=all&per_page=100") or []


FEEDBACK_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number title url state reviewDecision
      reviews(first: 100) { nodes { author { login } state body submittedAt } }
      comments(first: 100) { nodes { author { login } body createdAt } }
      reviewThreads(first: 100) {
        nodes {
          isResolved isOutdated path line originalLine startLine originalStartLine
          comments(first: 100) { nodes { author { login } body createdAt } }
        }
      }
    }
  }
}
"""


def issue(number: int) -> dict[str, Any]:
    return gh_json(
        "issue", "view", str(number), "--json", "number,title,body,state,labels,milestone,url"
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_setup(_: argparse.Namespace) -> None:
    existing = {m["title"]: m for m in milestones()}
    numbers = {phase_number(t): t for t in existing}
    for phase in load_phases():
        description = f"{phase.description}\n\nSpec: docs/plan.md §11"
        current = existing.get(phase.milestone)
        if current is None and phase.number in numbers:
            print(f"! {numbers[phase.number]!r} differs from plan heading {phase.milestone!r}")
            print("  Rename it on GitHub (or fix the plan) and run setup again.")
        elif current is None:
            gh(
                "api",
                "repos/{owner}/{repo}/milestones",
                "-f",
                f"title={phase.milestone}",
                "-f",
                f"description={description}",
            )
            print(f"+ milestone {phase.milestone}")
        elif (current.get("description") or "") != description:
            gh(
                "api",
                "-X",
                "PATCH",
                f"repos/{{owner}}/{{repo}}/milestones/{current['number']}",
                "-f",
                f"description={description}",
            )
            print(f"~ milestone {phase.milestone} (description updated from plan)")
        else:
            print(f"  milestone {phase.milestone}")

    labels = {lb["name"] for lb in gh_json("label", "list", "--limit", "200", "--json", "name")}
    for name, (color, description) in LABELS.items():
        if name in labels:
            print(f"  label {name}")
        else:
            gh("label", "create", name, "--color", color, "--description", description)
            print(f"+ label {name}")


def cmd_status(_: argparse.Namespace) -> None:
    phases = sorted(
        (m for m in milestones() if phase_number(m["title"]) is not None),
        key=lambda m: phase_number(m["title"]) or 0,
    )
    if not phases:
        fail("no phase milestones; run `scripts/tracker.py setup`")
    for m in phases:
        done = m["closed_issues"]
        total = done + m["open_issues"]
        state = "closed" if m["state"] == "closed" else f"{done}/{total} issues closed"
        print(f"  {m['title']}: {state}")

    current = next((m for m in phases if m["state"] == "open"), None)
    if current is None:
        print("\nAll phase milestones are closed.")
        return
    print(f"\nCurrent: {current['title']}")
    if current["open_issues"] == 0 and current["closed_issues"] == 0:
        print("  No issues yet. Plan the phase with the plan-phase skill.")
    elif current["open_issues"] == 0:
        print(
            "  Every issue is closed. Check the phase against plan §11, then close the milestone."
        )

    open_numbers = {
        i["number"]
        for i in gh_json("issue", "list", "--state", "open", "--limit", "1000", "--json", "number")
    }
    issues = gh_json(
        "issue",
        "list",
        "--milestone",
        current["title"],
        "--state",
        "open",
        "--limit",
        "200",
        "--json",
        "number,title,labels,body",
    )
    groups: dict[str, list[str]] = {"In progress": [], "Ready": [], "Blocked": []}
    for i in sorted(issues, key=lambda i: i["number"]):
        line = f"#{i['number']} {i['title']}"
        blockers = [n for n in dependencies(i["body"]) if n in open_numbers]
        if IN_PROGRESS in {lb["name"] for lb in i["labels"]}:
            groups["In progress"].append(line)
        elif blockers:
            groups["Blocked"].append(f"{line} (waits on {', '.join(f'#{n}' for n in blockers)})")
        else:
            groups["Ready"].append(line)
    for name, lines in groups.items():
        if lines:
            print(f"\n{name}:")
            for line in lines:
                print(f"  {line}")

    prs = gh_json("pr", "list", "--state", "open", "--json", "number,title,headRefName,url")
    if prs:
        print("\nOpen PRs (awaiting review):")
        for pr in prs:
            print(f"  #{pr['number']} {pr['title']} [{pr['headRefName']}] {pr['url']}")

    branch = git("branch", "--show-current")
    dirty = " (uncommitted changes)" if git("status", "--porcelain") else ""
    print(f"\nLocal branch: {branch}{dirty}")


def cmd_new(args: argparse.Namespace) -> None:
    phases = load_phases()
    docs = {name: path.read_text(encoding="utf-8") for name, path in DOCS.items()}
    drafts: list[tuple[str, Draft]] = []
    errors: list[str] = []
    for name in args.drafts:
        path = Path(name)
        draft = parse_draft(path.read_text(encoding="utf-8"))
        earlier = {stem for stem, _ in drafts}
        problems = validate_draft(draft, phases, docs)
        problems += [
            f"'Depends on' names draft:{stem}, which isn't an earlier draft in this run"
            for stem in draft_dependencies(draft.body)
            if stem not in earlier
        ]
        errors += [f"{name}: {p}" for p in problems]
        drafts.append((path.stem, draft))
    if errors:
        fail("nothing filed; drafts are invalid:\n" + "\n".join(f"  - {e}" for e in errors))

    existing = {
        i["title"].lower()
        for i in gh_json("issue", "list", "--state", "all", "--limit", "1000", "--json", "title")
    }
    duplicates = [d.title for _, d in drafts if d.title.lower() in existing]
    if duplicates:
        fail(
            "nothing filed; issues already exist titled:\n"
            + "\n".join(f"  - {t}" for t in duplicates)
        )

    filed: dict[str, int] = {}
    for stem, draft in drafts:
        milestone = next(p.milestone for p in phases if p.number == draft.phase)
        if args.dry_run:
            print(f"=== {stem}\nTitle:     {draft.title}\nMilestone: {milestone}")
            print(f"Labels:    {', '.join(draft.labels)}\n\n{draft.body}\n")
            continue
        label_args = [arg for label in draft.labels for arg in ("--label", label)]
        url = gh(
            "issue",
            "create",
            "--title",
            draft.title,
            "--milestone",
            milestone,
            *label_args,
            "--body-file",
            "-",
            stdin=resolve_drafts(draft.body, filed) + "\n",
        )
        filed[stem] = int(url.rstrip("/").rsplit("/", 1)[1])
        print(f"#{filed[stem]} {draft.title}  {url}")
    if args.dry_run:
        print(f"{len(drafts)} draft(s) valid; nothing filed (--dry-run).")


def cmd_start(args: argparse.Namespace) -> None:
    info = issue(args.number)
    if info["state"] != "OPEN":
        fail(f"#{args.number} is {info['state'].lower()}")
    if not info.get("milestone"):
        fail(f"#{args.number} has no milestone; file issues with `tracker.py new`")
    open_numbers = {
        i["number"]
        for i in gh_json("issue", "list", "--state", "open", "--limit", "1000", "--json", "number")
    }
    blockers = [n for n in dependencies(info["body"]) if n in open_numbers]
    if blockers and not args.force:
        fail(
            f"#{args.number} waits on {', '.join(f'#{n}' for n in blockers)}; "
            "pass --force to start anyway"
        )

    require_clean_tree()
    branch = branch_name(args.number, info["title"])
    git("fetch", "origin", "main")
    if git("branch", "--list", branch):
        git("switch", branch)
        print(f"Switched to existing branch {branch}")
    else:
        git("switch", "-c", branch, "--no-track", "origin/main")
        print(f"Created branch {branch} from origin/main")
    gh("issue", "edit", str(args.number), "--add-assignee", "@me", "--add-label", IN_PROGRESS)
    print(f"\n#{info['number']} {info['title']}\n{info['url']}\n\n{info['body']}")


def cmd_finish(args: argparse.Namespace) -> None:
    summary = Path(args.summary).read_text(encoding="utf-8")
    errors = validate_summary(summary)
    if errors:
        fail("summary is invalid:\n" + "\n".join(f"  - {e}" for e in errors))

    branch = git("branch", "--show-current")
    number = issue_from_branch(branch)
    if number is None:
        fail(f"branch {branch!r} isn't an issue branch; use `tracker.py start N`")
    require_clean_tree()
    git("fetch", "origin", "main")
    if git("rev-list", "--count", f"origin/main..{branch}") == "0":
        fail("no commits on this branch beyond origin/main")
    behind = git("rev-list", "--count", f"{branch}..origin/main")
    if behind != "0":
        fail(f"branch is {behind} commit(s) behind origin/main; rebase onto it first")

    just = ["just"] if shutil.which("just") else ["mise", "exec", "--", "just"]
    print("Running just check ...", flush=True)
    if subprocess.run([*just, "check"], cwd=REPO_ROOT, check=False).returncode != 0:
        fail("`just check` failed; fix it before opening the PR")

    git("push", "--force-with-lease", "-u", "origin", branch)
    info = issue(number)
    body = pr_body(summary, number, info["body"])
    existing = gh_json("pr", "list", "--head", branch, "--state", "open", "--json", "url")
    if existing:
        gh("pr", "edit", existing[0]["url"], "--body-file", "-", stdin=body)
        print(f"Updated {existing[0]['url']}")
    else:
        url = gh(
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            info["title"],
            "--body-file",
            "-",
            stdin=body,
        )
        print(url)


def cmd_feedback(args: argparse.Namespace) -> None:
    number = args.number
    if number is None:
        branch = git("branch", "--show-current")
        prs = gh_json("pr", "list", "--head", branch, "--state", "all", "--json", "number")
        if not prs:
            fail(f"branch {branch!r} has no PR; pass its number")
        number = prs[0]["number"]
    data = gh_json(
        "api",
        "graphql",
        "-F",
        "owner={owner}",
        "-F",
        "name={repo}",
        "-F",
        f"number={number}",
        "-f",
        f"query={FEEDBACK_QUERY}",
    )
    pr = data["data"]["repository"]["pullRequest"]
    if pr is None:
        fail(f"#{number} isn't a pull request")
    print(format_feedback(pr))


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    sub = parser.add_subparsers(required=True)
    sub.add_parser("setup", help="create or refresh milestones and labels").set_defaults(
        func=cmd_setup
    )
    sub.add_parser("status", help="show the current phase and its issues").set_defaults(
        func=cmd_status
    )
    p = sub.add_parser("new", help="validate issue drafts and file them in order")
    p.add_argument("drafts", nargs="+", metavar="DRAFT")
    p.add_argument("--dry-run", action="store_true", help="validate and print, don't file")
    p.set_defaults(func=cmd_new)
    p = sub.add_parser("start", help="create the issue branch and mark the issue in progress")
    p.add_argument("number", type=int)
    p.add_argument("--force", action="store_true", help="start even if dependencies are open")
    p.set_defaults(func=cmd_start)
    p = sub.add_parser("finish", help="run just check, push, and open or update the PR")
    p.add_argument("summary", help="markdown file with Summary and Verification sections")
    p.set_defaults(func=cmd_finish)
    p = sub.add_parser("feedback", help="print the reviews and comments on a PR")
    p.add_argument("number", type=int, nargs="?", help="PR number (default: this branch's PR)")
    p.set_defaults(func=cmd_feedback)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
