"""Tests for the parsing and validation in scripts/tracker.py (no GitHub calls)."""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[3] / "scripts" / "tracker.py"
_spec = importlib.util.spec_from_file_location("tracker", _SCRIPT)
assert _spec and _spec.loader
tracker = importlib.util.module_from_spec(_spec)
sys.modules["tracker"] = tracker  # dataclasses look up their module
_spec.loader.exec_module(tracker)

DOCS = {name: path.read_text(encoding="utf-8") for name, path in tracker.DOCS.items()}
PHASES = tracker.parse_phases(DOCS["plan"])

VALID = """\
---
title: Job queue with priorities and pause/resume
phase: 1
labels: backend, infra
---
## Spec
- plan §6.11
- dev-environment §7

## Deliverable
A persistent job queue.

## Acceptance criteria
- [ ] Higher-priority jobs run first.
- [ ] Pausing stops new jobs from starting.

## Out of scope
Quiet hours.
"""


def test_parse_phases_from_plan() -> None:
    assert [p.number for p in PHASES] == list(range(1, 9))
    first = PHASES[0]
    assert first.milestone == "Phase 1 — Infrastructure & Scaffold"
    assert first.description.startswith("Compose stack")
    assert "---" not in PHASES[-1].description


def test_phase_number() -> None:
    assert tracker.phase_number("Phase 3 — Exploration Map") == 3
    assert tracker.phase_number("Backlog") is None


def test_valid_draft() -> None:
    draft = tracker.parse_draft(VALID)
    assert draft.title == "Job queue with priorities and pause/resume"
    assert draft.phase == 1
    assert draft.labels == ["backend", "infra"]
    assert draft.body.startswith("## Spec")
    assert tracker.validate_draft(draft, PHASES, DOCS) == []


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (("phase: 1", "phase: 9"), "phase must be one of"),
        (("labels: backend, infra", "labels: backend, ui"), "unknown label 'ui'"),
        (("labels: backend, infra", "labels:"), "labels must include"),
        (("labels: backend, infra", "labels: bug"), "labels must include"),
        (("plan §6.11", "plan §6.99"), "'plan §6.99' has no matching heading"),
        (("- plan §6.11\n- dev-environment §7", "the queue section"), "cites nothing"),
        (("- [ ] Higher", "- Higher"), None),  # one checkbox left is still valid
        (("## Out of scope\nQuiet hours.", ""), "'Out of scope' is missing"),
        (("## Deliverable", "## Notes"), "unexpected section 'Notes'"),
        (("title: Job", "title: " + "x" * 80 + " Job"), "longer than 80"),
    ],
)
def test_invalid_draft(change: tuple[str, str], error: str | None) -> None:
    text = VALID.replace(*change)
    assert text != VALID
    errors = tracker.validate_draft(tracker.parse_draft(text), PHASES, DOCS)
    if error is None:
        assert errors == []
    else:
        assert any(error in e for e in errors), errors


def test_draft_without_front_matter() -> None:
    errors = tracker.validate_draft(tracker.parse_draft("## Spec\n"), PHASES, DOCS)
    assert "front matter: missing title" in errors


def test_criteria_need_checkboxes() -> None:
    text = VALID.replace("- [ ] ", "- ")
    errors = tracker.validate_draft(tracker.parse_draft(text), PHASES, DOCS)
    assert any("checklist items" in e for e in errors)


def test_depends_on() -> None:
    text = VALID + "\n## Depends on\n- #12\n- #3 (schema)\n"
    draft = tracker.parse_draft(text)
    assert tracker.validate_draft(draft, PHASES, DOCS) == []
    assert tracker.dependencies(draft.body) == [12, 3]
    bad = tracker.parse_draft(VALID + "\n## Depends on\nthe schema issue\n")
    assert any("#N" in e for e in tracker.validate_draft(bad, PHASES, DOCS))


def test_sections_accept_issue_form_headings() -> None:
    body = "### Spec\n\nplan §8\n\n### Acceptance criteria\n\n- [x] Done\n"
    assert tracker.sections(body) == {"Spec": "plan §8", "Acceptance criteria": "- [x] Done"}
    assert tracker.criteria(body) == ["- [x] Done"]


def test_ref_exists() -> None:
    assert tracker.ref_exists(DOCS["plan"], "8")
    assert tracker.ref_exists(DOCS["plan"], "6.11")
    assert not tracker.ref_exists(DOCS["plan"], "6.1.1")
    assert not tracker.ref_exists(DOCS["plan"], "6.12")


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Job queue with priorities and pause/resume", "job-queue-with-priorities-and-pause"),
        ("Alembic schema for §8", "alembic-schema-for-8"),
        ("!!!", "issue"),
    ],
)
def test_slugify(title: str, slug: str) -> None:
    assert tracker.slugify(title) == slug
    assert len(slug) <= 40


