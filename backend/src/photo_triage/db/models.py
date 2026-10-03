"""Tables (plan §8). Each phase adds its own tables in its own migration.

After changing a model, add a migration (`alembic revision --autogenerate`) and
check it by hand: the tests fail if the models and migrations disagree.
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
    file_size: Mapped[int]
    file_mtime: Mapped[float]
    """`st_mtime` when last scanned."""
    width: Mapped[int | None]
    height: Mapped[int | None]
    duration_s: Mapped[float | None]
    """Videos only."""
    taken_at: Mapped[dt.datetime | None]
    """Capture time as the camera recorded it: local wall-clock time, no timezone."""
    first_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    missing_since: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[ItemStatus] = mapped_column(
        _enum(ItemStatus, "item_status"), default=ItemStatus.ACTIVE
    )


class Job(Base):
    """One unit of work for the worker (plan §6.11). `item_id` is null for batch jobs."""

    __tablename__ = "jobs"
    __table_args__ = (
        # The worker claims the next pending job by priority, oldest first.
        sa.Index(
            "ix_jobs_pending_by_priority",
            "priority",
            "enqueued_at",
            "id",
            sqlite_where=sa.text("status = 'pending'"),
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
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    enqueued_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)


class JobStats(Base):
    """Work done per stage per day, for throughput and history charts (plan §7)."""

    __tablename__ = "job_stats"

    date: Mapped[dt.date] = mapped_column(primary_key=True)
    stage: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    processed: Mapped[int] = mapped_column(default=0)
    errors: Mapped[int] = mapped_column(default=0)
    busy_seconds: Mapped[float] = mapped_column(default=0.0)
