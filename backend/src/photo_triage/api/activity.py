"""The Activity page's data, and pausing and resuming the worker (plan §7, §6.11).

`GET /api/activity` gives the worker's state and one row per stage, counted from
`jobs`. `POST /api/scan` queues a walk of the library ("Scan now"). It and the
pause and resume endpoints answer with the new activity, and the event
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
from photo_triage.db.models import Item, Job, JobStats, JobStatus
from photo_triage.eta import finish_at
from photo_triage.pipeline.thumbnails import Thumbnail, thumbnail_path
from photo_triage.settings import Settings
from photo_triage.worker import controls, heartbeat
from photo_triage.worker.clock import Clock
from photo_triage.worker.queue import BATCH_STAGES, JobQueue, Stage
from photo_triage.worker.scan_schedule import last_walk, queue_walk
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


class CurrentItem(BaseModel):
    """The item a stage's running job is working on."""

    id: int
    path: str
    """Relative to `PHOTO_DIR`."""
    has_thumbnail: bool
    """Whether its 64 px thumbnail exists yet, at `/api/items/{id}/thumbnail?size=64`."""


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
    last_finished_at: AwareDatetime | None = None
    """Batch stages: when the latest run finished, if any has."""
    last_actor: str | None = None
    """Batch stages: who asked for the latest finished run. None when the schedule
    or the pipeline queued it."""
    next_scheduled_at: AwareDatetime | None = None
    """When the schedule queues the stage next: the walk, on `SCAN_SCHEDULE`."""
    throughput_per_min: float | None = None
    """Item stages (and noop): items done per minute of the worker's time on them, over the
    last `THROUGHPUT_WINDOW`. None with no work in that time."""
    eta_at: AwareDatetime | None = None
    """Item stages with work left and a throughput: when it will be done, counting
    only the time outside quiet hours. None while the stage or the worker is paused,
    or the worker is stopped."""
    current: CurrentItem | None = None
    """Item stages: the item its running job is working on. None while no job runs,
    and always for batch stages."""
    eta_windows: int | None = None
    """With quiet hours, how many working windows the work left spans (1: this one,
    or the next during quiet hours). None without quiet hours."""


class Activity(BaseModel):
    worker: WorkerActivity
    stages: list[StageActivity]
    """In priority order."""


type JobCounts = dict[tuple[Stage, JobStatus], int]
type Running = dict[str, tuple[int, str]]
"""Each stage's running item job: its item's id and path."""
type Throughputs = dict[str, float]
"""Items per busy minute, by stage."""

THROUGHPUT_WINDOW = dt.timedelta(hours=24)
"""How far back throughput looks: long enough to span a night's run during the
day, short enough to follow a change of pace."""


def read_running(session: Session, stages: list[Stage]) -> Running:
    """The item each stage is working on, from its running job. One worker means one
    at most, over `ix_jobs_stage_status`."""
    rows = session.execute(
        sa.select(Job.stage, Item.id, Item.path)
        .join(Item, Item.id == Job.item_id)
        .where(Job.stage.in_(stages), Job.status == JobStatus.RUNNING)
    )
    return {stage: (item_id, path) for stage, item_id, path in rows}


def read_throughputs(session: Session, now: dt.datetime) -> Throughputs:
    """Each stage's items per busy minute over `THROUGHPUT_WINDOW`, from the hourly
    `job_stats` rows (a range over its primary key). Busy time, not wall-clock time,
    so pauses and quiet hours don't drag it down."""
    since = (now - THROUGHPUT_WINDOW).replace(minute=0, second=0, microsecond=0)
    rows = session.execute(
        sa.select(
            JobStats.stage, sa.func.sum(JobStats.processed), sa.func.sum(JobStats.busy_seconds)
        )
        .where(JobStats.hour_start_at >= since)
        .group_by(JobStats.stage)
    )
    return {
        stage: processed / (busy_s / 60)
        for stage, processed, busy_s in rows
        if processed and busy_s
    }


class LastRun(BaseModel):
    finished_at: AwareDatetime | None = None
    actor: str | None = None


