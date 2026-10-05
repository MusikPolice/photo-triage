"""Pausing and resuming the worker, globally or per stage (plan §6.11).

The state lives in `worker_controls`, so a pause survives a restart, and the API can
change it while the worker runs. The worker reads it before each claim, so a job
that's already running finishes.

Functions take the caller's `Session` and don't commit.
"""

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from photo_triage.db.models import WorkerControl
from photo_triage.worker.queue import Stage

GLOBAL = "global"
"""The scope that pauses every stage."""


def pause(session: Session, scope: str, actor: str, now: dt.datetime) -> None:
    """Pause every stage (`GLOBAL`) or one stage."""
    _set(session, scope, paused=True, actor=actor, now=now)


def resume(session: Session, scope: str, actor: str, now: dt.datetime) -> None:
    """Undo `pause` for the same scope. Resuming globally doesn't resume stages
    paused one by one."""
    _set(session, scope, paused=False, actor=actor, now=now)


def paused_scopes(session: Session) -> set[str]:
    """The scopes paused now: `GLOBAL` and stage names."""
    return set(session.scalars(sa.select(WorkerControl.scope).where(WorkerControl.paused)))


def _set(session: Session, scope: str, *, paused: bool, actor: str, now: dt.datetime) -> None:
    if scope != GLOBAL and scope not in Stage:
        raise ValueError(f"{scope!r} is neither {GLOBAL!r} nor a stage")
    row = insert(WorkerControl).values(scope=scope, paused=paused, actor=actor, changed_at=now)
    session.execute(
        row.on_conflict_do_update(
            index_elements=[WorkerControl.scope],
            set_={
                WorkerControl.paused: row.excluded.paused,
                WorkerControl.actor: row.excluded.actor,
                WorkerControl.changed_at: row.excluded.changed_at,
            },
        )
    )
