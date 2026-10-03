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
