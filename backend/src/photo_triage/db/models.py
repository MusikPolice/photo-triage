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
from typing import Any

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


class TagSource(StrEnum):
    LLM = "llm"
    USER = "user"
    IMPORTED = "imported"
    """Already in the file when it was scanned (plan §6.1)."""


class ReviewKind(StrEnum):
    QUALITY = "quality"
    DUPLICATE = "duplicate"


class Decision(StrEnum):
    KEEP = "keep"
    DELETE = "delete"
    """Moved to the trash."""
    SKIP = "skip"


class GroupStatus(StrEnum):
    PENDING = "pending"
    """Waiting for review."""
    RESOLVED = "resolved"
    SKIPPED = "skipped"
    """Looked at and left for later."""


class WriteStatus(StrEnum):
    QUEUED = "queued"
    APPLIED = "applied"
    DRY_RUN = "dry_run"
    """Logged with `EXIF_WRITES_ENABLED` off, and not made."""
    ERROR = "error"
    """Failed; queued again while its job retries."""


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    PARKED = "parked"


class Item(Base):
    """A photo or video at one path, recognised by its content hash when it moves.

    `photo_triage.pipeline.scan` owns how items are found, hashed and moved.
    """

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    content_hash: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    """`photo_triage.pipeline.identity.content_hash`. Null until the file's first scan
    job. Not unique: exact copies at two paths are two items."""
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
    orientation: Mapped[int | None]
    """A photo's EXIF orientation, 1 to 8, if it has one. Its width and height are
    already turned by it; this is kept so a turn that doesn't swap them (180°, or a
    mirror) still counts as a change (`photo_triage.pipeline.metadata`)."""
    taken_at_local: Mapped[dt.datetime | None]
    """Capture time as the camera recorded it: local wall-clock time, no timezone."""
    reviewed: Mapped[list[str]] = mapped_column(
        sa.JSON, default=list, server_default=sa.text("'[]'")
    )
    """The review kinds the file is marked with (`XMP-phototriage:Reviewed`, plan
    §6.10), such as `duplicates`, as the last metadata read found them. They outlive
    the database, so a rebuilt one doesn't queue the item for that review again."""
    damaged: Mapped[bool] = mapped_column(default=False, server_default=sa.false())
    """A photo that decodes only partly, its file cut short: the rest of its
    picture is grey in its thumbnail (`photo_triage.pipeline.pictures`). Set by
    each scan."""
    first_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    missing_since: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[ItemStatus] = mapped_column(
        _enum(ItemStatus, "item_status"), default=ItemStatus.ACTIVE
    )