def read_last_walk(session: Session) -> LastRun:
    job = last_walk(session)
    return LastRun() if job is None else LastRun(finished_at=job.finished_at, actor=job.actor)


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
    walked: LastRun | None = None,
    throughputs: Throughputs | None = None,
    running: Running | None = None,
) -> Activity:
    """The activity at `now` (the app's clock), judging the heartbeat at `real_now`.
    Jobs are counted, the last walk read and the throughputs worked out, unless
    `counts` (from `count_jobs`), `walked` (from `read_last_walk`), `throughputs`
    (from `read_throughputs`) and `running` (from `read_running`) are given."""
    state = observed_state(session, settings.worker_quiet_hours, settings.tz, now, real_now)
    paused = controls.paused_scopes(session)
    last = heartbeat.last(session)
    stages = shown_stages(settings)
    if counts is None:
        counts = count_jobs(session, stages)
    if walked is None:
        walked = read_last_walk(session)
    if throughputs is None:
        throughputs = read_throughputs(session, now)
    if running is None:
        running = read_running(session, stages)
    held = state.status != WorkerStatus.RUNNING and state.status != WorkerStatus.QUIET
    return Activity(
        worker=WorkerActivity(
            status=state.status,
            paused=controls.GLOBAL in paused,
            quiet_until_at=state.quiet_until_at,
            last_seen_at=None if last is None else last.seen_at,
        ),
        stages=[
            _with_current(
                settings,
                running,
                _with_eta(
                    settings,
                    now,
                    throughputs,
                    held,
                    _with_runs(
                        settings, now, walked, _stage_activity(stage, counts, stage in paused)
                    ),
                ),
            )
            for stage in stages
        ],
    )


def _with_current(settings: Settings, running: Running, row: StageActivity) -> StageActivity:
    """Add the item the stage's running job is on, if any."""
    if row.stage not in running:
        return row
    item_id, path = running[row.stage]
    thumbnail = thumbnail_path(settings.derived_dir, item_id, Thumbnail.MAP)
    current = CurrentItem(id=item_id, path=path, has_thumbnail=thumbnail.exists())
    return row.model_copy(update={"current": current})


def _with_eta(
    settings: Settings, now: dt.datetime, throughputs: Throughputs, held: bool, row: StageActivity
) -> StageActivity:
    """Add an item stage's throughput, and its ETA unless it can't run now: it's
    paused, or the worker is paused or stopped. Quiet hours are counted in. Batch
    stages get neither: a run covers the whole library."""
    # Noop jobs stand for one unit of work each, as items do (dev only).
    if row.kind != "item" and row.stage != Stage.NOOP:
        return row
    rate = throughputs.get(row.stage)
    left = row.pending + row.running + row.errored
    if rate is None or left == 0 or held or row.paused:
        return row.model_copy(update={"throughput_per_min": rate})
    eta = finish_at(
        now, dt.timedelta(minutes=left / rate), settings.worker_quiet_hours, settings.tz
    )
    return row.model_copy(
        update={
            "throughput_per_min": rate,
            "eta_at": None if eta is None else eta.done_at,
            "eta_windows": None if eta is None else eta.windows,
        }
    )


def _with_runs(
    settings: Settings, now: dt.datetime, walked: LastRun, row: StageActivity
) -> StageActivity:
    """Add the last and next runs to the walk's row. Layout, duplicates and atlases
    get theirs with those jobs (Phases 3 and 4)."""
    if row.stage != Stage.WALK:
        return row
    return row.model_copy(
        update={
            "last_finished_at": walked.finished_at,
            "last_actor": walked.actor,
            "next_scheduled_at": settings.scan_schedule.next_at(now, settings.tz),
        }
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
    So is the last walk. The worker's state and heartbeat are read every time, and
    the next scheduled walk worked out, since quiet hours, a stale heartbeat and the
    schedule change with the clock and not with the data. Not thread-safe:
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
        self._walked: LastRun | None = None
        self._throughputs: Throughputs | None = None
        self._running: Running | None = None

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
                    self._walked = read_last_walk(session)
                    self._throughputs = read_throughputs(session, self._clock())
                    self._running = read_running(session, shown_stages(self._settings))
                    self._data_version = version
                return read_activity(
                    session,
                    self._settings,
                    self._clock(),
                    self._real_clock(),
                    self._counts,
                    self._walked,
                    self._throughputs,
                    self._running,
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


@router.post("/scan")
def scan_now(request: Request, session: DbSession, actor: Actor, clock: AppClock) -> Activity:
    """Queue a walk of the library, unless one is waiting or running."""
    with session.begin():
        queue_walk(session, JobQueue(clock), actor)
    get_feed(request).poke()
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