def test_branch_round_trip() -> None:
    branch = tracker.branch_name(17, "SSE activity stream")
    assert branch == "17-sse-activity-stream"
    assert tracker.issue_from_branch(branch) == 17
    assert tracker.issue_from_branch("main") is None


def test_summary_and_pr_body() -> None:
    summary = "## Summary\nAdded the queue.\n\n## Verification\nTests.\n"
    assert tracker.validate_summary(summary) == []
    assert tracker.validate_summary("## Summary\nx\n") == [
        "section 'Verification' is missing or empty"
    ]
    body = tracker.pr_body(summary, 4, tracker.parse_draft(VALID).body)
    assert "Closes #4" in body
    assert "- [ ] Higher-priority jobs run first." in body
    assert body.rstrip().endswith(tracker.ATTRIBUTION)


SHOTS_SUMMARY = """\
## Summary
See ![not this one](.screenshots/scratch/elsewhere.png).

## Verification
Looked.

## Screenshots
![Activity, desktop](.screenshots/scratch/activity-desktop.png)
![Activity, phone](.screenshots/scratch/activity-phone.png)
![Already published](https://example.com/old.png)
![Activity, desktop again](.screenshots/scratch/activity-desktop.png)
"""


def test_screenshot_paths_come_from_the_screenshots_section() -> None:
    assert tracker.screenshot_paths(SHOTS_SUMMARY) == [
        ".screenshots/scratch/activity-desktop.png",
        ".screenshots/scratch/activity-phone.png",
    ]
    assert tracker.screenshot_paths("## Summary\nx\n\n## Verification\ny\n") == []


def _shot(root: Path, path: str) -> str:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_bytes(b"\x89PNG")
    return path


def test_only_existing_scratch_pngs_can_be_published(tmp_path: Path) -> None:
    good = _shot(tmp_path, ".screenshots/scratch/activity-desktop.png")
    local = _shot(tmp_path, ".screenshots/local/home-desktop.png")
    sneaky = ".screenshots/scratch/../local/other.png"
    jpeg = _shot(tmp_path, ".screenshots/scratch/photo.jpg")
    missing = ".screenshots/scratch/missing.png"

    assert tracker.check_screenshots([good], root=tmp_path) == []
    errors = tracker.check_screenshots([local, sneaky, jpeg, missing], root=tmp_path)
    assert len(errors) == 4
    assert errors[0].startswith(f"{local}: only scratch-stack screenshots")
    assert errors[1].startswith(f"{sneaky}: only scratch-stack screenshots")
    assert errors[2] == f"{jpeg}: not a .png"
    assert errors[3].startswith(f"{missing}: no such file")


def test_screenshots_need_distinct_names(tmp_path: Path) -> None:
    a = _shot(tmp_path, ".screenshots/scratch/a/shot.png")
    b = _shot(tmp_path, ".screenshots/scratch/b/shot.png")
    assert tracker.check_screenshots([a, b], root=tmp_path) == [
        "two screenshots are named shot.png"
    ]


def test_embed_screenshots_swaps_in_published_urls() -> None:
    url = "https://raw.githubusercontent.com/o/r/abc/pr-4/activity-desktop.png"
    body = tracker.embed_screenshots(
        SHOTS_SUMMARY, {".screenshots/scratch/activity-desktop.png": url}
    )
    assert body.count(f"![Activity, desktop]({url})") == 1
    assert f"![Activity, desktop again]({url})" in body
    assert "![Activity, phone](.screenshots/scratch/activity-phone.png)" in body
    assert "![Already published](https://example.com/old.png)" in body


def test_draft_dependencies() -> None:
    text = VALID + "\n## Depends on\n- draft:01-schema\n- #5\n"
    draft = tracker.parse_draft(text)
    assert tracker.validate_draft(draft, PHASES, DOCS) == []
    assert tracker.draft_dependencies(draft.body) == ["01-schema"]
    assert tracker.dependencies(draft.body) == [5]
    resolved = tracker.resolve_drafts(draft.body, {"01-schema": 21})
    assert tracker.dependencies(resolved) == [21, 5]


def test_optional_sections_and_bug_label() -> None:
    text = VALID.replace("labels: backend, infra", "labels: backend, bug")
    text = text.replace("## Spec", "## Background\nScans crash on empty dirs.\n\n## Spec")
    text += "\n## Approach\nGuard the walker.\n"
    draft = tracker.parse_draft(text)
    assert draft.labels == ["backend", "bug"]
    assert tracker.validate_draft(draft, PHASES, DOCS) == []


# --- feedback ---------------------------------------------------------------


def _pr(**fields: object) -> dict[str, object]:
    pr: dict[str, object] = {
        "number": 7,
        "title": "Job queue",
        "url": "https://github.com/o/r/pull/7",
        "state": "OPEN",
        "reviewDecision": None,
        "reviews": {"nodes": []},
        "comments": {"nodes": []},
        "reviewThreads": {"nodes": []},
    }
    return pr | fields


