"""Writing many embedded items at once, for the benches that load the API's matrix."""

import datetime as dt

import numpy as np
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.db.models import Embedding, Item, Job, JobStatus, MediaType
from photo_triage.pipeline.embed import to_blob
from photo_triage.worker.queue import PRIORITY, Stage

NOW = dt.datetime(2026, 11, 21, tzinfo=dt.UTC)


def insert_embedded(engine: Engine, vectors: np.ndarray, model: str) -> None:
    """One active item per row of `vectors`, ids from 1, each embedded with its row
    under `model`, with a done clip job."""
    count = len(vectors)
    with Session(engine) as session, session.begin():
        session.execute(
            sa.insert(Item),
            [
                {
                    "id": i + 1,
                    "content_hash": f"{i:064x}",
                    "path": f"photos/{i}.jpg",
                    "media_type": MediaType.PHOTO,
                    "status": "active",
                    "file_size_bytes": 1,
                    "file_mtime_ns": 0,
                    "first_seen_at": NOW,
                    "last_seen_at": NOW,
                    "damaged": False,
                }
                for i in range(count)
            ],
        )
        session.execute(
            sa.insert(Embedding),
            [
                {"item_id": i + 1, "model": model, "vector": to_blob(vectors[i])}
                for i in range(count)
            ],
        )
        session.execute(
            sa.insert(Job),
            [
                {
                    "item_id": i + 1,
                    "stage": Stage.CLIP,
                    "priority": PRIORITY[Stage.CLIP],
                    "status": JobStatus.DONE,
                    "attempts": 0,
                    "enqueued_at": NOW,
                    "finished_at": NOW,
                }
                for i in range(count)
            ],
        )
