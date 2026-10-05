"""The Activity page's data, and pausing and resuming the worker (plan §7, §6.11).

`GET /api/activity` gives the worker's state and one row per stage, counted from
`jobs`. The pause and resume endpoints answer with the new activity, and the event
stream (`photo_triage.api.events`) pushes it to every open tab.
"""

import datetime as dt
from collections import Counter
from typing import Literal, cast

import sqlalchemy as sa
from fastapi import APIRouter, Request
from pydantic import AwareDatetime, BaseModel
from sqlalchemy.orm import Session

from photo_triage.api.deps import (
    Actor,
    AppClock,
    DbSession,
    get_clock,
    get_real_clock,
    get_settings,
)
from photo_triage.api.feed import Feed
from photo_triage.db.models import Job, JobStatus
from photo_triage.settings import Settings
from photo_triage.worker import controls, heartbeat
from photo_triage.worker.clock import Clock
from photo_triage.worker.queue import BATCH_STAGES, Stage
from photo_triage.worker.state import WorkerStatus, observed_state

router = APIRouter()


class WorkerActivity(BaseModel):
    status: WorkerStatus
    """`stopped` when the worker process isn't running, else `paused`, `quiet` or
    `running`."""
    paused: bool
    """Paused globally. Also while stopped, since the pause applies when it starts."""
    quiet_until_at: AwareDatetime | None
    """When quiet hours end, while `status` is `quiet`."""
    last_seen_at: AwareDatetime | None
    """The worker's latest heartbeat, or None if it has never run."""


class StageActivity(BaseModel):
    stage: Stage
    kind: Literal["item", "batch"]
    """Whether a job covers one item or the whole library."""
    paused: bool
    """Paused on its own, as opposed to with the whole worker."""
    done: int
    total: int
    """Every job: done, pending, running, errored and parked."""
    pending: int
    running: int
    errored: int
    """Failed, and waiting to be retried."""
    parked: int
    """Failed too many times, and waiting for someone to look."""


class Activity(BaseModel):
    worker: WorkerActivity
    stages: list[StageActivity]
    """In priority order."""


def read_activity(
    session: Session, settings: Settings, now: dt.datetime, real_now: dt.datetime
) -> Activity:
    """The activity at `now` (the app's clock), judging the heartbeat at `real_now`."""
    state = observed_state(session, settings.worker_quiet_hours, settings.tz, now, real_now)
    paused = controls.paused_scopes(session)
    last = heartbeat.last(session)
    counts: Counter[tuple[str, JobStatus]] = Counter()
    for stage, status, count in session.execute(
        sa.select(Job.stage, Job.status, sa.func.count()).group_by(Job.stage, Job.status)
    ):
        counts[stage, status] = count
    stages = [s for s in Stage if s != Stage.NOOP or settings.worker_noop_stage]
    return Activity(
        worker=WorkerActivity(
            status=state.status,
            paused=controls.GLOBAL in paused,
            quiet_until_at=state.quiet_until_at,
            last_seen_at=None if last is None else last.seen_at,
        ),
        stages=[_stage_activity(stage, counts, stage in paused) for stage in stages],
    )


def _stage_activity(
    stage: Stage, counts: Counter[tuple[str, JobStatus]], paused: bool
) -> StageActivity:
    by_status = {status: counts[stage, status] for status in JobStatus}
    return StageActivity(
        stage=stage,
        kind="batch" if stage in BATCH_STAGES else "item",
        paused=paused,
        done=by_status[JobStatus.DONE],
        total=sum(by_status.values()),
        pending=by_status[JobStatus.PENDING],
        running=by_status[JobStatus.RUNNING],
        errored=by_status[JobStatus.ERROR],
        parked=by_status[JobStatus.PARKED],
    )


def current_activity(request: Request, session: Session) -> Activity:
    now, real_now = get_clock(request)(), get_real_clock(request)()
    return read_activity(session, get_settings(request), now, real_now)


def get_feed(request: Request) -> Feed[Activity]:
    return cast(Feed[Activity], request.app.state.feed)


@router.get("/activity")
def get_activity(request: Request, session: DbSession) -> Activity:
    return current_activity(request, session)


@router.post("/worker/pause")
def pause_worker(request: Request, session: DbSession, actor: Actor, clock: AppClock) -> Activity:
    """Pause every stage. A job already running finishes."""
    return _set_paused(request, session, controls.GLOBAL, True, actor, clock)


@router.post("/worker/resume")
def resume_worker(request: Request, session: DbSession, actor: Actor, clock: AppClock) -> Activity:
    """Undo a global pause. Stages paused on their own stay paused."""
    return _set_paused(request, session, controls.GLOBAL, False, actor, clock)


@router.post("/worker/stages/{stage}/pause")
def pause_stage(
    request: Request, stage: Stage, session: DbSession, actor: Actor, clock: AppClock
) -> Activity:
    """Pause one stage. A job of that stage already running finishes."""
    return _set_paused(request, session, stage, True, actor, clock)


@router.post("/worker/stages/{stage}/resume")
def resume_stage(
    request: Request, stage: Stage, session: DbSession, actor: Actor, clock: AppClock
) -> Activity:
    return _set_paused(request, session, stage, False, actor, clock)


def _set_paused(
    request: Request, session: Session, scope: str, paused: bool, actor: str, clock: Clock
) -> Activity:
    set_control = controls.pause if paused else controls.resume
    with session.begin():
        set_control(session, scope, actor, clock())
    get_feed(request).poke()
    return current_activity(request, session)
