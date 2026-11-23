"""Metadata write-back (plan §6.10): queuing changes to what's in an item's file, and
the `metadata_write` stage that makes them.

`queue_write` records a change in `metadata_writes` and queues the item's
`metadata_write` job. The queue keeps one open job per item, so every write queued
before the job runs is made in one exiftool call. The stage reads the fields'
values from the file first, so the log has them before and after. With
`EXIF_WRITES_ENABLED` off it stops there, logging each write as a dry run.

Fields, and where each goes, by container (plan §6.10's table):

- `tags`, a list: `XMP-dc:Subject` everywhere; also `IPTC:Keywords` in a JPEG
  (HEIC has no IPTC), and `Keys:Keywords` in a video, as one comma-separated
  string.
- `reviewed`, a list of review kinds such as `duplicates`:
  `XMP-phototriage:Reviewed`, comma-separated.

A field's value is the whole new value: the tags written replace the file's.
"""

import datetime as dt
import logging
import os
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from photo_triage.db.models import Item, ItemStatus, Job, MetadataWrite, WriteStatus
from photo_triage.files import MetadataWriter
from photo_triage.pipeline.identity import VIDEO_SUFFIXES
from photo_triage.pipeline.metadata import KEYWORD_TAGS, keywords, values
from photo_triage.worker.queue import JobQueue, Stage

logger = logging.getLogger(__name__)

REVIEWED_TAG = "XMP-phototriage:Reviewed"


class Field(StrEnum):
    TAGS = "tags"
    REVIEWED = "reviewed"


Fields = Mapping[Field, list[str]]


class Container(StrEnum):
    JPEG = "jpeg"
    HEIC = "heic"
    QUICKTIME = "quicktime"


def container(path: str) -> Container:
    suffix = Path(path).suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return Container.QUICKTIME
    if suffix in (".heic", ".heif"):
        return Container.HEIC
    return Container.JPEG


def queue_write(
    session: Session,
    queue: JobQueue,
    item_id: int,
    fields: Fields,
    *,
    actor: str | None,
    now: dt.datetime,
) -> MetadataWrite:
    """Queue setting `fields` in the item's file. The caller commits."""
    if not fields:
        raise ValueError("a write needs at least one field")
    write = MetadataWrite(
        item_id=item_id,
        fields={str(name): list(value) for name, value in fields.items()},
        status=WriteStatus.QUEUED,
        actor=actor,
        created_at=now,
    )
    session.add(write)
    queue.enqueue(session, Stage.METADATA_WRITE, item_id)
    return write


def exiftool_args(kind: Container, fields: Fields) -> list[str]:
    """The exiftool assignments that set `fields` in a file of this kind. An empty
    list clears the field."""
    args: list[str] = []
    if (tags := fields.get(Field.TAGS)) is not None:
        args += _list_args("XMP-dc:Subject", tags)
        if kind == Container.JPEG:
            args += ["-charset", "iptc=UTF8", "-IPTC:CodedCharacterSet=UTF8"]
            args += _list_args("IPTC:Keywords", tags)
        elif kind == Container.QUICKTIME:
            args.append(f"-Keys:Keywords={', '.join(tags)}")
    if (reviewed := fields.get(Field.REVIEWED)) is not None:
        args.append(f"-{REVIEWED_TAG}={','.join(reviewed)}")
    return args


def _list_args(tag: str, items: Sequence[str]) -> list[str]:
    # The first assignment to a list tag replaces it, and the rest add to it.
    return [f"-{tag}={item}" for item in items] if items else [f"-{tag}="]


def read_fields(writer: MetadataWriter, path: Path) -> dict[Field, list[str]]:
    """The file's current values of every field."""
    tags = writer.read(path, [*KEYWORD_TAGS, REVIEWED_TAG])
    reviewed = [
        part.strip()
        for value in values(tags, REVIEWED_TAG)
        for part in str(value).split(",")
        if part.strip()
    ]
    return {Field.TAGS: list(keywords(tags)), Field.REVIEWED: reviewed}


