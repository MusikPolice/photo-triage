"""The `duplicates` batch job (plan §6.7): groups stored from items' embeddings, kept
across regroupings with their decisions, and queued after each layout fit."""

import datetime as dt
from pathlib import Path

import numpy as np
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from vectors import NOW, Library, unit

from photo_triage.db.models import (
    DuplicateGroup,
    DuplicateMember,
    GroupStatus,
    Item,
    ItemStatus,
    Job,
    JobStatus,
)
from photo_triage.ml import FakeClip
from photo_triage.pipeline import duplicates
from photo_triage.settings import Settings
from photo_triage.worker import duplicate_schedule
from photo_triage.worker.__main__ import main
from photo_triage.worker.queue import PRIORITY, JobQueue, Stage

THRESHOLD = 0.95


def near(axis: int, n: int = 0) -> np.ndarray:
    """A vector at similarity about 0.999 to the `axis`, different for each `n`."""
    weights = [0.0] * 40
    weights[axis] = 1
    weights[20 + n] = 0.03
    return unit(*weights)


def group(lib: Library) -> duplicates.Grouping:
    return duplicates.group(sessionmaker(lib.engine), lib.model, THRESHOLD, NOW)


def groups(engine: Engine) -> dict[int, tuple[GroupStatus, list[int]]]:
    """Each group's status and members, by rank."""
    with Session(engine) as session:
        found: dict[int, tuple[GroupStatus, list[int]]] = {}
        for g in session.scalars(sa.select(DuplicateGroup).order_by(DuplicateGroup.id)):
            members = session.scalars(
                sa.select(DuplicateMember.item_id)
                .where(DuplicateMember.group_id == g.id)
                .order_by(DuplicateMember.rank)
            ).all()
            found[g.id] = (g.status, list(members))
        return found


def decide(engine: Engine, group_id: int, status: GroupStatus) -> None:
    with Session(engine) as session, session.begin():
        g = session.get(DuplicateGroup, group_id)
        assert g is not None
        g.status, g.resolved_at, g.actor = status, NOW, "ada"


def size(engine: Engine, item_id: int, width: int, height: int) -> None:
    with Session(engine) as session, session.begin():
        item = session.get(Item, item_id)
        assert item is not None
        item.width, item.height = width, height


def test_pairs_at_the_threshold_are_linked_and_below_it_aren_t(
    lib: Library, migrated: Engine
) -> None:
    a = lib.add(unit(1, 0))
    b = lib.add(unit(1, np.tan(np.arccos(0.951))))  # 0.951 to a
    c = lib.add(unit(0, 1))
    d = lib.add(unit(np.tan(np.arccos(0.949)), 1))  # 0.949 to c
    lone = lib.add(unit(0, 0, 1))

    result = group(lib)

    assert [m for _, m in groups(migrated).values()] == [[a, b]]
    assert result == duplicates.Grouping(groups=1, items=2, pending=1)
    assert c not in [a, b] and d and lone  # the rest aren't grouped


def test_members_rank_by_pixels_for_the_keeper(lib: Library, migrated: Engine) -> None:
    small, large = lib.add(near(0, 1)), lib.add(near(0, 2))
    size(migrated, small, 640, 480)
    size(migrated, large, 4032, 3024)

    group(lib)

    [(status, members)] = groups(migrated).values()
    assert (status, members) == (GroupStatus.PENDING, [large, small])


def test_items_not_in_the_library_are_left_out(lib: Library, migrated: Engine) -> None:
    a, b, c = lib.add(near(0, 1)), lib.add(near(0, 2)), lib.add(near(0, 3))
    lib.set_status(c, ItemStatus.TRASHED)

    group(lib)

    assert sorted(m for _, ms in groups(migrated).values() for m in ms) == [a, b]


def test_grouping_again_with_no_change_changes_nothing(lib: Library, migrated: Engine) -> None:
    lib.add(near(0, 1))
    lib.add(near(0, 2))
    lib.add(near(5, 1))
    lib.add(near(5, 2))
    group(lib)
    before = groups(migrated)

    group(lib)

    assert groups(migrated) == before


def test_a_resolved_group_stays_resolved(lib: Library, migrated: Engine) -> None:
    a, b = lib.add(near(0, 1)), lib.add(near(0, 2))
    group(lib)
    [group_id] = groups(migrated)
    decide(migrated, group_id, GroupStatus.RESOLVED)

    group(lib)

    assert groups(migrated) == {group_id: (GroupStatus.RESOLVED, [a, b])}


def test_a_new_near_copy_reopens_a_resolved_group(lib: Library, migrated: Engine) -> None:
    a, b = lib.add(near(0, 1)), lib.add(near(0, 2))
    group(lib)
    [group_id] = groups(migrated)
    decide(migrated, group_id, GroupStatus.RESOLVED)
    c = lib.add(near(0, 3))

    group(lib)

    assert groups(migrated) == {group_id: (GroupStatus.PENDING, [a, b, c])}
    with Session(migrated) as session:
        g = session.get(DuplicateGroup, group_id)
        assert g is not None and (g.resolved_at, g.actor) == (None, None)


