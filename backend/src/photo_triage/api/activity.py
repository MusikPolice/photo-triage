"""The Activity page's data, and pausing and resuming the worker (plan §7, §6.11).

`GET /api/activity` gives the worker's state and one row per stage, counted from
`jobs`. The pause and resume endpoints answer with the new activity, and the event
stream (`photo_triage.api.events`) pushes it to every open tab, read by an
`ActivityReader`.
"""

import datetime as dt
from typing import Literal, cast

import sqlalchemy as sa
from fastapi import APIRouter, Request
from pydantic import AwareDatetime, BaseModel
from sqlalchemy.engine import Connection, Engine
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


type JobCounts = dict[tuple[Stage, JobStatus], int]


def shown_stages(settings: Settings) -> list[Stage]:
    """The stages the Activity page lists, in priority order."""
    return [s for s in Stage if s != Stage.NOOP or settings.worker_noop_stage]


def count_jobs(session: Session, stages: list[Stage]) -> JobCounts:
    """How many jobs each stage has in each status.

    One count per stage and status over `ix_jobs_stage_status`, in a single
    statement. On a large table that's several times faster than a `GROUP BY`,
    which aggregates every row rather than counting index ranges (#24).
    """
    keys = [(stage, status) for stage in stages for status in JobStatus]
    counts = [
        sa.select(sa.func.count()).where(Job.stage == stage, Job.status == status).scalar_subquery()
        for stage, status in keys
    ]
    row = session.execute(sa.select(*counts)).one()
    return dict(zip(keys, row, strict=True))


def read_activity(
    session: Session,
    settings: Settings,
    now: dt.datetime,
    real_now: dt.datetime,
    counts: JobCounts | None = None,
) -> Activity:
    """The activity at `now` (the app's clock), judging the heartbeat at `real_now`.
    Jobs are counted unless `counts` (from `count_jobs`) is given."""
    state = observed_state(session, settings.worker_quiet_hours, settings.tz, now, real_now)
    paused = controls.paused_scopes(session)
    last = heartbeat.last(session)
    stages = shown_stages(settings)
    if counts is None:
        counts = count_jobs(session, stages)
    return Activity(
        worker=WorkerActivity(
            status=state.status,
            paused=controls.GLOBAL in paused,
            quiet_until_at=state.quiet_until_at,
            last_seen_at=None if last is None else last.seen_at,
        ),
        stages=[_stage_activity(stage, counts, stage in paused) for stage in stages],
    )


def _stage_activity(stage: Stage, counts: JobCounts, paused: bool) -> StageActivity:
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


class ActivityReader:
    """Reads the activity for the event feed, recounting jobs only after a commit.

    `PRAGMA data_version` changes on a connection only when another connection
    commits, so the reader keeps a connection of its own, on which it only reads.
    The worker's state and heartbeat are read every time, since quiet hours and a
    stale heartbeat change with the clock and not with the data. Not thread-safe:
    the feed never runs two reads at once.
    """

    def __init__(self, engine: Engine, settings: Settings, clock: Clock, real_clock: Clock) -> None:
        self._engine = engine
        self._settings = settings
        self._clock = clock
        self._real_clock = real_clock
        self._connection: Connection | None = None
        self._data_version: int | None = None
        self._counts: JobCounts | None = None

    def __call__(self) -> Activity:
        if self._connection is None:
            self._connection = self._engine.connect()
            self._data_version = None
        try:
            with Session(bind=self._connection) as session:
                # Before counting, so a commit in between is counted again next time.
                version = session.execute(sa.text("PRAGMA data_version")).scalar_one()
                if version != self._data_version or self._counts is None:
                    self._counts = count_jobs(session, shown_stages(self._settings))
                    self._data_version = version
                return read_activity(
                    session, self._settings, self._clock(), self._real_clock(), self._counts
                )
        except Exception:
            self.close()  # start again on a fresh connection
            raise

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


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
