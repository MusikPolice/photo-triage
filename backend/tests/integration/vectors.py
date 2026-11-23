"""Items with CLIP vectors, for the neighbours and search tests: `Library` adds them,
and `unit` makes a vector to give one."""

import datetime as dt

import numpy as np
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.db.models import Embedding, Item, ItemStatus, Job, JobStatus, MediaType
from photo_triage.pipeline.embed import to_blob
from photo_triage.worker.queue import PRIORITY, Stage

NOW = dt.datetime(2026, 11, 21, tzinfo=dt.UTC)
DIM = 512


def unit(*weights: float) -> np.ndarray:
    """A unit vector with these first components."""
    vector = np.zeros(DIM, np.float32)
    vector[: len(weights)] = weights
    return vector / np.linalg.norm(vector)


class Library:
    def __init__(self, engine: Engine, model: str) -> None:
        self.engine = engine
        self.model = model
        self._next = 0

    def add(
        self,
        vector: np.ndarray | None,
        *,
        model: str | None = None,
        media_type: MediaType = MediaType.PHOTO,
    ) -> int:
        """An active item, embedded with `vector` unless None, and its done clip job."""
        self._next += 1
        with Session(self.engine) as session, session.begin():
            item = Item(
                content_hash=f"{self._next:064x}",
                path=f"photos/{self._next}.jpg",
                media_type=media_type,
                file_size_bytes=1,
                file_mtime_ns=0,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
            session.add(item)
            session.flush()
            if vector is not None:
                session.add(
                    Embedding(item_id=item.id, model=model or self.model, vector=to_blob(vector))
                )
                session.add(
                    Job(
                        item_id=item.id,
                        stage=Stage.CLIP,
                        priority=PRIORITY[Stage.CLIP],
                        status=JobStatus.DONE,
                        enqueued_at=NOW,
                        finished_at=NOW,
                    )
                )
            return item.id

    def set_status(self, item_id: int, status: ItemStatus) -> None:
        with Session(self.engine) as session, session.begin():
            item = session.get(Item, item_id)
            assert item is not None
            item.status = status
