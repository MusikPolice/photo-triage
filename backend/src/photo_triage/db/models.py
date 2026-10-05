"""Tables (plan §8). Each phase adds its own tables in its own migration.

After changing a model, add a migration (`alembic revision --autogenerate`) and
check it by hand: the tests fail if the models and migrations disagree.

Naming: every `*_at` column is a timezone-aware UTC datetime (`UTCDateTime`),
except `taken_at_local`, which says otherwise in its name. Sizes, durations and
raw timestamps carry their unit in the name (`file_size_bytes`, `duration_s`,
`file_mtime_ns`).
"""

import datetime as dt
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from photo_triage.db.columns import UTCDateTime


class Base(DeclarativeBase):
    metadata = sa.MetaData(
        # Stable constraint names, so later migrations can refer to them.
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def _enum(cls: type[StrEnum], name: str) -> sa.Enum:
    """Stored as the enum's value, in a VARCHAR with a CHECK constraint."""
    return sa.Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=16,
        values_callable=_values,
    )


def _values(cls: type[StrEnum]) -> list[str]:
    return [member.value for member in cls]


class MediaType(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"


class ItemStatus(StrEnum):
    ACTIVE = "active"
    TRASHED = "trashed"
    PURGED = "purged"
    MISSING = "missing"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    PARKED = "parked"


class Item(Base):
    """A photo or video, identified by its content hash so it survives moves."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    content_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    path: Mapped[str]
    """Relative to `PHOTO_DIR`."""
    media_type: Mapped[MediaType] = mapped_column(_enum(MediaType, "media_type"))
    file_size_bytes: Mapped[int]
    file_mtime_ns: Mapped[int]
    """`st_mtime_ns` when last scanned: nanoseconds since the Unix epoch. An integer,
    so comparing it with a fresh `stat()` is exact."""
    width: Mapped[int | None]
    """Pixels."""
    height: Mapped[int | None]
    """Pixels."""
    duration_s: Mapped[float | None]
    """Videos only."""
    taken_at_local: Mapped[dt.datetime | None]
    """Capture time as the camera recorded it: local wall-clock time, no timezone."""
    first_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    missing_since: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[ItemStatus] = mapped_column(
        _enum(ItemStatus, "item_status"), default=ItemStatus.ACTIVE
    )


JOB_IS_OPEN = "status IN ('pending', 'error')"
"""SQL for a job that may still run: never tried, or failed and waiting to retry.

A literal, not bound parameters: SQLite uses a partial index only when the query
repeats the index's condition exactly, and the queue's claim query does.
"""


class Job(Base):
    """One unit of work for the worker (plan §6.11). `item_id` is null for batch jobs.

    `photo_triage.worker.queue` owns the status transitions.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        # The worker claims the next open job by priority, oldest first.
        sa.Index(
            "ix_jobs_open_by_priority",
            "priority",
            "enqueued_at",
            "id",
            sqlite_where=sa.text(JOB_IS_OPEN),
        ),
        # At most one open job per stage and item. Batch jobs (null item) are
        # deduplicated by the queue, since SQLite treats every null as distinct.
        sa.Index(
            "uq_jobs_open_stage_item",
            "stage",
            "item_id",
            unique=True,
            sqlite_where=sa.text(JOB_IS_OPEN),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("items.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(sa.String(32))
    priority: Mapped[int]
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status"), default=JobStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(default=0)
    """Failed runs so far."""
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    enqueued_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    retry_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    """Set while a failed job waits out its backoff; it isn't claimed before then."""
    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)


class JobStats(Base):
    """Work done per stage per hour, for throughput, ETA and history (plan §7).

    Hours are UTC. The browser groups them into the viewer's local days or nights.
    A run counts in the hour it finished.
    """

    __tablename__ = "job_stats"

    hour_start_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, primary_key=True)
    stage: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    processed: Mapped[int] = mapped_column(default=0)
    """Successful runs."""
    errors: Mapped[int] = mapped_column(default=0)
    """Failed runs, including those that will be retried."""
    busy_seconds: Mapped[float] = mapped_column(default=0.0)
    """Time spent running, successful or not, measured with a monotonic timer."""


class WorkerControl(Base):
    """A pause or resume of the worker, globally or for one stage (plan §6.11).

    One row per scope, holding the latest change. No row means not paused.
    `photo_triage.worker.controls` reads and writes it.
    """

    __tablename__ = "worker_controls"

    scope: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    """`global`, or a stage name."""
    paused: Mapped[bool]
    actor: Mapped[str] = mapped_column(sa.String(255))
    """Who paused or resumed it (plan §10)."""
    changed_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


class WorkerHeartbeat(Base):
    """Whether the worker process is running, for the Activity page (plan §7).

    A single row (`id` 1), which the worker writes every few seconds while it runs.
    `photo_triage.worker.heartbeat` reads and writes it.
    """

    __tablename__ = "worker_heartbeat"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    """When the worker process last started."""
    seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    """The latest heartbeat. Stale means the worker crashed or was killed."""
    stopped_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    """When it last stopped cleanly. None while it runs, or if it crashed."""
