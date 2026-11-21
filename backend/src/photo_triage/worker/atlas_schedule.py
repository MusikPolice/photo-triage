"""When the `atlases` batch job is due (plan §6.2, §6.11).

The atlases are one set of sheets per layout version, in
`DATA_DIR/derived/atlases/<version>/` (`photo_triage.pipeline.atlases`). A set is due
whenever the newest complete set on disk isn't the current layout's: after each fit,
and at startup for a layout fitted before atlases existed. It's also due when the
layout is gone and a set is left, so the job removes it.

This reads the disk and the database only; it imports nothing heavy, so the worker
loop can call it after every layout job.
"""

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from photo_triage.db.models import JOB_IS_OPEN, Job, JobStatus
from photo_triage.worker.layout_schedule import latest_version
from photo_triage.worker.queue import JobQueue, Stage

ATLASES_FOLDER = "atlases"
MANIFEST = "manifest.bin"
"""Written last into a set's folder, so a set without one isn't complete."""


def atlas_root(derived_dir: Path) -> Path:
    return derived_dir / ATLASES_FOLDER


def set_path(derived_dir: Path, version: int) -> Path:
    """The folder holding layout `version`'s sheets and manifest."""
    return atlas_root(derived_dir) / str(version)


def current_set(derived_dir: Path) -> int | None:
    """The layout version of the newest complete set of sheets, or None."""
    try:
        entries = list(atlas_root(derived_dir).iterdir())
    except FileNotFoundError:
        return None
    versions = [
        int(entry.name)
        for entry in entries
        if entry.name.isdigit() and (entry / MANIFEST).is_file()
    ]
    return max(versions, default=None)


def atlases_waiting(session: Session) -> bool:
    """Whether an atlases job is already queued, or has failed too often and is
    parked, waiting for someone to retry it."""
    return bool(
        session.scalar(
            sa.select(
                sa.exists().where(
                    Job.stage == Stage.ATLASES,
                    sa.or_(sa.text(JOB_IS_OPEN), Job.status == JobStatus.PARKED),
                )
            )
        )
    )


def due(session: Session, derived_dir: Path) -> bool:
    """Whether the newest set of sheets isn't the current layout's."""
    return (session.scalar(latest_version()) or 0) != (current_set(derived_dir) or 0)


def queue_if_due(session: Session, queue: JobQueue, derived_dir: Path) -> bool:
    """Queue an atlases job if one is due and none is waiting. Returns whether it
    queued one."""
    if atlases_waiting(session) or not due(session, derived_dir):
        return False
    queue.enqueue(session, Stage.ATLASES)
    return True