def test_a_resolved_group_losing_a_member_stays_resolved(lib: Library, migrated: Engine) -> None:
    a, b, c = lib.add(near(0, 1)), lib.add(near(0, 2)), lib.add(near(0, 3))
    group(lib)
    [group_id] = groups(migrated)
    decide(migrated, group_id, GroupStatus.RESOLVED)
    lib.set_status(c, ItemStatus.TRASHED)

    group(lib)

    assert groups(migrated) == {group_id: (GroupStatus.RESOLVED, [a, b])}


def test_a_skipped_group_stays_skipped_until_it_changes(lib: Library, migrated: Engine) -> None:
    a, b = lib.add(near(0, 1)), lib.add(near(0, 2))
    group(lib)
    [group_id] = groups(migrated)
    decide(migrated, group_id, GroupStatus.SKIPPED)
    group(lib)
    assert groups(migrated)[group_id][0] == GroupStatus.SKIPPED

    c = lib.add(near(0, 3))
    group(lib)

    assert groups(migrated) == {group_id: (GroupStatus.PENDING, [a, b, c])}


def test_two_groups_joined_by_a_new_item_become_the_older_one(
    lib: Library, migrated: Engine
) -> None:
    # Two pairs, far enough apart not to link, until an item between them arrives.
    left = [lib.add(unit(1, 0, 0.01 * n)) for n in range(2)]
    right = [lib.add(unit(1, 0.62, 0.01 * n)) for n in range(2)]
    group(lib)
    first, second = groups(migrated)
    middle = lib.add(unit(1, 0.31))

    group(lib)

    [(group_id, (status, members))] = groups(migrated).items()
    assert group_id == first and second not in groups(migrated)
    assert status == GroupStatus.PENDING
    assert sorted(members) == sorted([*left, *right, middle])


def test_a_pending_group_that_s_gone_is_deleted_and_a_resolved_one_kept(
    lib: Library, migrated: Engine
) -> None:
    pending = [lib.add(near(0, 1)), lib.add(near(0, 2))]
    resolved = [lib.add(near(5, 1)), lib.add(near(5, 2))]
    group(lib)
    first, second = groups(migrated)
    decide(migrated, second, GroupStatus.RESOLVED)
    for item_id in (pending[1], resolved[1]):
        lib.set_status(item_id, ItemStatus.TRASHED)

    group(lib)

    assert groups(migrated) == {second: (GroupStatus.RESOLVED, resolved)}
    assert first not in groups(migrated)


def test_no_items_no_groups(lib: Library, migrated: Engine) -> None:
    assert group(lib) == duplicates.Grouping(groups=0, items=0, pending=0)


# --- When it's queued --------------------------------------------------------------


def _done(session: Session, stage: Stage, at: dt.datetime) -> None:
    session.add(
        Job(
            stage=stage,
            priority=PRIORITY[stage],
            status=JobStatus.DONE,
            enqueued_at=at,
            finished_at=at,
        )
    )


def test_grouping_is_due_after_each_layout_fit(migrated: Engine) -> None:
    queue = JobQueue(lambda: NOW)
    later = NOW + dt.timedelta(hours=1)
    with Session(migrated) as session, session.begin():
        assert not duplicate_schedule.queue_if_due(session, queue)  # no layout yet
        _done(session, Stage.LAYOUT, NOW)
        assert duplicate_schedule.queue_if_due(session, queue)
        assert not duplicate_schedule.queue_if_due(session, queue)  # already queued
        session.execute(sa.delete(Job))
        _done(session, Stage.LAYOUT, NOW)
        _done(session, Stage.DUPLICATES, later)
        assert not duplicate_schedule.due(session)
        _done(session, Stage.LAYOUT, later + dt.timedelta(minutes=1))
        assert duplicate_schedule.due(session)


def test_a_parked_grouping_isn_t_queued_again(migrated: Engine) -> None:
    with Session(migrated) as session, session.begin():
        _done(session, Stage.LAYOUT, NOW)
        session.add(
            Job(
                stage=Stage.DUPLICATES,
                priority=PRIORITY[Stage.DUPLICATES],
                status=JobStatus.PARKED,
                enqueued_at=NOW,
            )
        )
        assert not duplicate_schedule.queue_if_due(session, JobQueue(lambda: NOW))


def test_the_worker_groups_after_laying_out_the_library(
    migrated: Engine,
    settings: Settings,
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.trash_dir.mkdir()
    for name in ("PHOTO_DIR", "TRASH_DIR", "DATA_DIR", "MODELS_DIR"):
        monkeypatch.setenv(name, str(getattr(settings, name.lower())))
    monkeypatch.setenv("CLIP_MODEL", FakeClip().model)  # the model laid out

    assert main(["scan"]) == 0
    assert main(["--once"], clip=FakeClip()) == 0

    with Session(migrated) as session:
        statuses = session.scalars(sa.select(Job.status).where(Job.stage == Stage.DUPLICATES))
        assert list(statuses) == [JobStatus.DONE]