def apply(
    sessions: sessionmaker[Session],
    job: Job,
    writer: MetadataWriter,
    photo_dir: Path,
    *,
    enabled: bool,
    now: dt.datetime,
) -> None:
    """Make, or with `enabled` off log as a dry run, every write queued for `job`'s
    item, in one exiftool call. Raises `MetadataError` if exiftool fails, after
    marking the writes `error`, so the job is retried and they're made then."""
    assert job.item_id is not None
    with sessions() as session:
        item = session.get(Item, job.item_id)
        if item is None:
            return  # deleted, and its writes with it
        relative, status = item.path, item.status
        pending = _pending(session, job.item_id)
        asked = [(w.id, _fields(w.fields)) for w in pending]
    if not asked:
        return
    path = photo_dir / relative
    if status != ItemStatus.ACTIVE or not path.exists():
        # Nowhere to write. Queued again, its writes would be made: leave them for that.
        logger.warning("Item %d isn't in the library, so its metadata wasn't written", job.item_id)
        _finish(sessions, [i for i, _ in asked], {}, WriteStatus.ERROR, None)
        return

    current = read_fields(writer, path)
    # Each write's before is the value the writes ahead of it leave, so the log reads
    # as a sequence even when they're made together.
    logs: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
    final: dict[Field, list[str]] = {}
    for write_id, fields in asked:
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        for name, value in fields.items():
            if not _same(current[name], value):
                before[name], after[name] = current[name], value
            current[name] = value
            final[name] = value
        logs[write_id] = (before, after)
    changed = {name: value for name, value in final.items() if _changed(logs, name)}

    if not enabled:
        _finish(sessions, list(logs), logs, WriteStatus.DRY_RUN, now)
        logger.info("Dry run: item %d, %s", job.item_id, ", ".join(changed) or "no change")
        return
    if changed:
        try:
            writer.write(path, exiftool_args(container(relative), changed))
        except Exception:
            _finish(sessions, list(logs), logs, WriteStatus.ERROR, None)
            raise
    stat = os.stat(path)
    with sessions.begin() as session:
        item = session.get(Item, job.item_id)
        if item is not None and item.path == relative:
            # The mtime is kept, but the size changes: record both, so the next walk
            # doesn't take the file for changed and scan it again.
            item.file_size_bytes = stat.st_size
            item.file_mtime_ns = stat.st_mtime_ns
        _mark(session, logs, WriteStatus.APPLIED, now)
    logger.info("Wrote item %d's %s", job.item_id, ", ".join(changed) or "metadata (no change)")


def _pending(session: Session, item_id: int) -> list[MetadataWrite]:
    return list(
        session.scalars(
            sa.select(MetadataWrite)
            .where(
                MetadataWrite.item_id == item_id,
                MetadataWrite.status.in_([WriteStatus.QUEUED, WriteStatus.ERROR]),
            )
            .order_by(MetadataWrite.id)
        )
    )


def _fields(raw: Mapping[str, Any]) -> dict[Field, list[str]]:
    return {Field(name): [str(v) for v in value] for name, value in raw.items()}


def _same(a: Sequence[str], b: Sequence[str]) -> bool:
    # Tag order isn't meaningful, and exiftool may not keep it in every container.
    return sorted(a) == sorted(b)


def _changed(logs: Mapping[int, tuple[dict[str, Any], dict[str, Any]]], name: Field) -> bool:
    return any(name in after for _, after in logs.values())


def _finish(
    sessions: sessionmaker[Session],
    write_ids: Sequence[int],
    logs: Mapping[int, tuple[dict[str, Any], dict[str, Any]]],
    status: WriteStatus,
    now: dt.datetime | None,
) -> None:
    with sessions.begin() as session:
        _mark(session, {i: logs.get(i, ({}, {})) for i in write_ids}, status, now)


def _mark(
    session: Session,
    logs: Mapping[int, tuple[dict[str, Any], dict[str, Any]]],
    status: WriteStatus,
    now: dt.datetime | None,
) -> None:
    for write_id, (before, after) in logs.items():
        write = session.get(MetadataWrite, write_id)
        if write is None:
            continue
        write.status = status
        write.before, write.after = before, after
        write.applied_at = now