class ItemFrame(Base):
    """A frame sampled from a video, which the stages after `frames` read instead of
    the video (plan §6.1). Its image is `photo_triage.pipeline.frames.frame_path`.

    `photo_triage.pipeline.frames` replaces an item's frames together with their files.
    """

    __tablename__ = "item_frames"
    # Also serves reading an item's frames in order.
    __table_args__ = (sa.UniqueConstraint("item_id", "frame_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(sa.ForeignKey("items.id", ondelete="CASCADE"))
    frame_index: Mapped[int]
    """0 for the first frame, in the order they appear."""
    timestamp_s: Mapped[float]
    """Seconds from the start of the video."""


class Embedding(Base):
    """An item's CLIP embedding (plan §6.3): a video's is the mean of its frames'.

    One per item, from the model it was made with. `photo_triage.pipeline.embed`
    replaces it when the item or `CLIP_MODEL` changes.
    """

    __tablename__ = "embeddings"

    item_id: Mapped[int] = mapped_column(
        sa.ForeignKey("items.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(sa.String(64))
    """`CLIP_MODEL` when it was made, e.g. `ViT-B-32`."""
    vector: Mapped[bytes] = mapped_column(sa.LargeBinary)
    """Little-endian float32, of unit length: 2048 bytes for a 512-d model."""


class Layout(Base):
    """An item's place on the map (plan §6.4): UMAP over the CLIP embeddings.

    Each fit is one `layout_version`, and replaces the last in one transaction
    (`photo_triage.pipeline.layout`), so there's only ever one version to read.
    Coordinates are in [0, 1] on the layout's longer axis.
    """

    __tablename__ = "layout"
    __table_args__ = (
        # Purging an item cascades here, by item.
        sa.Index("ix_layout_item_id", "item_id"),
    )

    # The map reads a whole version: its primary key covers that.
    layout_version: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(
        sa.ForeignKey("items.id", ondelete="CASCADE"), primary_key=True
    )
    x: Mapped[float] = mapped_column(sa.Float)
    y: Mapped[float] = mapped_column(sa.Float)


class Quality(Base):
    """An item's quality signals (plan §6.8), each in [0, 1] with 1 the best, from
    `photo_triage.pipeline.quality`. A video's are the medians of its frames'.

    The composite score and flag reasons are worked out from these when read, so
    changing their weights or thresholds needs no re-run. The aesthetic and junk
    signals stay null until their stages fill them in.
    """

    __tablename__ = "quality"

    item_id: Mapped[int] = mapped_column(
        sa.ForeignKey("items.id", ondelete="CASCADE"), primary_key=True
    )
    blur: Mapped[float] = mapped_column(sa.Float)
    """`tanh(variance / 200)` of the grayscale Laplacian: low is blurry."""
    exposure: Mapped[float] = mapped_column(sa.Float)
    """Low for pixels clipped beyond 2% at either end, or a picture too dark or too
    bright overall."""
    dark_clipped_fraction: Mapped[float] = mapped_column(sa.Float)
    """The fraction of pixels clipped to black, which says an exposure is under."""
    bright_clipped_fraction: Mapped[float] = mapped_column(sa.Float)
    """The fraction of pixels clipped to white, which says an exposure is over."""
    mean_brightness: Mapped[float] = mapped_column(sa.Float)
    """The picture's mean brightness, 0 for black to 1 for white."""
    resolution: Mapped[float] = mapped_column(sa.Float)
    """A logistic curve in megapixels, 0.5 at 2 MP."""
    aesthetic: Mapped[float | None] = mapped_column(sa.Float)
    junk_label: Mapped[str | None] = mapped_column(sa.String(32))
    junk_score: Mapped[float | None] = mapped_column(sa.Float)


class Tag(Base):
    """A keyword on an item, and where it came from (plan §8).

    `photo_triage.pipeline.metadata` keeps the imported ones in step with the file.
    """

    __tablename__ = "tags"

    # The key also serves reading or replacing an item's tags from one source.
    item_id: Mapped[int] = mapped_column(
        sa.ForeignKey("items.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[TagSource] = mapped_column(_enum(TagSource, "tag_source"), primary_key=True)
    tag: Mapped[str] = mapped_column(sa.String(255), primary_key=True)
    model: Mapped[str | None] = mapped_column(sa.String(64))
    """The model that suggested it, for `llm` tags."""
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    actor: Mapped[str | None] = mapped_column(sa.String(255))
    """Who added it (plan §10), for `user` tags."""


class DuplicateGroup(Base):
    """Items whose CLIP embeddings are near enough to be the same picture (plan §6.7).

    `photo_triage.pipeline.duplicates` finds them, and keeps a group's id and status
    while its members stay the same, so a decision outlives a regrouping.
    """

    __tablename__ = "duplicate_groups"
    __table_args__ = (
        # The review lists pending groups, oldest first.
        sa.Index("ix_duplicate_groups_status_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[GroupStatus] = mapped_column(
        _enum(GroupStatus, "group_status"), default=GroupStatus.PENDING
    )
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    actor: Mapped[str | None] = mapped_column(sa.String(255))
    """Who resolved or skipped it (plan §10)."""
    leased_to: Mapped[str | None] = mapped_column(sa.String(64))
    """The reviewer looking at it now: a token each review page makes for itself,
    since with no sign-in every actor is the same (plan §10)."""
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    """When another reviewer may have it, if this one hasn't decided."""


class Review(Base):
    """A person's decision on an item in a review queue (plan §8, §10): kept, moved
    to the trash or skipped."""

    __tablename__ = "reviews"
    __table_args__ = (
        # An item's decisions.
        sa.Index("ix_reviews_item_id", "item_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(sa.ForeignKey("items.id", ondelete="CASCADE"))
    kind: Mapped[ReviewKind] = mapped_column(_enum(ReviewKind, "review_kind"))
    decision: Mapped[Decision] = mapped_column(_enum(Decision, "decision"))
    actor: Mapped[str] = mapped_column(sa.String(255))
    decided_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


class DuplicateMember(Base):
    """An item in a duplicate group, and its rank: 0 is the recommended keeper."""

    __tablename__ = "duplicate_members"
    __table_args__ = (
        # An item's group, and removing an item's memberships.
        sa.Index("ix_duplicate_members_item_id", "item_id"),
    )

    group_id: Mapped[int] = mapped_column(
        sa.ForeignKey("duplicate_groups.id", ondelete="CASCADE"), primary_key=True
    )
    item_id: Mapped[int] = mapped_column(
        sa.ForeignKey("items.id", ondelete="CASCADE"), primary_key=True
    )
    rank: Mapped[int]


class TrashEntry(Base):
    """An item in the trash (plan §6.8): where its file was, and where it is now.

    `photo_triage.api.trash` moves files in and out through `photo_triage.files`. A
    restore deletes the row, so there's one per trashed item.
    """

    __tablename__ = "trash"
    __table_args__ = (
        # The purge finds the entries past their date.
        sa.Index("ix_trash_purge_after", "purge_after"),
        # The trash page lists the newest first.
        sa.Index("ix_trash_trashed_at", "trashed_at"),
    )

    item_id: Mapped[int] = mapped_column(
        sa.ForeignKey("items.id", ondelete="CASCADE"), primary_key=True
    )
    original_path: Mapped[str]
    """Relative to `PHOTO_DIR`, as `items.path` was."""
    trash_path: Mapped[str]
    """Relative to `TRASH_DIR`. The same as `original_path`."""
    trashed_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    actor: Mapped[str] = mapped_column(sa.String(255))
    """Who trashed it (plan §10)."""
    purge_after: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    """`trashed_at` plus `TRASH_RETENTION_DAYS`."""


class MetadataWrite(Base):
    """A change to an item's file metadata, queued and then applied or logged by the
    `metadata_write` stage (plan §6.10): the write queue and the write log in one.

    `fields` holds the values asked for, by field (`photo_triage.pipeline.writeback`
    names them: `tags`, `reviewed`). `before` and `after` hold the values of those
    that changed, read from the file before the write and as written.
    """

    __tablename__ = "metadata_writes"
    __table_args__ = (
        # The stage reads an item's writes still to make.
        sa.Index("ix_metadata_writes_item_id_status", "item_id", "status"),
        # The write log lists the newest first, by status or all.
        sa.Index("ix_metadata_writes_status_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(sa.ForeignKey("items.id", ondelete="CASCADE"))
    fields: Mapped[dict[str, Any]] = mapped_column(sa.JSON)
    before: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON)
    after: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON)
    status: Mapped[WriteStatus] = mapped_column(
        _enum(WriteStatus, "write_status"), default=WriteStatus.QUEUED
    )
    actor: Mapped[str | None] = mapped_column(sa.String(255))
    """Who asked for it (plan §10). None when the pipeline did."""
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    applied_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    """When it was written, or logged as a dry run."""


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
        # The Activity page counts jobs by stage and status, one range at a time.
        sa.Index("ix_jobs_stage_status", "stage", "status"),
        # Reconcile reads an item's jobs for a stage, and deleting an item finds its
        # jobs. Without it, SQLite reads all of a stage's done jobs for every item.
        sa.Index("ix_jobs_item_stage_status", "item_id", "stage", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int | None] = mapped_column(sa.ForeignKey("items.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(sa.String(32))
    priority: Mapped[int]
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, "job_status"), default=JobStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(default=0)
    """Failed runs so far."""
    actor: Mapped[str | None] = mapped_column(sa.String(255))
    """Who queued it by hand, such as "Scan now" (plan §10). None when the schedule,
    the pipeline or reconciling queued it."""
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    enqueued_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    retry_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    """Set while a failed job waits out its backoff; it isn't claimed before then."""
    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    stage_version: Mapped[str | None] = mapped_column(sa.String(64))
    """The item stage's version when the job last ran (`ItemStage.version`), set when
    it's done or fails. Null for batch stages, and for jobs done before versions."""
    output_changed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    """Done jobs only: when this stage's output for the item last changed. A run that
    leaves it as it was keeps the previous done job's time, so later stages that read
    it aren't run again (`photo_triage.worker.reconcile`)."""


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