def _comment(body: str, login: str | None = "reviewer") -> dict[str, object]:
    author = {"login": login} if login else None
    return {"author": author, "body": body, "createdAt": "2026-10-07T12:00:00Z"}


def test_feedback_with_nothing() -> None:
    text = tracker.format_feedback(_pr())
    assert text.splitlines()[:3] == [
        "PR #7 Job queue",
        "https://github.com/o/r/pull/7",
        "State: open; review decision: none",
    ]
    assert text.endswith("No reviews or comments yet.")


def test_feedback_reviews_and_conversation() -> None:
    reviews = [
        {
            "author": {"login": "a"},
            "state": "CHANGES_REQUESTED",
            "body": "Rename it.\n\nPlease.",
            "submittedAt": "2026-10-07T10:00:00Z",
        },
        # The empty wrapper review that holds inline comments is left out.
        {
            "author": {"login": "a"},
            "state": "COMMENTED",
            "body": "",
            "submittedAt": "2026-10-07T10:01:00Z",
        },
        {
            "author": {"login": "b"},
            "state": "APPROVED",
            "body": "",
            "submittedAt": "2026-10-07T11:00:00Z",
        },
    ]
    pr = _pr(
        reviewDecision="CHANGES_REQUESTED",
        reviews={"nodes": reviews},
        comments={"nodes": [_comment("Looks close."), _comment("Gone.", login=None)]},
    )
    assert tracker.format_feedback(pr).splitlines()[2:] == [
        "State: open; review decision: changes requested",
        "",
        "Reviews:",
        "  @a changes requested (2026-10-07T10:00:00Z)",
        "    Rename it.",
        "",
        "    Please.",
        "  @b approved (2026-10-07T11:00:00Z)",
        "",
        "Conversation:",
        "  @reviewer (2026-10-07T12:00:00Z)",
        "    Looks close.",
        "  @ghost (2026-10-07T12:00:00Z)",
        "    Gone.",
    ]


def test_feedback_inline_threads() -> None:
    threads = [
        {
            "isResolved": False,
            "isOutdated": False,
            "path": "a.py",
            "line": 12,
            "originalLine": 12,
            "startLine": None,
            "originalStartLine": None,
            "comments": {"nodes": [_comment("Unit?"), _comment("Bytes.", login="me")]},
        },
        {
            "isResolved": True,
            "isOutdated": True,
            "path": "b.py",
            "line": None,
            "originalLine": 30,
            "startLine": None,
            "originalStartLine": 28,
            "comments": {"nodes": [_comment("Split this.")]},
        },
    ]
    text = tracker.format_feedback(_pr(reviewThreads={"nodes": threads}))
    assert text.split("\n\n", 1)[1].splitlines() == [
        "Inline comments:",
        "  a.py:12 (unresolved)",
        "    @reviewer (2026-10-07T12:00:00Z)",
        "      Unit?",
        "    @me (2026-10-07T12:00:00Z)",
        "      Bytes.",
        "  b.py:28-30 (resolved, outdated)",
        "    @reviewer (2026-10-07T12:00:00Z)",
        "      Split this.",
    ]


@pytest.fixture
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Fakes gh and git: the branch is 7-job-queue, whose PR is #7."""
    calls: list[tuple[str, ...]] = []

    def gh_json(*args: str) -> object:
        calls.append(args)
        if args[:2] == ("pr", "list"):
            return [{"number": 7}] if "7-job-queue" in args else []
        return {"data": {"repository": {"pullRequest": _pr()}}}

    monkeypatch.setattr(tracker, "gh_json", gh_json)
    monkeypatch.setattr(tracker, "git", lambda *args: "7-job-queue")
    return calls


def test_feedback_command(
    fake_gh: list[tuple[str, ...]], capsys: pytest.CaptureFixture[str]
) -> None:
    tracker.main(["feedback", "7"])
    assert capsys.readouterr().out.startswith("PR #7 Job queue\n")
    [query] = fake_gh
    assert query[:2] == ("api", "graphql")
    assert "number=7" in query


def test_feedback_defaults_to_the_branch_pr(
    fake_gh: list[tuple[str, ...]], capsys: pytest.CaptureFixture[str]
) -> None:
    tracker.main(["feedback"])
    assert "No reviews or comments yet." in capsys.readouterr().out
    assert fake_gh[0][:2] == ("pr", "list")
    assert "number=7" in fake_gh[1]


def test_feedback_without_a_pr(
    monkeypatch: pytest.MonkeyPatch, fake_gh: list[tuple[str, ...]]
) -> None:
    monkeypatch.setattr(tracker, "git", lambda *args: "8-no-pr")
    with pytest.raises(SystemExit):
        tracker.main(["feedback"])
